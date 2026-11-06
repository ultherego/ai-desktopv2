import tempfile
import io
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop.launcher import ALLOWED, build_command, main
from ai_desktop import launcher


class LauncherTests(unittest.TestCase):
    def test_only_safe_commands(self):
        self.assertEqual(set(ALLOWED), {"version", "help"})
        self.assertEqual(main(["run"]), 2)

    def test_fail_closed_for_wrong_directory(self):
        with self.assertRaises(ValueError):
            build_command(Path.home(), Path("/usr/bin/true"), "version")

    def test_offline_and_minimal_mounts(self):
        with tempfile.TemporaryDirectory(prefix="ai-desktop-launch-", dir="/tmp") as path:
            args = build_command(Path(path), Path("/tmp/fake-claude"), "version")
        self.assertIn("--unshare-net", args)
        self.assertIn("--clearenv", args)
        self.assertIn("--die-with-parent", args)
        self.assertIn("--unshare-user", args)
        self.assertIn("--ro-bind", args)
        writable = [args[i + 1:i + 3] for i, x in enumerate(args) if x == "--bind"]
        self.assertEqual(writable, [[path, "/work"]])
        self.assertEqual(args[-2:], ["/opt/claude", "--version"])

    def test_metadata_accessor_never_uses_the_supplied_home(self):
        with patch("ai_desktop.system_clients.inspect", return_value=SimpleNamespace(
                status="present", path="/opt/claude-code/bin/claude")) as inspect:
            self.assertEqual(launcher.claude_binary("/home/synthetic/unsafe"), Path("/opt/claude-code/bin/claude"))
        inspect.assert_called_once_with("claude")
        for status in ("missing", "unsafe", "unsupported", "changed", "unavailable"):
            with self.subTest(status=status), patch("ai_desktop.system_clients.inspect", return_value=SimpleNamespace(
                    status=status, path="/home/forbidden/claude")), self.assertRaises(ValueError):
                launcher.claude_binary("/home/synthetic/unsafe")

    def test_retained_commands_share_production_probe_without_legacy_execution(self):
        result = {"provider": "claude", "status": "compatible-untested", "version": "2.1.318",
                  "version_checked": True, "protocol_checked": True, "tested": False}
        for action in ("version", "help"):
            output = io.StringIO()
            with self.subTest(action=action), patch("ai_desktop.client_compat.inspect", return_value=result) as inspect, \
                    patch.object(launcher.subprocess, "run") as run, patch.object(launcher, "claude_binary") as legacy, \
                    redirect_stdout(output):
                self.assertEqual(main([action]), 0)
            inspect.assert_called_once_with("claude")
            run.assert_not_called()
            legacy.assert_not_called()
            self.assertIn("2.1.318: compatible-untested", output.getvalue())

        with patch("ai_desktop.client_compat.inspect", return_value={
                **result, "status": "client-protocol", "version": None, "protocol_checked": False}) as inspect, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["version"]), 2)
        inspect.assert_called_once_with("claude")


if __name__ == "__main__":
    unittest.main()
