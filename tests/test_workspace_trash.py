"""Recoverable deletion: real files, journal-first moves, restore decisions taken from the filesystem."""
import json
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import task_workspace as tw, workspace_trash as trash
from ai_desktop import fs_publish
from ai_desktop.workspace_executor import ExecutionDenied


class TrashTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.geteuid(), 0)
        home = pwd.getpwuid(os.getuid()).pw_dir
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ai-trash-", dir=home)))
        self.ws = str(self.root)
        (self.root / "src").mkdir()
        self.a = self.root / "src/a.py"
        self.b = self.root / "b.txt"
        self.a.write_text("print('a')\n")
        self.b.write_text("bee\n")
        self.a.chmod(0o640)

    def journal(self, plan):
        return json.loads((self.root / ".ai-desktop-tasks" / (plan + ".trash.json")).read_text())

    def test_files_move_to_the_private_trash_and_come_back_with_identity_and_mode(self):
        inode = self.a.stat().st_ino
        plan = trash.trash(self.ws, ["src/a.py", "b.txt"])
        self.assertFalse(self.a.exists() or self.b.exists())
        held = self.root / ".ai-desktop-tasks" / ("trash-" + plan)
        self.assertEqual(sorted(p.name for p in held.iterdir()), ["0", "1"])
        self.assertEqual(self.journal(plan)["status"], "trashed")
        self.assertTrue(trash.restore(self.ws, plan))
        self.assertEqual(self.a.read_text(), "print('a')\n")
        self.assertEqual((self.a.stat().st_ino, self.a.stat().st_mode & 0o777), (inode, 0o640))
        self.assertEqual(self.b.read_text(), "bee\n")
        self.assertEqual(self.journal(plan)["status"], "restored")
        self.assertTrue(trash.restore(self.ws, plan))                     # idempotent

    def test_a_full_trash_refuses_before_anything_moves_and_the_list_keeps_working(self):
        original = trash.tw.MAX_JOURNALS
        with patch.object(trash.tw, "MAX_JOURNALS", 2):
            for name in ("a.txt", "b.txt"):
                (self.root / name).write_text("x\n")
                trash.trash(str(self.root), [name])
            (self.root / "c.txt").write_text("keep\n")
            with self.assertRaises(trash.TrashFull):
                trash.trash(str(self.root), ["c.txt"])
            self.assertEqual((self.root / "c.txt").read_text(), "keep\n")
            self.assertEqual(len(trash.recovery_records(str(self.root))), 2)
        self.assertEqual(trash.tw.MAX_JOURNALS, original)

    def test_a_recreated_name_is_a_conflict_and_is_never_overwritten(self):
        plan = trash.trash(self.ws, ["b.txt"])
        self.b.write_text("new life\n")
        self.assertFalse(trash.restore(self.ws, plan))
        self.assertEqual(self.b.read_text(), "new life\n")
        self.assertEqual(self.journal(plan)["status"], "conflict")
        self.assertTrue((self.root / ".ai-desktop-tasks" / ("trash-" + plan) / "0").exists())    # the copy is kept
        self.b.unlink()
        self.assertTrue(trash.restore(self.ws, plan))                      # resolvable after the conflict is removed
        self.assertEqual(self.b.read_text(), "bee\n")

    def test_concurrent_creation_during_restore_cannot_be_overwritten(self):
        """A second writer can create a name after our lstat and before rename."""
        plan = trash.trash(self.ws, ["b.txt"])
        held = self.root / ".ai-desktop-tasks" / ("trash-" + plan) / "0"
        original = trash._fingerprint
        inserted = []

        def race(parent, leaf):
            evidence = original(parent, leaf)
            if leaf == "0" and not inserted:     # verified stash, AFTER destination absence check
                self.b.write_text("concurrent new file\n")
                inserted.append(True)
            return evidence

        with patch.object(trash, "_fingerprint", race):
            self.assertFalse(trash.restore(self.ws, plan))
        self.assertEqual(inserted, [True])
        self.assertEqual(self.b.read_text(), "concurrent new file\n")
        self.assertTrue(held.exists())            # original stays in the trash, not discarded
        self.assertEqual(self.journal(plan)["status"], "conflict")
        self.b.unlink()
        self.assertTrue(trash.restore(self.ws, plan))
        self.assertEqual(self.b.read_text(), "bee\n")

    def test_restore_fails_closed_when_renameat2_is_unavailable(self):
        plan = trash.trash(self.ws, ["b.txt"])
        held = self.root / ".ai-desktop-tasks" / ("trash-" + plan) / "0"
        with patch.object(fs_publish, "_RENAMEAT2", None):
            self.assertFalse(trash.restore(self.ws, plan))
        self.assertFalse(self.b.exists())
        self.assertTrue(held.exists())
        self.assertTrue(trash.restore(self.ws, plan))
        self.assertEqual(self.b.read_text(), "bee\n")

    def test_a_stored_file_that_no_longer_matches_the_record_is_not_restored(self):
        plan = trash.trash(self.ws, ["b.txt"])
        held = self.root / ".ai-desktop-tasks" / ("trash-" + plan) / "0"
        held.write_text("tampered in the trash\n")
        self.assertFalse(trash.restore(self.ws, plan))
        self.assertFalse(self.b.exists())                              # the substituted bytes never become your file
        held.unlink()
        self.assertFalse(trash.restore(self.ws, plan))                 # the stored file is gone: a conflict, never a guess
        self.assertFalse(self.b.exists())
        (held.parent / "0").write_text("other bytes entirely\n")      # a different file of another size
        self.assertFalse(trash.restore(self.ws, plan))
        self.assertFalse(self.b.exists())

    def test_a_trash_entry_never_blocks_task_edits_in_the_same_workspace(self):
        """Regression: the trash lives in the task state directory, whose journal scan used to reject unknown entries."""
        from ai_desktop.task_plan import TaskPlan
        plan = trash.trash(self.ws, ["b.txt"])
        self.assertEqual(tw.recovery_records(self.ws), [])
        self.assertEqual(tw._can_apply(self.ws), 0)
        before = tw.snapshot(self.ws, ["src/a.py", "new.txt"])
        source = {"instruction": "change", "files": [
            {"path": p, "content": f["file"]["content"] if f["file"] else None, "writable": True} for p, f in before["files"].items()]}
        proposed = {"summary": "change", "files": [{"path": p, "content": "new\n"} for p in before["files"]]}
        task = TaskPlan(before, source, proposed, "text")
        tw.apply(task, task.authorize("ZAPISZ " + task.sha256))
        self.assertEqual(tw.status(self.ws), [(task.sha256, "applied")])
        self.assertTrue(tw.rollback(self.ws, task.sha256))
        self.assertTrue(trash.restore(self.ws, plan))                       # and the other way round
        self.assertEqual(self.b.read_text(), "bee\n")
        trash.trash(self.ws, ["src/a.py"])
        self.assertEqual([r["status"] for r in tw.recovery_records(self.ws)], ["rolled-back"])

    def test_foreign_entries_in_the_state_directory_are_still_refused(self):
        trash.trash(self.ws, ["b.txt"])
        (self.root / ".ai-desktop-tasks" / "surprise.json").write_text("{}")
        with self.assertRaises(ValueError):
            tw.recovery_records(self.ws)

    def test_unusual_or_unsafe_targets_are_refused_before_anything_moves(self):
        os.symlink(self.b, self.root / "link.txt")
        os.link(self.b, self.root / "hard.txt")
        (self.root / "dir").mkdir()
        for names in (["link.txt"], ["hard.txt"], ["b.txt"], ["dir"], ["missing.txt"], ["../x"], ["a/b/c/d/e/f.txt"],
                      ["src/a.py", "src/a.py"], ["%d.txt" % i for i in range(9)]):
            with self.subTest(names=names), self.assertRaises(ExecutionDenied):
                trash.trash(self.ws, names)
        self.assertTrue(self.a.exists() and self.b.exists())
        self.assertFalse(any((self.root / ".ai-desktop-tasks").glob("trash-*")))

    def test_setuid_and_oversized_files_are_refused(self):
        self.a.chmod(0o4755)
        with self.assertRaises(ExecutionDenied):
            trash.trash(self.ws, ["src/a.py"])
        self.a.chmod(0o640)
        with patch.object(trash, "MAX_FILE_BYTES", 3), self.assertRaises(ExecutionDenied):
            trash.trash(self.ws, ["src/a.py"])
        self.assertTrue(self.a.exists())

    def test_a_file_that_changes_after_the_scan_aborts_and_puts_back_what_already_moved(self):
        calls = []
        real = trash._fingerprint

        def tampering(parent, leaf):
            result = real(parent, leaf)
            calls.append(leaf)
            if calls.count("b.txt") == 1 and leaf == "b.txt":
                self.b.write_text("changed after the scan\n")
            return result
        with patch.object(trash, "_fingerprint", tampering), self.assertRaises(ExecutionDenied):
            trash.trash(self.ws, ["src/a.py", "b.txt"])
        self.assertEqual(self.a.read_text(), "print('a')\n")
        self.assertEqual(self.b.read_text(), "changed after the scan\n")

    def test_a_crash_between_the_move_and_the_journal_update_is_recovered_from_the_filesystem(self):
        saves = []
        real = tw._save

        def dying(fd, name, value, *, first=False):
            saves.append(name)
            if len(saves) == 2:                                    # after the first rename, before it is journaled
                raise KeyboardInterrupt()
            return real(fd, name, value, first=first)
        with patch.object(tw, "_save", dying), patch.object(trash.secrets, "token_hex", return_value="c" * 32), \
                self.assertRaises(KeyboardInterrupt):
            trash.trash(self.ws, ["src/a.py", "b.txt"])
        self.assertFalse(self.a.exists())                          # moved, but the journal still says "prepared"
        self.assertEqual(self.journal("c" * 32)["entries"][0]["state"], "prepared")
        self.assertTrue(trash.restore(self.ws, "c" * 32))
        self.assertEqual((self.a.read_text(), self.b.read_text()), ("print('a')\n", "bee\n"))

    def test_a_real_sigkill_in_the_middle_of_the_moves_is_recovered(self):
        """The deleting process is killed by SIGKILL right after the first rename (no cleanup code runs at all)."""
        import signal
        plan = "d" * 32
        child = os.fork()
        if child == 0:                                             # the doomed deleter
            real = os.rename
            moved = []

            def kill_after_first(*args, **kwargs):
                real(*args, **kwargs)
                moved.append(1)
                if len(moved) == 1:
                    os.kill(os.getpid(), signal.SIGKILL)
            os.rename = kill_after_first
            trash.secrets.token_hex = lambda _n: plan
            trash.trash(self.ws, ["src/a.py", "b.txt"])
            os._exit(0)                                            # never reached
        _, status = os.waitpid(child, 0)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL)
        self.assertFalse(self.a.exists())                          # the first file was moved
        self.assertTrue(self.b.exists())                           # the second never was
        self.assertTrue(trash.restore(self.ws, plan))              # the lock died with the process; the files come back
        self.assertEqual((self.a.read_text(), self.b.read_text()), ("print('a')\n", "bee\n"))

    def test_a_journal_written_before_any_move_restores_to_a_noop(self):
        with patch.object(trash.os, "rename", side_effect=OSError("boom")), \
                patch.object(trash.secrets, "token_hex", return_value="d" * 32), self.assertRaises(ExecutionDenied):
            trash.trash(self.ws, ["b.txt"])
        self.assertEqual(self.b.read_text(), "bee\n")
        self.assertTrue(trash.restore(self.ws, "d" * 32))

    def test_tampered_journals_and_wrong_plans_are_refused(self):
        plan = trash.trash(self.ws, ["b.txt"])
        path = self.root / ".ai-desktop-tasks" / (plan + ".trash.json")
        good = path.read_text()
        for broken in (good.replace('"schema":1', '"schema":2'), good.replace('"b.txt"', '"../b.txt"'), "{}", good[:-3]):
            path.write_text(broken)
            path.chmod(0o600)
            with self.subTest(broken=broken[:30]), self.assertRaises(ExecutionDenied):
                trash.restore(self.ws, plan)
        path.write_text(good)
        path.chmod(0o600)
        for wrong in ("x", "e" * 32, None, "../" + "a" * 29):
            with self.subTest(plan=wrong), self.assertRaises(ExecutionDenied):
                trash.restore(self.ws, wrong)
        self.assertTrue(trash.restore(self.ws, plan))


if __name__ == "__main__":
    unittest.main()
