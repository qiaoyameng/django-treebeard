"""Tests for :meth:`MP_NodeManager.move_bulk`."""

from unittest import mock

import pytest

from treebeard.exceptions import InvalidMoveToDescendant, InvalidPosition, PathOverflow
from treebeard.mp_tree import BulkMoveResult, path_updated

from .models import MP_TestNode, MP_TestNodeShortPath, MP_TestNodeSorted


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def tree():
    """
    1
    2
      21
        211
      22
      23
    3
    4
      41
    """
    MP_TestNode.objects.load_bulk(
        [
            {"data": {"desc": "1"}},
            {
                "data": {"desc": "2"},
                "children": [
                    {"data": {"desc": "21"}, "children": [{"data": {"desc": "211"}}]},
                    {"data": {"desc": "22"}},
                    {"data": {"desc": "23"}},
                ],
            },
            {"data": {"desc": "3"}},
            {"data": {"desc": "4"}, "children": [{"data": {"desc": "41"}}]},
        ]
    )
    return {node.desc: node for node in MP_TestNode.objects.all()}


def _paths():
    return {node.desc: (node.path, node.depth, node.numchild) for node in MP_TestNode.objects.all()}


def _no_problems():
    assert MP_TestNode.objects.find_problems() == ([], [], [], [], [])


def test_empty_batch_is_noop():
    assert MP_TestNode.objects.move_bulk([]) == []


def test_returns_results_describing_final_state(tree):
    results = MP_TestNode.objects.move_bulk(
        [
            {"node": tree["3"].pk, "target": tree["1"].pk, "pos": "first-child"},
            {"node": tree["4"].pk, "target": None, "pos": "first-sibling"},
        ]
    )
    assert len(results) == 2
    assert all(isinstance(result, BulkMoveResult) for result in results)
    by_node = {result.node_pk: result for result in results}
    node3 = by_node[tree["3"].pk]
    assert node3.parent_pk == tree["1"].pk
    assert node3.depth == 2
    assert node3.sibling_index == 1
    assert node3.moved is True
    node4 = by_node[tree["4"].pk]
    assert node4.parent_pk is None
    assert node4.depth == 1
    assert node4.sibling_index == 1
    _no_problems()


def test_noop_move_reports_moved_false(tree):
    results = MP_TestNode.objects.move_bulk(
        [{"node": tree["3"].pk, "target": tree["4"].pk, "pos": "left"}]
    )
    assert results[0].moved is False
    assert results[0].new_path == tree["3"].path
    _no_problems()


def test_swap_two_roots_with_subtrees(tree):
    # Exchange 2 and 4 (both carry children) by final position.
    MP_TestNode.objects.move_bulk(
        [
            {"node": tree["4"].pk, "target": None, "pos": "first-sibling"},
            {"node": tree["2"].pk, "target": tree["4"].pk, "pos": "right"},
        ]
    )
    paths = _paths()
    roots = [node.desc for node in MP_TestNode.objects.filter(depth=1).order_by("path")]
    assert roots == ["4", "2", "1", "3"]
    # descendants follow their root and keep relative order/depth
    assert paths["41"][1] == 2
    assert paths["41"][0].startswith(paths["4"][0])
    assert paths["21"][1] == 2
    assert paths["211"][1] == 3
    assert paths["211"][0].startswith(paths["2"][0])
    assert MP_TestNode.objects.get(desc="4").numchild == 1
    assert MP_TestNode.objects.get(desc="2").numchild == 3
    _no_problems()


def test_swap_siblings_legal_but_sequential_moves_cannot_reach():
    """
    Final state [z, y, x] under P. Describing it with single moves in the
    natural order fails: moving z to the front gives [z, x, y], then moving x
    to the right of z is a no-op, so [z, y, x] is unreachable that way. The
    bulk planner works from the final state and reaches it directly.
    """
    MP_TestNode.objects.load_bulk(
        [
            {
                "data": {"desc": "P"},
                "children": [
                    {"data": {"desc": "x"}},
                    {"data": {"desc": "y"}},
                    {"data": {"desc": "z"}},
                ],
            }
        ]
    )
    parent = MP_TestNode.objects.get(desc="P")

    # Prove the sequential approach does not reach the requested final state.
    MP_TestNode.objects.move(MP_TestNode.objects.get(desc="z"), parent, "first-child")
    MP_TestNode.objects.move(MP_TestNode.objects.get(desc="x"), MP_TestNode.objects.get(desc="z"), "right")
    assert [node.desc for node in MP_TestNode.objects.get_children(parent)] == ["z", "x", "y"]

    # Rebuild and let the bulk API plan it from the final relationships.
    MP_TestNode.objects.all().delete()
    MP_TestNode.objects.load_bulk(
        [
            {
                "data": {"desc": "P"},
                "children": [
                    {"data": {"desc": "x"}},
                    {"data": {"desc": "y"}},
                    {"data": {"desc": "z"}},
                ],
            }
        ]
    )
    parent = MP_TestNode.objects.get(desc="P")
    nodes = {node.desc: node for node in MP_TestNode.objects.all()}
    MP_TestNode.objects.move_bulk(
        [
            {"node": nodes["z"].pk, "target": parent.pk, "pos": "first-child"},
            {"node": nodes["x"].pk, "target": nodes["y"].pk, "pos": "right"},
        ]
    )
    assert [node.desc for node in MP_TestNode.objects.get_children(parent)] == ["z", "y", "x"]
    assert MP_TestNode.objects.find_problems() == ([], [], [], [], [])


def test_target_parent_is_itself_moved(tree):
    # C moves under A, and 21 moves under C in the same batch.
    MP_TestNode.objects.move_bulk(
        [
            {"node": tree["3"].pk, "target": tree["1"].pk, "pos": "last-child"},
            {"node": tree["21"].pk, "target": tree["3"].pk, "pos": "last-child"},
        ]
    )
    a = MP_TestNode.objects.get(desc="1")
    c = MP_TestNode.objects.get(desc="3")
    node21 = MP_TestNode.objects.get(desc="21")
    node211 = MP_TestNode.objects.get(desc="211")
    assert [node.desc for node in MP_TestNode.objects.get_children(a)] == ["3"]
    assert [node.desc for node in MP_TestNode.objects.get_children(c)] == ["21"]
    assert node21.depth == 3
    assert node211.depth == 4
    assert node211.path.startswith(node21.path)
    # 2 lost one child (21), A gained one (C), C gained one (21)
    assert MP_TestNode.objects.get(desc="2").numchild == 2
    assert a.numchild == 1
    assert c.numchild == 1
    _no_problems()


def test_final_cycle_is_rejected(tree):
    # B under 22 and 22 under B: a cycle in the final state.
    with pytest.raises(InvalidMoveToDescendant):
        MP_TestNode.objects.move_bulk(
            [
                {"node": tree["3"].pk, "target": tree["22"].pk, "pos": "first-child"},
                {"node": tree["22"].pk, "target": tree["3"].pk, "pos": "first-child"},
            ]
        )
    # nothing was written
    assert _paths()["22"] == ("002002", 2, 0)
    _no_problems()


def test_ancestor_moving_under_descendant_that_stays_below_is_rejected(tree):
    # 211 stays inside 2's subtree (it only moves within it), so putting 2
    # under 211 is a cycle in the final state even though 211 also moves.
    with pytest.raises(InvalidMoveToDescendant):
        MP_TestNode.objects.move_bulk(
            [
                {"node": tree["211"].pk, "target": tree["22"].pk, "pos": "left"},
                {"node": tree["2"].pk, "target": tree["211"].pk, "pos": "first-child"},
            ]
        )
    _no_problems()


def test_ancestor_can_follow_descendant_out_of_subtree(tree):
    # 211 moves out from under 2 (to root 4), and only then does 2 move under
    # it: the final state is a chain and must be accepted.
    MP_TestNode.objects.move_bulk(
        [
            {"node": tree["211"].pk, "target": tree["4"].pk, "pos": "first-child"},
            {"node": tree["2"].pk, "target": tree["211"].pk, "pos": "first-child"},
        ]
    )
    node2 = MP_TestNode.objects.get(desc="2")
    node211 = MP_TestNode.objects.get(desc="211")
    assert node2.path.startswith(node211.path)
    assert MP_TestNode.objects.get(desc="21").numchild == 0
    assert MP_TestNode.objects.find_problems() == ([], [], [], [], [])


def test_duplicate_node_rejected(tree):
    with pytest.raises(InvalidPosition):
        MP_TestNode.objects.move_bulk(
            [
                {"node": tree["3"].pk, "target": tree["1"].pk, "pos": "first-child"},
                {"node": tree["3"].pk, "target": tree["4"].pk, "pos": "first-child"},
            ]
        )


def test_missing_node_rejected(tree):
    with pytest.raises(InvalidPosition):
        MP_TestNode.objects.move_bulk([{"node": 99999, "target": None, "pos": "last-sibling"}])


def test_missing_target_rejected(tree):
    with pytest.raises(InvalidPosition):
        MP_TestNode.objects.move_bulk(
            [{"node": tree["3"].pk, "target": 99999, "pos": "first-child"}]
        )


def test_invalid_pos_rejected(tree):
    with pytest.raises(InvalidPosition):
        MP_TestNode.objects.move_bulk([{"node": tree["3"].pk, "target": None, "pos": "first-child"}])


def test_move_to_self_as_child_rejected(tree):
    with pytest.raises(InvalidMoveToDescendant):
        MP_TestNode.objects.move_bulk(
            [{"node": tree["3"].pk, "target": tree["3"].pk, "pos": "first-child"}]
        )


def test_numchild_updated_on_cross_parent_move(tree):
    MP_TestNode.objects.move_bulk(
        [{"node": tree["21"].pk, "target": tree["4"].pk, "pos": "last-child"}]
    )
    assert MP_TestNode.objects.get(desc="2").numchild == 2
    assert MP_TestNode.objects.get(desc="4").numchild == 2
    node21 = MP_TestNode.objects.get(desc="21")
    assert node21.depth == 2
    assert node21.path.startswith(MP_TestNode.objects.get(desc="4").path)
    # its descendant 211 moves with it and keeps depth offset
    assert MP_TestNode.objects.get(desc="211").depth == 3
    _no_problems()


def test_passed_instances_are_refreshed_others_are_not(tree):
    node3 = MP_TestNode.objects.get(desc="3")
    node1 = MP_TestNode.objects.get(desc="1")
    stale_reference = MP_TestNode.objects.get(desc="3")
    MP_TestNode.objects.move_bulk(
        [{"node": node3, "target": node1, "pos": "first-child"}]
    )
    assert node3.path == "001001" and node3.depth == 2
    assert node1.numchild == 1
    # an instance not handed to the API keeps its stale cached values
    assert stale_reference.path == "003"
    assert stale_reference.depth == 1


def test_path_updated_sent_only_after_commit(tree):
    received = []
    handler = lambda sender, **kwargs: received.append((kwargs["old_path"], kwargs["new_path"]))
    path_updated.connect(handler)
    try:
        MP_TestNode.objects.move_bulk(
            [{"node": tree["3"].pk, "target": tree["1"].pk, "pos": "first-child"}]
        )
    finally:
        path_updated.disconnect(handler)
    # one net-change signal for the relocated subtree, no staging noise
    assert ("003", "001001") in received
    assert all(old != new for old, new in received)


def test_no_signal_on_validation_failure(tree):
    received = []
    handler = lambda sender, **kwargs: received.append((kwargs["old_path"], kwargs["new_path"]))
    path_updated.connect(handler)
    try:
        with pytest.raises(InvalidMoveToDescendant):
            MP_TestNode.objects.move_bulk(
                [
                    {"node": tree["3"].pk, "target": tree["22"].pk, "pos": "first-child"},
                    {"node": tree["22"].pk, "target": tree["3"].pk, "pos": "first-child"},
                ]
            )
    finally:
        path_updated.disconnect(handler)
    assert received == []


def test_mid_write_exception_rolls_back_entire_batch(tree, monkeypatch):
    before = list(
        MP_TestNode.objects.order_by("path").values_list("path", "depth", "numchild", "desc")
    )
    real_rewrite = MP_TestNode.objects._rewrite_subtree_paths
    state = {"count": 0}

    def flushing_rewrite(oldpath, newpath, using=None, emit_signal=False):
        state["count"] += 1
        if state["count"] == 2:
            raise RuntimeError("injected mid-write failure")
        return real_rewrite(oldpath, newpath, using=using, emit_signal=emit_signal)

    received = []
    handler = lambda sender, **kwargs: received.append(kwargs)
    path_updated.connect(handler)
    try:
        monkeypatch.setattr(
            MP_TestNode.objects,
            "_rewrite_subtree_paths",
            flushing_rewrite,
        )
        with pytest.raises(RuntimeError, match="injected mid-write failure"):
            MP_TestNode.objects.move_bulk(
                [
                    {"node": tree["4"].pk, "target": None, "pos": "first-sibling"},
                    {"node": tree["2"].pk, "target": tree["4"].pk, "pos": "right"},
                ]
            )
    finally:
        path_updated.disconnect(handler)

    after = list(
        MP_TestNode.objects.order_by("path").values_list("path", "depth", "numchild", "desc")
    )
    assert after == before
    assert received == []
    assert MP_TestNode.objects.find_problems() == ([], [], [], [], [])


def test_unique_path_temp_conflict_avoided_on_full_swap():
    # Swap two leaf siblings into each other's exact slot; staging must avoid
    # the unique constraint regardless of write order.
    MP_TestNode.objects.load_bulk(
        [{"data": {"desc": "P"}, "children": [{"data": {"desc": "a"}}, {"data": {"desc": "b"}}]}]
    )
    parent = MP_TestNode.objects.get(desc="P")
    a = MP_TestNode.objects.get(desc="a")
    b = MP_TestNode.objects.get(desc="b")
    MP_TestNode.objects.move_bulk(
        [
            {"node": b.pk, "target": parent.pk, "pos": "first-child"},
            {"node": a.pk, "target": parent.pk, "pos": "last-child"},
        ]
    )
    assert [node.desc for node in MP_TestNode.objects.get_children(parent)] == ["b", "a"]
    _no_problems()


class TestBulkMoveSorted:
    def _setup(self):
        MP_TestNodeSorted.objects.load_bulk(
            [
                {"data": {"desc": "a", "val1": 1, "val2": 1},
                        "children": [
                            {"data": {"desc": "a1", "val1": 1, "val2": 1}},
                            {"data": {"desc": "a2", "val1": 2, "val2": 0}},
                        ]},
                {"data": {"desc": "b", "val1": 2, "val2": 1}},
                {"data": {"desc": "c", "val1": 3, "val2": 1}},
            ]
        )
        return {node.desc: node for node in MP_TestNodeSorted.objects.all()}

    def test_sorted_child_position(self):
        nodes = self._setup()
        new_child = MP_TestNodeSorted.objects.add_root({"desc": "m", "val1": 1, "val2": 5})
        # sorted placement under 'a': val1=1 sorts ahead of a1(val1=1 tie val2)
        MP_TestNodeSorted.objects.move_bulk(
            [{"node": new_child.pk, "target": nodes["a"].pk, "pos": "sorted-child"}]
        )
        children = [node.desc for node in MP_TestNodeSorted.objects.get_children(nodes["a"])]
        # m(1,5) vs a1(1,1): val1 ties, val2 asc -> a1 before m; then a2
        assert children[0] == "a1"
        assert "m" in children and "a2" in children

    def test_explicit_non_sorted_pos_rejected(self):
        nodes = self._setup()
        with pytest.raises(InvalidPosition):
            MP_TestNodeSorted.objects.move_bulk(
                [{"node": nodes["c"].pk, "target": nodes["a"].pk, "pos": "first-child"}]
            )


def test_move_left_of_self_is_noop(tree):
    before = _paths()
    results = MP_TestNode.objects.move_bulk(
        [{"node": tree["22"].pk, "target": tree["22"].pk, "pos": "left"}]
    )
    assert results[0].moved is False
    assert _paths() == before
    _no_problems()


def test_move_right_of_self_is_noop(tree):
    before = _paths()
    results = MP_TestNode.objects.move_bulk(
        [{"node": tree["3"].pk, "target": tree["3"].pk, "pos": "right"}]
    )
    assert results[0].moved is False
    assert _paths() == before
    _no_problems()


def test_deep_subtree_depth_updated_on_move(tree):
    # move subtree rooted at 21 (contains 211) to root 4; depths must follow
    MP_TestNode.objects.move_bulk(
        [{"node": tree["21"].pk, "target": tree["4"].pk, "pos": "first-child"}]
    )
    assert MP_TestNode.objects.get(desc="21").depth == 2
    assert MP_TestNode.objects.get(desc="211").depth == 3
    _no_problems()


def test_depth_increases_when_moving_deeper(tree):
    # move root 3 under leaf 211: 3 becomes depth 4
    MP_TestNode.objects.move_bulk(
        [{"node": tree["3"].pk, "target": tree["211"].pk, "pos": "first-child"}]
    )
    node3 = MP_TestNode.objects.get(desc="3")
    assert node3.depth == 4
    assert node3.path == MP_TestNode.objects.get(desc="211").path + "001"
    assert MP_TestNode.objects.get(desc="211").numchild == 1
    _no_problems()


class TestBulkMovePathCapacity:
    pytestmark = pytest.mark.django_db(transaction=True)

    def test_root_swap_below_full_capacity_succeeds(self):
        # With enough free root slots, swapping two roots (one carrying a
        # subtree) works through root-level staging even on a tiny steplen.
        model = MP_TestNodeShortPath
        for index in range(6):
            model.objects.add_root({"desc": str(index)})
        model.objects.add_child(model.objects.get(path="1"), {"desc": "c"})
        first = model.objects.get(path="1")
        last = model.objects.get(path="6")
        model.objects.move_bulk(
            [
                {"node": last.pk, "target": None, "pos": "first-sibling"},
                {"node": first.pk, "target": None, "pos": "last-sibling"},
            ]
        )
        roots = [node.path for node in model.objects.filter(depth=1).order_by("path")]
        assert roots == list("123456")
        assert model.objects.get(pk=last.pk).path == "1"
        assert model.objects.get(pk=first.pk).path == "6"
        assert model.objects.get(desc="c").path == "61"
        assert model.objects.find_problems() == ([], [], [], [], [])

    def test_root_swap_at_full_capacity_raises_overflow(self):
        # Every root slot occupied: there is no temporary path to move through,
        # so the batch raises PathOverflow just like the single move API.
        model = MP_TestNodeShortPath
        for index in range(8):
            model.objects.add_root({"desc": str(index)})
        first = model.objects.get(path="1")
        last = model.objects.get(path="8")
        with pytest.raises(PathOverflow):
            model.objects.move_bulk(
                [
                    {"node": last.pk, "target": None, "pos": "first-sibling"},
                    {"node": first.pk, "target": last.pk, "pos": "right"},
                ]
            )
        assert model.objects.find_problems() == ([], [], [], [], [])

    def test_overflow_when_capacity_exceeded(self):
        model = MP_TestNodeShortPath
        for index in range(8):
            model.objects.add_root({"desc": str(index)})
        extra = model.objects.add_root  # cannot add; instead use existing child
        # promote a child to root would need a 9th root slot: create a child and
        # move it to root level -> exceeds the 8-slot root capacity
        parent = model.objects.get(path="1")
        child = model.objects.add_child(parent, {"desc": "c"})
        with pytest.raises(PathOverflow):
            model.objects.move_bulk(
                [{"node": child.pk, "target": None, "pos": "last-sibling"}]
            )
        assert model.objects.find_problems() == ([], [], [], [], [])

    def test_depth_overflow_when_moving_too_deep(self):
        from django.db import connection, models as dj_models
        from treebeard.mp_tree import MP_Node

        class TinyNode(MP_Node):
            steplen = 1
            alphabet = "012345678"
            path = dj_models.CharField(max_length=1, unique=True)
            depth = dj_models.PositiveIntegerField()
            numchild = dj_models.PositiveIntegerField(default=0)
            desc = dj_models.CharField(max_length=255, default="")

            class Meta:
                app_label = "tests"
                managed = False

        with connection.schema_editor() as editor:
            editor.create_model(TinyNode)
        try:
            root = TinyNode.objects.add_root({"desc": "r"})
            leaf = TinyNode.objects.add_root({"desc": "l"})
            with pytest.raises(PathOverflow):
                TinyNode.objects.move_bulk(
                    [{"node": leaf.pk, "target": root.pk, "pos": "first-child"}]
                )
        finally:
            with connection.schema_editor() as editor:
                editor.delete_model(TinyNode)


def test_bulk_move_with_uuid_pk():
    from .models import MP_TestNodeCustomId

    MP_TestNodeCustomId.objects.load_bulk(
        [
            {"data": {"desc": "a"}},
            {"data": {"desc": "b"}, "children": [{"data": {"desc": "b1"}}]},
            {"data": {"desc": "c"}},
        ]
    )
    nodes = {node.desc: node for node in MP_TestNodeCustomId.objects.all()}
    results = MP_TestNodeCustomId.objects.move_bulk(
        [
            {"node": nodes["c"].pk, "target": nodes["b"].pk, "pos": "first-child"},
            {"node": nodes["a"].pk, "target": nodes["b"].pk, "pos": "left"},
        ]
    )
    assert all(isinstance(result.node_pk, type(nodes["a"].pk)) for result in results)
    b = MP_TestNodeCustomId.objects.get(desc="b")
    assert [node.desc for node in MP_TestNodeCustomId.objects.get_children(b)] == ["c", "b1"]
    roots = [node.desc for node in MP_TestNodeCustomId.objects.filter(depth=1).order_by("path")]
    assert roots == ["a", "b"]
    assert MP_TestNodeCustomId.objects.find_problems() == ([], [], [], [], [])


def test_multiple_independent_group_reorders_and_moves(tree):
    # reorder children of 2 and move 4 under 3 at the same time
    MP_TestNode.objects.move_bulk(
        [
            {"node": tree["23"].pk, "target": tree["21"].pk, "pos": "left"},
            {"node": tree["4"].pk, "target": tree["3"].pk, "pos": "first-child"},
        ]
    )
    node2 = MP_TestNode.objects.get(desc="2")
    node3 = MP_TestNode.objects.get(desc="3")
    assert [node.desc for node in MP_TestNode.objects.get_children(node2)] == ["23", "21", "22"]
    assert [node.desc for node in MP_TestNode.objects.get_children(node3)] == ["4"]
    assert [node.desc for node in MP_TestNode.objects.get_children(MP_TestNode.objects.get(desc="4"))] == ["41"]
    assert MP_TestNode.objects.get(desc="41").depth == 3
    roots = [node.desc for node in MP_TestNode.objects.filter(depth=1).order_by("path")]
    assert roots == ["1", "2", "3"]
    assert MP_TestNode.objects.find_problems() == ([], [], [], [], [])


def test_saturated_permutation_group_under_a_moving_parent(tree):
    # Children of 2 are packed (21,22,23); reorder them while moving 2 itself
    # under root 4. The rotation must use 2's *final* path.
    MP_TestNode.objects.move_bulk(
        [
            {"node": tree["23"].pk, "target": tree["21"].pk, "pos": "left"},
            {"node": tree["2"].pk, "target": tree["4"].pk, "pos": "first-child"},
        ]
    )
    node4 = MP_TestNode.objects.get(desc="4")
    node2 = MP_TestNode.objects.get(desc="2")
    assert node2.path.startswith(node4.path)
    assert [node.desc for node in MP_TestNode.objects.get_children(node2)] == ["23", "21", "22"]
    node231 = MP_TestNode.objects.get(desc="211")
    assert node231.path.startswith(node2.path)
    assert node231.depth == 4
    assert MP_TestNode.objects.find_problems() == ([], [], [], [], [])


def test_reorder_group_inside_a_subtree_whose_ancestor_moves(tree):
    # reorder 2's children (23 before 21) while moving the whole subtree 2
    # under root 3. The reordered group travels with 2 and must keep its order.
    MP_TestNode.objects.move_bulk(
        [
            {"node": tree["23"].pk, "target": tree["21"].pk, "pos": "left"},
            {"node": tree["2"].pk, "target": tree["3"].pk, "pos": "first-child"},
        ]
    )
    node2 = MP_TestNode.objects.get(desc="2")
    node3 = MP_TestNode.objects.get(desc="3")
    assert node2.path.startswith(node3.path)
    assert [node.desc for node in MP_TestNode.objects.get_children(node2)] == ["23", "21", "22"]
    assert MP_TestNode.objects.get(desc="211").depth == 4
    assert MP_TestNode.objects.find_problems() == ([], [], [], [], [])

