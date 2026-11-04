"""Native directory operations on disposable files: race, stale consent and actionable trash recovery."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import fs_publish, task_workspace as tw, workspace_trash as trash
from ai_desktop.task_plan import TaskPlan
from ai_desktop.workspace_executor import ExecutionDenied


class AtomicPublishTests(unittest.TestCase):
    def test_a_concurrent_destination_is_never_replaced_and_the_source_is_kept(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            (root / "stage").write_text("model\n")
            (root / "target").write_text("user\n")
            fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(FileExistsError):
                    fs_publish.rename_noreplace(fd, "stage", fd, "target")
                self.assertEqual((root / "target").read_text(), "user\n")
                self.assertEqual((root / "stage").read_text(), "model\n")
                (root / "target").unlink()
                fs_publish.rename_noreplace(fd, "stage", fd, "target")
                self.assertEqual((root / "target").read_text(), "model\n")
            finally:
                os.close(fd)

    def test_exchange_swaps_two_entries_atomically_and_fails_closed_without_the_syscall(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            (root / "stage").write_text("model\n")
            (root / "target").write_text("user\n")
            fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fs_publish.rename_exchange(fd, "stage", fd, "target")
                self.assertEqual((root / "target").read_text(), "model\n")
                self.assertEqual((root / "stage").read_text(), "user\n")
                with self.assertRaises(FileNotFoundError):                     # both names must exist
                    fs_publish.rename_exchange(fd, "missing", fd, "target")
                with patch.object(fs_publish, "_RENAMEAT2", None), patch.object(os, "replace") as replace:
                    with self.assertRaises(OSError):
                        fs_publish.rename_exchange(fd, "stage", fd, "target")
                    replace.assert_not_called()
            finally:
                os.close(fd)

    def test_missing_syscall_refuses_without_calling_replacing_rename(self):
        with patch.object(fs_publish, "_RENAMEAT2", None), patch.object(os, "replace") as replace:
            with self.assertRaises(OSError):
                fs_publish.rename_noreplace(-1, "a", -1, "b")
            replace.assert_not_called()


class VersionedFilesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ai-review-")))
        self.ws = str(self.root)

    def test_create_collision_after_the_final_state_check_preserves_the_other_writer(self):
        before = tw.snapshot(self.ws, ["new.txt"], must_be_absent=["new.txt"])
        source = {"instruction": "create", "files": [{"path": "new.txt", "content": None, "writable": True}]}
        plan = TaskPlan(before, source, {"summary": "create", "files": [{"path": "new.txt", "content": "model\n"}]}, "text")
        real = tw.rename_noreplace

        def race(src_dir, src, dst_dir, dst):
            (self.root / dst).write_text("concurrent user file\n")
            return real(src_dir, src, dst_dir, dst)

        with patch.object(tw, "rename_noreplace", race), self.assertRaises(FileExistsError):
            tw.apply(plan, plan.authorize("ZAPISZ " + plan.sha256))
        self.assertEqual((self.root / "new.txt").read_text(), "concurrent user file\n")
        self.assertFalse(tw.rollback(self.ws, plan.sha256))  # conflict: never remove the other writer
        self.assertEqual((self.root / "new.txt").read_text(), "concurrent user file\n")

    def test_a_deletion_changed_while_the_question_is_shown_is_refused_before_any_move(self):
        target = self.root / "one.txt"
        target.write_text("old\n")
        prepared = trash.prepare(self.ws, ["one.txt"])
        target.write_text("new\n")
        with self.assertRaises(ExecutionDenied):
            trash.execute(prepared, prepared.authorize())
        self.assertEqual(target.read_text(), "new\n")
        self.assertFalse((self.root / tw.STATE).exists())

    def test_deletion_approval_is_immutable_expiring_and_one_use(self):
        (self.root / "one.txt").write_text("one\n")
        now = [0]
        prepared = trash.prepare(self.ws, ["one.txt"], clock=lambda: now[0])
        detached = prepared.data()
        detached["entries"][0]["path"] = "another.txt"
        approval = prepared.authorize()
        now[0] = 181
        with self.assertRaises(ExecutionDenied):
            trash.execute(prepared, approval)
        prepared = trash.prepare(self.ws, ["one.txt"])
        approval = prepared.authorize()
        plan = trash.execute(prepared, approval)
        with self.assertRaises(ExecutionDenied):
            trash.execute(prepared, approval)
        self.assertTrue(trash.restore(self.ws, plan))
        self.assertEqual((self.root / "one.txt").read_text(), "one\n")

    def test_partial_failure_preserves_both_versions_and_exposes_the_journal(self):
        a, b = self.root / "a.txt", self.root / "b.txt"
        a.write_text("original a\n")
        b.write_text("original b\n")
        prepared = trash.prepare(self.ws, ["a.txt", "b.txt"])
        real_save, calls = tw._save, []

        def save(*args, **kwargs):
            result = real_save(*args, **kwargs)
            calls.append(1)
            if len(calls) == 2:             # a is now in trash, b has not moved yet
                a.write_text("new a\n")
                b.write_text("new b\n")
            return result

        with patch.object(tw, "_save", save), self.assertRaises(trash.TrashRecoveryRequired) as caught:
            trash.execute(prepared, prepared.authorize())
        plan = caught.exception.plan
        record, = trash.recovery_records(self.ws)
        self.assertEqual((record["plan"], record["status"]), (plan, "conflict"))
        self.assertEqual(a.read_text(), "new a\n")
        self.assertEqual(b.read_text(), "new b\n")
        self.assertEqual((self.root / tw.STATE / ("trash-" + plan) / "0").read_text(), "original a\n")
        a.unlink()
        self.assertTrue(trash.restore(self.ws, plan))
        self.assertEqual((a.read_text(), b.read_text()), ("original a\n", "new b\n"))

    def test_a_replacement_in_the_rename_window_is_put_back_and_the_operation_is_refused(self):
        target = self.root / "one.txt"
        target.write_text("approved\n")
        prepared = trash.prepare(self.ws, ["one.txt"])
        real = os.rename

        def race(src, dst, **kwargs):
            target.write_text("concurrent change\n")
            return real(src, dst, **kwargs)

        with patch.object(trash.os, "rename", race), self.assertRaises(ExecutionDenied):
            trash.execute(prepared, prepared.authorize())
        self.assertEqual(target.read_text(), "concurrent change\n")
        record, = trash.recovery_records(self.ws)
        self.assertEqual(record["status"], "restored")
