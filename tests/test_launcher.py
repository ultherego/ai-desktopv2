import tempfile
from pathlib import Path
import unittest

from ai_desktop.launcher import ALLOWED, build_command, main


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


if __name__ == "__main__":
    unittest.main()
