import inspect
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_desktop import diagnose
from ai_desktop import launcher


class ClaudeSecurityTests(unittest.TestCase):
    def test_legacy_host_ask_is_blocked_before_profile_or_process_access(self):
        with (
            patch.object(diagnose, "serialize_report", return_value='{}'),
            patch.object(diagnose.subprocess, "run") as run,
            patch.object(Path, "read_bytes") as read,
        ):
            self.assertEqual(diagnose.ask({}), 2)
        read.assert_not_called()
        run.assert_not_called()

    def test_ask_rejects_unapproved_payload_before_launch(self):
        with patch.object(diagnose.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                diagnose.ask({"api_key": "top-secret"})
            run.assert_not_called()

    def test_launcher_is_offline_and_readonly(self):
        with tempfile.TemporaryDirectory(prefix="ai-desktop-launch-", dir="/tmp") as workspace:
            args = launcher.build_command(Path(workspace), Path("/opt/claude"), "version")
        self.assertIn("--unshare-net", args)
        self.assertIn("--clearenv", args)
        self.assertIn("--ro-bind", args)
        self.assertNotIn("--share-net", args)
        self.assertEqual(args[-2:], ["/opt/claude", "--version"])


if __name__ == "__main__":
    unittest.main()
