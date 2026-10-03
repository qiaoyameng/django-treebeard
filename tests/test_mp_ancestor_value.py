"""Tests for nearest-ancestor value annotations on MP_Node querysets."""

import pytest
from django.conf import settings
from django.db import connections
from django.db.models import F, Q
from django.test.utils import CaptureQueriesContext
from django.core.paginator import Paginator

from tests.models import MP_TestNodeBilling

pytestmark = pytest.mark.django_db

settings.DATABASES["billing_other"] = {
    **settings.DATABASES["default"],
    # shared-cache in-memory DB so every connection opened for the alias sees
    # the same (separate) database; plain ":memory:" gives a fresh DB per
    # connection, hiding schema and rows created on another connection
    "NAME": "file:billing_other?mode=memory&cache=shared",
    "OPTIONS": {"uri": True},
    "CONN_MAX_AGE": None,
}


@pytest.fixture
def billing_tree():
    """
    Root 001 R1         (undefined)
       001001 A         (defined "X")
          001001001 Ax  (undefined -> X)
             001001001001 Axc (defined NULL: explicit clear)
                001001001001001 Axcd (undefined -> NULL)
          001001002 Ab  (defined "Y" -> Y)
       001002 B         (undefined -> None, never defined)
    Root 002 R2         (defined "Z")
       002001 C         (undefined -> Z)
    """
    objs = MP_TestNodeBilling.objects
    r1 = objs.add_root({"desc": "R1", "billing_defined": False})
    a = objs.add_child(r1, {"desc": "A", "billing_defined": True, "billing_rate": "X"})
    ax = objs.add_child(a, {"desc": "Ax", "billing_defined": False})
    axc = objs.add_child(ax, {"desc": "Axc", "billing_defined": True, "billing_rate": None})
    axcd = objs.add_child(axc, {"desc": "Axcd", "billing_defined": False})
    ab = objs.add_child(a, {"desc": "Ab", "billing_defined": True, "billing_rate": "Y"})
    objs.add_child(r1, {"desc": "B", "billing_defined": False})
    r2 = objs.add_root({"desc": "R2", "billing_defined": True, "billing_rate": "Z"})
    objs.add_child(r2, {"desc": "C", "billing_defined": False})
    return {
        "r1": r1,
        "a": a,
        "ax": ax,
        "axc": axc,
        "axcd": axcd,
        "ab": ab,
        "r2": r2,
    }


DEFINED = Q(billing_defined=True)


def annotated(**kwargs):
    return MP_TestNodeBilling.objects.annotate_ancestor_value(
        "rate", DEFINED, "billing_rate", **kwargs
    )


def values_by_desc(qs):
    return {obj.desc: (obj.rate, obj.rate_source) for obj in qs}


def test_inherits_from_self_and_nearest_ancestor(billing_tree):
    result = values_by_desc(annotated())
    a = billing_tree["a"]
    ax = billing_tree["ax"]
    axc = billing_tree["axc"]
    axcd = billing_tree["axcd"]
    ab = billing_tree["ab"]
    r2 = billing_tree["r2"]

    # self definition wins over ancestors
    assert result["A"] == ("X", a.path)
    assert result["Ab"] == ("Y", ab.path)
    # nearest defined ancestor
    assert result["Ax"] == ("X", a.path)
    assert result["R2"] == ("Z", r2.path)
    assert result["C"] == ("Z", r2.path)
    # explicit NULL override blocks the higher ancestor
    assert result["Axc"] == (None, axc.path)
    assert result["Axcd"] == (None, axc.path)
    # never defined anywhere on the path: value AND source are NULL
    assert result["R1"] == (None, None)
    assert result["B"] == (None, None)


def test_explicit_null_is_distinguishable_from_undefined(billing_tree):
    rows = {obj.desc: obj for obj in annotated()}
    # explicit clear: value is NULL but a source is present
    assert rows["Axc"].rate is None and rows["Axc"].rate_source == rows["Axc"].path
    # no definition at all: value and source are both NULL
    assert rows["B"].rate is None and rows["B"].rate_source is None


def test_roots_do_not_share_values(billing_tree):
    # a node in tree 2 must never inherit a value from tree 1
    result = values_by_desc(annotated().filter(desc__in=["R2", "C"]))
    assert result == {"R2": ("Z", billing_tree["r2"].path), "C": ("Z", billing_tree["r2"].path)}


def test_path_matching_respects_step_boundaries():
    root = MP_TestNodeBilling.objects.add_root({"desc": "root", "billing_defined": True, "billing_rate": "ROOT"})
    child = MP_TestNodeBilling.objects.add_child(root, {"desc": "child", "billing_defined": False})
    # "0010" is a prefix of "001001" but not on a step boundary:
    # it must never be treated as an ancestor.
    MP_TestNodeBilling.objects.create(
        path="0010",
        depth=99,
        numchild=0,
        desc="bogus",
        billing_rate="BAD",
        billing_defined=True,
    )
    row = annotated().get(pk=child.pk)
    assert row.rate == "ROOT"
    assert row.rate_source == root.path
    bogus = annotated().get(desc="bogus")
    assert bogus.rate == "BAD"
    assert bogus.rate_source == "0010"


def test_target_filters_do_not_hide_ancestor_sources(billing_tree):
    # only undefined, deep nodes are targeted, yet the defined ancestors
    # (excluded from the target queryset) must still be found as sources
    qs = annotated().filter(desc__in=["Ax", "Axcd", "C", "B"]).exclude(billing_defined=True)
    result = values_by_desc(qs)
    assert result["Ax"] == ("X", billing_tree["a"].path)
    assert result["Axcd"] == (None, billing_tree["axc"].path)
    assert result["C"] == ("Z", billing_tree["r2"].path)
    assert result["B"] == (None, None)


def test_ordering_and_pagination(billing_tree):
    qs = annotated().order_by("-rate")
    # Axc/Axcd carry explicit NULL overrides, plus R1/B are undefined:
    # five defined rows sort first, four NULL rows last under descending order
    assert [obj.rate for obj in qs[:5]] == ["Z", "Z", "Y", "X", "X"]
    assert all(obj.rate is None for obj in qs[5:])
    page = list(qs[:3])
    assert len(page) == 3

    paginator = Paginator(annotated().order_by("path"), 3)
    assert paginator.count == 9
    page_rows = [(obj.desc, obj.rate, obj.rate_source) for obj in paginator.page(2)]
    assert [row[0] for row in page_rows] == ["Axc", "Axcd", "Ab"]


def test_fixed_query_count():
    roots = []
    for i in range(10):
        root = MP_TestNodeBilling.objects.add_root({"desc": f"r{i}", "billing_defined": True, "billing_rate": f"V{i}"})
        for j in range(2):
            MP_TestNodeBilling.objects.add_child(root, {"desc": f"r{i}-{j}", "billing_defined": False})
        roots.append(root)

    with CaptureQueriesContext(connections["default"]) as ctx:
        rows = list(annotated().filter(desc__startswith="r").order_by("desc")[:15])
    assert len(rows) == 15
    selects = [q for q in ctx.captured_queries if q["sql"].lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1, selects

    # widening the target does not add per-node ancestor queries
    with CaptureQueriesContext(connections["default"]) as ctx:
        rows = list(annotated())
    assert len(rows) == 30
    selects = [q for q in ctx.captured_queries if q["sql"].lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1


def test_annotation_reflects_moves():
    top = MP_TestNodeBilling.objects.add_root({"desc": "top", "billing_defined": True, "billing_rate": "TOP"})
    node = MP_TestNodeBilling.objects.add_child(top, {"desc": "moving", "billing_defined": False})

    other = MP_TestNodeBilling.objects.add_root({"desc": "other", "billing_defined": True, "billing_rate": "OTHER"})

    def row():
        return annotated().get(pk=node.pk)

    assert row().rate == "TOP"
    assert row().rate_source == top.path

    # moving the target into the other tree changes the nearest source
    MP_TestNodeBilling.objects.move(node, other, pos="last-child")
    assert row().rate == "OTHER"
    assert row().rate_source == other.path

    # moving it back under a fresh leaf root restores the previous source
    top2 = MP_TestNodeBilling.objects.add_root(
        {"desc": "top2", "billing_defined": True, "billing_rate": "TOP2"}
    )
    MP_TestNodeBilling.objects.move(node, top2, pos="last-child")
    assert row().rate == "TOP2"
    assert row().rate_source == top2.path

    # moving the (subtree containing the) nearest source rewrites the source
    mid = MP_TestNodeBilling.objects.add_root({"desc": "mid", "billing_defined": True, "billing_rate": "MID"})
    leaf = MP_TestNodeBilling.objects.add_child(mid, {"desc": "leaf", "billing_defined": False})
    assert annotated().get(pk=leaf.pk).rate_source == mid.path
    top.refresh_from_db()
    MP_TestNodeBilling.objects.move(mid, top, pos="last-child")
    mid.refresh_from_db()
    leaf.refresh_from_db()
    assert annotated().get(pk=leaf.pk).rate == "MID"
    assert annotated().get(pk=leaf.pk).rate_source == mid.path
    assert mid.path != "003"


@pytest.mark.django_db(databases="__all__")
def test_uses_database_alias():
    # The annotation and its correlated subqueries must run entirely against
    # the queryset's database alias. A separate, shared-cache in-memory
    # SQLite database is used so reads and the outer query share the data.
    connection = connections["billing_other"]
    try:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS tests_mp_testnodebilling")
            cursor.execute(
                """
                CREATE TABLE tests_mp_testnodebilling (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path VARCHAR(255) NOT NULL UNIQUE,
                    depth INTEGER UNSIGNED NOT NULL,
                    numchild INTEGER UNSIGNED NOT NULL,
                    "desc" VARCHAR(255) NOT NULL,
                    billing_rate VARCHAR(32) NULL,
                    billing_defined BOOL NOT NULL
                )
                """
            )
            cursor.executemany(
                """
                INSERT INTO tests_mp_testnodebilling
                    (path, depth, numchild, "desc", billing_rate, billing_defined)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [
                    ("001", 1, 1, "root", "X", 1),
                    ("001001", 2, 0, "child", None, 0),
                    ("002", 1, 0, "other", "Z", 1),
                ],
            )

        # db_manager (rather than objects.using, which is shadowed by the
        # custom queryset's own ``using`` method) pins the manager to the
        # extra database alias
        manager = MP_TestNodeBilling.objects.db_manager("billing_other")
        qs = manager.annotate_ancestor_value("rate", DEFINED, "billing_rate").order_by("path")

        # the generated SQL really targets this alias when executed
        assert qs.db == "billing_other"

        with CaptureQueriesContext(connection) as ctx:
            rows = list(qs)
        assert [(obj.desc, obj.rate, obj.rate_source) for obj in rows] == [
            ("root", "X", "001"),
            ("child", "X", "001"),
            ("other", "Z", "002"),
        ]
        assert len(ctx.captured_queries) == 1

        # nothing leaked into the default database
        assert MP_TestNodeBilling.objects.count() == 0
    finally:
        connection.close()


def test_custom_source_options(billing_tree):
    # source=False skips the source annotation
    row = (
        MP_TestNodeBilling.objects.annotate_ancestor_value(
            "rate", DEFINED, "billing_rate", source=False
        )
        .get(desc="Ax")
    )
    assert row.rate == "X"
    assert not hasattr(row, "rate_source")

    # custom source annotation name and source field (pk)
    row = (
        MP_TestNodeBilling.objects.annotate_ancestor_value(
            "rate", DEFINED, "billing_rate", source="src", source_field="id"
        )
        .get(desc="Ax")
    )
    assert row.rate == "X"
    assert row.src == billing_tree["a"].pk


def test_field_expression(billing_tree):
    row = (
        MP_TestNodeBilling.objects.annotate_ancestor_value(
            "rate", DEFINED, F("billing_rate")
        )
        .get(desc="Ax")
    )
    assert row.rate == "X"
