"""E5/E6 real temporary-workspace integration: no user files or AI are touched."""
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop.typed_plan import PlanSession, PlanAuthorization, PlanDenied
from ai_desktop.workspace_executor import (
    ExecutionDenied, WorkspaceExecutor, inspect_state
)


class E5E6WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aid-workspace-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "notes").mkdir()
        self.ops = [{"action": "create_text", "path": "notes/new.txt",
                     "content": "SYNTHETIC ONLY\n"}]

    def approved(self, ops=None):
        ops = self.ops if ops is None else ops
        state_ops = tuple((op["path"], op["content"]) for op in ops)
        state = inspect_state(str(self.root), state_ops)
        session = PlanSession()
        preview = session.preview(str(self.root), ops, state_sha256=state)
        approval = session.authorize(
            confirmed_sha256=preview.sha256,
            nonce=preview.nonce, state_sha256=state)
        return session, approval

    def test_create_audit_and_rollback_one_time(self):
        session, authorization = self.approved()
        executor = WorkspaceExecutor(str(self.root))
        try:
            result = executor.apply(session, authorization)
            self.assertEqual(result["status"], "APPLIED")
            path = self.root / "notes/new.txt"
            self.assertEqual(path.read_text(), "SYNTHETIC ONLY\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            audits = sorted(self.root.glob(".ai-desktop-audit-*.json"))
            self.assertEqual(len(audits), 1)
            self.assertEqual(json.loads(audits[0].read_text())["event"], "applied")
            with self.assertRaises(ExecutionDenied):
                executor.apply(session, authorization)
            result = executor.rollback()
            self.assertEqual(result["status"], "ROLLED_BACK")
            self.assertFalse(path.exists())
            self.assertEqual(len(list(self.root.glob(".ai-desktop-audit-*.json"))), 2)
            with self.assertRaises(ExecutionDenied):
                executor.rollback()
        finally:
            executor.close()

    def test_existing_destination_refused_and_unmodified(self):
        session, approval = self.approved()
        dest = self.root / "notes/new.txt"
        dest.write_text("BEFORE")
        executor = WorkspaceExecutor(str(self.root))
        try:
            with self.assertRaises(ExecutionDenied):
                executor.apply(session, approval)
        finally:
            executor.close()
        self.assertEqual(dest.read_text(), "BEFORE")

    def test_symlink_parent_and_target_refused(self):
        with tempfile.TemporaryDirectory(prefix="aid-outside-") as outside:
            (self.root / "notes/new.txt").symlink_to(Path(outside) / "outside.txt")
            session, approval = self.approved_fails_on_symlink()
            (self.root / "notes/new.txt").unlink()
            (self.root / "notes").rmdir()
            (self.root / "notes").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ExecutionDenied):
                inspect_state(str(self.root), (("notes/new.txt", "x"),))
            self.assertFalse((Path(outside) / "new.txt").exists())

    def approved_fails_on_symlink(self):
        with self.assertRaises(ExecutionDenied):
            self.approved()
        return None, None

    def test_replaced_parent_after_preview_does_not_escape_scope(self):
        with tempfile.TemporaryDirectory(prefix="aid-outside-") as outside:
            session, approval = self.approved()
            (self.root / "notes").rmdir()
            (self.root / "notes").symlink_to(outside, target_is_directory=True)
            executor = WorkspaceExecutor(str(self.root))
            try:
                with self.assertRaises(ExecutionDenied):
                    executor.apply(session, approval)
            finally:
                executor.close()
            self.assertFalse((Path(outside) / "new.txt").exists())

    def test_forged_authorization_and_replay_refused(self):
        session, approval = self.approved()
        forged = PlanAuthorization(
            workspace=approval.workspace, plan_sha256=approval.plan_sha256,
            state_sha256=approval.state_sha256, operations=approval.operations)
        with self.assertRaises(ExecutionDenied):
            WorkspaceExecutor(str(self.root)).apply(session, forged)
        self.assertIs(session.consume_for_executor(approval), approval)
        with self.assertRaises(PlanDenied):
            session.consume_for_executor(approval)

    def test_modified_new_file_is_not_removed_by_rollback(self):
        session, authorization = self.approved()
        executor = WorkspaceExecutor(str(self.root))
        try:
            executor.apply(session, authorization)
            target = self.root / "notes/new.txt"
            target.write_text("USER MODIFIED")
            with self.assertRaises(ExecutionDenied):
                executor.rollback()
            self.assertEqual(target.read_text(), "USER MODIFIED")
        finally:
            executor.close()

    def test_partial_failure_rolls_back_first_created_file(self):
        ops = self.ops + [
            {"action": "create_text", "path": "notes/second.txt",
             "content": "ANOTHER\\n"},
        ]
        session, approval = self.approved(ops)
        original = WorkspaceExecutor._write_exclusive
        def fails_second(fd, name, content):
            if name == "second.txt":
                raise OSError("SYNTHETIC WRITE FAILURE")
            return original(fd, name, content)
        executor = WorkspaceExecutor(str(self.root))
        try:
            with patch.object(WorkspaceExecutor, "_write_exclusive",
                              side_effect=fails_second):
                with self.assertRaises(ExecutionDenied):
                    executor.apply(session, approval)
            self.assertFalse((self.root / "notes/new.txt").exists())
        finally:
            executor.close()

    def test_partial_rollback_retains_only_failed_entries_for_retry(self):
        ops = self.ops + [
            {"action": "create_text", "path": "notes/second.txt", "content": "SECOND"},
        ]
        session, approval = self.approved(ops)
        executor = WorkspaceExecutor(str(self.root))
        self.addCleanup(executor.close)
        executor.apply(session, approval)
        held = [entry[0] for entry in executor._created]
        unlink = os.unlink

        def fail_one(name, **kwargs):
            if name == "new.txt":
                raise PermissionError("SYNTHETIC SECRET MUST NOT LEAK")
            return unlink(name, **kwargs)

        with patch("ai_desktop.workspace_executor.os.unlink", side_effect=fail_one):
            with self.assertRaisesRegex(ExecutionDenied, "^EXECUTOR: ROLLBACK FAILED$"):
                executor.rollback()
        self.assertTrue((self.root / "notes/new.txt").exists())
        self.assertFalse((self.root / "notes/second.txt").exists())
        self.assertEqual(len(executor._created), 1)
        os.fstat(held[0])
        with self.assertRaises(OSError):
            os.fstat(held[1])
        # An arrival at an already retired target must never be deleted on retry.
        (self.root / "notes/second.txt").write_text("USER ARRIVAL")
        self.assertEqual(executor.rollback()["status"], "ROLLED_BACK")
        self.assertFalse((self.root / "notes/new.txt").exists())
        self.assertEqual((self.root / "notes/second.txt").read_text(), "USER ARRIVAL")
        for fd in held:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_failed_apply_retains_incomplete_automatic_rollback(self):
        ops = self.ops + [
            {"action": "create_text", "path": "notes/second.txt", "content": "SECOND"},
        ]
        session, approval = self.approved(ops)
        executor = WorkspaceExecutor(str(self.root))
        self.addCleanup(executor.close)
        write = WorkspaceExecutor._write_exclusive

        def fail_second(fd, name, content):
            if name == "second.txt":
                raise OSError("SYNTHETIC WRITE FAILURE")
            return write(fd, name, content)

        with patch.object(WorkspaceExecutor, "_write_exclusive", side_effect=fail_second):
            with patch("ai_desktop.workspace_executor.os.unlink", side_effect=PermissionError):
                with self.assertRaisesRegex(ExecutionDenied, "^EXECUTOR: ROLLBACK FAILED$"):
                    executor.apply(session, approval)
        self.assertTrue((self.root / "notes/new.txt").exists())
        self.assertEqual(len(executor._created), 1)
        with self.assertRaises(ExecutionDenied):
            executor.apply(session, approval)
        self.assertEqual(executor.rollback()["status"], "ROLLED_BACK")
        self.assertFalse((self.root / "notes/new.txt").exists())

    def test_audit_failure_can_be_retried_without_deleting_later_arrival(self):
        session, approval = self.approved()
        executor = WorkspaceExecutor(str(self.root))
        self.addCleanup(executor.close)
        executor.apply(session, approval)
        with patch.object(executor, "_audit_entry", side_effect=OSError("SECRET")):
            with self.assertRaisesRegex(ExecutionDenied, "^EXECUTOR: REFUSED$"):
                executor.rollback()
        self.assertFalse((self.root / "notes/new.txt").exists())
        (self.root / "notes/new.txt").write_text("USER ARRIVAL")
        self.assertEqual(executor.rollback()["status"], "ROLLED_BACK")
        self.assertEqual((self.root / "notes/new.txt").read_text(), "USER ARRIVAL")
        events = [json.loads(p.read_text())["event"] for p in
                  self.root.glob(".ai-desktop-audit-*.json")]
        self.assertEqual(events.count("rolled_back"), 1)

    def test_modified_file_survives_repeated_rollback_and_close_releases_fds(self):
        session, approval = self.approved()
        executor = WorkspaceExecutor(str(self.root))
        self.addCleanup(executor.close)
        executor.apply(session, approval)
        held = [executor._root_fd, executor._created[0][0]]
        target = self.root / "notes/new.txt"
        target.write_text("USER MODIFIED")
        for _ in range(2):
            with self.assertRaisesRegex(ExecutionDenied, "^EXECUTOR: ROLLBACK FAILED$"):
                executor.rollback()
            self.assertEqual(target.read_text(), "USER MODIFIED")
            self.assertEqual(len(executor._created), 1)
        executor.close()
        executor.close()
        for fd in held:
            with self.assertRaises(OSError):
                os.fstat(fd)
        with self.assertRaises(ExecutionDenied):
            executor.rollback()

    def test_missing_target_is_retired_and_not_deleted_if_it_reappears(self):
        session, approval = self.approved()
        executor = WorkspaceExecutor(str(self.root))
        self.addCleanup(executor.close)
        executor.apply(session, approval)
        target = self.root / "notes/new.txt"
        target.unlink()
        with patch.object(executor, "_audit_entry", side_effect=OSError):
            with self.assertRaises(ExecutionDenied):
                executor.rollback()
        target.write_text("NEW FILE")
        executor.rollback()
        self.assertEqual(target.read_text(), "NEW FILE")

    def test_fifo_substitution_does_not_block_rollback(self):
        session, approval = self.approved()
        executor = WorkspaceExecutor(str(self.root))
        self.addCleanup(executor.close)
        executor.apply(session, approval)
        target = self.root / "notes/new.txt"
        target.unlink()
        os.mkfifo(target)
        with self.assertRaisesRegex(ExecutionDenied, "^EXECUTOR: ROLLBACK FAILED$"):
            executor.rollback()
        self.assertTrue(target.exists())

    def test_partial_write_and_failed_cleanup_remain_recoverable(self):
        session, approval = self.approved()
        executor = WorkspaceExecutor(str(self.root))
        self.addCleanup(executor.close)
        write = os.write
        calls = []

        def fail_after_prefix(fd, data):
            calls.append(1)
            if len(calls) == 1:
                return write(fd, data[:3])
            raise OSError("SYNTHETIC WRITE FAILURE")

        with patch("ai_desktop.workspace_executor.os.write", side_effect=fail_after_prefix):
            with patch("ai_desktop.workspace_executor.os.unlink", side_effect=PermissionError):
                with self.assertRaisesRegex(ExecutionDenied, "^EXECUTOR: ROLLBACK FAILED$"):
                    executor.apply(session, approval)
        target = self.root / "notes/new.txt"
        self.assertEqual(target.read_bytes(), b"SYN")
        self.assertEqual(len(executor._created), 1)
        self.assertEqual(executor.rollback()["status"], "ROLLED_BACK")
        self.assertFalse(target.exists())

    def test_incomplete_rollback_audit_can_be_cleaned_and_retried(self):
        session, approval = self.approved()
        executor = WorkspaceExecutor(str(self.root))
        self.addCleanup(executor.close)
        executor.apply(session, approval)
        unlink = os.unlink

        def deny_audit_cleanup(name, **kwargs):
            if name.startswith(".ai-desktop-audit-"):
                raise PermissionError
            return unlink(name, **kwargs)

        with patch("ai_desktop.workspace_executor.os.fsync", side_effect=OSError):
            with patch("ai_desktop.workspace_executor.os.unlink", side_effect=deny_audit_cleanup):
                with self.assertRaisesRegex(ExecutionDenied, "^EXECUTOR: REFUSED$"):
                    executor.rollback()
        self.assertFalse((self.root / "notes/new.txt").exists())
        self.assertEqual(len(executor._created), 1)
        self.assertEqual(executor.rollback()["status"], "ROLLED_BACK")
        events = [json.loads(p.read_text())["event"] for p in
                  self.root.glob(".ai-desktop-audit-*.json")]
        self.assertEqual(sorted(events), ["applied", "rolled_back"])

    def test_partial_write_does_not_delete_concurrently_modified_content(self):
        session, approval = self.approved()
        executor = WorkspaceExecutor(str(self.root))
        self.addCleanup(executor.close)
        write = os.write
        calls = []
        target = self.root / "notes/new.txt"

        def write_then_modify(fd, data):
            calls.append(1)
            if len(calls) == 1:
                return write(fd, data[:3])
            target.write_text("USER MODIFIED")
            raise OSError("SYNTHETIC WRITE FAILURE")

        with patch("ai_desktop.workspace_executor.os.write", side_effect=write_then_modify):
            with self.assertRaisesRegex(ExecutionDenied, "^EXECUTOR: ROLLBACK FAILED$"):
                executor.apply(session, approval)
        self.assertEqual(target.read_text(), "USER MODIFIED")
        with self.assertRaises(ExecutionDenied):
            executor.rollback()
        self.assertEqual(target.read_text(), "USER MODIFIED")

    def test_initial_file_fstat_failure_closes_fd_and_preserves_unknown_file(self):
        session, approval = self.approved()
        executor = WorkspaceExecutor(str(self.root))
        self.addCleanup(executor.close)
        fstat = os.fstat
        failed_fds = []

        def fail_created_file(fd):
            info = fstat(fd)
            if not failed_fds and info.st_mode & 0o170000 == 0o100000:
                failed_fds.append(fd)
                raise OSError("SYNTHETIC FSTAT FAILURE")
            return info

        with patch("ai_desktop.workspace_executor.os.fstat", side_effect=fail_created_file):
            with self.assertRaisesRegex(ExecutionDenied, "^EXECUTOR: ROLLBACK FAILED$"):
                executor.apply(session, approval)
        self.assertEqual(len(failed_fds), 1)
        with self.assertRaises(OSError):
            os.fstat(failed_fds[0])
        self.assertTrue((self.root / "notes/new.txt").exists())
        # Unknown identity is never reconstructed from a potentially replaced path.
        with self.assertRaises(ExecutionDenied):
            executor.rollback()
        self.assertEqual(len(executor._created), 1)

    def test_audit_descriptor_exhaustion_happens_before_write_and_allows_retry(self):
        session, approval = self.approved()
        executor = WorkspaceExecutor(str(self.root))
        self.addCleanup(executor.close)
        executor.apply(session, approval)
        with patch("ai_desktop.workspace_executor.os.dup", side_effect=OSError):
            with self.assertRaisesRegex(ExecutionDenied, "^EXECUTOR: REFUSED$"):
                executor.rollback()
        self.assertFalse((self.root / "notes/new.txt").exists())
        self.assertEqual(len(list(self.root.glob(".ai-desktop-audit-*.json"))), 1)
        self.assertEqual(executor.rollback()["status"], "ROLLED_BACK")

    def test_plan_wrong_workspace_or_root_refused(self):
        session, approval = self.approved()
        with self.assertRaises(ExecutionDenied):
            WorkspaceExecutor("/tmp").apply(session, approval)
        with patch("ai_desktop.workspace_executor.os.geteuid", return_value=0):
            with self.assertRaises(ExecutionDenied):
                inspect_state(str(self.root), approval.operations)

    def test_successful_multi_step_creation_and_reversal(self):
        ops = self.ops + [
            {"action": "create_text", "path": "notes/other.txt",
             "content": "ANOTHER\n"},
        ]
        session, approval = self.approved(ops)
        executor = WorkspaceExecutor(str(self.root))
        try:
            self.assertEqual(executor.apply(session, approval)["count"], 2)
            self.assertTrue((self.root / "notes/other.txt").exists())
            executor.rollback()
            self.assertFalse((self.root / "notes/new.txt").exists())
            self.assertFalse((self.root / "notes/other.txt").exists())
        finally:
            executor.close()

    def test_failure_on_second_target_rolls_back_first(self):
        ops = self.ops + [
            {"action": "create_text", "path": "notes/second.txt",
             "content": "ANOTHER\n"},
        ]
        session, approval = self.approved(ops)
        # Simulate a race: target appears after review, but before apply.
        (self.root / "notes/second.txt").write_text("EXTERNAL")
        executor = WorkspaceExecutor(str(self.root))
        try:
            with self.assertRaises(ExecutionDenied):
                executor.apply(session, approval)
            self.assertFalse((self.root / "notes/new.txt").exists())
            self.assertEqual((self.root / "notes/second.txt").read_text(), "EXTERNAL")
        finally:
            executor.close()


if __name__ == "__main__":
    unittest.main()

