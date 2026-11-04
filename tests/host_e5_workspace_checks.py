"""CachyOS E5 host gate: synthetic, private temporary workspace only."""
from pathlib import Path
import os
import tempfile
import unittest

from ai_desktop.typed_plan import PlanSession
from ai_desktop.workspace_executor import ExecutionDenied, WorkspaceExecutor, inspect_state


class E5HostCycle(unittest.TestCase):
    def test_real_create_audit_rollback(self):
        with tempfile.TemporaryDirectory(prefix="aid-e5-host-") as tmp:
            root = Path(tmp)
            (root / "notes").mkdir()
            operations = [{"action": "create_text", "path": "notes/synthetic.txt",
                           "content": "SYNTHETIC\n"}]
            state = inspect_state(tmp, (("notes/synthetic.txt", "SYNTHETIC\n"),))
            plan = PlanSession()
            preview = plan.preview(tmp, operations, state_sha256=state)
            approval = plan.authorize(confirmed_sha256=preview.sha256,
                                       nonce=preview.nonce, state_sha256=state)
            executor = WorkspaceExecutor(tmp)
            try:
                self.assertEqual(executor.apply(plan, approval)["status"], "APPLIED")
                self.assertEqual((root / "notes/synthetic.txt").read_text(), "SYNTHETIC\n")
                self.assertEqual(executor.rollback()["status"], "ROLLED_BACK")
                self.assertFalse((root / "notes/synthetic.txt").exists())
            finally:
                executor.close()

    def test_real_permission_failure_preserves_rollback_retry(self):
        with tempfile.TemporaryDirectory(prefix="aid-e5-retry-host-") as tmp:
            root = Path(tmp)
            notes = root / "notes"
            notes.mkdir()
            operations = [{"action": "create_text", "path": "notes/synthetic.txt",
                           "content": "SYNTHETIC\n"}]
            state = inspect_state(tmp, (("notes/synthetic.txt", "SYNTHETIC\n"),))
            plan = PlanSession()
            preview = plan.preview(tmp, operations, state_sha256=state)
            approval = plan.authorize(confirmed_sha256=preview.sha256,
                                       nonce=preview.nonce, state_sha256=state)
            executor = WorkspaceExecutor(tmp)
            try:
                executor.apply(plan, approval)
                os.chmod(notes, 0o500)
                try:
                    with self.assertRaisesRegex(ExecutionDenied,
                                                "^EXECUTOR: ROLLBACK FAILED$"):
                        executor.rollback()
                    self.assertEqual((notes / "synthetic.txt").read_text(), "SYNTHETIC\n")
                finally:
                    os.chmod(notes, 0o700)
                self.assertEqual(executor.rollback()["status"], "ROLLED_BACK")
                self.assertFalse((notes / "synthetic.txt").exists())
                with self.assertRaises(ExecutionDenied):
                    executor.rollback()
            finally:
                executor.close()

    def test_actual_symlink_escape_refused(self):
        with tempfile.TemporaryDirectory(prefix="aid-e5-host-") as tmp:
            with tempfile.TemporaryDirectory(prefix="aid-e5-outside-") as outside:
                (Path(tmp) / "link").symlink_to(outside, target_is_directory=True)
                with self.assertRaises(ExecutionDenied):
                    inspect_state(tmp, (("link/x.txt", "SYNTHETIC"),))
                self.assertFalse((Path(outside) / "x.txt").exists())


if __name__ == "__main__":
    unittest.main()

