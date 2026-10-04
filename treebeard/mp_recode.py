"""Maintenance-window tool to re-encode Materialized Path trees.

When the sibling capacity of the current ``steplen``/``alphabet`` combination
is about to run out, the ``path`` values of an :class:`~treebeard.mp_tree.MP_Node`
table must be rewritten under new encoding parameters. This module provides an
explicit, standalone migration tool for that maintenance window:

* The tool takes the **old** and the **new** ``alphabet``/``steplen`` as
  arguments. It never reads ``model.steplen``/``model.alphabet``, so the data
  can be migrated *before* the model class is switched to the new
  configuration. Do not point the model class at the new parameters until
  :func:`recode_tree` has completed successfully.
* The tree structure and the sibling order are first restored by decoding
  every stored path with the **old** parameters. Siblings are ordered by the
  decoded integer value of their path step, not by database string ordering.
* Before anything is written, the new parameters are validated: alphabet
  sanity, database collation compatibility, per-level sibling capacity and
  the ``path`` field ``max_length``.
* Corrupt rows (characters outside the old alphabet, path lengths that are
  not a multiple of the old ``steplen``, wrong ``depth``) and orphan nodes
  abort the migration and are reported by primary key. They are never
  silently re-attached to the root.
* The write phase runs in a single transaction and moves every row through a
  temporary path that cannot collide with any old or new path, so the unique
  constraint on ``path`` is never violated mid-migration. If the transaction
  fails, the database is left untouched; after a crash the state can be
  determined at any time with :func:`check_encoding`, which is safe to run
  repeatedly.
* ``depth`` and ``numchild`` are recomputed from the decoded structure
  (stored values are not trusted) and verified against the database after
  the rewrite, inside the same transaction.

Typical usage during a maintenance window::

    from treebeard.mp_recode import recode_tree, check_encoding

    recode_tree(MyNode, old_alphabet="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                old_steplen=4, new_alphabet="...", new_steplen=6)
    # only now switch MyNode.alphabet / MyNode.steplen to the new values

A management command wrapper is available as ``manage.py recode_mp_tree``.
"""

import collections
import dataclasses

from django.db import connections, router, transaction

from treebeard.exceptions import PathOverflow
from treebeard.mp_tree import path_updated
from treebeard.numconv import NumConv

#: Candidate prefixes for temporary paths. The first one that appears in
#: neither the old nor the new alphabet is used, which guarantees that a
#: temporary path can never collide with a real path.
_TEMP_SENTINELS = ("~", "!", "\x1f", "\x1e", " ")

#: Alphabet used to encode row numbers in temporary paths.
_TEMP_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


class RecodeError(Exception):
    """
    Raised when a recode cannot proceed or fails verification.

    ``problems`` maps a problem category to a list of affected primary keys
    (or paths), e.g. ``{"orphans": [1, 2], "evil_chars": [3]}``.
    """

    def __init__(self, message, problems=None):
        super().__init__(message)
        self.problems = problems or {}


@dataclasses.dataclass
class RecodeChange:
    """The planned rewrite of a single node."""

    pk: object
    old_path: str
    new_path: str
    depth: int
    numchild: int


@dataclasses.dataclass
class RecodePlan:
    """The full result of planning a recode; also returned after execution."""

    model: type
    old_alphabet: str
    old_steplen: int
    new_alphabet: str
    new_steplen: int
    changes: list
    max_depth: int

    @property
    def node_count(self):
        return len(self.changes)

    def summary(self):
        return (
            f"{self.node_count} nodes, max depth {self.max_depth}, "
            f"steplen {self.old_steplen} -> {self.new_steplen}, "
            f"max path length {self.max_depth * self.new_steplen}"
        )


def _db_alias(model):
    return router.db_for_write(model)


def _fetch_rows(model, using):
    return list(model.objects.using(using).values("pk", "path", "depth", "numchild"))


def check_encoding(model, alphabet, steplen):
    """
    Validates every row of ``model`` against explicit encoding parameters.

    This is the repeatable check used before and after a migration; it does
    not depend on the model class ``alphabet``/``steplen`` attributes, so it
    can be run with either the old or the new configuration at any time.

    :returns: a dict mapping problem categories to lists of node pks:
        ``evil_chars``, ``bad_steplen``, ``wrong_depth``, ``orphans`` and
        ``wrong_numchild``. All lists are empty when the data is healthy.
    """
    conv = NumConv(alphabet)
    problems = {"evil_chars": [], "bad_steplen": [], "wrong_depth": [], "orphans": [], "wrong_numchild": []}
    rows = _fetch_rows(model, _db_alias(model))
    by_path = {}
    decoded = {}
    for row in rows:
        path = row["path"]
        if any(char not in conv.cached_map for char in path):
            problems["evil_chars"].append(row["pk"])
            continue
        if not path or len(path) % steplen:
            problems["bad_steplen"].append(row["pk"])
            continue
        by_path[path] = row
        decoded[row["pk"]] = path

    children_count = {path: 0 for path in by_path}
    for pk, path in decoded.items():
        row = by_path[path]
        depth = len(path) // steplen
        if row["depth"] != depth:
            problems["wrong_depth"].append(pk)
            continue
        if depth > 1:
            parent_path = path[:-steplen]
            if parent_path not in by_path:
                problems["orphans"].append(pk)
                continue
            children_count[parent_path] += 1

    for pk, path in decoded.items():
        row = by_path[path]
        if row["depth"] == len(path) // steplen and row["numchild"] != children_count[path]:
            problems["wrong_numchild"].append(pk)

    return problems


def _decode_tree(model, old_alphabet, old_steplen):
    """
    Restores the tree structure and sibling order using the old encoding.

    :returns: a tuple ``(rows_by_path, children)`` where ``rows_by_path`` maps
        old path -> row dict and ``children`` maps a parent old path (""
        for roots) to a list of child paths ordered by decoded step value.
    :raise RecodeError: listing corrupt or orphan nodes; nothing is repaired
        or re-attached here.
    """
    if old_steplen < 1:
        raise RecodeError(f"old steplen must be >= 1, got {old_steplen}")
    conv = NumConv(old_alphabet)  # raises ValueError on duplicate characters

    rows = _fetch_rows(model, _db_alias(model))
    problems = {"evil_chars": [], "bad_steplen": [], "wrong_depth": []}
    rows_by_path = {}
    for row in rows:
        path = row["path"]
        if any(char not in conv.cached_map for char in path):
            problems["evil_chars"].append(row["pk"])
        elif not path or len(path) % old_steplen:
            problems["bad_steplen"].append(row["pk"])
        elif row["depth"] != len(path) // old_steplen:
            problems["wrong_depth"].append(row["pk"])
        else:
            rows_by_path[path] = row
    problems = {key: value for key, value in problems.items() if value}
    if problems:
        raise RecodeError(
            "Cannot recode: the table contains rows that are corrupt under "
            f"the old encoding (alphabet={old_alphabet!r}, steplen={old_steplen}). "
            "Fix or remove them before migrating.",
            problems=problems,
        )

    children = {}
    for path in rows_by_path:
        parent_path = path[:-old_steplen]
        step = conv.str2int(path[-old_steplen:])
        children.setdefault(parent_path, []).append((step, path))

    orphans = []
    for parent_path, entries in children.items():
        if parent_path and parent_path not in rows_by_path:
            orphans.extend(rows_by_path[path]["pk"] for _, path in entries)
    if orphans:
        raise RecodeError(
            "Cannot recode: orphan nodes found (their parent path is missing). "
            "They are listed below and were NOT re-attached anywhere.",
            problems={"orphans": sorted(orphans)},
        )

    ordered_children = {parent: [path for _, path in sorted(entries)] for parent, entries in children.items()}
    return rows_by_path, ordered_children


def _assert_db_ordering(model, alphabet):
    """
    Ensures the database sorts the new alphabet characters in the same order
    as their numeric values, otherwise path ordering (and therefore sibling
    ordering and range queries) would break.
    """
    chars = list(alphabet)
    if chars != sorted(chars):
        raise RecodeError(
            "The new alphabet must list characters in ascending codepoint "
            f"order, got {alphabet!r}"
        )
    connection = connections[_db_alias(model)]
    union = " UNION ALL ".join(["SELECT %s AS ch"] * len(chars))
    sql = f"SELECT ch FROM ({union}) AS treebeard_recode_ordercheck ORDER BY ch"
    with connection.cursor() as cursor:
        cursor.execute(sql, chars)
        db_order = [row[0] for row in cursor.fetchall()]
    if db_order != chars:
        raise RecodeError(
            "The database collation does not sort the new alphabet in "
            "codepoint order; path ordering would be corrupted. "
            f"Expected {chars!r}, database returned {db_order!r}. "
            "Choose a different alphabet or a binary/case-sensitive collation."
        )


def _new_step(conv, alphabet, steplen, pos):
    key = conv.int2str(pos)
    if len(key) > steplen:
        raise PathOverflow(f"Path Overflow: cannot encode position {pos} with steplen={steplen}")
    return f"{alphabet[0] * (steplen - len(key))}{key}"


def plan_recode(model, old_alphabet, old_steplen, new_alphabet, new_steplen):
    """
    Decodes the tree with the old parameters, validates the new parameters
    and computes the new path/depth/numchild of every node.

    Performs no writes.

    :returns: a :class:`RecodePlan`
    :raise RecodeError: on corrupt data or invalid new parameters
    :raise PathOverflow: when a level has more siblings than the new
        parameters can express, or paths would exceed ``path.max_length``
    """
    if new_steplen < 1:
        raise RecodeError(f"new steplen must be >= 1, got {new_steplen}")
    new_conv = NumConv(new_alphabet)  # raises ValueError on duplicate characters
    if len(new_alphabet) < 2:
        raise RecodeError("the new alphabet must contain at least 2 characters")

    rows_by_path, children = _decode_tree(model, old_alphabet, old_steplen)

    _assert_db_ordering(model, new_alphabet)

    capacity = len(new_alphabet) ** new_steplen - 1
    too_crowded = {parent: len(entries) for parent, entries in children.items() if len(entries) > capacity}
    if too_crowded:
        details = {parent or "<roots>": count for parent, count in sorted(too_crowded.items(), key=lambda kv: -kv[1])}
        raise PathOverflow(
            f"Path Overflow: the new encoding (radix={len(new_alphabet)}, "
            f"steplen={new_steplen}) allows at most {capacity} siblings per "
            f"parent, but these parents have more: {details}"
        )

    max_depth = max((len(path) // old_steplen for path in rows_by_path), default=0)
    max_length = model._meta.get_field("path").max_length
    if max_depth * new_steplen > max_length:
        raise PathOverflow(
            f"Path Overflow: depth {max_depth} with steplen {new_steplen} needs "
            f"{max_depth * new_steplen} characters but path.max_length is "
            f"{max_length}. Increase path.max_length and UPDATE your database "
            "schema before recoding."
        )

    changes = []
    queue = collections.deque([("", "")])
    while queue:
        old_parent_path, new_parent_path = queue.popleft()
        for pos, old_path in enumerate(children.get(old_parent_path, []), start=1):
            new_path = new_parent_path + _new_step(new_conv, new_alphabet, new_steplen, pos)
            row = rows_by_path[old_path]
            child_paths = children.get(old_path, [])
            changes.append(
                RecodeChange(
                    pk=row["pk"],
                    old_path=old_path,
                    new_path=new_path,
                    depth=len(new_path) // new_steplen,
                    numchild=len(child_paths),
                )
            )
            if child_paths:
                queue.append((old_path, new_path))

    return RecodePlan(
        model=model,
        old_alphabet=old_alphabet,
        old_steplen=old_steplen,
        new_alphabet=new_alphabet,
        new_steplen=new_steplen,
        changes=changes,
        max_depth=max_depth,
    )


def _pick_sentinel(old_alphabet, new_alphabet):
    used = set(old_alphabet) | set(new_alphabet)
    for candidate in _TEMP_SENTINELS:
        if candidate not in used:
            return candidate
    raise RecodeError("could not find a temporary path prefix outside both alphabets")


def recode_tree(model, old_alphabet, old_steplen, new_alphabet, new_steplen, dry_run=False):
    """
    Rewrites every ``path`` in ``model``'s table from the old encoding to the
    new one, without touching primary keys or any business field.

    The whole rewrite runs in a single transaction. Rows are first moved to
    collision-proof temporary paths and then to their final new paths, so the
    unique constraint on ``path`` is never violated by intermediate states.
    ``depth`` and ``numchild`` are recomputed from the decoded structure and
    the result is verified inside the transaction; any mismatch rolls
    everything back, leaving the old encoding intact.

    After a crash, run :func:`check_encoding` with the old and the new
    parameters to determine which state the table is in; the migration can
    then simply be re-executed.

    :param dry_run: if True, only compute and return the plan, write nothing.
    :returns: the executed :class:`RecodePlan`
    """
    plan = plan_recode(model, old_alphabet, old_steplen, new_alphabet, new_steplen)
    if dry_run or not plan.changes:
        return plan

    using = _db_alias(model)
    sentinel = _pick_sentinel(old_alphabet, new_alphabet)
    token_conv = NumConv(_TEMP_ALPHABET)
    max_length = model._meta.get_field("path").max_length
    longest_temp = 1 + len(token_conv.int2str(len(plan.changes)))
    if longest_temp > max_length:
        raise RecodeError(
            f"Temporary paths need {longest_temp} characters but "
            f"path.max_length is {max_length}; cannot stage the migration safely."
        )

    with transaction.atomic(using=using):
        # Phase 1: move every row to a unique temporary path that cannot
        # collide with any old or new path (the sentinel character appears
        # in neither alphabet).
        model.objects.using(using).bulk_update(
            [
                model(pk=change.pk, path=f"{sentinel}{token_conv.int2str(i)}")
                for i, change in enumerate(plan.changes, start=1)
            ],
            ["path"],
        )

        # Phase 2: write the final encoding together with the recomputed
        # depth and numchild.
        model.objects.using(using).bulk_update(
            [
                model(pk=change.pk, path=change.new_path, depth=change.depth, numchild=change.numchild)
                for change in plan.changes
            ],
            ["path", "depth", "numchild"],
        )

        for change in plan.changes:
            path_updated.send(sender=model, old_path=change.old_path, new_path=change.new_path, using=using)

        # Verify inside the transaction: the database must now match the
        # plan exactly, and be fully healthy under the new encoding.
        expected = {change.pk: change for change in plan.changes}
        mismatched = []
        for row in _fetch_rows(model, using):
            change = expected[row["pk"]]
            if (
                row["path"] != change.new_path
                or row["depth"] != change.depth
                or row["numchild"] != change.numchild
            ):
                mismatched.append(row["pk"])
        if mismatched:
            raise RecodeError(
                "Post-write verification failed; rolling back.",
                problems={"mismatched": mismatched},
            )
        problems = check_encoding(model, new_alphabet, new_steplen)
        problems = {key: value for key, value in problems.items() if value}
        if problems:
            raise RecodeError("Post-write encoding check failed; rolling back.", problems=problems)

    return plan
