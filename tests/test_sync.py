"""Tests for treebeard.sync.sync_tree"""

import pytest
from django.db.models.signals import post_save

from tests.models import (
    MP_TestNodeExternal,
    MP_TestNodeExternalShortPath,
    MP_TestNodeExternalSorted,
)
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
from treebeard.mp_tree import nodes_deleted, path_updated
from treebeard.sync import sync_tree

pytestmark = pytest.mark.django_db


def make_tree(model, spec, parent=None):
    """Builds a tree from a nested spec of (external_id, desc, children)."""
    for external_id, desc, children in spec:
        instance = model(external_id=external_id, desc=desc)
        node = model.objects.add_root(instance=instance) if parent is None else model.objects.add_child(
            parent, instance=instance
        )
        make_tree(model, children, node)


def tree_structure(model):
    """Returns [(external_id, parent_external_id, desc)] in path order."""
    nodes = list(model.objects.all().order_by("path"))
    by_path = {node.path: node for node in nodes}
    return [
        (
            node.external_id,
            (by_path[node.path[: -model.steplen]].external_id if node.depth > 1 else None),
            node.desc,
        )
        for node in nodes
    ]


def kinds(plan):
    return [change.kind for change in plan.changes]


class TestBasicSync:
    def test_creates_structure_from_empty(self):
        manifest = [
            {"external_id": "a", "data": {"desc": "A"}, "children": [
                {"external_id": "b", "data": {"desc": "B"}},
                {"external_id": "c", "data": {"desc": "C"}, "children": [
                    {"external_id": "d", "data": {"desc": "D"}},
                ]},
            ]},
            {"external_id": "e", "data": {"desc": "E"}},
        ]
        plan = sync_tree(MP_TestNodeExternal, manifest)
        assert plan.applied
        assert plan.new_summary
        assert kinds(plan) == ["add"] * 5
        assert tree_structure(MP_TestNodeExternal) == [
            ("a", None, "A"),
            ("b", "a", "B"),
            ("c", "a", "C"),
            ("d", "c", "D"),
            ("e", None, "E"),
        ]

    def test_sync_via_manager(self):
        plan = MP_TestNodeExternal.objects.sync([{"external_id": "a", "data": {"desc": "A"}}])
        assert plan.applied
        assert MP_TestNodeExternal.objects.get(external_id="a").desc == "A"

    def test_idempotent_resync(self):
        manifest = [
            {"external_id": "a", "data": {"desc": "A"}, "children": [
                {"external_id": "b", "data": {"desc": "B"}},
            ]},
        ]
        sync_tree(MP_TestNodeExternal, manifest)
        plan = sync_tree(MP_TestNodeExternal, manifest)
        assert not plan.has_changes
        assert kinds(plan) == []
        # a dry-run after a successful sync is empty too
        assert not sync_tree(MP_TestNodeExternal, manifest, dry_run=True).has_changes

    def test_flat_manifest(self):
        manifest = [
            {"external_id": "a", "parent": None, "data": {"desc": "A"}},
            {"external_id": "c", "parent": "a", "position": 1, "data": {"desc": "C"}},
            {"external_id": "b", "parent": "a", "position": 0, "data": {"desc": "B"}},
        ]
        sync_tree(MP_TestNodeExternal, manifest)
        assert tree_structure(MP_TestNodeExternal) == [
            ("a", None, "A"),
            ("b", "a", "B"),
            ("c", "a", "C"),
        ]

    def test_scoped_to_subtree(self):
        make_tree(MP_TestNodeExternal, [
            ("r1", "R1", [("a", "A", [])]),
            ("r2", "R2", [("z", "Z", [])]),
        ])
        before = tree_structure(MP_TestNodeExternal)
        parent = MP_TestNodeExternal.objects.get(external_id="r1")
        manifest = [
            {"external_id": "a", "data": {"desc": "A2"}, "children": [
                {"external_id": "b", "data": {"desc": "B"}},
            ]},
        ]
        sync_tree(MP_TestNodeExternal, manifest, parent=parent)
        after = tree_structure(MP_TestNodeExternal)
        # r2 and its child are untouched
        assert ("r2", None, "R2") in after
        assert ("z", "r2", "Z") in after
        assert [entry for entry in before if entry[0] in ("r2", "z")] == [
            entry for entry in after if entry[0] in ("r2", "z")
        ]
        assert ("a", "r1", "A2") in after
        assert ("b", "a", "B") in after

    def test_rename_then_move_keeps_pk(self):
        """A node can be renamed and moved in one sync and keeps its pk."""
        make_tree(MP_TestNodeExternal, [
            ("x", "X", [("b", "old name", [])]),
            ("y", "Y", []),
        ])
        pk_before = MP_TestNodeExternal.objects.get(external_id="b").pk
        manifest = [
            {"external_id": "x", "data": {"desc": "X"}},
            {"external_id": "y", "data": {"desc": "Y"}, "children": [
                {"external_id": "b", "data": {"desc": "new name"}},
            ]},
        ]
        plan = sync_tree(MP_TestNodeExternal, manifest)
        # the update is planned before the move
        assert kinds(plan) == ["update", "move"]
        node = MP_TestNodeExternal.objects.get(external_id="b")
        assert node.pk == pk_before
        assert node.desc == "new name"
        parent = MP_TestNodeExternal.objects.get_children(MP_TestNodeExternal.objects.get(external_id="y"))
        assert [n.pk for n in parent] == [pk_before]

    def test_reorder_siblings(self):
        make_tree(MP_TestNodeExternal, [
            ("r", "R", [("a", "A", []), ("b", "B", []), ("c", "C", [])]),
        ])
        parent = MP_TestNodeExternal.objects.get(external_id="r")
        manifest = [
            {"external_id": "c", "data": {"desc": "C"}},
            {"external_id": "a", "data": {"desc": "A"}},
            {"external_id": "b", "data": {"desc": "B"}},
        ]
        plan = sync_tree(MP_TestNodeExternal, manifest, parent=parent)
        assert plan.has_changes
        children = MP_TestNodeExternal.objects.get_children(parent)
        assert [n.external_id for n in children] == ["c", "a", "b"]
        # reordering is stable
        assert not sync_tree(MP_TestNodeExternal, manifest, parent=parent).has_changes

    def test_reparent_with_descendants(self):
        make_tree(MP_TestNodeExternal, [
            ("a", "A", [("b", "B", [("c", "C", [])])]),
            ("d", "D", []),
        ])
        manifest = [
            {"external_id": "a", "data": {"desc": "A"}},
            {"external_id": "d", "data": {"desc": "D"}, "children": [
                {"external_id": "b", "data": {"desc": "B"}},
            ]},
        ]
        sync_tree(MP_TestNodeExternal, manifest)
        assert tree_structure(MP_TestNodeExternal) == [
            ("a", None, "A"),
            ("d", None, "D"),
            ("b", "d", "B"),
            ("c", "b", "C"),
        ]
        problems = MP_TestNodeExternal.objects.find_problems()
        assert problems == ([], [], [], [], [])

    def test_swap_parent_and_child(self):
        """B becomes parent of A while A becomes child of B."""
        make_tree(MP_TestNodeExternal, [("a", "A", [("b", "B", [])])])
        manifest = [
            {"external_id": "b", "data": {"desc": "B"}, "children": [
                {"external_id": "a", "data": {"desc": "A"}},
            ]},
        ]
        sync_tree(MP_TestNodeExternal, manifest)
        assert tree_structure(MP_TestNodeExternal) == [("b", None, "B"), ("a", "b", "A")]

    def test_deep_restructure(self):
        """X -> Q -> P becomes Q -> P -> X (requires moving whole branches)."""
        make_tree(MP_TestNodeExternal, [("x", "X", [("q", "Q", [("p", "P", [])])])])
        manifest = [
            {"external_id": "q", "data": {"desc": "Q"}, "children": [
                {"external_id": "p", "data": {"desc": "P"}, "children": [
                    {"external_id": "x", "data": {"desc": "X"}},
                ]},
            ]},
        ]
        sync_tree(MP_TestNodeExternal, manifest)
        assert tree_structure(MP_TestNodeExternal) == [
            ("q", None, "Q"),
            ("p", "q", "P"),
            ("x", "p", "X"),
        ]
        assert MP_TestNodeExternal.objects.find_problems() == ([], [], [], [], [])


class TestMissingNodes:
    def test_missing_kept_by_default(self):
        make_tree(MP_TestNodeExternal, [
            ("r", "R", [("a", "A", []), ("m", "M", [("n", "N", [])])]),
        ])
        parent = MP_TestNodeExternal.objects.get(external_id="r")
        manifest = [{"external_id": "a", "data": {"desc": "A"}}]
        plan = sync_tree(MP_TestNodeExternal, manifest, parent=parent)
        assert kinds(plan) == ["keep"]
        assert not plan.has_changes
        assert ("m", "r", "M") in tree_structure(MP_TestNodeExternal)
        assert ("n", "m", "N") in tree_structure(MP_TestNodeExternal)

    def test_kept_nodes_move_after_manifest_children(self):
        make_tree(MP_TestNodeExternal, [
            ("r", "R", [("m", "M", []), ("a", "A", [])]),
        ])
        parent = MP_TestNodeExternal.objects.get(external_id="r")
        manifest = [{"external_id": "a", "data": {"desc": "A"}}]
        plan = sync_tree(MP_TestNodeExternal, manifest, parent=parent)
        assert "reorder" in kinds(plan)
        children = MP_TestNodeExternal.objects.get_children(parent)
        assert [n.external_id for n in children] == ["a", "m"]
        # and it is stable
        assert not sync_tree(MP_TestNodeExternal, manifest, parent=parent).has_changes

    def test_missing_deleted(self):
        make_tree(MP_TestNodeExternal, [
            ("r", "R", [("a", "A", []), ("m", "M", [("n", "N", [])])]),
        ])
        parent = MP_TestNodeExternal.objects.get(external_id="r")
        manifest = [{"external_id": "a", "data": {"desc": "A"}}]
        plan = sync_tree(MP_TestNodeExternal, manifest, parent=parent, on_missing="delete")
        assert kinds(plan) == ["delete"]
        assert plan.changes[0].identity == "m"
        assert plan.changes[0].detail["descendants"] == 1
        assert tree_structure(MP_TestNodeExternal) == [("r", None, "R"), ("a", "r", "A")]
        assert MP_TestNodeExternal.objects.find_problems() == ([], [], [], [], [])

    def test_delete_moves_manifest_descendants_out_first(self):
        """A missing intermediate node is deleted, its manifest child is kept."""
        make_tree(MP_TestNodeExternal, [
            ("a", "A", [("m", "M", [("b", "B", [])])]),
        ])
        manifest = [
            {"external_id": "a", "data": {"desc": "A"}, "children": [
                {"external_id": "b", "data": {"desc": "B"}},
            ]},
        ]
        plan = sync_tree(MP_TestNodeExternal, manifest, on_missing="delete")
        assert kinds(plan) == ["move", "delete"]
        assert tree_structure(MP_TestNodeExternal) == [("a", None, "A"), ("b", "a", "B")]

    def test_delete_protected_by_business_rule(self):
        make_tree(MP_TestNodeExternal, [
            ("r", "R", [("a", "A", []), ("m", "M", [])]),
        ])
        parent = MP_TestNodeExternal.objects.get(external_id="r")
        manifest = [{"external_id": "a", "data": {"desc": "A"}}]
        with pytest.raises(ProtectedNode, match="business lock"):
            sync_tree(
                MP_TestNodeExternal,
                manifest,
                parent=parent,
                on_missing="delete",
                protect=lambda node: "business lock" if node.external_id == "m" else False,
            )
        # nothing was written
        assert ("m", "r", "M") in tree_structure(MP_TestNodeExternal)

    def test_protection_checked_in_dry_run(self):
        make_tree(MP_TestNodeExternal, [("r", "R", [("m", "M", [])])])
        parent = MP_TestNodeExternal.objects.get(external_id="r")
        with pytest.raises(ProtectedNode):
            sync_tree(
                MP_TestNodeExternal,
                [],
                parent=parent,
                on_missing="delete",
                protect=lambda node: True,
                dry_run=True,
            )


class TestManifestValidation:
    def test_duplicate_identity(self):
        manifest = [
            {"external_id": "a", "data": {"desc": "A"}},
            {"external_id": "a", "data": {"desc": "A2"}},
        ]
        with pytest.raises(DuplicateIdentity):
            sync_tree(MP_TestNodeExternal, manifest)
        assert MP_TestNodeExternal.objects.count() == 0

    def test_cycle(self):
        manifest = [
            {"external_id": "a", "parent": "b", "data": {"desc": "A"}},
            {"external_id": "b", "parent": "a", "data": {"desc": "B"}},
        ]
        with pytest.raises(ManifestCycle):
            sync_tree(MP_TestNodeExternal, manifest)
        assert MP_TestNodeExternal.objects.count() == 0

    def test_unknown_parent_reference(self):
        manifest = [{"external_id": "a", "parent": "ghost", "data": {"desc": "A"}}]
        with pytest.raises(SyncError, match="unknown parent"):
            sync_tree(MP_TestNodeExternal, manifest)

    def test_invalid_positions(self):
        manifest = [
            {"external_id": "a", "parent": None, "position": 0, "data": {"desc": "A"}},
            {"external_id": "b", "parent": None, "position": 0, "data": {"desc": "B"}},
        ]
        with pytest.raises(InvalidManifestOrder):
            sync_tree(MP_TestNodeExternal, manifest)
        manifest[1]["position"] = 2  # gap: 0 and 2 is not a permutation of 0..1
        with pytest.raises(InvalidManifestOrder):
            sync_tree(MP_TestNodeExternal, manifest)
        assert MP_TestNodeExternal.objects.count() == 0

    def test_mixed_formats_rejected(self):
        manifest = [
            {"external_id": "a", "parent": None, "data": {"desc": "A"}},
            {"external_id": "b", "data": {"desc": "B"}, "children": []},
        ]
        with pytest.raises(SyncError):
            sync_tree(MP_TestNodeExternal, manifest)

    def test_invalid_data_fields(self):
        with pytest.raises(SyncError, match="not a field"):
            sync_tree(MP_TestNodeExternal, [{"external_id": "a", "data": {"nope": 1}}])
        with pytest.raises(SyncError, match="identity field"):
            sync_tree(MP_TestNodeExternal, [{"external_id": "a", "data": {"external_id": "a"}}])
        with pytest.raises(SyncError, match="managed by treebeard"):
            sync_tree(MP_TestNodeExternal, [{"external_id": "a", "data": {"path": "0001"}}])
        assert MP_TestNodeExternal.objects.count() == 0

    def test_cross_scope_identity_conflict(self):
        make_tree(MP_TestNodeExternal, [
            ("r1", "R1", [("a", "A", [])]),
            ("r2", "R2", [("b", "B", [])]),
        ])
        parent = MP_TestNodeExternal.objects.get(external_id="r1")
        manifest = [
            {"external_id": "a", "data": {"desc": "A"}},
            {"external_id": "b", "data": {"desc": "B"}},  # lives under r2
        ]
        with pytest.raises(IdentityConflict, match="outside the synchronized scope"):
            sync_tree(MP_TestNodeExternal, manifest, parent=parent)
        # nothing changed
        assert ("b", "r2", "B") in tree_structure(MP_TestNodeExternal)

    def test_identity_field_must_exist(self):
        with pytest.raises(SyncError):
            sync_tree(MP_TestNodeExternal, [], identity_field="no_such_field")


class TestCapacity:
    def test_depth_overflow(self):
        # MP_TestNodeExternalShortPath: steplen 1, path max_length 4 -> 4 levels max
        manifest = [{"external_id": "1", "data": {"desc": "1"}, "children": [
            {"external_id": "2", "data": {"desc": "2"}, "children": [
                {"external_id": "3", "data": {"desc": "3"}, "children": [
                    {"external_id": "4", "data": {"desc": "4"}, "children": [
                        {"external_id": "5", "data": {"desc": "5"}},
                    ]},
                ]},
            ]},
        ]}]
        with pytest.raises(PathOverflow):
            sync_tree(MP_TestNodeExternalShortPath, manifest)
        assert MP_TestNodeExternalShortPath.objects.count() == 0

    def test_sibling_overflow(self):
        # alphabet "012345678", steplen 1 -> at most 8 children per parent
        manifest = [
            {"external_id": str(i), "data": {"desc": str(i)}} for i in range(9)
        ]
        with pytest.raises(PathOverflow):
            sync_tree(MP_TestNodeExternalShortPath, manifest)
        assert MP_TestNodeExternalShortPath.objects.count() == 0

    def test_kept_nodes_count_towards_sibling_capacity(self):
        make_tree(MP_TestNodeExternalShortPath, [("r", "R", [(str(i), str(i), []) for i in range(7)])])
        parent = MP_TestNodeExternalShortPath.objects.get(external_id="r")
        # 7 kept + 2 manifest children = 9 > 8
        manifest = [{"external_id": "a", "data": {"desc": "a"}}, {"external_id": "b", "data": {"desc": "b"}}]
        with pytest.raises(PathOverflow):
            sync_tree(MP_TestNodeExternalShortPath, manifest, parent=parent)
        assert MP_TestNodeExternalShortPath.objects.get(external_id="r").numchild == 7


class TestDryRunAndSummary:
    def test_dry_run_does_not_write(self):
        make_tree(MP_TestNodeExternal, [("a", "A", [("b", "B", [])])])
        before = tree_structure(MP_TestNodeExternal)
        manifest = [
            {"external_id": "a", "data": {"desc": "A2"}, "children": [
                {"external_id": "c", "data": {"desc": "C"}},
            ]},
        ]
        plan = sync_tree(MP_TestNodeExternal, manifest, dry_run=True)
        assert not plan.applied
        assert "update" in kinds(plan)
        assert "add" in kinds(plan)
        assert tree_structure(MP_TestNodeExternal) == before
        # old structure outline is included
        assert "'a'" in plan.old_structure
        assert plan.old_structure.splitlines()[1].startswith("  ")
        assert plan.old_summary

    def test_apply_with_matching_summary(self):
        make_tree(MP_TestNodeExternal, [("a", "A", [])])
        manifest = [{"external_id": "a", "data": {"desc": "A2"}}]
        plan = sync_tree(MP_TestNodeExternal, manifest, dry_run=True)
        applied = sync_tree(MP_TestNodeExternal, manifest, expected_summary=plan.old_summary)
        assert applied.applied
        assert applied.new_summary != applied.old_summary

    def test_stale_summary_aborts(self):
        make_tree(MP_TestNodeExternal, [("a", "A", [])])
        manifest = [{"external_id": "a", "data": {"desc": "A2"}}]
        plan = sync_tree(MP_TestNodeExternal, manifest, dry_run=True)
        # concurrent modification between dry-run and apply
        MP_TestNodeExternal.objects.add_root(instance=MP_TestNodeExternal(external_id="x", desc="X"))
        with pytest.raises(StaleSyncSummary):
            sync_tree(MP_TestNodeExternal, manifest, expected_summary=plan.old_summary)
        # the aborted apply did not write
        assert MP_TestNodeExternal.objects.get(external_id="a").desc == "A"


class TestSignals:
    def test_signals_are_emitted(self):
        make_tree(MP_TestNodeExternal, [
            ("a", "A", [("b", "B", [])]),
            ("d", "D", []),
        ])
        fired = {"path_updated": 0, "nodes_deleted": 0, "post_save": 0}

        def on_path_updated(sender, **kwargs):
            fired["path_updated"] += 1

        def on_nodes_deleted(sender, **kwargs):
            fired["nodes_deleted"] += 1

        def on_post_save(sender, instance, **kwargs):
            fired["post_save"] += 1

        path_updated.connect(on_path_updated, sender=MP_TestNodeExternal)
        nodes_deleted.connect(on_nodes_deleted, sender=MP_TestNodeExternal)
        post_save.connect(on_post_save, sender=MP_TestNodeExternal)
        try:
            manifest = [
                {"external_id": "a", "data": {"desc": "A"}, "children": [
                    {"external_id": "c", "data": {"desc": "C"}},  # new node
                ]},
                {"external_id": "d", "data": {"desc": "D"}, "children": [
                    {"external_id": "b", "data": {"desc": "B2"}},  # renamed + moved
                ]},
                # "e" is missing and gets deleted
            ]
            make_tree(MP_TestNodeExternal, [("e", "E", [])])
            sync_tree(MP_TestNodeExternal, manifest, on_missing="delete")
        finally:
            path_updated.disconnect(on_path_updated, sender=MP_TestNodeExternal)
            nodes_deleted.disconnect(on_nodes_deleted, sender=MP_TestNodeExternal)
            post_save.disconnect(on_post_save, sender=MP_TestNodeExternal)
        assert fired["path_updated"] >= 1  # the move of b
        assert fired["nodes_deleted"] == 1  # the deletion of e
        assert fired["post_save"] >= 2  # the addition of c and the rename of b


class TestSortedModel:
    def test_sorted_placement(self):
        make_tree(MP_TestNodeExternalSorted, [
            ("r", "m", [("a", "b", []), ("b", "a", [])]),
        ])
        parent = MP_TestNodeExternalSorted.objects.get(external_id="r")
        # children are kept sorted by desc
        children = MP_TestNodeExternalSorted.objects.get_children(parent)
        assert [n.desc for n in children] == ["a", "b"]
        manifest = [
            {"external_id": "a", "data": {"desc": "a"}},
            {"external_id": "b", "data": {"desc": "b"}},
            {"external_id": "c", "data": {"desc": "c"}},
        ]
        sync_tree(MP_TestNodeExternalSorted, manifest, parent=parent)
        children = MP_TestNodeExternalSorted.objects.get_children(parent)
        assert [n.external_id for n in children] == ["a", "b", "c"]
        assert not sync_tree(MP_TestNodeExternalSorted, manifest, parent=parent).has_changes

    def test_manifest_order_must_match_node_order_by(self):
        manifest = [
            {"external_id": "b", "data": {"desc": "b"}},
            {"external_id": "a", "data": {"desc": "a"}},
        ]
        with pytest.raises(InvalidManifestOrder, match="node_order_by"):
            sync_tree(MP_TestNodeExternalSorted, manifest)
        assert MP_TestNodeExternalSorted.objects.count() == 0

    def test_sorted_move_to_other_parent(self):
        make_tree(MP_TestNodeExternalSorted, [
            ("r1", "a", [("x", "b", [])]),
            ("r2", "z", []),
        ])
        manifest = [
            {"external_id": "r1", "data": {"desc": "a"}},
            {"external_id": "r2", "data": {"desc": "z"}, "children": [
                {"external_id": "x", "data": {"desc": "b"}},
            ]},
        ]
        sync_tree(MP_TestNodeExternalSorted, manifest)
        assert tree_structure(MP_TestNodeExternalSorted) == [
            ("r1", None, "a"),
            ("r2", None, "z"),
            ("x", "r2", "b"),
        ]


class TestEdgeCases:
    def test_stale_parent_instance_is_refreshed(self):
        make_tree(MP_TestNodeExternal, [("r", "R", [])])
        parent = MP_TestNodeExternal.objects.get(external_id="r")
        # modify the tree after the parent instance was fetched
        MP_TestNodeExternal.objects.add_child(
            MP_TestNodeExternal.objects.get(external_id="r"),
            instance=MP_TestNodeExternal(external_id="a", desc="A"),
        )
        manifest = [{"external_id": "a", "data": {"desc": "A"}}, {"external_id": "b", "data": {"desc": "B"}}]
        plan = sync_tree(MP_TestNodeExternal, manifest, parent=parent)
        assert plan.applied
        children = MP_TestNodeExternal.objects.get_children(MP_TestNodeExternal.objects.get(external_id="r"))
        assert [n.external_id for n in children] == ["a", "b"]

    def test_delete_mode_is_idempotent(self):
        make_tree(MP_TestNodeExternal, [
            ("r", "R", [("a", "A", []), ("m", "M", [("n", "N", [])])]),
        ])
        parent = MP_TestNodeExternal.objects.get(external_id="r")
        manifest = [{"external_id": "a", "data": {"desc": "A"}}]
        sync_tree(MP_TestNodeExternal, manifest, parent=parent, on_missing="delete")
        plan = sync_tree(MP_TestNodeExternal, manifest, parent=parent, on_missing="delete")
        assert not plan.has_changes

    def test_empty_manifest_delete_clears_scope_only(self):
        make_tree(MP_TestNodeExternal, [
            ("r1", "R1", [("a", "A", [])]),
            ("r2", "R2", []),
        ])
        parent = MP_TestNodeExternal.objects.get(external_id="r1")
        sync_tree(MP_TestNodeExternal, [], parent=parent, on_missing="delete")
        assert tree_structure(MP_TestNodeExternal) == [("r1", None, "R1"), ("r2", None, "R2")]

    def test_anchor_itself_is_never_modified(self):
        make_tree(MP_TestNodeExternal, [("r", "R", [("a", "A", [])])])
        parent = MP_TestNodeExternal.objects.get(external_id="r")
        path_before, desc_before = parent.path, parent.desc
        manifest = [{"external_id": "b", "data": {"desc": "B"}}]
        sync_tree(MP_TestNodeExternal, manifest, parent=parent, on_missing="delete")
        parent.refresh_from_db()
        assert parent.path == path_before
        assert parent.desc == desc_before

    def test_on_missing_validation(self):
        with pytest.raises(ValueError, match="on_missing"):
            sync_tree(MP_TestNodeExternal, [], on_missing="explode")
