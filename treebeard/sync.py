"""
Synchronize a materialized path subtree with an external manifest.

This module implements a sync entry point on top of the existing
:py:class:`~treebeard.mp_tree.MP_Node` add/move/delete primitives. Instead of
wiping a subtree and reloading it with
:py:meth:`~treebeard.mp_tree.MP_NodeManager.load_bulk`, the manifest delivered
by an upstream system (e.g. a daily directory tree export) is reconciled
against the current database content: nodes are matched by a stable external
identity, and the differences are applied as field updates, moves, additions
and (optionally) deletions. Primary keys of existing nodes are preserved, so
renaming a node and then moving it does not break foreign keys pointing at
it.

Manifest format
---------------

The manifest can be given in two shapes:

* nested (like :py:meth:`~treebeard.mp_tree.MP_NodeManager.load_bulk`)::

      [
          {"external_id": "a", "data": {"desc": "A"}, "children": [
              {"external_id": "b", "data": {"desc": "B"}},
          ]},
      ]

* flat, with parent references and optional explicit positions::

      [
          {"external_id": "a", "parent": None, "data": {"desc": "A"}},
          {"external_id": "b", "parent": "a", "position": 0, "data": {"desc": "B"}},
      ]

``data`` holds model field values used to create missing nodes and to update
existing ones. The identity key (``external_id`` by default, configurable via
``identity_field``) must be a real field on the model, preferably with a
unique constraint.

Signals and instance caches
---------------------------

Synchronization uses the regular treebeard primitives, so the usual signals
apply:

* additions fire Django's ``pre_save``/``post_save`` for every new node;
* field updates fire ``pre_save``/``post_save`` with ``update_fields`` set to
  the changed fields;
* moves fire treebeard's :py:data:`treebeard.mp_tree.path_updated` for every
  rewritten branch (a single move can rewrite several sibling branches);
* deletions go through the queryset ``delete()`` and therefore fire Django's
  ``pre_delete``/``post_delete`` for every removed node, plus treebeard's
  :py:data:`treebeard.mp_tree.nodes_deleted` once per deleted branch.

Internally the sync never relies on cached node state: every node is
re-fetched from the database right before it is moved or saved, because a
move rewrites the ``path``/``depth`` of other nodes (treebeard itself clears
``_cached_parent_obj`` and friends in ``refresh_from_db()``). For the same
reason, any node instance your own code fetched *before* applying a sync must
be considered stale afterwards - call ``refresh_from_db()`` or re-fetch it.
The class-level ``numconv_obj`` cache is not affected by synchronization.
"""

import hashlib
from dataclasses import dataclass, field

from django.core.exceptions import FieldDoesNotExist
from django.db import transaction

from treebeard.exceptions import (
    DuplicateIdentity,
    IdentityConflict,
    InvalidManifestOrder,
    ManifestCycle,
    PathOverflow,
    ProtectedNode,
    StaleSyncSummary,
    SyncError,
)
from treebeard.mp_tree import MP_Node

TREE_FIELDS = ("path", "depth", "numchild")

#: Change kinds reported in a :class:`SyncPlan`.
CHANGE_ADD = "add"
CHANGE_UPDATE = "update"
CHANGE_MOVE = "move"
CHANGE_REORDER = "reorder"
CHANGE_DELETE = "delete"
CHANGE_KEEP = "keep"


@dataclass
class SyncChange:
    """A single planned change. See :attr:`SyncPlan.changes`."""

    kind: str
    identity: object = None
    detail: dict = field(default_factory=dict)

    def __str__(self):
        return f"{self.kind} {self.identity!r} {self.detail}".rstrip()


@dataclass
class SyncPlan:
    """
    The result of :func:`sync_tree`.

    ``changes`` lists every planned (dry-run) or applied change,
    ``old_summary`` is a digest of the scope structure before the sync and
    ``old_structure`` is a human readable outline of it. After a successful
    apply, ``applied`` is True and ``new_summary`` holds the digest of the
    resulting structure.
    """

    changes: list
    old_summary: str
    old_structure: str
    applied: bool = False
    new_summary: str | None = None

    @property
    def has_changes(self):
        """:returns: True if the plan contains any modification."""
        return any(change.kind != CHANGE_KEEP for change in self.changes)

    def __str__(self):
        lines = [f"sync plan ({len(self.changes)} changes, applied={self.applied})"]
        lines.extend(f"  {change}" for change in self.changes)
        return "\n".join(lines)


class _MNode:
    """A node of the parsed manifest tree."""

    __slots__ = ("identity", "data", "children", "parent_identity")

    def __init__(self, identity, data, parent_identity):
        self.identity = identity
        self.data = data
        self.children = []
        self.parent_identity = parent_identity


def _iter_manifest(forest):
    """Iterates over the manifest tree, parents before children (BFS)."""
    queue = list(forest)
    while queue:
        mnode = queue.pop(0)
        yield mnode
        queue.extend(mnode.children)


def _iter_groups(forest):
    """Yields (parent_identity, children) sibling groups, top levels first."""
    yield None, list(forest)
    for mnode in _iter_manifest(forest):
        if mnode.children:
            yield mnode.identity, mnode.children


class _Prepared:
    """Everything derived from the manifest and the current database state."""

    def __init__(self):
        self.anchor = None
        self.scope_nodes = []  # all nodes in scope, ordered by path
        self.pk_by_identity = {}  # identity -> pk, for scope nodes
        self.node_by_pk = {}
        self.identity_by_path = {}
        self.node_by_path = {}
        self.parent_identity_by_pk = {}
        self.changes = []
        self.summary = ""
        self.structure = ""
        self.updates = {}  # identity -> {field: new_value}
        self.phase1_delete_pks = []  # missing subtrees without manifest content, deleted before moves
        self.phase3_delete_pks = []  # missing subtrees that had manifest content, deleted after moves


def _validate_data_fields(model, data, identity_field, pk_name):
    for key in data:
        if key == identity_field:
            raise SyncError(
                f"{identity_field!r} is the identity field and must be given as the manifest key, not in 'data'"
            )
        if key in TREE_FIELDS or key == pk_name:
            raise SyncError(f"{key!r} is managed by treebeard and cannot be synchronized")
        try:
            model_field = model._meta.get_field(key)
        except FieldDoesNotExist:
            raise SyncError(f"{key!r} is not a field of {model.__name__}") from None
        if not model_field.concrete or model_field.many_to_many:
            raise SyncError(f"{key!r} is not a concrete model field and cannot be synchronized")


def _parse_manifest(model, manifest, identity_field):
    """
    Validates and normalizes the manifest.

    :returns: a tuple (forest, mnodes) where ``forest`` is the ordered list of
        top-level :class:`_MNode` and ``mnodes`` maps identity to :class:`_MNode`.
    """
    entries = list(manifest)
    for entry in entries:
        if not isinstance(entry, dict):
            raise SyncError(f"Manifest entries must be dictionaries, got {entry!r}")

    pk_name = model._meta.pk.attname
    flat = any("parent" in entry for entry in entries)
    mnodes = {}

    def make_node(entry, parent_identity):
        if identity_field not in entry:
            raise SyncError(f"Manifest entry is missing the identity key {identity_field!r}: {entry!r}")
        identity = entry[identity_field]
        if identity is None:
            raise SyncError("Manifest identities must not be None")
        if identity in mnodes:
            raise DuplicateIdentity(f"Identity {identity!r} appears more than once in the manifest")
        data = entry.get("data") or {}
        if not isinstance(data, dict):
            raise SyncError(f"The 'data' of manifest entry {identity!r} must be a dictionary")
        _validate_data_fields(model, data, identity_field, pk_name)
        mnode = _MNode(identity, data, parent_identity)
        mnodes[identity] = mnode
        return mnode

    if flat:
        for entry in entries:
            if "children" in entry:
                raise SyncError("Cannot mix the flat manifest format ('parent' keys) with 'children'")
            if "parent" not in entry:
                raise SyncError(f"Flat manifest entry is missing the 'parent' key: {entry!r}")
            make_node(entry, entry["parent"])
        # unknown parent references
        for mnode in mnodes.values():
            if mnode.parent_identity is not None and mnode.parent_identity not in mnodes:
                raise SyncError(
                    f"Manifest entry {mnode.identity!r} references unknown parent {mnode.parent_identity!r}"
                )
        # cycles
        for mnode in mnodes.values():
            seen = {mnode.identity}
            cursor = mnode
            while cursor.parent_identity is not None:
                if cursor.parent_identity in seen:
                    raise ManifestCycle(f"The manifest parent references form a cycle at {mnode.identity!r}")
                seen.add(cursor.parent_identity)
                cursor = mnodes[cursor.parent_identity]
        # sibling groups, in manifest order, with optional explicit positions
        children_of = {}
        positions = {}
        for entry in entries:
            mnode = mnodes[entry[identity_field]]
            children_of.setdefault(mnode.parent_identity, []).append(mnode)
            positions[mnode.identity] = entry.get("position")
        forest = []
        for parent_identity, siblings in children_of.items():
            given = [positions[sibling.identity] for sibling in siblings]
            if any(pos is not None for pos in given):
                if not all(isinstance(pos, int) and not isinstance(pos, bool) for pos in given):
                    raise InvalidManifestOrder(
                        f"Siblings under {parent_identity!r} must either omit 'position' entirely "
                        "or all give an integer position"
                    )
                if sorted(given) != list(range(len(siblings))):
                    raise InvalidManifestOrder(
                        f"Positions under {parent_identity!r} must be a permutation of "
                        f"0..{len(siblings) - 1}, got {sorted(given)}"
                    )
                siblings.sort(key=lambda sibling: positions[sibling.identity])
            if parent_identity is None:
                forest = siblings
            else:
                mnodes[parent_identity].children = siblings
        return forest, mnodes

    # nested format
    def walk(entry, parent_identity):
        if "parent" in entry or "position" in entry:
            raise SyncError("Cannot mix the nested manifest format ('children') with 'parent'/'position' keys")
        mnode = make_node(entry, parent_identity)
        children = entry.get("children") or []
        if not isinstance(children, (list, tuple)):
            raise SyncError(f"The 'children' of manifest entry {mnode.identity!r} must be a list")
        mnode.children = [walk(child, mnode.identity) for child in children]
        return mnode

    forest = [walk(entry, None) for entry in entries]
    return forest, mnodes


def _snapshot(model, anchor, identity_field, prep):
    """Loads the current scope content into a :class:`_Prepared`."""
    prep.anchor = model.objects.get(pk=anchor.pk) if anchor is not None else None
    queryset = model.objects.all().order_by("path")
    if prep.anchor is not None:
        queryset = queryset.filter(path__startswith=prep.anchor.path).exclude(pk=prep.anchor.pk)
    prep.scope_nodes = list(queryset)
    anchor_path = prep.anchor.path if prep.anchor is not None else ""
    for node in prep.scope_nodes:
        identity = getattr(node, identity_field)
        prep.node_by_pk[node.pk] = node
        prep.node_by_path[node.path] = node
        prep.identity_by_path[node.path] = identity
        parent_path = node.path[: -model.steplen]
        prep.parent_identity_by_pk[node.pk] = (
            None if parent_path == anchor_path else prep.identity_by_path.get(parent_path)
        )
        if identity is None:
            continue
        if identity in prep.pk_by_identity:
            raise DuplicateIdentity(
                f"Identity {identity!r} is used by more than one node inside the synchronized scope"
            )
        prep.pk_by_identity[identity] = node.pk


def _summarize(model, prep, identity_field):
    """Computes the structure digest and a human readable outline of the scope."""
    field_names = [f.name for f in model._meta.concrete_fields if f.name not in TREE_FIELDS]
    field_attnames = [f.attname for f in model._meta.concrete_fields if f.name not in TREE_FIELDS]
    anchor_depth = prep.anchor.depth if prep.anchor is not None else 0
    lines = []
    outline = []
    for node in prep.scope_nodes:
        identity = getattr(node, identity_field)
        fingerprint = ",".join(
            f"{name}={getattr(node, attname)!r}" for name, attname in zip(field_names, field_attnames)
        )
        lines.append(f"{node.pk}\x1f{identity!r}\x1f{prep.parent_identity_by_pk[node.pk]!r}\x1f{fingerprint}")
        outline.append(f"{'  ' * (node.depth - anchor_depth - 1)}{identity!r}")
    summary = hashlib.sha256("\x1e".join(lines).encode()).hexdigest()
    return summary, "\n".join(outline)


def _check_capacity(model, prep, forest, mnodes, identity_field, on_missing, phase1_subtree_paths):
    """
    Ensures the target structure fits into the available path space.

    Raises PathOverflow before anything is written when the manifest is too
    deep, when a kept node would be carried too deep by a move, or when a
    sibling group would grow beyond what ``steplen``/``alphabet`` can encode.
    """
    steplen = model.steplen
    max_length = model._meta.get_field("path").max_length
    max_step = len(model.alphabet) ** steplen - 1
    anchor_depth = prep.anchor.depth if prep.anchor is not None else 0

    final_depth = {}

    def assign(mnode, depth):
        final_depth[mnode.identity] = depth
        for child in mnode.children:
            assign(child, depth + 1)

    for root in forest:
        assign(root, anchor_depth + 1)
    if final_depth and max(final_depth.values()) * steplen > max_length:
        raise PathOverflow(
            f"The manifest is too deep for the path field: needs depth {max(final_depth.values())}, "
            f"but only {max_length // steplen} levels fit"
        )

    # Missing nodes that stay in the tree are carried along when a manifest
    # ancestor is moved; make sure they would not be carried too deep.
    manifest_ids = set(mnodes)
    for node in prep.scope_nodes:
        identity = getattr(node, identity_field)
        if identity in manifest_ids:
            continue
        if any(node.path.startswith(path) for path in phase1_subtree_paths):
            continue  # deleted before any move happens, never rewritten
        ancestor_identity = None
        ancestor_depth = None
        for depth in range(node.depth - 1, anchor_depth, -1):
            candidate = prep.identity_by_path.get(node.path[: depth * steplen])
            if candidate in manifest_ids:
                ancestor_identity = candidate
                ancestor_depth = depth
                break
        if ancestor_identity is not None:
            carried_depth = final_depth[ancestor_identity] + (node.depth - ancestor_depth)
            if carried_depth * steplen > max_length:
                raise PathOverflow(
                    f"Node {identity!r} would be carried to depth {carried_depth} by the move of "
                    f"{ancestor_identity!r}, but only {max_length // steplen} levels fit"
                )

    # Sibling groups that receive manifest children must not overflow one path step.
    kept_children = {}
    if on_missing == "keep":
        for node in prep.scope_nodes:
            if getattr(node, identity_field) not in manifest_ids:
                parent_path = node.path[: -steplen]
                kept_children[parent_path] = kept_children.get(parent_path, 0) + 1
    anchor_path = prep.anchor.path if prep.anchor is not None else ""
    for parent_identity, children in _iter_groups(forest):
        if parent_identity is None:
            base_path = anchor_path
        elif parent_identity in prep.pk_by_identity:
            base_path = prep.node_by_pk[prep.pk_by_identity[parent_identity]].path
        else:
            base_path = None  # a new node, no existing children
        total = len(children) + kept_children.get(base_path, 0)
        if total > max_step:
            raise PathOverflow(
                f"{total} children would end up under {parent_identity!r}, "
                f"but at most {max_step} fit in one path step"
            )


def _find_missing(model, prep, mnodes, identity_field):
    """
    Splits the scope nodes absent from the manifest into top-most missing
    nodes with and without manifest-referenced descendants.

    :returns: (top_missing, with_manifest, without_manifest) - lists of pks.
    """
    manifest_ids = set(mnodes)
    missing = [node for node in prep.scope_nodes if getattr(node, identity_field) not in manifest_ids]
    missing_paths = {node.path for node in missing}
    top_missing = []
    for node in missing:
        if not any(node.path[: depth * model.steplen] in missing_paths for depth in range(1, node.depth)):
            top_missing.append(node)
    with_manifest = []
    without_manifest = []
    manifest_paths = [
        node.path for node in prep.scope_nodes if getattr(node, identity_field) in manifest_ids
    ]
    for node in top_missing:
        if any(path.startswith(node.path) for path in manifest_paths):
            with_manifest.append(node.pk)
        else:
            without_manifest.append(node.pk)
    return top_missing, with_manifest, without_manifest


def _check_sorted_order(model, prep, forest, mnodes):
    """
    For models with node_order_by, the manifest sibling order must agree with
    the effective values of the ordering fields, otherwise it can never be
    the resulting order and is rejected as invalid.
    """
    if not model.node_order_by:
        return

    def sort_key(mnode):
        db_node = prep.node_by_pk.get(prep.pk_by_identity.get(mnode.identity))
        key = []
        for field_name in model.node_order_by:
            descending = field_name.startswith("-")
            plain_name = field_name.lstrip("-")
            if plain_name in mnode.data:
                value = mnode.data[plain_name]
            elif db_node is not None:
                value = getattr(db_node, plain_name)
            else:
                value = model._meta.get_field(plain_name).get_default()
            key.append((descending, value))
        return key

    def compare(key_a, key_b):
        for (descending, value_a), (_, value_b) in zip(key_a, key_b):
            if value_a is None or value_b is None or value_a == value_b:
                continue
            less = value_a < value_b
            return 1 if less == descending else -1
        return 0

    for parent_identity, children in _iter_groups(forest):
        keys = [sort_key(mnode) for mnode in children]
        for pos in range(len(keys) - 1):
            if compare(keys[pos], keys[pos + 1]) > 0:
                raise InvalidManifestOrder(
                    f"The order of siblings under {parent_identity!r} contradicts node_order_by "
                    f"({model.node_order_by}): {children[pos].identity!r} must not come before "
                    f"{children[pos + 1].identity!r}"
                )


def _prepare(model, forest, mnodes, parent, identity_field, on_missing, protect):
    """Validates the manifest against the current state and plans the changes."""
    prep = _Prepared()
    _snapshot(model, parent, identity_field, prep)
    prep.summary, prep.structure = _summarize(model, prep, identity_field)
    manifest_ids = set(mnodes)

    # identities must not collide with nodes outside the scope
    conflicts = (
        model.objects.filter(**{f"{identity_field}__in": manifest_ids})
        .exclude(pk__in=[node.pk for node in prep.scope_nodes])
        .values_list(identity_field, flat=True)
    )
    conflicts = sorted(repr(identity) for identity in set(conflicts))
    if conflicts:
        raise IdentityConflict(
            f"Manifest identities exist outside the synchronized scope: {', '.join(conflicts)}"
        )

    top_missing, with_manifest, without_manifest = _find_missing(model, prep, mnodes, identity_field)
    if on_missing == "delete":
        prep.phase1_delete_pks = without_manifest
        prep.phase3_delete_pks = with_manifest

    phase1_subtree_paths = [prep.node_by_pk[pk].path for pk in prep.phase1_delete_pks]
    _check_capacity(model, prep, forest, mnodes, identity_field, on_missing, phase1_subtree_paths)
    _check_sorted_order(model, prep, forest, mnodes)

    # business protection for deletions
    if on_missing == "delete" and protect is not None:
        protected = []
        for node in prep.scope_nodes:
            identity = getattr(node, identity_field)
            if identity in manifest_ids:
                continue
            reason = protect(node)
            if reason:
                protected.append(f"{identity!r}" + (f" ({reason})" if isinstance(reason, str) else ""))
        if protected:
            raise ProtectedNode(f"Cannot delete protected nodes: {', '.join(protected)}")

    # plan field updates and additions
    for mnode in _iter_manifest(forest):
        pk = prep.pk_by_identity.get(mnode.identity)
        if pk is None:
            prep.changes.append(
                SyncChange(CHANGE_ADD, mnode.identity, {"parent": mnode.parent_identity, "data": mnode.data})
            )
            continue
        node = prep.node_by_pk[pk]
        changed = {name: value for name, value in mnode.data.items() if getattr(node, name) != value}
        if changed:
            prep.updates[mnode.identity] = changed
            prep.changes.append(
                SyncChange(
                    CHANGE_UPDATE,
                    mnode.identity,
                    {"fields": {name: {"old": getattr(node, name), "new": value} for name, value in changed.items()}},
                )
            )

    # plan moves and reorders
    anchor_path = prep.anchor.path if prep.anchor is not None else ""
    for parent_identity, children in _iter_groups(forest):
        desired = [mnode.identity for mnode in children]
        if parent_identity is None:
            base_path = anchor_path
        elif parent_identity in prep.pk_by_identity:
            base_path = prep.node_by_pk[prep.pk_by_identity[parent_identity]].path
        else:
            base_path = None  # new node, no children yet
        current = [
            getattr(node, identity_field)
            for node in prep.scope_nodes
            if node.path[: -model.steplen] == base_path
        ] if base_path is not None else []
        if on_missing == "delete":
            # nodes scheduled for deletion do not occupy sibling slots
            current = [identity for identity in current if identity in manifest_ids]
        kept = [identity for identity in current if identity not in manifest_ids] if on_missing == "keep" else []
        # additions are appended to their parent group before placements run
        simulated = current + [identity for identity in desired if identity not in prep.pk_by_identity]
        if simulated == desired + kept:
            continue
        moved = False
        for index, identity in enumerate(desired):
            pk = prep.pk_by_identity.get(identity)
            if pk is None:
                continue
            current_parent = prep.parent_identity_by_pk.get(pk)
            if current_parent != parent_identity:
                moved = True
                prep.changes.append(
                    SyncChange(
                        CHANGE_MOVE,
                        identity,
                        {"from_parent": current_parent, "to_parent": parent_identity, "to_index": index},
                    )
                )
        if not moved:
            prep.changes.append(SyncChange(CHANGE_REORDER, parent_identity, {"order": desired}))

    # plan deletions / kept missing nodes
    for pk in prep.phase1_delete_pks + prep.phase3_delete_pks:
        node = prep.node_by_pk[pk]
        descendants = sum(1 for other in prep.scope_nodes if other.path.startswith(node.path)) - 1
        prep.changes.append(
            SyncChange(CHANGE_DELETE, getattr(node, identity_field), {"pk": node.pk, "descendants": descendants})
        )
    if on_missing == "keep":
        for node in top_missing:
            prep.changes.append(SyncChange(CHANGE_KEEP, getattr(node, identity_field), {"pk": node.pk}))

    return prep


def _children_identities(model, parent_node, identity_field):
    if parent_node is None:
        queryset = model.objects.get_root_nodes()
    else:
        queryset = model.objects.get_children(parent_node)
    return list(queryset.values_list(identity_field, flat=True))


def _move(model, node, target, pos, anchor):
    """
    Moves ``node`` relative to ``target``, first parking it outside its own
    branch when the target is currently one of its descendants (which
    treebeard would reject with InvalidMoveToDescendant).
    """
    manager = model.objects
    node = manager.get(pk=node.pk)
    target = manager.get(pk=target.pk)
    if target.path.startswith(node.path):
        sorted_model = bool(model.node_order_by)
        if anchor is not None:
            park_pos = "sorted-child" if sorted_model else "last-child"
            manager.move(node, manager.get(pk=anchor.pk), park_pos)
        else:
            park_target = manager.get_root_nodes().exclude(pk=node.pk).last()
            if park_target is None:
                raise SyncError(f"Cannot relocate {node.pk}: no position available to park the node")
            park_pos = "sorted-sibling" if sorted_model else "last-sibling"
            manager.move(node, park_target, park_pos)
        node = manager.get(pk=node.pk)
        target = manager.get(pk=target.pk)
    manager.move(node, target, pos)


def _execute(model, prep, forest, mnodes, identity_field, on_missing):
    """Applies the prepared plan. Must be called inside a transaction."""
    manager = model.objects
    sorted_model = bool(model.node_order_by)
    anchor = prep.anchor

    # 1. field updates first (rename before move), so instances keep their pk
    #    and moves below operate on up-to-date values (relevant for sorted trees)
    for identity, values in prep.updates.items():
        node = manager.get(pk=prep.pk_by_identity[identity])
        for field_name, value in values.items():
            setattr(node, field_name, value)
        node.save(update_fields=list(values))

    # 2. missing subtrees without manifest content can go right away
    for pk in prep.phase1_delete_pks:
        manager.filter(pk=pk).delete()

    # 3. additions, parents before children
    pk_by_identity = dict(prep.pk_by_identity)
    for mnode in _iter_manifest(forest):
        if mnode.identity in pk_by_identity:
            continue
        instance = model(**{identity_field: mnode.identity, **mnode.data})
        if mnode.parent_identity is None:
            if anchor is not None:
                new_node = manager.add_child(manager.get(pk=anchor.pk), instance=instance)
            else:
                new_node = manager.add_root(instance=instance)
        else:
            new_node = manager.add_child(manager.get(pk=pk_by_identity[mnode.parent_identity]), instance=instance)
        pk_by_identity[mnode.identity] = new_node.pk

    # 4. placements
    order_changed = {
        identity
        for identity, values in prep.updates.items()
        if any(field_name.lstrip("-") in values for field_name in model.node_order_by)
    }
    for parent_identity, children in _iter_groups(forest):
        # the parent is re-fetched before every placement: previous moves and
        # additions change its path/numchild, and get_children relies on them
        group_parent_pk = anchor.pk if parent_identity is None and anchor is not None else (
            pk_by_identity[parent_identity] if parent_identity is not None else None
        )
        if sorted_model:
            for mnode in children:
                node = manager.get(pk=pk_by_identity[mnode.identity])
                group_parent = manager.get(pk=group_parent_pk) if group_parent_pk is not None else None
                expected_parent_path = group_parent.path if group_parent is not None else ""
                in_place = node.path[: -model.steplen] == expected_parent_path
                if not in_place or mnode.identity in order_changed:
                    if group_parent is None:
                        target = manager.get_root_nodes().exclude(pk=node.pk).first()
                        if target is not None:
                            _move(model, node, target, "sorted-sibling", None)
                    else:
                        _move(model, node, group_parent, "sorted-child", anchor)
            continue
        desired = [mnode.identity for mnode in children]
        for index, identity in enumerate(desired):
            node = manager.get(pk=pk_by_identity[identity])
            group_parent = manager.get(pk=group_parent_pk) if group_parent_pk is not None else None
            siblings = _children_identities(model, group_parent, identity_field)
            if index < len(siblings) and siblings[index] == identity:
                continue
            if index == 0:
                if group_parent is not None:
                    _move(model, node, group_parent, "first-child", anchor)
                else:
                    first_root = manager.get_first_root_node()
                    if first_root is not None and first_root.pk != node.pk:
                        _move(model, node, first_root, "first-sibling", None)
            else:
                predecessor = manager.get(pk=pk_by_identity[desired[index - 1]])
                _move(model, node, predecessor, "right", anchor)

    # 5. missing subtrees that still contained manifest nodes at plan time;
    #    by now those must have been moved out, verify before deleting
    manifest_ids = set(mnodes)
    for pk in prep.phase3_delete_pks:
        node = manager.get(pk=pk)
        remaining = manager.filter(path__startswith=node.path).values_list(identity_field, flat=True)
        still_referenced = sorted(repr(identity) for identity in remaining if identity in manifest_ids)
        if still_referenced:
            raise ProtectedNode(
                f"Cannot delete {getattr(node, identity_field)!r}: "
                f"manifest nodes still live below it: {', '.join(still_referenced)}"
            )
        manager.filter(pk=pk).delete()


def sync_tree(
    model,
    manifest,
    *,
    parent=None,
    identity_field="external_id",
    on_missing="keep",
    protect=None,
    dry_run=False,
    expected_summary=None,
):
    """
    Synchronizes a subtree of an :py:class:`~treebeard.mp_tree.MP_Node` model
    with an external manifest, using add/move/update/delete operations
    instead of deleting and reloading the subtree.

    :param model:

        The :py:class:`~treebeard.mp_tree.MP_Node` subclass to synchronize.

    :param manifest:

        The desired state of the subtree, either as a nested structure (like
        :py:meth:`~treebeard.mp_tree.MP_NodeManager.load_bulk`) or as a flat
        list with ``parent`` references. See the module docstring for details.

    :param parent:

        The node whose subtree is synchronized. The node itself is never
        modified; the top level of the manifest describes its children. If
        not given, the whole forest (all root nodes) is synchronized.

    :param identity_field:

        The model field holding the stable external identity. Matched nodes
        keep their primary key, no matter how their fields or position change.

    :param on_missing:

        What to do with nodes inside the scope that are missing from the
        manifest: ``"keep"`` (the default) leaves them in place, ``"delete"``
        removes them and their descendants.

    :param protect:

        Optional callable receiving each node that would be deleted. If it
        returns a truthy value (a reason string, ideally), a
        :py:class:`~treebeard.exceptions.ProtectedNode` error is raised and
        nothing is written. Django-level ``on_delete=PROTECT`` relations also
        abort the sync through the usual ``ProtectedError``.

    :param dry_run:

        If True, only compute and return the :class:`SyncPlan`; the database
        is not modified.

    :param expected_summary:

        A summary digest obtained from an earlier dry-run
        (:attr:`SyncPlan.old_summary`). When given, the apply aborts with
        :py:class:`~treebeard.exceptions.StaleSyncSummary` if the current
        structure no longer matches, guarding against concurrent changes
        between planning and applying.

    :returns: A :class:`SyncPlan` describing the changes.

    The manifest is fully validated before anything is written: duplicate
    identities, cycles, unknown parent references, invalid ordering and
    structures that do not fit the available path space are rejected without
    touching the database. Applying runs inside a single transaction, and
    applying the same manifest twice produces no changes the second time.
    """
    if not (isinstance(model, type) and issubclass(model, MP_Node)):
        raise SyncError("sync_tree only supports MP_Node models")
    if on_missing not in ("keep", "delete"):
        raise ValueError("on_missing must be 'keep' or 'delete'")
    try:
        model._meta.get_field(identity_field)
    except FieldDoesNotExist:
        raise SyncError(f"{identity_field!r} is not a field of {model.__name__}") from None

    forest, mnodes = _parse_manifest(model, manifest, identity_field)

    if dry_run:
        prep = _prepare(model, forest, mnodes, parent, identity_field, on_missing, protect)
        return SyncPlan(changes=prep.changes, old_summary=prep.summary, old_structure=prep.structure)

    with transaction.atomic():
        prep = _prepare(model, forest, mnodes, parent, identity_field, on_missing, protect)
        if expected_summary is not None and prep.summary != expected_summary:
            raise StaleSyncSummary(
                "The tree changed since the summary was computed; re-run the dry-run before applying"
            )
        _execute(model, prep, forest, mnodes, identity_field, on_missing)
        # recompute the summary of the resulting structure
        after = _Prepared()
        _snapshot(model, parent, identity_field, after)
        new_summary, _ = _summarize(model, after, identity_field)
        return SyncPlan(
            changes=prep.changes,
            old_summary=prep.summary,
            old_structure=prep.structure,
            applied=True,
            new_summary=new_summary,
        )
