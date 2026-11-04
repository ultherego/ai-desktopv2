import unittest
from pathlib import Path
from ai_desktop.sandbox import build_command


class SandboxTests(unittest.TestCase):
    def test_no_host_mounts(self):
        args = build_command(Path("/home/test"), "net:[1]", "user:[2]")
        self.assertIn("--unshare-net", args)
        self.assertIn("--unshare-user", args)
        self.assertNotIn("--bind", args)
        self.assertNotIn("--share-net", args)
        self.assertIn("--ro-bind", args)
        self.assertIn("--clearenv", args)


if __name__ == "__main__":
    unittest.main()
