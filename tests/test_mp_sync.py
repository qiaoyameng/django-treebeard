import pytest

from treebeard.exceptions import (
    CrossScopeIdentityConflict,
    InvalidSyncData,
    InvalidSyncOrdering,
    PathOverflow,
    ProtectedNodeError,
    SyncCycleError,
    SyncSummaryMismatch,
)
from treebeard.mp_tree import nodes_deleted, path_updated

from tests.models import (
    MP_TestNodeExternal,
    MP_TestNodeExternalCode,
    MP_TestNodeExternalShortPath,
    MP_TestNodeExternalSorted,
)


def item(external_id, desc=None, children=None, **data):
    fields = {"external_id": external_id, **data}
    if desc is not None:
        fields["desc"] = desc
    result = {"data": fields, "children": children or []}
    return result


def tree(model):
    return [
        (node.external_id, node.desc, node.depth, node.numchild)
        for node in model.objects.order_by("path")
    ]


@pytest.mark.django_db
def test_plan_does_not_write_and_lists_old_summary_and_changes():
    root = MP_TestNodeExternal.objects.add_root({"external_id": "a", "desc": "A"})
    MP_TestNodeExternal.objects.add_child(root, {"external_id": "b", "desc": "B"})
    manifest = [item("a", "A changed", [item("b", "B"), item("c", "C")])]

    plan = MP_TestNodeExternal.objects.plan_sync_tree(manifest, root=root)

    assert [(change.action, change.external_id) for change in plan.changes] == [("update", "a"), ("create", "c")]
    assert plan.old_structure.node_count == 2
    assert plan.old_structure.root_external_id == "a"
    assert tree(MP_TestNodeExternal) == [("a", "A", 1, 1), ("b", "B", 2, 0)]


@pytest.mark.django_db
def test_applies_updates_creates_moves_and_repeating_is_noop():
    root = MP_TestNodeExternal.objects.add_root({"external_id": "root", "desc": "Root"})
    first = MP_TestNodeExternal.objects.add_child(root, {"external_id": "a", "desc": "A"})
    second = MP_TestNodeExternal.objects.add_child(root, {"external_id": "b", "desc": "B"})
    MP_TestNodeExternal.objects.add_child(first, {"external_id": "a1", "desc": "A1"})

    manifest = [
        item(
            "root",
            "Root",
            [
                item("b", "B", [item("a", "A renamed", [item("a1", "A1")])]),
            ],
        )
    ]
    plan = MP_TestNodeExternal.objects.plan_sync_tree(manifest, root=root)
    result = MP_TestNodeExternal.objects.apply_sync_tree(plan)

    moved_a = MP_TestNodeExternal.objects.get(external_id="a")
    assert moved_a.pk == first.pk
    assert MP_TestNodeExternal.objects.get(external_id="b").pk == second.pk
    assert moved_a.desc == "A renamed"
    assert MP_TestNodeExternal.objects.get_parent(moved_a).external_id == "b"
    assert [change.action for change in result.changes].count("move") == 1

    repeat_plan = MP_TestNodeExternal.objects.plan_sync_tree(manifest, root=root)
    repeat = MP_TestNodeExternal.objects.apply_sync_tree(repeat_plan)
    assert repeat.changes == []
    assert repeat.updated == repeat.moved == repeat.deleted
    assert repeat.created == {}
    assert tree(MP_TestNodeExternal) == [
        ("root", "Root", 1, 1),
        ("b", "B", 2, 1),
        ("a", "A renamed", 3, 1),
        ("a1", "A1", 4, 0),
    ]


@pytest.mark.django_db
def test_missing_nodes_can_be_retained_or_deleted_after_referenced_descendant_moves():
    root = MP_TestNodeExternal.objects.add_root({"external_id": "root", "desc": "Root"})
    obsolete = MP_TestNodeExternal.objects.add_child(root, {"external_id": "old", "desc": "Old"})
    keep = MP_TestNodeExternal.objects.add_child(obsolete, {"external_id": "keep", "desc": "Keep"})
    MP_TestNodeExternal.objects.add_child(obsolete, {"external_id": "gone", "desc": "Gone"})

    manifest = [item("root", "Root", [item("keep", "Keep")])]
    result = MP_TestNodeExternal.objects.sync_tree(manifest, root=root, on_missing="delete")

    assert result.deleted == ["old", "gone"]
    assert MP_TestNodeExternal.objects.filter(external_id__in=["old", "gone"]).count() == 0
    refreshed_keep = MP_TestNodeExternal.objects.get(external_id="keep")
    assert refreshed_keep.pk == keep.pk
    assert MP_TestNodeExternal.objects.get_parent(refreshed_keep).external_id == "root"


@pytest.mark.django_db
def test_business_protection_blocks_deletion_before_writes():
    root = MP_TestNodeExternal.objects.add_root({"external_id": "root", "desc": "Root"})
    MP_TestNodeExternal.objects.add_child(root, {"external_id": "a", "desc": "A"})
    manifest = [item("root", "Root")]

    def deny(nodes):
        raise ProtectedNodeError("business rule")

    with pytest.raises(ProtectedNodeError):
        MP_TestNodeExternal.objects.plan_sync_tree(manifest, root=root, on_missing="delete", can_delete=deny)
    assert MP_TestNodeExternal.objects.filter(external_id="a").exists()


@pytest.mark.django_db
def test_stale_summary_is_rejected():
    root = MP_TestNodeExternal.objects.add_root({"external_id": "root", "desc": "Root"})
    plan = MP_TestNodeExternal.objects.plan_sync_tree([item("root", "Root")], root=root)
    root.desc = "changed elsewhere"
    root.save(update_fields=["desc"])

    with pytest.raises(SyncSummaryMismatch):
        MP_TestNodeExternal.objects.apply_sync_tree(plan)


@pytest.mark.django_db
def test_invalid_manifests_do_not_write():
    root = MP_TestNodeExternal.objects.add_root({"external_id": "root", "desc": "Root"})
    valid = [item("root", "Root")]

    with pytest.raises(InvalidSyncData):
        MP_TestNodeExternal.objects.plan_sync_tree([item("x"), item("x")])
    with pytest.raises(InvalidSyncData):
        MP_TestNodeExternal.objects.plan_sync_tree([item("root", "Root", [item("root", "Root")])], root=root)
    with pytest.raises(InvalidSyncData):
        MP_TestNodeExternal.objects.plan_sync_tree([{"data": {"desc": "bad"}}])
    with pytest.raises(InvalidSyncData):
        MP_TestNodeExternal.objects.plan_sync_tree([item("root", "Root", [item("x", path="001")])], root=root)
    assert tree(MP_TestNodeExternal) == [("root", "Root", 1, 0)]
    assert valid


@pytest.mark.django_db
def test_cross_scope_identity_conflict_is_rejected():
    first = MP_TestNodeExternal.objects.add_root({"external_id": "a", "desc": "A"})
    second = MP_TestNodeExternal.objects.add_root({"external_id": "b", "desc": "B"})
    MP_TestNodeExternal.objects.add_child(second, {"external_id": "x", "desc": "X"})

    with pytest.raises(CrossScopeIdentityConflict):
        MP_TestNodeExternal.objects.plan_sync_tree(
            [item("a", "A", [item("x", "X moved")])], root=first
        )
    assert MP_TestNodeExternal.objects.get_parent(MP_TestNodeExternal.objects.get(external_id="x")).pk == second.pk


@pytest.mark.django_db
def test_manifest_cycle_is_rejected():
    root = MP_TestNodeExternal.objects.add_root({"external_id": "root", "desc": "Root"})
    child = MP_TestNodeExternal.objects.add_child(root, {"external_id": "a", "desc": "A"})
    grandchild = MP_TestNodeExternal.objects.add_child(child, {"external_id": "b", "desc": "B"})

    with pytest.raises(SyncCycleError):
        MP_TestNodeExternal.objects.plan_sync_tree(
            [item("root", "Root", [item("b", "B", [item("a", "A")])])], root=root
        )
    assert MP_TestNodeExternal.objects.get_parent(grandchild).pk == child.pk


@pytest.mark.django_db
def test_invalid_sorted_manifest_is_rejected():
    root = MP_TestNodeExternalSorted.objects.add_root({"external_id": "root", "seq": 0})
    MP_TestNodeExternalSorted.objects.add_child(root, {"external_id": "a", "seq": 2})
    MP_TestNodeExternalSorted.objects.add_child(root, {"external_id": "b", "seq": 1})

    with pytest.raises(InvalidSyncOrdering):
        MP_TestNodeExternalSorted.objects.plan_sync_tree(
            [item("root", children=[item("a", seq=2), item("b", seq=1)])], root=root
        )


@pytest.mark.django_db
def test_path_overflow_is_rejected_before_writes():
    root = MP_TestNodeExternalShortPath.objects.add_root({"external_id": "r"})

    with pytest.raises(PathOverflow):
        MP_TestNodeExternalShortPath.objects.plan_sync_tree(
            [
                {
                    "data": {"external_id": "a"},
                    "children": [
                        {"data": {"external_id": "b"}, "children": [{"data": {"external_id": "c"}, "children": [
                            {"data": {"external_id": "d"}, "children": [{"data": {"external_id": "e"}, "children": []}]}
                        ]}]}
                    ],
                }
            ]
        )
    assert MP_TestNodeExternalShortPath.objects.count() == 1
    assert root.pk is not None


@pytest.mark.django_db
def test_subtree_depth_capacity_uses_existing_root_path():
    model = MP_TestNodeExternalShortPath
    root = model.objects.add_root({"external_id": "root"})
    valid_manifest = [
        item("root", children=[item("a", children=[item("b", children=[item("c")])])])
    ]
    plan = model.objects.plan_sync_tree(valid_manifest, root=root)
    assert [change.action for change in plan.changes] == ["create", "create", "create"]

    invalid_manifest = [
        item("root", children=[item("a", children=[item("b", children=[item("c", children=[item("d")])])])])
    ]
    with pytest.raises(PathOverflow):
        model.objects.plan_sync_tree(invalid_manifest, root=root)


@pytest.mark.django_db
def test_signals_and_instance_cache_behaviour():
    root = MP_TestNodeExternal.objects.add_root({"external_id": "root", "desc": "Root"})
    first = MP_TestNodeExternal.objects.add_child(root, {"external_id": "a", "desc": "A"})
    second = MP_TestNodeExternal.objects.add_child(root, {"external_id": "b", "desc": "B"})
    stale_first = type(first).objects.get(pk=first.pk)
    path_events = []
    delete_events = []
    path_updated.connect(lambda *args, **kwargs: path_events.append(kwargs), weak=False)
    nodes_deleted.connect(lambda *args, **kwargs: delete_events.append(kwargs), weak=False)

    try:
        MP_TestNodeExternal.objects.sync_tree(
            [item("root", "Root", [item("b", "B", [item("a", "A")])])], root=root, on_missing="retain"
        )
    finally:
        path_updated.disconnect(sender=MP_TestNodeExternal)
        nodes_deleted.disconnect(sender=MP_TestNodeExternal)

    assert path_events
    assert delete_events == []
    with pytest.raises(AssertionError):
        assert stale_first.path == MP_TestNodeExternal.objects.get(pk=first.pk).path
    refreshed = type(first).objects.get(pk=first.pk)
    assert MP_TestNodeExternal.objects.get_parent(refreshed).pk == second.pk


@pytest.mark.django_db
def test_sorted_sync_uses_ordering_and_preserves_primary_keys():
    root = MP_TestNodeExternalSorted.objects.add_root({"external_id": "root", "seq": 0})
    first = MP_TestNodeExternalSorted.objects.add_child(root, {"external_id": "a", "seq": 1})
    MP_TestNodeExternalSorted.objects.add_child(root, {"external_id": "b", "seq": 2})

    result = MP_TestNodeExternalSorted.objects.sync_tree(
        [
            item(
                "root",
                children=[
                    item("b", seq=1),
                    item("a", seq=2, children=[item("c", seq=1)]),
                ],
            )
        ],
        root=root,
    )

    assert MP_TestNodeExternalSorted.objects.get(external_id="a").pk == first.pk
    assert [node.external_id for node in MP_TestNodeExternalSorted.objects.get_children(root)] == ["b", "a"]
    assert MP_TestNodeExternalSorted.objects.get(external_id="c").get_depth() == 3
    assert [change.action for change in result.changes].count("move") == 1


@pytest.mark.django_db
def test_sibling_count_overflow_is_rejected():
    model = MP_TestNodeExternalShortPath
    root = model.objects.add_root({"external_id": "root"})
    for index in range(8):
        model.objects.add_child(root, {"external_id": f"old-{index}"})

    manifest = [item("root", children=[item(f"old-{index}") for index in range(8)] + [item("new")])]

    with pytest.raises(PathOverflow):
        model.objects.plan_sync_tree(manifest, root=root, on_missing="retain")
    assert model.objects.count() == 9
    assert not model.objects.filter(external_id="new").exists()


@pytest.mark.django_db
def test_custom_identity_attribute_name():
    result = MP_TestNodeExternalCode.objects.sync_tree(
        [{"data": {"external_code": "x", "desc": "X"}, "children": []}],
        external_id_field="external_code",
    )

    assert result.created["x"].external_code == "x"
    assert MP_TestNodeExternalCode.objects.get(external_code="x").desc == "X"


@pytest.mark.django_db
def test_unsorted_reorder_only_moves_necessary_anchors():
    root = MP_TestNodeExternal.objects.add_root({"external_id": "root", "desc": "Root"})
    for external_id in ("a", "b", "c"):
        MP_TestNodeExternal.objects.add_child(root, {"external_id": external_id, "desc": external_id.upper()})

    result = MP_TestNodeExternal.objects.sync_tree(
        [item("root", "Root", [item("a"), item("c"), item("b")])], root=root
    )

    assert [change.external_id for change in result.changes if change.action == "move"] == ["c"]
    assert [node.external_id for node in MP_TestNodeExternal.objects.get_children(root)] == ["a", "c", "b"]
