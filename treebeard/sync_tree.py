"""Synchronize materialized-path trees from external identity manifests."""

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Callable

from django.apps import apps
from django.db import transaction

from treebeard.exceptions import (
    CrossScopeIdentityConflict,
    InvalidSyncData,
    InvalidSyncOrdering,
    PathOverflow,
    ProtectedNodeError,
    SyncCycleError,
    SyncSummaryMismatch,
)

CREATE = "create"
UPDATE = "update"
MOVE = "move"
DELETE = "delete"
RETAIN = "retain"
DELETE_MISSING = "delete"


@dataclass(frozen=True)
class SyncSummary:
    root_external_id: Any | None
    root_path: str | None
    root_pk: Any | None
    node_count: int
    max_depth: int
    checksum: str


@dataclass(frozen=True)
class SyncChange:
    action: str
    external_id: Any
    fields: dict[str, Any] = field(default_factory=dict)
    old_parent_external_id: Any | None = None
    new_parent_external_id: Any | None = None


@dataclass(frozen=True)
class SyncResult:
    plan: "SyncPlan"
    created: dict[Any, Any]
    updated: list[Any]
    moved: list[Any]
    deleted: list[Any]

    @property
    def changes(self):
        return self.plan.changes


@dataclass
class _Entry:
    external_id: Any
    parent_external_id: Any | None
    data: dict[str, Any]
    children: list["_Entry"]
    raw_item: dict | None = None
    old: "_OldNode | None" = None


@dataclass(frozen=True)
class _OldNode:
    pk: Any
    external_id: Any
    path: str
    depth: int
    parent_external_id: Any | None
    values: dict[str, Any]


@dataclass(frozen=True)
class _Snapshot:
    nodes: dict[Any, _OldNode]
    summary: SyncSummary


@dataclass
class SyncPlan:
    changes: list[SyncChange]
    summary: SyncSummary
    missing_policy: str
    creates: list[Any]
    updates: dict[Any, dict[str, Any]]
    moves: dict[Any, tuple[Any | None, int]]
    deletes: list[Any]
    external_id_field: str
    can_delete: Callable | None
    _entries: dict[Any, _Entry]
    _roots: list[_Entry]
    _old_nodes: dict[Any, _OldNode]
    _field_values: dict[Any, dict[str, Any]]
    _model_label: str

    @property
    def old_structure(self):
        return self.summary


def _objects(model, using=None):
    return model.objects if using is None else model.objects.using(using)


def _manager(model, using=None):
    queryset = _objects(model, using)
    return queryset.model.objects.db_manager(using) if using is not None else queryset.model.objects


def plan_mp_sync(
    model,
    manifest,
    *,
    using=None,
    root=None,
    external_id_field="external_id",
    on_missing=RETAIN,
    can_delete=None,
):
    if on_missing not in (RETAIN, DELETE_MISSING):
        raise InvalidSyncData("on_missing must be 'retain' or 'delete'.")
    if can_delete is not None and not callable(can_delete):
        raise InvalidSyncData("can_delete must be callable.")

    identity_field = _identity_field(model, external_id_field)
    entries, roots = _parse_manifest(manifest, root, identity_field)
    snapshot = _snapshot(model, using, root, identity_field.attname)
    _validate_identities(model, using, entries, snapshot, identity_field.attname)

    for entry in entries.values():
        entry.old = snapshot.nodes.get(entry.external_id)

    field_values = {}
    for entry in entries.values():
        values = dict(entry.old.values) if entry.old is not None else {identity_field.attname: entry.external_id}
        values.update(_entry_data(model, entry, identity_field.attname))
        field_values[entry.external_id] = values
    _validate_cycles(entries)
    _validate_ordering(model, entries, roots, field_values)
    _validate_capacity(model, root, entries, roots, snapshot, on_missing, identity_field.attname)

    if on_missing == DELETE_MISSING:
        missing_nodes = [node for node in snapshot.nodes.values() if node.external_id not in entries]
        missing_roots = _minimal_roots(snapshot, missing_nodes)
        if missing_roots and can_delete:
            queryset = _objects(model, using).filter(pk__in=[node.pk for node in missing_roots])
            if can_delete(queryset) is False:
                raise ProtectedNodeError("Deletion was refused by the business protection callback.")

    changes, creates, updates, moves = _changes(
        model, root, entries, roots, snapshot, field_values, identity_field.attname
    )
    missing = [ext for ext in snapshot.nodes if ext not in entries]
    if on_missing == DELETE_MISSING:
        changes.extend(SyncChange(DELETE, ext) for ext in missing)

    return SyncPlan(
        changes=changes,
        summary=snapshot.summary,
        missing_policy=on_missing,
        creates=creates,
        updates=updates,
        moves=moves,
        deletes=missing,
        external_id_field=identity_field.name,
        can_delete=can_delete,
        _entries=entries,
        _roots=roots,
        _old_nodes=snapshot.nodes,
        _field_values=field_values,
        _model_label=model._meta.label,
    )


@transaction.atomic
def apply_mp_sync(plan, *, using=None):
    model = apps.get_model(plan._model_label)
    if model is None:
        raise InvalidSyncData("Unknown model in synchronization plan.")

    root = None
    if plan.summary.root_pk is not None:
        root = _objects(model, using).filter(pk=plan.summary.root_pk).first()
        if root is None:
            raise SyncSummaryMismatch("Synchronized root no longer exists.")

    identity_attr = model._meta.get_field(plan.external_id_field).attname
    current = _snapshot(model, using, root, identity_attr, lock=True)
    if current.summary.checksum != plan.summary.checksum:
        raise SyncSummaryMismatch("The tree changed after this synchronization plan was created.")

    created = {}
    updated = []
    moved = []
    deleted = []

    for external_id, values in plan.updates.items():
        node = _objects(model, using).get(pk=plan._old_nodes[external_id].pk)
        for attr_name, value in values.items():
            setattr(node, attr_name, value)
        node.save(using=using, update_fields=list(values))
        updated.append(node.pk)

    pre_deleted = []
    if plan.missing_policy == DELETE_MISSING:
        missing_roots = _minimal_roots(
            current, [node for node in current.nodes.values() if node.external_id not in plan._entries]
        )
        if missing_roots and plan.can_delete is not None:
            queryset = _objects(model, using).filter(pk__in=[node.pk for node in missing_roots])
            if plan.can_delete(queryset) is False:
                raise ProtectedNodeError("Deletion was refused by the business protection callback.")
        pre_deleted = _delete_missing_roots(model, using, current, set(plan._entries))
        deleted.extend(pre_deleted)
    if plan.creates or plan.moves:
        _manager(model, using).fix_tree(fix_paths=True, parent=root)

    _apply_structure(model, using, plan, root, current, created, moved)

    if plan.missing_policy == DELETE_MISSING:
        deleted.extend(_delete_remaining_missing(model, using, plan, root))

    return SyncResult(plan=plan, created=created, updated=updated, moved=moved, deleted=deleted)


def _identity_field(model, field_name):
    try:
        field = model._meta.get_field(field_name)
    except Exception as exc:
        raise InvalidSyncData(f"Model {model._meta.label} has no field '{field_name}'.") from exc
    if not field.concrete or field.many_to_many or field.primary_key or field.name in {"path", "depth", "numchild"}:
        raise InvalidSyncData(f"Field '{field_name}' cannot be used as an external identity field.")
    return field


def _parse_manifest(manifest, fixed_root, identity_field):
    if not isinstance(manifest, list):
        raise InvalidSyncData("The manifest must be a list of node dictionaries.")
    entries = {}
    roots = _parse_items(manifest, None, entries, identity_field)

    if fixed_root is not None and len(roots) != 1:
        raise InvalidSyncData("A subtree manifest must contain exactly one root node.")
    if fixed_root is not None and roots[0].external_id != getattr(fixed_root, identity_field.name):
        raise InvalidSyncData("The manifest root external id does not match the synchronized root.")

    for root_entry in roots:
        _parse_children(root_entry, entries, identity_field)
    return entries, roots


def _parse_items(items, parent_external_id, entries, identity_field):
    if not isinstance(items, list):
        raise InvalidSyncData("Children must be a list of node dictionaries.")
    parsed = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("data"), dict):
            raise InvalidSyncData("Each manifest node must contain a 'data' dictionary.")
        data = item["data"]
        external_id = item.get("external_id", data.get(identity_field.name, data.get(identity_field.attname)))
        if external_id is None:
            raise InvalidSyncData("Each manifest node must have an external id.")
        explicit = item.get("external_id")
        embedded = data.get(identity_field.name, data.get(identity_field.attname))
        if explicit is not None and embedded is not None and explicit != embedded:
            raise InvalidSyncData(f"Conflicting external ids for node {external_id!r}.")
        if external_id in entries:
            raise InvalidSyncData(f"Duplicate external id in manifest: {external_id!r}.")
        _validate_data_fields(data, identity_field)
        entry = _Entry(
            external_id=external_id,
            parent_external_id=parent_external_id,
            data={
                key: value
                for key, value in data.items()
                if key not in {identity_field.name, identity_field.attname}
            },
            children=[],
        )
        entry.raw_item = item
        entries[external_id] = entry
        parsed.append(entry)
    return parsed



def _parse_children(parent_entry, entries, identity_field):
    raw_item = parent_entry.raw_item or {}
    children = _parse_items(raw_item.get("children", []), parent_entry.external_id, entries, identity_field)
    parent_entry.children = children
    for child in children:
        _parse_children(child, entries, identity_field)


def _validate_data_fields(data, identity_field):
    model = identity_field.model
    for key in data:
        if key in {identity_field.name, identity_field.attname}:
            continue
        try:
            field = model._meta.get_field(key)
        except Exception as exc:
            raise InvalidSyncData(f"Unknown field in manifest: {key!r}.") from exc
        if not field.concrete or field.many_to_many:
            raise InvalidSyncData(f"Field {key!r} cannot be synchronized from manifest data.")
        if field.primary_key or field.name in {"path", "depth", "numchild"}:
            raise InvalidSyncData(f"Field {key!r} is managed by Treebeard and cannot be synchronized.")


def _entry_data(model, entry, identity_attr):
    values = {identity_attr: entry.external_id}
    for key, value in entry.data.items():
        field = model._meta.get_field(key)
        if field.attname == identity_attr:
            continue
        values[field.attname] = value
    return values


def _snapshot(model, using, root, identity_attr, lock=False):
    queryset = _objects(model, using)
    root_node = None
    if root is not None:
        root_node = queryset.filter(pk=root.pk).first()
        if root_node is None:
            raise InvalidSyncData("The synchronized root does not exist.")
        queryset = queryset.filter(path__startswith=root_node.path)
    if lock:
        queryset = queryset.select_for_update()

    rows = list(queryset.order_by("path").values())
    path_to_external = {row["path"]: row[identity_attr] for row in rows}
    nodes = {}
    serialized_rows = []
    field_names = [field.attname for field in model._meta.concrete_fields]
    for row in rows:
        parent_path = model._get_parent_path_from_path(row["path"])
        values = {name: row.get(name) for name in field_names}
        node = _OldNode(
            pk=row[model._meta.pk.attname],
            external_id=row[identity_attr],
            path=row["path"],
            depth=len(row["path"]) // model.steplen,
            parent_external_id=path_to_external.get(parent_path),
            values=values,
        )
        nodes[node.external_id] = node
        serialized_rows.append(values)

    serialized = json.dumps(serialized_rows, sort_keys=True, separators=(",", ":"), default=str)
    summary = SyncSummary(
        root_external_id=getattr(root_node, identity_attr, None),
        root_path=root_node.path if root_node else None,
        root_pk=root_node.pk if root_node else None,
        node_count=len(nodes),
        max_depth=max((node.depth for node in nodes.values()), default=0),
        checksum=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    )
    return _Snapshot(nodes, summary)


def _validate_identities(model, using, entries, snapshot, identity_attr):
    rows = list(
        _objects(model, using).filter(**{f"{identity_attr}__in": list(entries)}).values("pk", identity_attr)
    )
    seen = {}
    for row in rows:
        seen.setdefault(row[identity_attr], []).append(row["pk"])
    duplicated = {external_id: pks for external_id, pks in seen.items() if len(pks) > 1}
    if duplicated:
        raise InvalidSyncData(f"Duplicate external ids in database: {sorted(duplicated, key=str)!r}.")
    if snapshot.summary.root_pk is None:
        return
    scope_pks = {node.pk for node in snapshot.nodes.values()}
    outside = [external_id for external_id, pks in seen.items() if pks[0] not in scope_pks]
    if outside:
        ids = sorted(outside, key=str)
        raise CrossScopeIdentityConflict(f"External ids exist outside synchronized scope: {ids!r}.")


def _validate_cycles(entries):
    for external_id, entry in entries.items():
        if entry.old is None:
            continue
        ancestor = entry.parent_external_id
        seen = set()
        while ancestor is not None:
            if ancestor == external_id:
                raise SyncCycleError(f"Manifest creates a cycle at external id {external_id!r}.")
            if ancestor in seen:
                raise SyncCycleError(f"Manifest contains a cycle through external id {ancestor!r}.")
            seen.add(ancestor)
            parent_entry = entries.get(ancestor)
            if parent_entry is None or parent_entry.old is None:
                break
            if parent_entry.old.path.startswith(entry.old.path) and parent_entry.external_id != external_id:
                raise SyncCycleError(f"Manifest creates a cycle at external id {external_id!r}.")
            ancestor = parent_entry.parent_external_id


def _validate_ordering(model, entries, roots, field_values):
    if not model.node_order_by:
        return

    stack = list(roots)
    while stack:
        parent_entry = stack.pop()
        children = parent_entry.children
        for index, child in enumerate(children[1:], start=1):
            if _sort_key(model, children[index - 1], field_values) > _sort_key(model, child, field_values):
                raise InvalidSyncOrdering(f"Children of {parent_entry.external_id!r} violate node_order_by.")
        stack.extend(children)


def _sort_key(model, entry, field_values):
    values = field_values[entry.external_id]
    key = []
    for order_field in model.node_order_by:
        descending = order_field.startswith("-")
        attr = model._meta.get_field(order_field[1:] if descending else order_field).attname
        value = values.get(attr)
        if descending:
            if value is None:
                value = (1, 0)
            elif isinstance(value, (int, float)):
                value = (0, -value)
            else:
                value = (0, _ReversedComparable(value))
        else:
            if value is None:
                value = (0, value)
            else:
                value = (1, value)
        key.append(value)
    return tuple(key)


class _ReversedComparable:
    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        return self.value > other.value

    def __eq__(self, other):
        return self.value == other.value


def _desired_groups(entries, roots):
    groups = {}
    for root_entry in roots:
        groups.setdefault(None, []).append(root_entry)
    for entry in entries.values():
        if entry.parent_external_id is not None:
            groups.setdefault(entry.parent_external_id, []).append(entry)
    for children in groups.values():
        children.sort(key=lambda entry: (entry.parent_external_id is not None,))
    return groups


def _actual_groups(snapshot, root_external_id):
    groups = {}
    for node in snapshot.nodes.values():
        parent_external_id = node.parent_external_id
        if (root_external_id is None or node.external_id != root_external_id) and (
            parent_external_id is not None or root_external_id is None
        ):
            groups.setdefault(parent_external_id, []).append(node)
    for children in groups.values():
        children.sort(key=lambda node: node.path)
    return groups


def _changes(model, fixed_root, entries, roots, snapshot, field_values, identity_attr):
    changes = []
    creates = []
    updates = {}
    moves = {}
    desired_groups = _desired_groups(entries, roots)
    actual_groups = _actual_groups(snapshot, getattr(fixed_root, identity_attr, None) if fixed_root else None)

    for parent_external_id, desired_children in desired_groups.items():
        actual_children = actual_groups.get(parent_external_id, [])
        simulated_order = [
            node.external_id
            for node in actual_children
            if node.external_id in entries and entries[node.external_id].parent_external_id == parent_external_id
        ]
        for index, entry in enumerate(desired_children):
            old = snapshot.nodes.get(entry.external_id)
            if old is None:
                simulated_order.insert(index, entry.external_id)
                creates.append(entry.external_id)
                changes.append(SyncChange(CREATE, entry.external_id, entry.data, None, parent_external_id))
                continue
            changed = _changed_fields(model, old, field_values[entry.external_id], identity_attr)
            if changed:
                updates[entry.external_id] = changed
                changes.append(SyncChange(UPDATE, entry.external_id, changed))
            fixed = fixed_root is not None and entry.external_id == getattr(fixed_root, identity_attr)
            old_parent = old.parent_external_id
            parent_changed = old_parent != parent_external_id
            order_changed = False
            if old_parent == parent_external_id and entry.external_id in simulated_order:
                order_changed = simulated_order.index(entry.external_id) != index
                if order_changed:
                    simulated_order.remove(entry.external_id)
                    simulated_order.insert(index, entry.external_id)
            elif parent_changed:
                simulated_order.insert(index, entry.external_id)
            if not fixed and (parent_changed or order_changed):
                moves[entry.external_id] = (parent_external_id, index)
                changes.append(SyncChange(MOVE, entry.external_id, {}, old_parent, parent_external_id))

    return changes, creates, updates, moves


def _changed_fields(model, old_node, new_values, identity_attr):
    changed = {}
    editable = {
        field.attname
        for field in model._meta.concrete_fields
        if not field.primary_key and field.attname not in {"path", "depth", "numchild", identity_attr}
    }
    for attr_name in editable.intersection(new_values):
        if old_node.values.get(attr_name) != new_values[attr_name]:
            changed[attr_name] = new_values[attr_name]
    return changed


def _validate_capacity(model, fixed_root, entries, roots, snapshot, missing_policy, identity_attr):
    path_max = model._meta.get_field("path").max_length
    depths = {}
    for entry in entries.values():
        current = entry
        fixed_external_id = getattr(fixed_root, identity_attr, None)
        if fixed_root is None:
            depth = 1
        elif entry.external_id == fixed_external_id:
            depth = fixed_root.depth
        else:
            depth = fixed_root.depth + 1
        while current.parent_external_id is not None:
            parent = entries[current.parent_external_id]
            if fixed_root is None or parent.external_id != fixed_external_id:
                depth += 1
            current = parent
        depths[entry.external_id] = depth
    if max(depths.values(), default=0) * model.steplen > path_max:
        raise PathOverflow("The synchronized tree is deeper than the configured materialized path allows.")

    desired_groups = _desired_groups(entries, roots)
    desired_direct = {
        parent_external_id: {entry.external_id for entry in children}
        for parent_external_id, children in desired_groups.items()
    }
    for parent_external_id, desired_children in desired_groups.items():
        old_children = _old_direct_children(model, snapshot, parent_external_id)
        if missing_policy == RETAIN:
            extra_children = sum(
                1 for node in old_children if node.external_id not in desired_direct[parent_external_id]
            )
        else:
            extra_children = 0
        required = len(desired_children) + extra_children
        capacity = model._str2int(model.alphabet[-1] * model.steplen)
        if required > capacity:
            raise PathOverflow("The synchronized tree contains more siblings than the path alphabet can represent.")


def _old_direct_children(model, snapshot, parent_external_id):
    if parent_external_id is None:
        return [node for node in snapshot.nodes.values() if node.depth == 1]
    parent = snapshot.nodes.get(parent_external_id)
    if parent is None:
        return []
    start_path = parent.path + model.alphabet[0] * model.steplen
    end_path = parent.path + model.alphabet[-1] * model.steplen
    return [
        node
        for node in snapshot.nodes.values()
        if node.depth == parent.depth + 1 and start_path <= node.path <= end_path
    ]


def _subtree_has_desired(node, snapshot, desired_ids):
    return any(
        other.path.startswith(node.path) and other.external_id in desired_ids
        for other in snapshot.nodes.values()
    )


def _minimal_roots(snapshot, nodes):
    result = []
    for node in sorted(nodes, key=lambda item: item.path):
        if not any(node.path.startswith(other.path) for other in nodes if other is not node):
            result.append(node)
    return result


def _delete_missing_roots(model, using, snapshot, desired_ids):
    missing = [node for node in snapshot.nodes.values() if node.external_id not in desired_ids]
    roots = _minimal_roots(snapshot, missing)
    safe_roots = [node for node in roots if not _subtree_has_desired(node, snapshot, desired_ids)]
    if not safe_roots:
        return []
    root_paths = {node.path for node in safe_roots}
    deleted_external = [
        node.external_id
        for node in sorted(snapshot.nodes.values(), key=lambda item: item.path)
        if any(node.path.startswith(root_path) for root_path in root_paths)
    ]
    _objects(model, using).filter(pk__in=[node.pk for node in safe_roots]).delete()
    return deleted_external


def _apply_structure(model, using, plan, fixed_root, snapshot, created, moved):
    fixed_external_id = plan.summary.root_external_id
    desired_groups = _desired_groups(plan._entries, plan._roots)

    for entry in plan._entries.values():
        if fixed_root is not None and entry.external_id == fixed_external_id:
            continue

        parent_external_id = entry.parent_external_id
        siblings = desired_groups.get(parent_external_id, [])
        index = siblings.index(entry)
        previous = siblings[index - 1] if index else None
        following = next(
            (
                candidate
                for candidate in siblings[index + 1 :]
                if candidate.external_id != entry.external_id
            ),
            None,
        )

        if entry.external_id in plan.creates:
            instance = model(**entry.data)
            setattr(instance, plan.external_id_field, entry.external_id)
            created_node = _create_node(
                model,
                using,
                instance,
                parent_external_id,
                previous,
                following,
                fixed_root,
                created,
                plan.external_id_field,
            )
            created[entry.external_id] = created_node
        elif entry.external_id in plan.moves:
            node = _objects(model, using).get(pk=plan._old_nodes[entry.external_id].pk)
            _move_node(
                model,
                using,
                node,
                parent_external_id,
                previous,
                following,
                fixed_root,
                created,
                plan.external_id_field,
            )
            moved.append(node.pk)


def _create_node(
    model, using, instance, parent_external_id, previous, following, fixed_root, created, identity_field_name
):
    objects = _manager(model, using)
    if model.node_order_by:
        if parent_external_id is None:
            return objects.add_root(instance=instance)
        parent = _fresh_parent(model, using, parent_external_id, fixed_root, created, identity_field_name)
        return objects.add_child(parent, instance=instance)

    if parent_external_id is None:
        first_root = objects.get_root_nodes().first()
        if previous is not None:
            target = _fresh_or_created(model, using, previous, created, identity_field_name)
            return objects.add_sibling(target, "right", instance=instance)
        if following is not None:
            target = _fresh_or_created(model, using, following, created, identity_field_name)
            return objects.add_sibling(target, "first-sibling", instance=instance)
        if first_root is not None:
            return objects.add_sibling(first_root, "first-sibling", instance=instance)
        return objects.add_root(instance=instance)

    parent = _fresh_parent(model, using, parent_external_id, fixed_root, created, identity_field_name)
    if previous is not None:
        target = _fresh_or_created(model, using, previous, created, identity_field_name)
        return objects.add_sibling(target, "right", instance=instance)
    first_child = objects.get_children(parent).first()
    if first_child is not None:
        return objects.add_sibling(first_child, "first-sibling", instance=instance)
    return objects.add_child(parent, instance=instance)


def _move_node(
    model, using, node, parent_external_id, previous, following, fixed_root, created, identity_field_name
):
    objects = _manager(model, using)
    if model.node_order_by:
        if parent_external_id is None:
            target = objects.get_root_nodes().exclude(pk=node.pk).first()
            if target is None:
                return
            objects.move(node, target, "sorted-sibling")
        else:
            parent = _fresh_parent(model, using, parent_external_id, fixed_root, created, identity_field_name)
            objects.move(node, parent, "sorted-child")
        return

    if parent_external_id is None:
        if previous is not None and previous.external_id != node.external_id:
            target = _fresh_or_created(model, using, previous, created, identity_field_name)
            objects.move(node, target, "right")
        elif following is not None and following.external_id != node.external_id:
            target = _fresh_or_created(model, using, following, created, identity_field_name)
            objects.move(node, target, "first-sibling")
        else:
            target = objects.get_root_nodes().exclude(pk=node.pk).first()
            if target is not None:
                objects.move(node, target, "last-sibling")
        return

    parent = _fresh_parent(model, using, parent_external_id, fixed_root, created, identity_field_name)
    if previous is not None and previous.external_id != node.external_id:
        target = _fresh_or_created(model, using, previous, created, identity_field_name)
        objects.move(node, target, "right")
    elif following is not None and following.external_id != node.external_id:
        target = _fresh_or_created(model, using, following, created, identity_field_name)
        objects.move(node, target, "first-sibling")
    else:
        objects.move(node, parent, "last-child")


def _fresh_parent(model, using, parent_external_id, fixed_root, created, identity_field_name):
    if fixed_root is not None and parent_external_id == fixed_root.external_id:
        return _objects(model, using).get(pk=fixed_root.pk)
    if parent_external_id in created:
        return _objects(model, using).get(pk=created[parent_external_id].pk)
    return _objects(model, using).get(**{identity_field_name: parent_external_id})


def _fresh_or_created(model, using, entry, created, identity_field_name):
    if entry.external_id in created:
        return _objects(model, using).get(pk=created[entry.external_id].pk)
    return _objects(model, using).get(**{identity_field_name: entry.external_id})



def _delete_remaining_missing(model, using, plan, fixed_root):
    identity_attr = model._meta.get_field(plan.external_id_field).attname
    snapshot = _snapshot(model, using, fixed_root, identity_attr)
    desired_ids = set(plan._entries)
    missing = [node for node in snapshot.nodes.values() if node.external_id not in desired_ids]
    roots = _minimal_roots(snapshot, missing)
    referenced = [node for node in roots if _subtree_has_desired(node, snapshot, desired_ids)]
    if referenced:
        raise InvalidSyncData("A missing node still contains a node referenced by the manifest.")
    if plan.can_delete is not None and roots:
        queryset = _objects(model, using).filter(pk__in=[node.pk for node in roots])
        if plan.can_delete(queryset) is False:
            raise ProtectedNodeError("Deletion was refused by the business protection callback.")
    deleted_external = [
        node.external_id
        for node in sorted(snapshot.nodes.values(), key=lambda item: item.path)
        if any(node.path.startswith(root.path) for root in roots)
    ]
    _objects(model, using).filter(pk__in=[node.pk for node in roots]).delete()
    return deleted_external
