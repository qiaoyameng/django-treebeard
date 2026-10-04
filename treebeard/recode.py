"""
Maintenance tool for changing the Materialized Path encoding parameters
(``alphabet`` / ``steplen``) of an :class:`~treebeard.mp_tree.MP_Node`
subclass without touching primary keys or business fields.

The node model class must still be configured with the *old* parameters while
this tool runs; only after a successful migration should ``alphabet`` and
``steplen`` be changed in the model definition and deployed.

Typical maintenance window::

    from treebeard.recode import recode_mp_paths

    result = recode_mp_paths(
        MyNode,
        old_alphabet=MyNode.alphabet,
        old_steplen=MyNode.steplen,
        new_alphabet="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
        new_steplen=4,
    )

The migration happens in two physical writes per node so that the UNIQUE
constraint on ``path`` can never be violated, even when the old and new path
strings overlap:

 1. Every path is moved out of the way by prefixing a *marker* character that
    exists in neither the old nor the new alphabet (``<marker><old_path>``).
 2. The marked paths are replaced by the newly encoded paths.

A durable mapping/journal table records the state of every row, which makes
the process resumable after a crash and gives a deterministic recovery path
(:func:`resume_recode`, :func:`rollback_recode`, :func:`forget_recode`).
"""

import re
from dataclasses import dataclass, field

from django.db import connections, router, transaction
from django.db.models import Case, CharField, F, Value, When
from django.db.models.functions import Concat

from treebeard.numconv import NumConv

# ---------------------------------------------------------------------------
# Problem categories (returned by analyze_mp_paths / raised by a full recode)
# ---------------------------------------------------------------------------

BAD_ALPHABET = "bad_alphabet"
"""A path contains characters that are not in the old alphabet."""

BAD_STEPLEN = "bad_steplen"
"""A path length is not a multiple of the old steplen."""

ZERO_STEP = "zero_step"
"""A step decodes to 0 (treebeard sibling positions start at 1)."""

ORPHAN = "orphan"
"""A non-root node references a parent path that does not exist."""

WRONG_DEPTH = "wrong_depth"
"""The stored depth does not match the decoded path depth."""

WRONG_NUMCHILD = "wrong_numchild"
"""The stored numchild does not match the number of direct children."""

COLLATION_INCOMPATIBLE = "collation_incompatible"
"""Database ordering does not match the new alphabet ordering."""

COLLATION_CASE = "collation_case"
"""Distinct alphabet characters compare equal (usually case-insensitive DB)."""

CHARSET = "charset"
"""An alphabet/marker character cannot be stored in the path column."""

CAPACITY = "capacity"
"""A level has more siblings than new_steplen can express."""

PATH_TOO_LONG = "path_too_long"
"""A rewritten path does not fit in the path column."""

NO_MARKER_SPACE = "no_marker_space"
"""No marker headroom exists for the two-phase rewrite."""

CONFIG_MISMATCH = "config_mismatch"
"""The model is not configured with the declared old parameters."""


class RecodeError(Exception):
    """Base class for materialized path recoding errors."""


class RecodeValidationError(RecodeError):
    """Raised when the input parameters or the tree data cannot be migrated."""

    def __init__(self, message, problems=None):
        super().__init__(message)
        self.problems = problems or []


class RecodeStateError(RecodeError):
    """Raised when migration metadata is missing, present or inconsistent."""


@dataclass
class RecodePlan:
    """Result of a read-only analysis/dry run."""

    model_label: str
    node_count: int
    new_alphabet: str
    new_steplen: int
    capacity_per_level: int
    max_siblings: int
    max_path_length: int
    path_max_length: int
    marker: str | None
    problems: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.problems

    def raise_if_problems(self):
        if self.problems:
            categories = sorted({p["category"] for p in self.problems})
            raise RecodeValidationError(
                f"{len(self.problems)} problem(s) found ({', '.join(categories)}); "
                "migration aborted, nothing was written",
                self.problems,
            )


@dataclass
class RecodeResult:
    """Result of a completed (or resumed) migration."""

    model_label: str
    node_count: int
    old_alphabet: str
    old_steplen: int
    new_alphabet: str
    new_steplen: int
    marker: str
    resumed: bool = False
    phase_a_written: int = 0
    phase_b_written: int = 0


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------

def _encode_step(converter, steplen, ordinal):
    """Encode a 1-indexed sibling ordinal as a zero-padded fixed-width step."""
    key = converter.int2str(ordinal)
    if len(key) > steplen:
        raise RecodeValidationError(
            f"sibling ordinal {ordinal} cannot be encoded with steplen={steplen}"
        )
    return converter.alphabet[0] * (steplen - len(key)) + key


def _table_names(model):
    label = f"{model._meta.app_label}_{model._meta.model_name}"
    return f"recode_{label}_map", f"recode_{label}_journal"


def _path_column_info(cursor, model):
    """
    Return ``(data_type, type_name, char_length, collation_name)`` for the
    ``path`` column from the database catalog.
    """
    table = model._meta.db_table
    vendor = cursor.db.vendor
    if vendor == "sqlite":
        # pragma_table_info columns: cid,name,type,notnull,dflt_value,pk
        cursor.execute("SELECT TYPE FROM pragma_table_info(%s)", [table])
        coltype = None
        for row in cursor.fetchall():
            coltype = row[0]
        return (coltype or "varchar").upper(), (coltype or "varchar").upper(), _declared_length(coltype), None
    if vendor == "postgresql":
        cursor.execute(
            """
            SELECT data_type, udt_name, character_maximum_length, collation_name
            FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
            """,
            [table, "path"],
        )
        return cursor.fetchone()
    if vendor == "mysql":
        cursor.execute(
            """
            SELECT DATA_TYPE, COLUMN_TYPE, CHARACTER_MAXIMUM_LENGTH, COLLATION_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
            """,
            [table, "path"],
        )
        return cursor.fetchone()
    # mssql
    cursor.execute(
        """
        SELECT ty.name, ty.name, c.max_length,
               col.collation_name
        FROM sys.columns c
        JOIN sys.types ty ON c.user_type_id = ty.user_type_id
        WHERE c.object_id = OBJECT_ID(%s) AND c.name = %s
        """,
        [table, "path"],
    )
    row = cursor.fetchone()
    if row:
        data_type, type_name, max_bytes, collation = row
        # n* types store UTF-16; max_length is in bytes.
        char_length = int(max_bytes) / 2 if type_name in ("nvarchar", "nchar") else int(max_bytes)
        return data_type, type_name, int(char_length), collation
    return None, None, None, None


def _declared_length(coltype):
    match = re.search(r"\((\d+)\)", coltype or "")
    return int(match.group(1)) if match else None

# ---------------------------------------------------------------------------
# Structure analysis (old encoding)
# ---------------------------------------------------------------------------

def analyze_mp_paths(model, old_alphabet, old_steplen, using=None):
    """
    Decode every path using the *old* encoding and rebuild the tree structure
    in memory.

    :returns: a tuple ``(nodes, problems)`` where ``nodes`` is an ordered list
              of dicts ``{pk, path, depth, numchild, parent, steps}`` and
              ``problems`` is a list of ``{category, pk, detail}`` records.
              Nodes with fatal structural problems (bad characters/length,
              orphans) are still returned but must not be migrated.
    """
    using = using or router.db_for_read(model)
    converter = NumConv(old_alphabet)
    problems = []

    rows = list(
        model.objects.using(using)
        .values("pk", "path", "depth", "numchild")
        .order_by("depth", "path")
    )

    nodes = []
    by_path = {}
    for row in rows:
        node = {
            "pk": row["pk"],
            "path": row["path"],
            "depth": row["depth"],
            "numchild": row["numchild"],
            "parent": "",
            "steps": [],
            "fatal": False,
        }
        path = row["path"]
        if any(char not in old_alphabet for char in path):
            problems.append({"category": BAD_ALPHABET, "pk": row["pk"], "path": path})
            node["fatal"] = True
        elif len(path) % old_steplen:
            problems.append(
                {
                    "category": BAD_STEPLEN,
                    "pk": row["pk"],
                    "path": path,
                    "detail": f"length {len(path)} is not a multiple of steplen {old_steplen}",
                }
            )
            node["fatal"] = True
        else:
            steps = []
            try:
                for idx in range(0, len(path), old_steplen):
                    steps.append(converter.str2int(path[idx : idx + old_steplen]))
            except ValueError:
                problems.append({"category": BAD_ALPHABET, "pk": row["pk"], "path": path})
                node["fatal"] = True
            else:
                node["steps"] = steps
                if any(step < 1 for step in steps):
                    problems.append(
                        {
                            "category": ZERO_STEP,
                            "pk": row["pk"],
                            "path": path,
                            "detail": "a step decodes to 0; treebeard positions start at 1",
                        }
                    )
                    node["fatal"] = True
                decoded_depth = len(steps)
                if decoded_depth > 1:
                    node["parent"] = path[: (decoded_depth - 1) * old_steplen]
                if row["depth"] != decoded_depth:
                    problems.append(
                        {
                            "category": WRONG_DEPTH,
                            "pk": row["pk"],
                            "path": path,
                            "detail": f"stored depth {row['depth']} != decoded {decoded_depth}",
                        }
                    )
                node["depth"] = decoded_depth
        nodes.append(node)
        if not node["fatal"]:
            by_path[node["path"]] = node

    # Orphan detection (parents must exist; roots are never orphans)
    for node in nodes:
        if node["fatal"]:
            continue
        if node["parent"]:
            parent = by_path.get(node["parent"])
            if parent is None:
                problems.append(
                    {
                        "category": ORPHAN,
                        "pk": node["pk"],
                        "path": node["path"],
                        "detail": f"missing parent path {node['parent']!r}",
                    }
                )
                node["fatal"] = True

    # numchild recount (only counts non-fatal direct children)
    children_count = {}
    for node in nodes:
        if node["fatal"] or not node["parent"]:
            continue
        children_count[node["parent"]] = children_count.get(node["parent"], 0) + 1
    for node in nodes:
        if node["fatal"]:
            continue
        real = children_count.get(node["path"], 0)
        node["real_numchild"] = real
        if node["numchild"] != real:
            problems.append(
                {
                    "category": WRONG_NUMCHILD,
                    "pk": node["pk"],
                    "path": node["path"],
                    "detail": f"stored numchild {node['numchild']} != counted {real}",
                }
            )

    return nodes, problems


def _group_siblings(nodes):
    """
    Group healthy nodes by parent path and order each sibling set by the
    numeric value of the last old-encoding step (the canonical sibling order).
    """
    groups = {}
    for node in nodes:
        if node["fatal"]:
            continue
        groups.setdefault(node["parent"], []).append(node)
    for siblings in groups.values():
        siblings.sort(key=lambda item: item["steps"][-1])
    return groups


def _build_new_paths(nodes, new_alphabet, new_steplen):
    """
    Assign new paths. Sibling ordinal (1-indexed) follows the decoded order
    under the old encoding. Returns ``(assignments, plan_problems,
    max_siblings, capacity)`` where assignments maps ``pk -> new_path``.
    """
    converter = NumConv(new_alphabet)
    capacity = len(new_alphabet) ** new_steplen - 1
    groups = _group_siblings(nodes)
    problems = []
    max_siblings = 0
    assignments = {}
    new_path_of = {}

    # Parents must be encoded before their children so the new parent path
    # prefix exists. Roots (parent_path == "") come first.
    ordered_parents = sorted(groups, key=lambda parent_path: (len(parent_path), parent_path))
    for parent_path in ordered_parents:
        siblings = groups[parent_path]
        count = len(siblings)
        max_siblings = max(max_siblings, count)
        if count > capacity:
            detail = f"{count} children under {parent_path or 'root'} exceed capacity {capacity}"
            for node in siblings:
                problems.append({"category": CAPACITY, "pk": node["pk"], "path": node["path"], "detail": detail})
            continue
        parent_new_path = new_path_of.get(parent_path, "") if parent_path else ""
        for ordinal, node in enumerate(siblings, start=1):
            step = _encode_step(converter, new_steplen, ordinal)
            new_path = parent_new_path + step
            assignments[node["pk"]] = new_path
            new_path_of[node["path"]] = new_path
    return assignments, problems, max_siblings, capacity


# ---------------------------------------------------------------------------
# New-encoding validation: charset + database collation compatibility
# ---------------------------------------------------------------------------

def _probe_strings(alphabet, steplen):
    """Single chars plus adjacent-character pairs/strings actually exercised."""
    probes = list(alphabet)
    n = len(alphabet)
    limit = min(steplen, 3)
    # every adjacent pair (catches context-sensitive collation edge cases)
    for i in range(n - 1):
        pair = alphabet[i] + alphabet[i + 1]
        probes.append(pair.ljust(limit, alphabet[0])[:limit])
    # first-char transitions across zero padding
    if steplen >= 2:
        probes.append(alphabet[-1] + alphabet[0])
        probes.append(alphabet[0] + alphabet[-1])
    # de-duplicate while preserving order
    seen = set()
    ordered = []
    for probe in probes:
        if probe not in seen:
            seen.add(probe)
            ordered.append(probe)
    return ordered


def check_database_compatibility(
    model, alphabet, steplen, marker=None, using=None, collation_override=None
):
    """
    Verify that the database column can store every character and that its
    ordering matches the alphabet ordering (required for treebeard path
    queries and DFS ordering).

    Uses a temporary probe table so nothing is written to the real tree.
    """
    using = using or router.db_for_write(model)
    problems = []
    connection = connections[using]
    vendor = connection.vendor
    probes = _probe_strings(alphabet, steplen)
    if marker:
        probes.append(marker)

    path_info = None
    with connection.cursor() as cursor:
        path_info = _path_column_info(cursor, model)
    _data_type, type_name, _char_length, collation_name = path_info
    # ``collation_override`` exists so callers (and tests) can probe a
    # different collation than the one currently reported by the column; the
    # migration itself always uses the column's own collation.
    if collation_override is not None:
        collation_name = collation_override

    temp_name = f"recode_probe_{abs(hash((model._meta.label, alphabet, steplen, marker))) % 10_000_000}"
    qn = connection.ops.quote_name

    if vendor == "sqlite":
        collate = f" COLLATE {collation_name}" if collation_name else ""
        ddl = f"CREATE TEMPORARY TABLE {qn(temp_name)} (val VARCHAR(255){collate} NOT NULL)"
        insert = f"INSERT INTO {qn(temp_name)} (val) VALUES (%s)"
        order_clause = f"val{collate}"
    elif vendor == "postgresql":
        collate = f' COLLATE "{collation_name}"' if collation_name else ""
        ddl = (
            f"CREATE TEMPORARY TABLE {qn(temp_name)} "
            f"(val VARCHAR(1024){collate} NOT NULL)"
        )
        insert = f"INSERT INTO {qn(temp_name)} (val) VALUES (%s)"
        order_clause = f"val{collate}"
    elif vendor == "mysql":
        # MySQL has no temporary tables sharing a transaction-safe rollback;
        # DDL auto-commits, so the table is dropped in finally.
        collate = f" COLLATE {collation_name}" if collation_name else ""
        ddl = f"CREATE TEMPORARY TABLE {qn(temp_name)} (val VARCHAR(255){collate} NOT NULL)"
        insert = f"INSERT INTO {qn(temp_name)} (val) VALUES (%s)"
        order_clause = f"val{collate}"
    else:  # mssql
        temp_name = f"#recode_probe_{abs(hash((model._meta.label, alphabet, steplen, marker))) % 10_000_000}"
        collate = f" COLLATE {collation_name}" if collation_name else ""
        ddl = f"CREATE TABLE {temp_name} (val NVARCHAR(255){collate} NOT NULL)"
        insert = f"INSERT INTO {temp_name} (val) VALUES (%s)"
        order_clause = f"val{collate}"

    index = {char: i for i, char in enumerate(alphabet)}
    expected_sorted = sorted(probes, key=lambda value: tuple(index.get(char, -1) for char in value))

    try:
        with connection.cursor() as cursor:
            cursor.execute(ddl)
            cursor.executemany(insert, [(probe,) for probe in probes])
            # charset roundtrip
            cursor.execute(f"SELECT val FROM {qn(temp_name) if vendor != 'mssql' else temp_name}")
            stored = {row[0] for row in cursor.fetchall()}
            for probe in probes:
                if probe not in stored:
                    problems.append(
                        {
                            "category": CHARSET,
                            "pk": None,
                            "path": None,
                            "detail": f"character(s) {probe!r} not stored/returned intact (column {type_name})",
                        }
                    )
            # case-folding / distinctness
            cursor.execute(
                f"SELECT val FROM {qn(temp_name) if vendor != 'mssql' else temp_name} GROUP BY {order_clause}"
            )
            distinct = [row[0] for row in cursor.fetchall()]
            if len(distinct) != len(probes):
                problems.append(
                    {
                        "category": COLLATION_CASE,
                        "pk": None,
                        "path": None,
                        "detail": (
                            f"{len(probes) - len(distinct)} probe value(s) compare equal under the "
                            f"database collation{f' ({collation_name})' if collation_name else ''}; "
                            "choose a case-sensitive/binary collation or a case-unambiguous alphabet"
                        ),
                    }
                )
            else:
                # ordering
                cursor.execute(
                    f"SELECT val FROM {qn(temp_name) if vendor != 'mssql' else temp_name} "
                    f"ORDER BY {order_clause}"
                )
                db_order = [row[0] for row in cursor.fetchall()]
                if db_order != expected_sorted:
                    mismatch = next(
                        (idx for idx, (got, want) in enumerate(zip(db_order, expected_sorted)) if got != want),
                        None,
                    )
                    problems.append(
                        {
                            "category": COLLATION_INCOMPATIBLE,
                            "pk": None,
                            "path": None,
                            "detail": (
                                "database ordering does not match alphabet ordering"
                                f"{f' under {collation_name}' if collation_name else ''}"
                                + (f" at probe {mismatch}" if mismatch is not None else "")
                                + "; choose a binary/case-sensitive collation"
                            ),
                        }
                    )
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"DROP TABLE {qn(temp_name) if vendor != 'mssql' else temp_name}")
        except Exception:
            pass

    return problems

# ---------------------------------------------------------------------------
# Durable mapping/journal tables (deterministic recovery)
# ---------------------------------------------------------------------------

JOURNAL_TABLE = "treebeard_mp_recode_journal"

MARKER_CANDIDATES = [".", ",", ":", "[", "]", '"', "'"]


def _table_exists(cursor, table_name):
    vendor = cursor.db.vendor
    if vendor == "sqlite":
        cursor.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = %s", [table_name])
    elif vendor == "postgresql":
        cursor.execute("SELECT 1 FROM information_schema.tables WHERE table_name = %s", [table_name])
    elif vendor == "mysql":
        cursor.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s",
            [table_name],
        )
    else:  # mssql (user tables; temp tables are handled separately)
        cursor.execute("SELECT 1 FROM sys.tables WHERE name = %s", [table_name])
    return cursor.fetchone() is not None


def _mysql_column_charset_collation(cursor, model):
    cursor.execute(
        """
        SELECT CHARACTER_SET_NAME, COLLATION_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """,
        [model._meta.db_table, "path"],
    )
    row = cursor.fetchone()
    if row and row[0]:
        return f" CHARACTER SET {row[0]} COLLATE {row[1]}"
    return ""


def _ensure_tables(model, using=None):
    """Create the shared journal table and the per-model mapping table."""
    using = using or router.db_for_write(model)
    connection = connections[using]
    qn = connection.ops.quote_name
    map_table, _ = _table_names(model)
    path_field = model._meta.get_field("path")
    path_len = path_field.max_length or 255
    with connection.cursor() as cursor:
        charset_suffix = ""
        if connection.vendor == "mysql":
            charset_suffix = _mysql_column_charset_collation(cursor, model)
        if not _table_exists(cursor, JOURNAL_TABLE):
            cursor.execute(
                f"CREATE TABLE {qn(JOURNAL_TABLE)} ("
                "model_label VARCHAR(255) PRIMARY KEY NOT NULL, "
                "old_alphabet VARCHAR(255) NOT NULL, "
                "old_steplen INTEGER NOT NULL, "
                "new_alphabet VARCHAR(255) NOT NULL, "
                "new_steplen INTEGER NOT NULL, "
                "marker VARCHAR(16) NOT NULL, "
                "phase VARCHAR(8) NOT NULL, "
                "started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
                f"){charset_suffix}"
            )
        if not _table_exists(cursor, map_table):
            text_type = "VARCHAR"
            cursor.execute(
                f"CREATE TABLE {qn(map_table)} ("
                "node_pk VARCHAR(255) PRIMARY KEY NOT NULL, "
                f"old_path {text_type}({path_len}) NOT NULL, "
                f"marked_path {text_type}({path_len + 16}) NOT NULL, "
                f"new_path {text_type}({path_len}) NOT NULL, "
                "stage VARCHAR(8) NOT NULL, "
                "old_depth INTEGER NOT NULL, "
                "old_numchild INTEGER NOT NULL, "
                "new_depth INTEGER NOT NULL, "
                "new_numchild INTEGER NOT NULL"
                f"){charset_suffix}"
            )
            cursor.execute(f"CREATE INDEX {qn(map_table + '_stage')} ON {qn(map_table)} (stage)")


def _get_journal(model, using=None):
    using = using or router.db_for_write(model)
    connection = connections[using]
    qn = connection.ops.quote_name
    with connection.cursor() as cursor:
        if not _table_exists(cursor, JOURNAL_TABLE):
            return None
        cursor.execute(f"SELECT * FROM {qn(JOURNAL_TABLE)} WHERE model_label = %s", [model._meta.label])
        row = cursor.fetchone()
        if not row:
            return None
        cols = [desc[0] for desc in cursor.description]
        return dict(zip(cols, row))


def _set_phase(model, phase, using):
    connection = connections[using]
    qn = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {qn(JOURNAL_TABLE)} SET phase = %s, updated_at = CURRENT_TIMESTAMP WHERE model_label = %s",
            [phase, model._meta.label],
        )


def _map_rows(model, stage=None, using=None):
    using = using or router.db_for_write(model)
    connection = connections[using]
    map_table, _ = _table_names(model)
    qn = connection.ops.quote_name
    query = (
        f"SELECT node_pk, old_path, marked_path, new_path, stage, "
        f"old_depth, old_numchild, new_depth, new_numchild FROM {qn(map_table)}"
    )
    params = []
    if stage:
        placeholders = ", ".join(["%s"] * len(stage))
        query += f" WHERE stage IN ({placeholders})"
        params = list(stage)
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        cols = [desc[0] for desc in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _chunks(sequence, size):
    for i in range(0, len(sequence), size):
        yield sequence[i : i + size]


def _set_map_stage(cursor, map_table, pks, stage):
    qn = cursor.db.ops.quote_name
    placeholders = ", ".join(["%s"] * len(pks))
    cursor.execute(
        f"UPDATE {qn(map_table)} SET stage = %s WHERE node_pk IN ({placeholders})",
        [stage, *pks],
    )


# ---------------------------------------------------------------------------
# Planning / dry run
# ---------------------------------------------------------------------------

def plan_recode(
    model,
    old_alphabet,
    old_steplen,
    new_alphabet,
    new_steplen,
    *,
    repair_fields=False,
    using=None,
):
    """
    Read-only analysis of a prospective migration.

    Decodes the tree under the old encoding, validates the new alphabet,
    per-level capacity, path column length and database collation, and plans
    the collision-free marker for the two-phase rewrite. Nothing in the tree
    is modified.
    """
    using = using or router.db_for_read(model)
    _validate_params(old_alphabet, old_steplen, new_alphabet, new_steplen)

    nodes, problems = analyze_mp_paths(model, old_alphabet, old_steplen, using=using)
    if repair_fields:
        # Stored depth/numchild are recomputed from the decoded structure
        # during the write anyway; only genuinely structural corruption
        # (bad paths, orphans) must block the migration.
        problems = [
            problem
            for problem in problems
            if problem["category"] not in {WRONG_DEPTH, WRONG_NUMCHILD}
        ]
    assignments, capacity_problems, max_siblings, capacity = _build_new_paths(
        nodes, new_alphabet, new_steplen
    )
    problems.extend(capacity_problems)

    path_len = (model._meta.get_field("path").max_length) or 255
    max_new_length = max((len(path) for path in assignments.values()), default=0)
    max_old_length = max((len(node["path"]) for node in nodes), default=0)
    if max_new_length > path_len:
        problems.append(
            {
                "category": PATH_TOO_LONG,
                "pk": None,
                "path": None,
                "detail": f"longest rewritten path is {max_new_length} chars but the column holds {path_len}",
            }
        )

    marker = next((char for char in MARKER_CANDIDATES if char not in old_alphabet and char not in new_alphabet), None)
    if marker is None:
        problems.append(
            {
                "category": NO_MARKER_SPACE,
                "pk": None,
                "path": None,
                "detail": "no marker character is available outside the old and new alphabets",
            }
        )
    elif max_old_length + len(marker) > path_len:
        problems.append(
            {
                "category": NO_MARKER_SPACE,
                "pk": None,
                "path": None,
                "detail": (
                    f"longest path is {max_old_length} chars and the marker adds {len(marker)}; "
                    f"the column holds {path_len}. Widen the path column before migrating"
                ),
            }
        )

    # Database storage + collation probes run only when the data itself is sound
    # enough to be worth checking, and never when capacity already overflows
    # (the produced paths would not be representative).
    fatal_data = any(problem["category"] in {BAD_ALPHABET, BAD_STEPLEN, ZERO_STEP, ORPHAN} for problem in problems)
    if not fatal_data and not any(problem["category"] == CAPACITY for problem in problems):
        problems.extend(
            check_database_compatibility(
                model, new_alphabet, new_steplen, marker=marker, using=using
            )
        )

    return RecodePlan(
        model_label=model._meta.label,
        node_count=len(nodes),
        new_alphabet=new_alphabet,
        new_steplen=new_steplen,
        capacity_per_level=capacity,
        max_siblings=max_siblings,
        max_path_length=max_new_length,
        path_max_length=path_len,
        marker=marker,
        problems=problems,
    )


def _validate_params(old_alphabet, old_steplen, new_alphabet, new_steplen):
    for name, alphabet in (("old_alphabet", old_alphabet), ("new_alphabet", new_alphabet)):
        if not isinstance(alphabet, str) or len(alphabet) < 2:
            raise RecodeValidationError(f"{name} must be a string of at least 2 characters")
        if len(set(alphabet)) != len(alphabet):
            raise RecodeValidationError(f"{name} contains duplicated characters: {alphabet!r}")
    for name, steplen in (("old_steplen", old_steplen), ("new_steplen", new_steplen)):
        if not isinstance(steplen, int) or steplen < 1:
            raise RecodeValidationError(f"{name} must be a positive integer")

# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def recode_mp_paths(
    model,
    old_alphabet,
    old_steplen,
    new_alphabet,
    new_steplen,
    *,
    using=None,
    batch_size=500,
    dry_run=False,
    repair_fields=False,
):
    """
    Recode all materialized paths of ``model`` from the old encoding to the new
    encoding. Primary keys and business fields are never modified.

    The model must still run with the old encoding; deploy new
    ``alphabet``/``steplen`` class attributes only after this succeeds.

    :param dry_run: perform all read-only checks and return a
                    :class:`RecodePlan` without writing anything.
    :raises RecodeValidationError: if the data or target encoding is invalid.
    :raises RecodeStateError: if a previous migration is in progress or if the
                              model class is already configured with different
                              parameters.
    """
    using = using or router.db_for_write(model)

    if model.alphabet != old_alphabet or model.steplen != old_steplen:
        raise RecodeStateError(
            f"{CONFIG_MISMATCH}: model {model._meta.label} is configured with "
            f"alphabet={model.alphabet!r}, steplen={model.steplen}, but the declared old "
            f"parameters are alphabet={old_alphabet!r}, steplen={old_steplen}. "
            "Run the migration while the model still uses the old encoding"
        )

    journal = _get_journal(model, using=using)
    if journal is not None:
        if journal["phase"] == "done":
            raise RecodeStateError(
                f"{model._meta.label} is already recoded to the recorded target parameters; "
                "use resume_recode() to verify/continue, rollback_recode() to undo, "
                "or forget_recode() to clear the migration metadata"
            )
        raise RecodeStateError(
            f"{model._meta.label} has an interrupted recoding (phase {journal['phase']}); "
            "use resume_recode() or rollback_recode()"
        )

    plan = plan_recode(
        model,
        old_alphabet=old_alphabet,
        old_steplen=old_steplen,
        new_alphabet=new_alphabet,
        new_steplen=new_steplen,
        repair_fields=repair_fields,
        using=using,
    )
    if dry_run:
        return plan
    plan.raise_if_problems()

    nodes, _ = analyze_mp_paths(model, old_alphabet, old_steplen, using=using)
    assignments, _, _, _ = _build_new_paths(nodes, new_alphabet, new_steplen)

    if not nodes:
        return RecodeResult(
            model_label=model._meta.label,
            node_count=0,
            old_alphabet=old_alphabet,
            old_steplen=old_steplen,
            new_alphabet=new_alphabet,
            new_steplen=new_steplen,
            marker=plan.marker,
        )

    _ensure_tables(model, using=using)
    _create_journal(model, old_alphabet, old_steplen, new_alphabet, new_steplen, plan.marker, using)
    _populate_mapping(model, nodes, assignments, plan.marker, new_steplen, using)
    counts = _run_phase_a(model, using, batch_size)
    counts.update(_run_phase_b(model, using, batch_size))
    _verify_final(model, new_alphabet, new_steplen, using)

    return RecodeResult(
        model_label=model._meta.label,
        node_count=len(nodes),
        old_alphabet=old_alphabet,
        old_steplen=old_steplen,
        new_alphabet=new_alphabet,
        new_steplen=new_steplen,
        marker=plan.marker,
        phase_a_written=counts["a"],
        phase_b_written=counts["b"],
    )


def resume_recode(model, *, using=None, batch_size=500):
    """
    Resume an interrupted migration from its durable mapping/journal tables.

    The mapping makes recovery deterministic: every row is either at its old
    path, its marked path or its new path, and the table records which.
    Raises :class:`RecodeStateError` if no migration metadata exists.
    """
    using = using or router.db_for_write(model)
    journal = _require_journal(model, using)
    marker = journal["marker"]
    new_alphabet = journal["new_alphabet"]
    new_steplen = journal["new_steplen"]

    _guard_resumable(model, using)
    counts = _run_phase_a(model, using, batch_size)
    counts.update(_run_phase_b(model, using, batch_size))
    if journal["phase"] != "done":
        _set_phase(model, "done", using)
    _verify_final(model, new_alphabet, new_steplen, using)

    rows = _map_rows(model, using=using)
    return RecodeResult(
        model_label=model._meta.label,
        node_count=len(rows),
        old_alphabet=journal["old_alphabet"],
        old_steplen=journal["old_steplen"],
        new_alphabet=new_alphabet,
        new_steplen=new_steplen,
        marker=marker,
        resumed=True,
        phase_a_written=counts.get("a", 0),
        phase_b_written=counts.get("b", 0),
    )


def rollback_recode(model, *, using=None, batch_size=500, drop_tables=True):
    """
    Undo a migration (completed or interrupted), restoring exactly the old
    paths/depth/numchild values recorded before the writes.

    The restoration is also collision-free: marked/new paths are first moved
    back to marked space and then to the original paths.
    """
    using = using or router.db_for_write(model)
    journal = _require_journal(model, using)
    rows = _map_rows(model, using=using)
    if not rows:
        _finish(model, using, drop_tables)
        return 0

    marker = journal["marker"]
    for chunk in _chunks(rows, batch_size):
        chunk = [row for row in chunk if row["stage"] == "b"]
        if chunk:
            _undo_b_chunk(model, chunk, using)
    for chunk in _chunks(rows, batch_size):
        chunk = [row for row in chunk if row["stage"] in ("a", "b")]
        if chunk:
            _undo_a_chunk(model, chunk, marker, using)
    restored = len(rows)
    if drop_tables:
        _finish(model, using, drop_tables)
    return restored


def forget_recode(model, *, using=None):
    """Drop the mapping/journal tables after a migration is confirmed good."""
    _finish(model, using or router.db_for_write(model), True)


def recode_status(model, *, using=None):
    """Return the recorded journal row for ``model`` or ``None``."""
    return _get_journal(model, using=using or router.db_for_read(model))


# ---------------------------------------------------------------------------
# Internals: writes
# ---------------------------------------------------------------------------

def _create_journal(model, old_alphabet, old_steplen, new_alphabet, new_steplen, marker, using):
    connection = connections[using]
    qn = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {qn(JOURNAL_TABLE)} "
            "(model_label, old_alphabet, old_steplen, new_alphabet, new_steplen, marker, phase) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [model._meta.label, old_alphabet, old_steplen, new_alphabet, new_steplen, marker, "map"],
        )


def _populate_mapping(model, nodes, assignments, marker, new_steplen, using):
    connection = connections[using]
    map_table, _ = _table_names(model)
    qn = connection.ops.quote_name
    rows = []
    for node in nodes:
        if node["fatal"]:
            continue
        new_path = assignments[node["pk"]]
        rows.append(
            (
                str(node["pk"]),
                node["path"],
                marker + node["path"],
                new_path,
                "p",
                node["depth"],
                node["numchild"],
                len(new_path) // new_steplen,
                node.get("real_numchild", node["numchild"]),
            )
        )
    placeholders = ", ".join(["%s"] * 9)
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.executemany(
                f"INSERT INTO {qn(map_table)} "
                "(node_pk, old_path, marked_path, new_path, stage, old_depth, old_numchild, new_depth, new_numchild) "
                f"VALUES ({placeholders})",
                rows,
            )
        else:
            cursor.executemany(
                f"INSERT INTO {qn(map_table)} "
                "(node_pk, old_path, marked_path, new_path, stage, old_depth, old_numchild, new_depth, new_numchild) "
                f"VALUES ({placeholders})",
                rows,
            )
    _set_phase(model, "a", using)


def _run_phase_a(model, using, batch_size):
    """old_path -> marker + old_path (move every row out of old path space)."""
    written = 0
    journal = _get_journal(model, using=using)
    marker = journal["marker"] if journal else None
    while True:
        rows = _map_rows(model, stage=("p",), using=using)[:batch_size]
        if not rows:
            break
        old_paths = [row["old_path"] for row in rows]
        with transaction.atomic(using=using):
            model.objects.using(using).filter(path__in=old_paths).update(
                path=Concat(Value(marker), F("path"))
            )
            connection = connections[using]
            map_table, _ = _table_names(model)
            with connection.cursor() as cursor:
                _set_map_stage(cursor, map_table, [row["node_pk"] for row in rows], "a")
        written += len(rows)
    return {"a": written}


def _run_phase_b(model, using, batch_size):
    """marked_path -> new_path, recomputing depth and numchild from the map."""
    written = 0
    while True:
        rows = _map_rows(model, stage=("a",), using=using)[:batch_size]
        if not rows:
            break
        whens_path = [When(path=row["marked_path"], then=Value(row["new_path"])) for row in rows]
        whens_depth = [When(path=row["marked_path"], then=Value(row["new_depth"])) for row in rows]
        whens_numchild = [
            When(path=row["marked_path"], then=Value(row["new_numchild"])) for row in rows
        ]
        marked_paths = [row["marked_path"] for row in rows]
        with transaction.atomic(using=using):
            model.objects.using(using).filter(path__in=marked_paths).update(
                path=Case(*whens_path, output_field=CharField()),
                depth=Case(*whens_depth),
                numchild=Case(*whens_numchild),
            )
            connection = connections[using]
            map_table, _ = _table_names(model)
            with connection.cursor() as cursor:
                _set_map_stage(cursor, map_table, [row["node_pk"] for row in rows], "b")
        written += len(rows)
    _set_phase(model, "done", using)
    return {"b": written}


def _undo_b_chunk(model, rows, using):
    """new_path -> marked_path."""
    whens = [When(path=row["new_path"], then=Value(row["marked_path"])) for row in rows]
    whens_depth = [When(path=row["new_path"], then=Value(row["old_depth"])) for row in rows]
    whens_numchild = [When(path=row["new_path"], then=Value(row["old_numchild"])) for row in rows]
    new_paths = [row["new_path"] for row in rows]
    with transaction.atomic(using=using):
        model.objects.using(using).filter(path__in=new_paths).update(
            path=Case(*whens, output_field=CharField()),
            depth=Case(*whens_depth),
            numchild=Case(*whens_numchild),
        )
        connection = connections[using]
        map_table, _ = _table_names(model)
        with connection.cursor() as cursor:
            _set_map_stage(cursor, map_table, [row["node_pk"] for row in rows], "a")


def _undo_a_chunk(model, rows, marker, using):
    """marked_path -> old_path."""
    marked_paths = [row["marked_path"] for row in rows]
    with transaction.atomic(using=using):
        whens_path = [
            When(path=row["marked_path"], then=Value(row["old_path"])) for row in rows
        ]
        whens_depth = [
            When(path=row["marked_path"], then=Value(row["old_depth"])) for row in rows
        ]
        whens_numchild = [
            When(path=row["marked_path"], then=Value(row["old_numchild"])) for row in rows
        ]
        model.objects.using(using).filter(path__in=marked_paths).update(
            path=Case(*whens_path, output_field=CharField()),
            depth=Case(*whens_depth),
            numchild=Case(*whens_numchild),
        )
        connection = connections[using]
        map_table, _ = _table_names(model)
        with connection.cursor() as cursor:
            _set_map_stage(cursor, map_table, [row["node_pk"] for row in rows], "p")


def _finish(model, using, drop_tables):
    connection = connections[using]
    map_table, _ = _table_names(model)
    qn = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {qn(JOURNAL_TABLE)} WHERE model_label = %s", [model._meta.label]
        )
        if drop_tables and _table_exists(cursor, map_table):
            cursor.execute(f"DROP TABLE {qn(map_table)}")


def _require_journal(model, using):
    journal = _get_journal(model, using=using)
    if journal is None:
        raise RecodeStateError(f"no recoding metadata found for {model._meta.label}")
    return journal


def _guard_resumable(model, using):
    """
    Verify the durable mapping agrees with the physical paths so recovery is
    deterministic. A crash while the mapping was being written can leave tree
    rows without a mapping row, which cannot be resumed blindly.
    """
    rows = _map_rows(model, using=using)
    if not rows:
        raise RecodeStateError(
            "mapping table is empty; the migration cannot be resumed. "
            "Use rollback_recode() to clean up and restart with recode_mp_paths()"
        )
    total_nodes = model.objects.using(using).count()
    if total_nodes != len(rows):
        raise RecodeStateError(
            f"mapping covers {len(rows)} node(s) but the table holds {total_nodes}; "
            "the mapping was interrupted while being written. "
            "Use rollback_recode() to clean up and restart with recode_mp_paths()"
        )
    map_pks = {row["node_pk"] for row in rows}
    real_pks = {str(pk) for pk in model.objects.using(using).values_list("pk", flat=True)}
    if map_pks != real_pks:
        raise RecodeStateError(
            "mapping rows do not match the current node primary keys; "
            "use rollback_recode() to clean up and restart"
        )
    by_pk = {row["node_pk"]: row for row in rows}
    # Every physical row must sit in exactly one of the three states. The
    # marker prefix is the only unambiguous discriminator (new and old path
    # spaces can overlap textually); for un-prefixed paths the mapping stage
    # tells old (p) from new (b).
    for current_pk, current_path in model.objects.using(using).values_list("pk", "path"):
        row = by_pk[str(current_pk)]
        if row["stage"] == "a":
            consistent = current_path == row["marked_path"]
        elif row["stage"] == "b":
            consistent = current_path == row["new_path"]
        elif row["stage"] == "p":
            consistent = current_path == row["old_path"]
        else:
            consistent = False
        if not consistent:
            raise RecodeStateError(
                f"path {current_path!r} (pk {current_pk}) is not described by the migration mapping; "
                "cannot resume"
            )


def _verify_final(model, new_alphabet, new_steplen, using):
    nodes, problems = analyze_mp_paths(model, new_alphabet, new_steplen, using=using)
    fatal = {
        BAD_ALPHABET,
        BAD_STEPLEN,
        ZERO_STEP,
        ORPHAN,
        WRONG_DEPTH,
        WRONG_NUMCHILD,
    }
    blocking = [problem for problem in problems if problem["category"] in fatal]
    if blocking:
        raise RecodeValidationError(
            "post-write verification found structural problems", blocking
        )
    rows = _map_rows(model, using=using)
    current_paths = {node["path"] for node in nodes if not node["fatal"]}
    expected_paths = {row["new_path"] for row in rows}
    if current_paths != expected_paths or len(current_paths) != len(rows):
        raise RecodeValidationError(
            "post-write verification failed: rewritten paths do not match the migration mapping",
            [{"category": "mapping_mismatch", "pk": None, "path": None}],
        )
    # nothing may remain in marked (intermediate) space
    if any(node["path"].startswith(rows[0]["marked_path"][0]) for node in nodes if rows):
        raise RecodeValidationError(
            "post-write verification failed: intermediate (marked) paths remain",
            [{"category": "marked_remaining", "pk": None, "path": None}],
        )
