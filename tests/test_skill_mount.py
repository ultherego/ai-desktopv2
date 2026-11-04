import tempfile
from pathlib import Path
import unittest

from ai_desktop.skill_mount import PROBE, build_command, source_hash


class SkillMountTests(unittest.TestCase):
    def test_probe_syntax(self):
        compile(PROBE, "<probe>", "exec")

    def test_network_and_read_only_mount(self):
        args = build_command(Path("/tmp/test-skills"), Path("/home/test"), "net:[12]", "abc")
        self.assertIn("--unshare-net", args)
        self.assertIn("--clearenv", args)
        self.assertNotIn("--bind", args)
        mounts = [args[i + 1:i + 3] for i, x in enumerate(args) if x == "--ro-bind"]
        self.assertIn(["/tmp/test-skills", "/home/sandbox/.claude/skills"], mounts)
        self.assertEqual(args[-4:-2], ["-I", "-B"])

    def test_reject_symlink_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "cachyos-diagnostics").symlink_to("/tmp")
            with self.assertRaises(ValueError):
                source_hash(root)


if __name__ == "__main__":
    unittest.main()
