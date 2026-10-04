"""Tests for treebeard.mp_recode (maintenance-window path re-encoding)."""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from tests.models import MP_TestNode as MP
from treebeard.exceptions import PathOverflow
from treebeard.mp_recode import RecodeError, check_encoding, plan_recode, recode_tree
from treebeard.mp_tree import path_updated
from treebeard.numconv import NumConv

OLD_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
OLD_STEPLEN = 3  # matches MP_TestNode, but the tool must not rely on that

TREE_DATA = [
    {"data": {"desc": "1"}},
    {
        "data": {"desc": "2"},
        "children": [
            {"data": {"desc": "21"}},
            {"data": {"desc": "22"}},
            {
                "data": {"desc": "23"},
                "children": [
                    {"data": {"desc": "231"}},
                ],
            },
            {"data": {"desc": "24"}},
        ],
    },
    {"data": {"desc": "3"}},
    {
        "data": {"desc": "4"},
        "children": [
            {"data": {"desc": "41"}},
        ],
    },
]


def structure(model, alphabet, steplen):
    """Decodes the table with explicit params into a comparable structure.

    Maps pk -> (parent_pk, sibling_index, depth, numchild).
    """
    conv = NumConv(alphabet)
    rows = list(model.objects.values("pk", "path", "depth", "numchild"))
    by_path = {row["path"]: row for row in rows}
    result = {}
    for row in rows:
        path = row["path"]
        parent_path = path[:-steplen]
        parent_pk = by_path[parent_path]["pk"] if parent_path else None
        siblings = sorted(
            (conv.str2int(other[-steplen:]), other) for other in by_path if other[:-steplen] == parent_path
        )
        index = [p for _, p in siblings].index(path)
        result[row["pk"]] = (parent_pk, index, row["depth"], row["numchild"])
    return result


def business_fields(model):
    return {node.pk: node.desc for node in model.objects.all()}


def load_tree():
    MP.objects.load_bulk(TREE_DATA)


def no_problems(model, alphabet, steplen):
    problems = check_encoding(model, alphabet, steplen)
    return all(not pks for pks in problems.values())


@pytest.mark.django_db
class TestRecode:
    def test_recode_preserves_structure_pks_and_business_fields(self):
        load_tree()
        before = structure(MP, OLD_ALPHABET, OLD_STEPLEN)
        before_desc = business_fields(MP)

        plan = recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, OLD_ALPHABET, 5)

        assert plan.node_count == len(before)
        assert structure(MP, OLD_ALPHABET, 5) == before
        assert business_fields(MP) == before_desc
        assert all(len(node.path) % 5 == 0 for node in MP.objects.all())
        # the model class still carries the old config; the tool never needed it
        assert MP.steplen == OLD_STEPLEN
        assert no_problems(MP, OLD_ALPHABET, 5)

    def test_tiny_alphabet_forces_rearrangement_and_compaction(self):
        load_tree()
        # punch holes: delete node "22" and root "3"
        MP.objects.filter(desc__in=("22", "3")).delete()
        before = structure(MP, OLD_ALPHABET, OLD_STEPLEN)
        before_paths = set(MP.objects.values_list("path", flat=True))

        recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, "01", 4)

        after_paths = set(MP.objects.values_list("path", flat=True))
        assert structure(MP, "01", 4) == before
        # paths really were rewritten to the tiny alphabet
        assert before_paths != after_paths
        assert all(set(path) <= {"0", "1"} for path in after_paths)
        # holes were compacted: children of "2" (now root 2 = "0010") are
        # 0001..0003, and "41" follows its parent "4" (now root 3 = "0011")
        children = sorted(MP.objects.filter(depth=2).values_list("path", flat=True))
        assert children == ["00100001", "00100010", "00100011", "00110001"]
        assert no_problems(MP, "01", 4)

    def test_capacity_overflow_aborts_and_leaves_data_untouched(self):
        load_tree()
        before = list(MP.objects.values_list("pk", "path"))
        # 4 roots do not fit in radix=2, steplen=1 (capacity 1)
        with pytest.raises(PathOverflow):
            recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, "01", 1)
        assert list(MP.objects.values_list("pk", "path")) == before
        assert no_problems(MP, OLD_ALPHABET, OLD_STEPLEN)

    def test_capacity_exact_fit_works(self):
        load_tree()
        # 4 roots and 4 children of "2": radix=2, steplen=3 -> capacity 7
        recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, "01", 3)
        assert no_problems(MP, "01", 3)

    def test_max_path_length_overflow(self):
        node = MP.objects.add_root({"desc": "deep"})
        for _ in range(59):
            node = MP.objects.add_child(node, {"desc": "deep"})
        assert node.depth == 60  # 60 * 3 = 180 chars, fits max_length=255
        with pytest.raises(PathOverflow):
            recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, OLD_ALPHABET, 5)  # 60 * 5 = 300 > 255

    def test_orphan_aborts_and_is_listed(self):
        load_tree()
        orphan = MP.objects.get(desc="21")
        MP.objects.filter(pk=orphan.pk).update(path="009001")  # parent "009" missing
        before = list(MP.objects.values_list("pk", "path"))

        with pytest.raises(RecodeError) as excinfo:
            recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, OLD_ALPHABET, 5)

        assert excinfo.value.problems["orphans"] == [orphan.pk]
        # nothing was rewritten, and the orphan was not re-attached to root
        assert list(MP.objects.values_list("pk", "path")) == before

    @pytest.mark.parametrize(
        "corrupt",
        [
            {"path": "00z"},  # char outside the old alphabet
            {"path": "0001"},  # length not a multiple of steplen
            {"depth": 7},  # depth inconsistent with path
        ],
    )
    def test_corrupt_rows_abort(self, corrupt):
        load_tree()
        node = MP.objects.get(desc="1")
        MP.objects.filter(pk=node.pk).update(**corrupt)
        with pytest.raises(RecodeError) as excinfo:
            recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, OLD_ALPHABET, 5)
        assert node.pk in excinfo.value.problems.popitem()[1]

    def test_numchild_and_depth_are_recomputed(self):
        load_tree()
        MP.objects.filter(desc="2").update(numchild=99)
        MP.objects.filter(desc="21").update(depth=9)
        # wrong depth is corruption and must abort...
        with pytest.raises(RecodeError):
            recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, OLD_ALPHABET, 5)
        MP.objects.filter(desc="21").update(depth=2)
        # ...while a wrong numchild is simply recomputed
        recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, OLD_ALPHABET, 5)
        assert MP.objects.get(desc="2").numchild == 4
        assert no_problems(MP, OLD_ALPHABET, 5)

    def test_rewrite_avoids_unique_constraint_collisions(self):
        MP.objects.load_bulk([{"data": {"desc": "1"}}, {"data": {"desc": "2"}}, {"data": {"desc": "3"}}])
        MP.objects.filter(desc="1").delete()  # remaining roots: "002", "003"
        # new encoding compacts to "001"/"002": each new path equals the
        # *other* row's old path, so a naive UPDATE would hit the unique
        # constraint
        recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, "0123456789", 3)
        got = list(MP.objects.order_by("path").values_list("desc", "path"))
        assert got == [("2", "001"), ("3", "002")]

    def test_dry_run_writes_nothing(self):
        load_tree()
        before = list(MP.objects.values_list("pk", "path", "depth", "numchild"))
        plan = recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, "01", 4, dry_run=True)
        assert plan.node_count == len(before)
        assert list(MP.objects.values_list("pk", "path", "depth", "numchild")) == before

    def test_empty_table(self):
        plan = recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, "01", 4)
        assert plan.node_count == 0

    def test_invalid_new_alphabet(self):
        load_tree()
        with pytest.raises(ValueError):
            recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, "001", 4)  # duplicates
        with pytest.raises(RecodeError):
            recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, "Z0", 4)  # not sort-compatible
        with pytest.raises(RecodeError):
            recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, "0", 4)  # radix 1

    def test_path_updated_signal_is_sent(self):
        load_tree()
        seen = []
        receiver = lambda signal, sender, old_path, new_path, using: seen.append((old_path, new_path))  # noqa: E731
        path_updated.connect(receiver, weak=False)
        try:
            recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, OLD_ALPHABET, 5)
        finally:
            path_updated.disconnect(receiver)
        assert len(seen) == MP.objects.count()

    def test_check_encoding_is_repeatable(self):
        load_tree()
        for _ in range(2):
            assert no_problems(MP, OLD_ALPHABET, OLD_STEPLEN)
            assert not no_problems(MP, "01", 4)
        recode_tree(MP, OLD_ALPHABET, OLD_STEPLEN, "01", 4)
        for _ in range(2):
            assert no_problems(MP, "01", 4)
            assert not no_problems(MP, OLD_ALPHABET, OLD_STEPLEN)

    def test_plan_is_deterministic(self):
        load_tree()
        plan1 = plan_recode(MP, OLD_ALPHABET, OLD_STEPLEN, "01", 4)
        plan2 = plan_recode(MP, OLD_ALPHABET, OLD_STEPLEN, "01", 4)
        assert [(c.pk, c.new_path) for c in plan1.changes] == [(c.pk, c.new_path) for c in plan2.changes]


@pytest.mark.django_db
class TestManagementCommand:
    def test_full_migration_then_check(self, capsys):
        load_tree()
        call_command(
            "recode_mp_tree",
            "tests.MP_TestNode",
            "--old-alphabet",
            OLD_ALPHABET,
            "--old-steplen",
            "3",
            "--new-alphabet",
            "01",
            "--new-steplen",
            "4",
        )
        assert no_problems(MP, "01", 4)
        # repeatable post-migration check with the new parameters
        call_command("recode_mp_tree", "tests.MP_TestNode", "--old-alphabet", "01", "--old-steplen", "4", "--check")

    def test_check_reports_problems(self):
        load_tree()
        with pytest.raises(CommandError):
            call_command(
                "recode_mp_tree",
                "tests.MP_TestNode",
                "--old-alphabet",
                "01",
                "--old-steplen",
                "4",
                "--check",
            )

    def test_command_requires_new_params(self):
        load_tree()
        with pytest.raises(CommandError):
            call_command(
                "recode_mp_tree",
                "tests.MP_TestNode",
                "--old-alphabet",
                OLD_ALPHABET,
                "--old-steplen",
                "3",
            )
