"""
Tests for the Materialized Path encoding migration tool
(``treebeard.recode``): changing alphabet/steplen during a maintenance window.
"""

import pytest
from django.db import connections

from tests.models import MP_TestNode, MP_TestNodeShortPath
from treebeard.recode import (
    CAPACITY,
    COLLATION_CASE,
    NO_MARKER_SPACE,
    ORPHAN,
    PATH_TOO_LONG,
    WRONG_DEPTH,
    WRONG_NUMCHILD,
    RecodeStateError,
    RecodeValidationError,
    analyze_mp_paths,
    forget_recode,
    plan_recode,
    recode_mp_paths,
    recode_status,
    resume_recode,
    rollback_recode,
)

OLD_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
TINY_ALPHABET = "0123"
BIG_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def tree(db):
    """A small tree built with the standard API (steplen=3, 36-char alphabet)."""
    root = MP_TestNode.objects.add_root({"desc": "root"})
    alpha = MP_TestNode.objects.add_child(root, {"desc": "alpha"})
    beta = MP_TestNode.objects.add_child(root, {"desc": "beta"})
    MP_TestNode.objects.add_child(alpha, {"desc": "alpha-1"})
    MP_TestNode.objects.add_child(alpha, {"desc": "alpha-2"})
    MP_TestNode.objects.add_child(beta, {"desc": "beta-1"})
    return root


def _snapshot():
    return {
        node.pk: (node.path, node.depth, node.numchild, node.desc)
        for node in MP_TestNode.objects.all()
    }


@pytest.fixture
def tiny_encoding():
    """Run with the model configured for the *new* tiny encoding, then restore."""
    original_alphabet = MP_TestNode.alphabet
    original_steplen = MP_TestNode.steplen
    yield
    MP_TestNode.alphabet = original_alphabet
    MP_TestNode.steplen = original_steplen
    MP_TestNode.numconv_obj.cache_clear()


class TestHappyPath:
    def test_recode_roundtrip(self, tree):
        before = _snapshot()
        result = recode_mp_paths(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet=TINY_ALPHABET,
            new_steplen=3,
        )
        assert result.node_count == 6
        assert recode_status(MP_TestNode)["phase"] == "done"

        # primary keys and business fields untouched, paths rewritten
        after = MP_TestNode.objects.in_bulk(before.keys())
        new_paths = sorted(node.path for node in after.values())
        assert all(set(path) <= set(TINY_ALPHABET) for path in new_paths)
        for pk, (old_path, depth, numchild, desc) in before.items():
            assert after[pk].desc == desc
            assert after[pk].path != old_path or old_path == after[pk].path

    def test_treebeard_api_works_with_new_encoding(self, tree, tiny_encoding):
        recode_mp_paths(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet=TINY_ALPHABET,
            new_steplen=3,
        )
        MP_TestNode.alphabet = TINY_ALPHABET
        MP_TestNode.numconv_obj.cache_clear()

        roots = list(MP_TestNode.get_root_nodes())
        assert [node.desc for node in roots] == ["root"]
        children = list(MP_TestNode.objects.get_children(roots[0]))
        assert [node.desc for node in children] == ["alpha", "beta"]
        # the tree accepts further inserts under the new encoding
        new_child = MP_TestNode.objects.add_child(roots[0], {"desc": "gamma"})
        assert set(new_child.path[-3:]) <= set(TINY_ALPHABET)

    def test_rollback_restores_exact_state(self, tree):
        before = _snapshot()
        recode_mp_paths(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet=TINY_ALPHABET,
            new_steplen=3,
        )
        assert rollback_recode(MP_TestNode) == 6
        assert _snapshot() == before
        assert recode_status(MP_TestNode) is None

    def test_widen_steplen_roundtrip(self, tree, tiny_encoding):
        result = recode_mp_paths(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet=BIG_ALPHABET,
            new_steplen=4,
        )
        assert result.node_count == 6
        MP_TestNode.alphabet = BIG_ALPHABET
        MP_TestNode.steplen = 4
        MP_TestNode.numconv_obj.cache_clear()
        for node in MP_TestNode.objects.all():
            assert len(node.path) % 4 == 0
        problems = MP_TestNode.objects.find_problems()
        assert problems == ([], [], [], [], [])

    def test_empty_table(self, db):
        result = recode_mp_paths(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet=TINY_ALPHABET,
            new_steplen=3,
        )
        assert result.node_count == 0

class TestForcedRearrangement:
    """Tiny alphabets where the sibling labels must actually move."""

    def _raw_insert(self, path, depth, numchild=0, desc=""):
        MP_TestNode.objects.create(path=path, depth=depth, numchild=numchild, desc=desc)

    def test_siblings_reordered_and_relabeled(self, db):
        # Old radix-36 encoding with a hole (003) and 009: under a radix-4
        # alphabet the ordinals 1, 3, 9 become 001, 002, 003 — all labels
        # change, exercising the collision-free intermediate rewrite.
        self._raw_insert("001", 1, 0, "first")
        self._raw_insert("003", 1, 0, "second")
        self._raw_insert("009", 1, 0, "third")

        recode_mp_paths(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet=TINY_ALPHABET,
            new_steplen=3,
        )
        roots = list(MP_TestNode.get_root_nodes())
        assert [node.desc for node in roots] == ["first", "second", "third"]
        assert [node.path for node in roots] == ["001", "002", "003"]

    def test_branches_carried_with_parent(self, db):
        # parent starts at old ordinal 5 (005) with a child; after recoding it
        # becomes 001 and the whole subtree prefix must move atomically.
        self._raw_insert("005", 1, 1, "parent")
        self._raw_insert("005002", 2, 1, "middle")
        self._raw_insert("005002007", 3, 0, "leaf")

        recode_mp_paths(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet=TINY_ALPHABET,
            new_steplen=3,
        )
        leaf = MP_TestNode.objects.get(desc="leaf")
        assert leaf.path == "001001001"
        assert leaf.depth == 3
        middle = MP_TestNode.objects.get(desc="middle")
        assert middle.path == "001001"
        assert middle.numchild == 1

    def test_steplen_shrink_repacks_every_step(self, db):
        self._raw_insert("001", 1, 1, "r")
        self._raw_insert("001001001", 3, 0, "deep")
        # create the missing depth-2 parent
        self._raw_insert("001001", 2, 1, "mid")
        recode_mp_paths(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet=TINY_ALPHABET,
            new_steplen=2,
        )
        assert MP_TestNode.objects.get(desc="deep").path == "010101"


class TestValidation:
    def test_capacity_overflow_aborts_with_list(self, db):
        # radix 4, steplen 3 -> 63 capacity; 64 roots overflow
        for i in range(1, 65):
            MP_TestNode.objects.create(
                path=MP_TestNode._get_path("", 1, i), depth=1, numchild=0
            )
        plan = plan_recode(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet=TINY_ALPHABET,
            new_steplen=3,
        )
        capacity_problems = [p for p in plan.problems if p["category"] == CAPACITY]
        assert capacity_problems
        with pytest.raises(RecodeValidationError) as exc:
            recode_mp_paths(
                MP_TestNode,
                old_alphabet=OLD_ALPHABET,
                old_steplen=3,
                new_alphabet=TINY_ALPHABET,
                new_steplen=3,
            )
        assert any(p["category"] == CAPACITY for p in exc.value.problems)
        # nothing written: still old encoding
        assert all(set(n.path) <= set(OLD_ALPHABET) for n in MP_TestNode.objects.all())
        assert recode_status(MP_TestNode) is None

    def test_tiny_alphabet_capacity_steplen_one(self, db):
        # radix 2, steplen 1 -> only one sibling per parent; 2 roots overflow
        MP_TestNode.objects.create(path="001", depth=1, numchild=0)
        MP_TestNode.objects.create(path="002", depth=1, numchild=0)
        plan = plan_recode(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet="01",
            new_steplen=1,
        )
        assert any(p["category"] == CAPACITY for p in plan.problems)
        assert plan.capacity_per_level == 1

    def test_path_too_long(self, db):
        # MP_TestNodeShortPath: steplen=1, radix 9, path max_length=4.
        # A depth-4 chain exists at capacity; recoding steplen 1 -> 2 exceeds
        # the 4-char column on the first depth-3 node.
        path = "1"
        MP_TestNodeShortPath.objects.create(path="1", depth=1, numchild=1)
        MP_TestNodeShortPath.objects.create(path="11", depth=2, numchild=1)
        MP_TestNodeShortPath.objects.create(path="111", depth=3, numchild=1)
        MP_TestNodeShortPath.objects.create(path="1111", depth=4, numchild=0)
        plan = plan_recode(
            MP_TestNodeShortPath,
            old_alphabet="012345678",
            old_steplen=1,
            new_alphabet="012345678",
            new_steplen=2,
        )
        assert any(p["category"] == PATH_TOO_LONG for p in plan.problems)

    def test_no_marker_space(self, db):
        # A depth-4 tree fills the 4-char column completely, so the marker
        # prefix has nowhere to go even though the new encoding itself fits.
        MP_TestNodeShortPath.objects.create(path="1", depth=1, numchild=1)
        MP_TestNodeShortPath.objects.create(path="11", depth=2, numchild=1)
        MP_TestNodeShortPath.objects.create(path="111", depth=3, numchild=1)
        MP_TestNodeShortPath.objects.create(path="1111", depth=4, numchild=0)
        plan = plan_recode(
            MP_TestNodeShortPath,
            old_alphabet="012345678",
            old_steplen=1,
            new_alphabet="012345678",
            new_steplen=1,
        )
        assert any(p["category"] == NO_MARKER_SPACE for p in plan.problems)

    def test_orphan_aborts_and_is_listed(self, db):
        good = MP_TestNode.objects.create(path="001", depth=1, numchild=0, desc="good")
        orphan = MP_TestNode.objects.create(path="009001", depth=2, numchild=0, desc="orphan")
        nodes, problems = analyze_mp_paths(MP_TestNode, OLD_ALPHABET, 3)
        orphan_problems = [p for p in problems if p["category"] == ORPHAN]
        assert [p["pk"] for p in orphan_problems] == [orphan.pk]
        with pytest.raises(RecodeValidationError) as exc:
            recode_mp_paths(
                MP_TestNode,
                old_alphabet=OLD_ALPHABET,
                old_steplen=3,
                new_alphabet=TINY_ALPHABET,
                new_steplen=3,
            )
        assert any(p["category"] == ORPHAN for p in exc.value.problems)
        # the valid node must not have been silently re-parented to root
        assert MP_TestNode.objects.get(pk=good.pk).path == "001"

    def test_bad_alphabet_path_aborts(self, db):
        MP_TestNode.objects.create(path="zzz", depth=1, numchild=0)
        with pytest.raises(RecodeValidationError):
            recode_mp_paths(
                MP_TestNode,
                old_alphabet=OLD_ALPHABET,
                old_steplen=3,
                new_alphabet=TINY_ALPHABET,
                new_steplen=3,
            )

    def test_bad_steplen_path_aborts(self, db):
        MP_TestNode.objects.create(path="0010", depth=1, numchild=0)
        with pytest.raises(RecodeValidationError) as exc:
            recode_mp_paths(
                MP_TestNode,
                old_alphabet=OLD_ALPHABET,
                old_steplen=3,
                new_alphabet=TINY_ALPHABET,
                new_steplen=3,
            )
        assert any(p["category"] == "bad_steplen" for p in exc.value.problems)

    def test_wrong_depth_and_numchild_block_by_default(self, tree):
        MP_TestNode.objects.filter(desc="root").update(depth=5)
        MP_TestNode.objects.filter(desc="alpha").update(numchild=9)
        with pytest.raises(RecodeValidationError) as exc:
            recode_mp_paths(
                MP_TestNode,
                old_alphabet=OLD_ALPHABET,
                old_steplen=3,
                new_alphabet=TINY_ALPHABET,
                new_steplen=3,
            )
        categories = {p["category"] for p in exc.value.problems}
        assert WRONG_DEPTH in categories
        assert WRONG_NUMCHILD in categories

    def test_repair_fields_recomputes_them(self, tree):
        MP_TestNode.objects.filter(desc="root").update(depth=5, numchild=7)
        result = recode_mp_paths(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet=TINY_ALPHABET,
            new_steplen=3,
            repair_fields=True,
        )
        assert result.node_count == 6
        root = MP_TestNode.objects.get(desc="root")
        assert root.depth == 1
        assert root.numchild == 2


class TestModelConfiguration:
    def test_cannot_run_with_model_on_new_encoding(self, tree):
        # Operator changed the class attributes before migrating the data
        MP_TestNode.alphabet = TINY_ALPHABET
        MP_TestNode.numconv_obj.cache_clear()
        try:
            with pytest.raises(RecodeStateError) as exc:
                recode_mp_paths(
                    MP_TestNode,
                    old_alphabet=OLD_ALPHABET,
                    old_steplen=3,
                    new_alphabet=TINY_ALPHABET,
                    new_steplen=3,
                )
            assert "config_mismatch" in str(exc.value)
        finally:
            MP_TestNode.alphabet = OLD_ALPHABET
            MP_TestNode.numconv_obj.cache_clear()

    def test_invalid_alphabet_rejected(self, tree):
        with pytest.raises(RecodeValidationError):
            plan_recode(
                MP_TestNode,
                old_alphabet=OLD_ALPHABET,
                old_steplen=3,
                new_alphabet="0012",  # duplicated character
                new_steplen=3,
            )


class TestCollation:
    def test_binary_column_passes_case_mixed_alphabet(self, db):
        # SQLite's path column is BINARY, so a case-mixed alphabet is usable.
        from treebeard.recode import check_database_compatibility

        assert check_database_compatibility(MP_TestNode, "0123456789abcdef", 2, marker=".") == []
        assert check_database_compatibility(MP_TestNode, "0123456789ABCDEF", 2, marker=".") == []

    def test_case_folding_is_flagged_against_nocase(self, db):
        # Same probe path as a real migration, but forced onto SQLite's
        # built-in NOCASE collation where a/A compare equal.
        from treebeard.recode import check_database_compatibility

        problems = check_database_compatibility(
            MP_TestNode,
            "0123456789abcdefABCDEF",
            2,
            marker=".",
            collation_override="NOCASE",
        )
        assert any(p["category"] == COLLATION_CASE for p in problems)

    def test_out_of_order_alphabet_is_flagged(self, db):
        # The declared order disagrees with BINARY code-point ordering
        # ('4' is placed before '2', though code point 0x34 > 0x32).
        from treebeard.recode import COLLATION_INCOMPATIBLE, check_database_compatibility

        problems = check_database_compatibility(MP_TestNode, "01423", 1, marker=".")
        assert any(p["category"] == COLLATION_INCOMPATIBLE for p in problems)

class TestRecovery:
    def _build_bigger_tree(self, n=5):
        root = MP_TestNode.objects.add_root({"desc": "root"})
        for i in range(1, n + 1):
            child = MP_TestNode.objects.add_child(root, {"desc": f"c{i}"})
            MP_TestNode.objects.add_child(child, {"desc": f"c{i}-x"})
        return root

    def test_resume_after_phase_a_crash(self, db, monkeypatch):
        self._build_bigger_tree(5)
        before = {n.pk: n.desc for n in MP_TestNode.objects.all()}

        real_phase_a = None
        from treebeard import recode as recode_module

        calls = {"n": 0}
        original = recode_module._set_map_stage

        def flaky_set_stage(cursor, map_table, pks, stage):
            original(cursor, map_table, pks, stage)
            if stage == "a":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("simulated crash during phase A")

        monkeypatch.setattr(recode_module, "_set_map_stage", flaky_set_stage)
        with pytest.raises(RuntimeError):
            recode_mp_paths(
                MP_TestNode,
                old_alphabet=OLD_ALPHABET,
                old_steplen=3,
                new_alphabet=TINY_ALPHABET,
                new_steplen=3,
                batch_size=3,
            )
        monkeypatch.undo()

        status = recode_status(MP_TestNode)
        assert status is not None
        # deterministic resume
        result = resume_recode(MP_TestNode, batch_size=3)
        assert result.resumed is True
        assert result.node_count == 11
        after = MP_TestNode.objects.in_bulk(before.keys())
        assert {pk: node.desc for pk, node in after.items()} == before
        assert MP_TestNode.objects.find_problems() == ([], [], [], [], [])

    def test_resume_after_phase_b_crash(self, db, monkeypatch):
        self._build_bigger_tree(5)
        from treebeard import recode as recode_module

        calls = {"n": 0}
        original = recode_module._set_map_stage

        def flaky_set_stage(cursor, map_table, pks, stage):
            original(cursor, map_table, pks, stage)
            if stage == "b":
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("simulated crash during phase B")

        monkeypatch.setattr(recode_module, "_set_map_stage", flaky_set_stage)
        with pytest.raises(RuntimeError):
            recode_mp_paths(
                MP_TestNode,
                old_alphabet=OLD_ALPHABET,
                old_steplen=3,
                new_alphabet=TINY_ALPHABET,
                new_steplen=3,
                batch_size=4,
            )
        monkeypatch.undo()

        result = resume_recode(MP_TestNode, batch_size=4)
        assert result.resumed is True
        assert MP_TestNode.objects.find_problems() == ([], [], [], [], [])

    def test_starting_new_run_while_interrupted_is_guarded(self, db, monkeypatch):
        self._build_bigger_tree(2)
        from treebeard import recode as recode_module

        original = recode_module._set_map_stage

        def fail_once(cursor, map_table, pks, stage):
            original(cursor, map_table, pks, stage)
            if stage == "a":
                raise RuntimeError("boom")

        monkeypatch.setattr(recode_module, "_set_map_stage", fail_once)
        with pytest.raises(RuntimeError):
            recode_mp_paths(
                MP_TestNode,
                old_alphabet=OLD_ALPHABET,
                old_steplen=3,
                new_alphabet=TINY_ALPHABET,
                new_steplen=3,
                batch_size=2,
            )
        monkeypatch.undo()

        with pytest.raises(RecodeStateError):
            recode_mp_paths(
                MP_TestNode,
                old_alphabet=OLD_ALPHABET,
                old_steplen=3,
                new_alphabet=TINY_ALPHABET,
                new_steplen=3,
            )

        # rollback is the deterministic way out
        assert rollback_recode(MP_TestNode) == 5
        assert recode_status(MP_TestNode) is None

    def test_resume_completed_is_idempotent(self, tree):
        recode_mp_paths(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet=TINY_ALPHABET,
            new_steplen=3,
        )
        # resuming a completed migration rewrites nothing and verifies again
        result = resume_recode(MP_TestNode)
        assert result.resumed is True
        assert result.phase_a_written == 0
        assert result.phase_b_written == 0

    def test_fresh_run_against_completed_migration_is_rejected(self, tree):
        recode_mp_paths(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet=TINY_ALPHABET,
            new_steplen=3,
        )
        with pytest.raises(RecodeStateError):
            recode_mp_paths(
                MP_TestNode,
                old_alphabet=OLD_ALPHABET,
                old_steplen=3,
                new_alphabet=TINY_ALPHABET,
                new_steplen=3,
            )
        # rollback/forget recover the operator deterministically
        rollback_recode(MP_TestNode)
        # re-running the original migration is allowed again
        result = recode_mp_paths(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet=TINY_ALPHABET,
            new_steplen=3,
        )
        assert result.node_count == 6

    def test_forget_then_status_clears(self, tree):
        recode_mp_paths(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet=TINY_ALPHABET,
            new_steplen=3,
        )
        forget_recode(MP_TestNode)
        assert recode_status(MP_TestNode) is None
        # the recoded data itself remains untouched
        assert all(set(n.path) <= set(TINY_ALPHABET) for n in MP_TestNode.objects.all())


class TestDryRun:
    def test_dry_run_writes_nothing(self, tree):
        plan = recode_mp_paths(
            MP_TestNode,
            old_alphabet=OLD_ALPHABET,
            old_steplen=3,
            new_alphabet=TINY_ALPHABET,
            new_steplen=3,
            dry_run=True,
        )
        assert plan.ok
        assert plan.node_count == 6
        assert recode_status(MP_TestNode) is None
        # paths still old-encoded
        assert all(set(n.path) <= set("0123456789") for n in MP_TestNode.objects.all())
        # the model still works unchanged
        assert MP_TestNode.objects.find_problems() == ([], [], [], [], [])


class TestManagementCommand:
    def test_check_command(self, tree, capsys):
        from django.core.management import call_command

        call_command(
            "recode_mp_tree",
            "tests.MP_TestNode",
            "--old-alphabet",
            OLD_ALPHABET,
            "--old-steplen",
            "3",
            "--new-alphabet",
            TINY_ALPHABET,
            "--new-steplen",
            "3",
            "--check",
        )
        out = capsys.readouterr().out
        assert "OK" in out
        assert recode_status(MP_TestNode) is None

    def test_run_and_rollback_command(self, tree, capsys):
        from django.core.management import call_command

        call_command(
            "recode_mp_tree",
            "tests.MP_TestNode",
            "--old-alphabet",
            OLD_ALPHABET,
            "--old-steplen",
            "3",
            "--new-alphabet",
            TINY_ALPHABET,
            "--new-steplen",
            "3",
            "--batch-size",
            "2",
        )
        assert recode_status(MP_TestNode)["phase"] == "done"

        call_command("recode_mp_tree", "tests.MP_TestNode", "--rollback")
        assert recode_status(MP_TestNode) is None

    def test_failed_check_exit_code(self, db, capsys):
        from django.core.management import call_command

        MP_TestNode.objects.create(path="zzz", depth=1, numchild=0)
        with pytest.raises(SystemExit) as exc:
            call_command(
                "recode_mp_tree",
                "tests.MP_TestNode",
                "--old-alphabet",
                OLD_ALPHABET,
                "--old-steplen",
                "3",
                "--new-alphabet",
                TINY_ALPHABET,
                "--new-steplen",
                "3",
                "--check",
            )
        assert exc.value.code == 1
