"""E5 durable recovery: real killed child processes, temporary workspaces only."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from ai_desktop import workspace_journal as journal
from ai_desktop.typed_plan import PlanSession
from ai_desktop.workspace_executor import (
    ExecutionDenied, WorkspaceExecutor, inspect_state, recover
)

REPO = str(Path(__file__).resolve().parent.parent)
OPS = [{"action": "create_text", "path": "notes/one.txt", "content": "FIRST FILE\n"},
       {"action": "create_text", "path": "notes/two.txt", "content": "SECOND FILE\n"}]

CHILD = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, os.environ["AID_REPO"])
    from ai_desktop import workspace_journal as j
    from ai_desktop.typed_plan import PlanSession
    from ai_desktop.workspace_executor import WorkspaceExecutor, inspect_state
    root, crash_at, ops = sys.argv[1], int(sys.argv[2]), __import__("json").loads(sys.argv[3])
    real, calls = j.write, [0]
    def crashing(*args, **kwargs):
        calls[0] += 1
        if calls[0] == crash_at:
            os._exit(77)   # hard kill: no cleanup, no finally blocks
        return real(*args, **kwargs)
    j.write = crashing
    state = inspect_state(root, tuple((o["path"], o["content"]) for o in ops))
    session = PlanSession()
    preview = session.preview(root, ops, state_sha256=state)
    auth = session.authorize(confirmed_sha256=preview.sha256, nonce=preview.nonce,
                             state_sha256=state)
    WorkspaceExecutor(root).apply(session, auth)
""")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aid-recovery-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "notes").mkdir()

    def child(self, crash_at):
        env = dict(os.environ, AID_REPO=REPO, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run(
            [sys.executable, "-I", "-c", CHILD, str(self.root), str(crash_at),
             json.dumps(OPS)],
            env=env,
            cwd=self.temp.name, capture_output=True, timeout=60).returncode

    def journals(self):
        return sorted(self.root.glob(".ai-desktop-journal-*.json"))

    def apply_plan(self, ops=OPS):
        state = inspect_state(str(self.root), tuple((o["path"], o["content"]) for o in ops))
        session = PlanSession()
        preview = session.preview(str(self.root), ops, state_sha256=state)
        auth = session.authorize(confirmed_sha256=preview.sha256, nonce=preview.nonce,
                                 state_sha256=state)
        executor = WorkspaceExecutor(str(self.root))
        self.addCleanup(executor.close)
        return executor, executor.apply(session, auth)

    def test_crash_with_recorded_inodes_is_recovered_without_residue(self):
        # 4 = everything written, recorded and audited but never marked applied.
        self.assertEqual(self.child(4), 77)
        with self.assertRaisesRegex(ExecutionDenied, "^EXECUTOR: REFUSED$"):
            self.apply_plan()  # must recover first
        self.assertEqual(recover(str(self.root))["status"], "RECOVERED")
        self.assertFalse((self.root / "notes/one.txt").exists())
        self.assertFalse((self.root / "notes/two.txt").exists())

    def test_crash_before_inode_is_recorded_keeps_file_until_user_decides(self):
        # 2/3 = file created, inode not yet journaled: identity is unproven,
        # so recovery must not delete it, and must keep blocking new applies.
        for crash_at, survivor in ((2, "one"), (3, "two")):
            with self.subTest(crash_at=crash_at):
                self.assertEqual(self.child(crash_at), 77)
                target = self.root / f"notes/{survivor}.txt"
                self.assertTrue(target.exists())
                result = recover(str(self.root))
                self.assertEqual((result["status"], result["conflicts"]), ("INCOMPLETE", 1))
                self.assertTrue(target.exists())
                with self.assertRaises(ExecutionDenied):
                    self.apply_plan()
                for path in ("one", "two"):  # the user inspects and removes them
                    (self.root / f"notes/{path}.txt").unlink(missing_ok=True)
                self.assertEqual(recover(str(self.root))["status"], "RECOVERED")
                for path in self.journals() + list(self.root.glob(".ai-desktop-audit-*.json")):
                    path.unlink()

    def test_crash_before_any_file_leaves_nothing_to_remove(self):
        self.assertEqual(self.child(2), 77)
        (self.root / "notes/one.txt").unlink()
        self.assertEqual(recover(str(self.root))["recovered"], 1)
        self.assertEqual(recover(str(self.root))["recovered"], 0)  # idempotent

    def test_recovered_workspace_accepts_the_same_plan_again(self):
        self.assertEqual(self.child(4), 77)
        recover(str(self.root))
        executor, result = self.apply_plan()
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual((self.root / "notes/two.txt").read_text(), "SECOND FILE\n")

    def test_modified_file_is_kept_and_journal_stays_for_retry(self):
        self.assertEqual(self.child(4), 77)
        target = self.root / "notes/one.txt"
        target.write_text("USER EDIT")
        result = recover(str(self.root))
        self.assertEqual((result["status"], result["conflicts"]), ("INCOMPLETE", 1))
        self.assertEqual(target.read_text(), "USER EDIT")
        self.assertFalse((self.root / "notes/two.txt").exists())
        self.assertEqual(json.loads(self.journals()[0].read_text())["status"], "pending")
        with self.assertRaises(ExecutionDenied):
            self.apply_plan()
        target.unlink()
        self.assertEqual(recover(str(self.root))["status"], "RECOVERED")

    def test_replaced_file_with_different_inode_is_not_deleted(self):
        self.assertEqual(self.child(4), 77)
        target = self.root / "notes/one.txt"
        # Hold the old inode open so the filesystem cannot hand its number to
        # the replacement (inode reuse with identical bytes is a documented limit).
        with open(target, "rb"):
            target.unlink()
            target.write_text("FIRST FILE\n")  # identical bytes, different inode
            self.assertEqual(recover(str(self.root))["conflicts"], 1)
        self.assertTrue(target.exists())

    def test_user_file_created_after_interrupted_intent_is_never_removed(self):
        # Crash after the intent was journaled but before the executor created
        # anything; the user then makes a 0600 file with a prefix-compatible body.
        self.assertEqual(self.child(2), 77)
        for body in ("", "FIRST", "FIRST FILE\n"):
            with self.subTest(body=body):
                target = self.root / "notes/one.txt"
                target.unlink(missing_ok=True)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(fd, body.encode())
                os.close(fd)
                result = recover(str(self.root))
                self.assertEqual(result["status"], "INCOMPLETE")
                self.assertTrue(target.exists())
                self.assertEqual(target.read_text(), body)

    def test_symlinked_parent_is_refused_and_target_untouched(self):
        self.assertEqual(self.child(4), 77)
        outside = Path(tempfile.mkdtemp(prefix="aid-outside-", dir=self.temp.name))
        victim = outside / "one.txt"
        victim.write_text("FIRST FILE\n")
        os.rename(self.root / "notes", self.root / "notes-moved")
        os.symlink(outside, self.root / "notes")
        self.assertEqual(recover(str(self.root))["conflicts"], 1)
        self.assertTrue(victim.exists())

    def test_completed_apply_is_kept_unless_rollback_is_explicit(self):
        executor, _ = self.apply_plan()
        executor.close()
        names = self.journals()
        self.assertEqual(json.loads(names[0].read_text())["status"], "applied")
        self.assertEqual(oct(names[0].stat().st_mode & 0o777), "0o600")
        self.assertEqual(recover(str(self.root))["recovered"], 0)
        self.assertTrue((self.root / "notes/one.txt").exists())
        self.assertEqual(recover(str(self.root), rollback_plans=("f" * 64,))["recovered"], 0)
        plan = json.loads(names[0].read_text())["plan_sha256"]
        self.assertEqual(recover(str(self.root), rollback_plans=(plan,))["recovered"], 1)
        self.assertFalse((self.root / "notes/one.txt").exists())
        self.assertEqual(json.loads(names[0].read_text())["status"], "rolled_back")
        events = sorted(json.loads(p.read_text())["event"]
                        for p in self.root.glob(".ai-desktop-audit-*.json"))
        self.assertEqual(events, ["applied", "recovered"])

    def test_in_process_rollback_marks_journal_and_plan_can_repeat(self):
        executor, _ = self.apply_plan()
        executor.rollback()
        self.assertEqual(json.loads(self.journals()[0].read_text())["status"], "rolled_back")
        executor2, result = self.apply_plan()
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(len(self.journals()), 2)

    def test_failed_apply_marks_journal_rolled_back(self):
        real, calls = journal.write, []

        def fail_third(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise journal.JournalError("JOURNAL: REFUSED")
            return real(*args, **kwargs)

        with patch("ai_desktop.workspace_executor.journal.write", side_effect=fail_third):
            with self.assertRaises(ExecutionDenied):
                self.apply_plan()
        self.assertFalse((self.root / "notes/one.txt").exists())
        self.assertFalse((self.root / "notes/two.txt").exists())
        self.assertEqual(json.loads(self.journals()[0].read_text())["status"], "rolled_back")
        self.assertEqual(recover(str(self.root))["recovered"], 0)

    def test_failed_rollback_keeps_journal_usable_for_restart_recovery(self):
        executor, _ = self.apply_plan()
        with patch("ai_desktop.workspace_executor.os.unlink", side_effect=PermissionError):
            with self.assertRaisesRegex(ExecutionDenied, "ROLLBACK FAILED"):
                executor.rollback()
        executor.close()
        plan = json.loads(self.journals()[0].read_text())["plan_sha256"]
        self.assertEqual(recover(str(self.root), rollback_plans=(plan,))["status"], "RECOVERED")
        self.assertFalse((self.root / "notes/one.txt").exists())

    def test_forged_or_malformed_journals_delete_nothing(self):
        victim = self.root / "notes/keep.txt"
        victim.write_text("KEEP")
        good = {"schema": 1, "plan_sha256": "a" * 64, "status": "pending",
                "entries": [{"path": "notes/keep.txt", "content": "KEEP", "inode": None}]}
        forged = [
            dict(good, entries=[{"path": "../keep.txt", "content": "KEEP", "inode": None}]),
            dict(good, entries=[{"path": "/etc/passwd", "content": "x", "inode": None}]),
            dict(good, entries=[{"path": ".ssh/id", "content": "x", "inode": None}]),
            dict(good, status="evil"),
            dict(good, extra=1),
            dict(good, entries=[]),
        ]
        for index, doc in enumerate(forged):
            (self.root / (journal.PREFIX + f"{index:032x}.json")).write_text(json.dumps(doc))
        (self.root / (journal.PREFIX + f"{99:032x}.json")).write_text("{not json")
        link = self.root / (journal.PREFIX + f"{98:032x}.json")
        os.symlink(victim, link)
        result = recover(str(self.root))
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(result["invalid"], len(forged) + 2)
        self.assertEqual(victim.read_text(), "KEEP")

    def test_valid_foreign_journal_cannot_remove_unrecorded_file_with_other_content(self):
        victim = self.root / "notes/keep.txt"
        victim.write_text("USER DATA")
        doc = {"schema": 1, "plan_sha256": "b" * 64, "status": "pending",
               "entries": [{"path": "notes/keep.txt", "content": "OTHER", "inode": None}]}
        (self.root / (journal.PREFIX + f"{1:032x}.json")).write_text(json.dumps(doc))
        self.assertEqual(recover(str(self.root))["conflicts"], 1)
        self.assertEqual(victim.read_text(), "USER DATA")

    def test_results_never_contain_paths_or_contents(self):
        self.assertEqual(self.child(4), 77)
        (self.root / "notes/one.txt").write_text("SECRET USER TEXT")
        text = json.dumps(recover(str(self.root)))
        for needle in ("SECRET", "one.txt", self.temp.name):
            self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
