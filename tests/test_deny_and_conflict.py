"""A deny rule covers what it names however it is spelled; a settled rollback conflict does not stop other writes."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import permissions as p, task_workspace as w
from ai_desktop.task_plan import TaskPlan


class DenySpellingTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix="ai-deny-", dir=os.path.expanduser("~")))
        self.home = Path(self.temp)
        self.home.chmod(0o700)
        self.enterContext(patch.object(p, "_home", return_value=str(self.home)))
        self.config = self.home / ".config/ai-desktop/permissions.toml"
        self.config.parent.mkdir(parents=True, mode=0o700)
        (self.home / "Prywatne/sub").mkdir(parents=True)
        (self.home / "Prywatne/a.txt").write_text("a")
        (self.home / "Prywatne/sub/b.txt").write_text("b")
        (self.home / "Prywatne2.txt").write_text("c")

    def policy(self, deny):
        self.config.write_text('version=2\n[network]\nclaude=true\n[files]\nread=["~/"]\nwrite=["~/"]\ndeny=["%s"]\n' % deny)
        self.config.chmod(0o600)
        return p.load_policy()

    def test_every_spelling_protects_files_and_subdirectories(self):
        for deny in ("~/Prywatne", "~/Prywatne/", "~/Prywatne/*", "~/Prywatne/**"):
            with self.subTest(deny=deny):
                policy = self.policy(deny)
                for name in ("Prywatne/a.txt", "Prywatne/sub/b.txt"):
                    self.assertFalse(policy.allows(str(self.home / name)), name)
                    self.assertFalse(policy.can_write(str(self.home / name)), name)

    def test_a_similar_name_next_to_it_stays_allowed(self):
        policy = self.policy("~/Prywatne")
        self.assertTrue(policy.allows(str(self.home / "Prywatne2.txt")))
        self.assertTrue(policy.can_write(str(self.home / "Prywatne2.txt")))

    def test_an_exact_file_rule_still_blocks_that_file(self):
        policy = self.policy("~/Prywatne2.txt")
        self.assertFalse(policy.allows(str(self.home / "Prywatne2.txt")))
        self.assertTrue(policy.allows(str(self.home / "Prywatne/a.txt")))


class ConflictTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix="ai-conflict-"))
        self.root = str(Path(self.temp).resolve())
        os.chmod(self.root, 0o755)
        self.target = Path(self.root) / "f.txt"
        self.target.write_text("v0\n")
        self.target.chmod(0o644)

    def apply(self, name, new):
        absent = [] if (Path(self.root) / name).exists() else [name]
        before = w.snapshot(self.root, [name], must_be_absent=absent)
        current = before["files"][name]["file"]
        source = {"instruction": "x", "files": [{"path": name, "content": current["content"] if current else None,
                                                 "writable": True}]}
        plan = TaskPlan(before, source, {"summary": "s", "files": [{"path": name, "content": new}]}, "text")
        w.apply(plan, plan.authorize("SAVE " + plan.sha256))
        return plan.sha256

    def replace_with(self, text):
        temp = Path(self.root) / "t"
        temp.write_text(text)
        temp.chmod(0o644)
        os.replace(temp, self.target)

    def test_rollbacks_in_reverse_order_both_succeed(self):
        first = self.apply("f.txt", "v1\n")
        second = self.apply("f.txt", "v2\n")
        self.assertTrue(w.rollback(self.root, second))
        self.assertTrue(w.rollback(self.root, first))
        self.assertEqual(self.target.read_text(), "v0\n")

    def test_a_later_edit_is_kept_and_does_not_block_further_writes(self):
        plan = self.apply("f.txt", "v1\n")
        self.replace_with("user\n")
        self.assertFalse(w.rollback(self.root, plan))
        self.assertEqual(self.target.read_text(), "user\n")
        self.assertEqual(dict(w.status(self.root))[plan], "conflict")
        w._can_apply(self.root)
        self.apply("other.txt", "x\n")
        self.assertEqual((Path(self.root) / "other.txt").read_text(), "x\n")

    def test_a_conflict_can_be_retried_after_restoring_the_written_bytes(self):
        plan = self.apply("f.txt", "v1\n")
        self.replace_with("user\n")
        self.assertFalse(w.rollback(self.root, plan))
        self.replace_with("v1\n")
        self.assertTrue(w.rollback(self.root, plan))
        self.assertEqual(self.target.read_text(), "v0\n")

    def test_a_leftover_temporary_journal_blocks_nothing(self):
        self.apply("f.txt", "v1\n")
        (Path(self.root) / w.STATE / (".pending-" + "ab" * 12)).write_text("")
        w._can_apply(self.root)
        self.apply("other.txt", "x\n")

    def test_a_truncated_journal_still_blocks(self):
        self.apply("f.txt", "v1\n")
        (Path(self.root) / w.STATE / ("c" * 64 + ".json")).write_text('{"sch')
        with self.assertRaises(ValueError):
            w._can_apply(self.root)

    def test_a_leftover_stage_still_blocks(self):
        plan = self.apply("f.txt", "v1\n")
        self.replace_with("user\n")
        self.assertFalse(w.rollback(self.root, plan))
        journal = w._load
        record = next(r for r in w.recovery_records(self.root) if r["plan"] == plan)
        self.assertTrue(record["settled"])
        root = os.open(self.root, os.O_RDONLY)
        try:
            state = os.open(os.path.join(self.root, w.STATE), os.O_RDONLY)
            try:
                value = journal(state, plan + ".json")
            finally:
                os.close(state)
        finally:
            os.close(root)
        stage = Path(self.root) / value["entries"][0]["temp"]
        stage.write_text("leftover")
        with self.assertRaises(ValueError):
            w._can_apply(self.root)


if __name__ == "__main__":
    unittest.main()
