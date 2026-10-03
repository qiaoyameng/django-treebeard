import pytest

from treebeard.mp_tree import PROJECTION_PROMOTE, PROJECTION_PRUNE
from tests.models import MP_TestNode


pytestmark = pytest.mark.django_db


def build_projection_tree():
    root = MP_TestNode.objects.add_root({"desc": "root"})
    hidden_child = MP_TestNode.objects.add_child(root, {"desc": "hidden-child"})
    grandchild = MP_TestNode.objects.add_child(hidden_child, {"desc": "grandchild"})
    great_grandchild = MP_TestNode.objects.add_child(grandchild, {"desc": "great-grandchild"})
    visible_child = MP_TestNode.objects.add_child(root, {"desc": "visible-child"})
    second_root = MP_TestNode.objects.add_root({"desc": "second-root"})
    return root, hidden_child, grandchild, great_grandchild, visible_child, second_root


def projected_rows(projection):
    by_pk = {row["pk"]: row for row in projection["nodes"]}

    def label(pk):
        return None if pk is None else by_pk.get(pk, {}).get("fields", {}).get("desc")

    return [
        (
            row["fields"]["desc"],
            label(row["parent"]),
            row["depth"],
            row["numchild"],
            row["open"],
            row["close"],
            row["parent_in_page"],
        )
        for row in projection["nodes"]
    ]


def test_project_tree_prunes_hidden_subtree(django_assert_max_num_queries):
    root, hidden_child, grandchild, great_grandchild, visible_child, second_root = build_projection_tree()
    allowed = MP_TestNode.objects.filter(
        pk__in=[root.pk, grandchild.pk, great_grandchild.pk, visible_child.pk, second_root.pk]
    )

    with django_assert_max_num_queries(1):
        projection = MP_TestNode.objects.project_tree(allowed, mode=PROJECTION_PRUNE)

    assert projection["multi_root"] is True
    assert projection["max_visible_depth"] == 2
    assert projection["truncated"] is False
    assert projection["total_nodes"] == 3
    assert projection["root_pks"] == [root.pk, second_root.pk]
    assert projected_rows(projection) == [
        ("root", None, 1, 1, True, [], False),
        ("visible-child", "root", 2, 0, True, [], True),
        ("second-root", None, 1, 0, False, [0], False),
    ]
    assert all(set(row["fields"]) == {"desc"} for row in projection["nodes"])
    assert all("path" not in row and "numchild" not in row["fields"] for row in projection["nodes"])

    hidden_child.refresh_from_db()
    assert hidden_child.depth == 2
    assert hidden_child.numchild == 1
    assert hidden_child.path == root.path + "001"


def test_project_tree_promotes_allowed_descendants(django_assert_max_num_queries):
    root, hidden_child, grandchild, great_grandchild, visible_child, second_root = build_projection_tree()
    allowed_pks = [root.pk, grandchild.pk, great_grandchild.pk, visible_child.pk, second_root.pk]

    with django_assert_max_num_queries(1):
        projection = MP_TestNode.objects.project_tree(allowed_pks, mode=PROJECTION_PROMOTE)

    assert projection["multi_root"] is True
    assert projection["max_visible_depth"] == 3
    assert projection["total_nodes"] == 5
    assert projection["root_pks"] == [root.pk, second_root.pk]
    assert projected_rows(projection) == [
        ("root", None, 1, 2, True, [], False),
        ("grandchild", "root", 2, 1, True, [], True),
        ("great-grandchild", "grandchild", 3, 0, True, [], True),
        ("visible-child", "root", 2, 0, False, [0], True),
        ("second-root", None, 1, 0, False, [0], False),
    ]


def test_project_tree_accepts_primary_key_queryset_in_one_query(django_assert_max_num_queries):
    root, _hidden_child, grandchild, _great_grandchild, visible_child, second_root = build_projection_tree()
    allowed = MP_TestNode.objects.filter(
        pk__in=[root.pk, grandchild.pk, visible_child.pk, second_root.pk]
    ).values_list("pk", flat=True)

    with django_assert_max_num_queries(1):
        projection = MP_TestNode.objects.project_tree(allowed, mode=PROJECTION_PROMOTE)

    assert [row["pk"] for row in projection["nodes"]] == [root.pk, grandchild.pk, visible_child.pk, second_root.pk]


def test_project_tree_recomputes_max_visible_depth():
    root, _hidden_child, grandchild, great_grandchild, _visible_child, second_root = build_projection_tree()
    allowed = [root.pk, grandchild.pk, great_grandchild.pk, second_root.pk]

    projection = MP_TestNode.objects.project_tree(allowed, mode=PROJECTION_PROMOTE, max_visible_depth=2)

    assert projection["max_visible_depth"] == 2
    assert projection["truncated"] is True
    assert projection["total_nodes"] == 3
    assert projected_rows(projection) == [
        ("root", None, 1, 1, True, [], False),
        ("grandchild", "root", 2, 0, True, [], True),
        ("second-root", None, 1, 0, False, [0], False),
    ]


def test_project_tree_paginates_after_projection_with_consistent_metadata():
    root, _hidden_child, grandchild, great_grandchild, visible_child, second_root = build_projection_tree()
    allowed = [root.pk, grandchild.pk, great_grandchild.pk, visible_child.pk, second_root.pk]

    first_page = MP_TestNode.objects.project_tree(allowed, mode=PROJECTION_PROMOTE, offset=1, limit=2)

    assert first_page["total_nodes"] == 5
    assert first_page["offset"] == 1
    assert first_page["limit"] == 2
    assert first_page["has_previous"] is True
    assert first_page["has_next"] is True
    assert first_page["open_pks"] == [root.pk, grandchild.pk]
    assert [node["open"] for node in first_page["nodes"]] == [True, True]
    assert projected_rows(first_page) == [
        ("grandchild", None, 2, 1, True, [], False),
        ("great-grandchild", "grandchild", 3, 0, True, [], True),
    ]

    last_page = MP_TestNode.objects.project_tree(allowed, mode=PROJECTION_PROMOTE, offset=3, limit=2)

    assert last_page["has_previous"] is True
    assert last_page["has_next"] is False
    assert last_page["open_pks"] == []
    assert projected_rows(last_page) == [
        ("visible-child", None, 2, 0, False, [0], False),
        ("second-root", None, 1, 0, False, [0], False),
    ]

    empty_page = MP_TestNode.objects.project_tree(allowed, mode=PROJECTION_PROMOTE, offset=5, limit=2)

    assert empty_page["nodes"] == []
    assert empty_page["total_nodes"] == 5
    assert empty_page["max_visible_depth"] == 3
    assert empty_page["has_previous"] is True
    assert empty_page["has_next"] is False


def test_project_tree_single_root_is_not_multi_root():
    root = MP_TestNode.objects.add_root({"desc": "root"})
    child = MP_TestNode.objects.add_child(root, {"desc": "child"})

    projection = MP_TestNode.objects.project_tree([root.pk, child.pk])

    assert projection["multi_root"] is False
    assert projection["root_pks"] == [root.pk]


def test_project_tree_rejects_sliced_queryset():
    build_projection_tree()

    with pytest.raises(ValueError, match="offset and limit"):
        MP_TestNode.objects.project_tree(MP_TestNode.objects.all()[:10])


def test_project_tree_rejects_invalid_arguments():
    build_projection_tree()

    with pytest.raises(ValueError, match="mode"):
        MP_TestNode.objects.project_tree([], mode="hide")

    with pytest.raises(ValueError, match="max_visible_depth"):
        MP_TestNode.objects.project_tree([], max_visible_depth=0)

    with pytest.raises(ValueError, match="offset"):
        MP_TestNode.objects.project_tree([], offset=-1)

    with pytest.raises(ValueError, match="limit"):
        MP_TestNode.objects.project_tree([], limit=0)

    with pytest.raises(ValueError, match="offset requires limit"):
        MP_TestNode.objects.project_tree([], offset=1)


def test_project_tree_empty_allowed_nodes():
    projection = MP_TestNode.objects.project_tree([], mode=PROJECTION_PROMOTE)

    assert projection == {
        "mode": PROJECTION_PROMOTE,
        "multi_root": False,
        "max_visible_depth": 0,
        "truncated": False,
        "root_pks": [],
        "nodes": [],
        "total_nodes": 0,
        "offset": 0,
        "limit": None,
        "has_previous": False,
        "has_next": False,
        "open_pks": [],
    }
