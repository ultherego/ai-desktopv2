"""LC4 acceptance helper against a synthetic HOME and a real policy file."""
import contextlib
import io
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import permissions as p
from scripts import lc4_check as check

POLICY = '["~/ai-lc4-check/**"]'
DENY = '["~/ai-lc4-check/secret/**"]'


class Lc4CheckTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.geteuid(), 0)
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ai-lc4-home-", dir=real_home)))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}))
        self.enterContext(patch.object(p, "_identity", return_value=(os.getuid(), str(self.home))))
        self.config = self.home / ".config/ai-desktop/permissions.toml"
        self.config.parent.mkdir(parents=True, mode=0o700)
        self.policy(POLICY, DENY)
        (self.home / "hello.py").write_text("mine\n")
        self.root = self.home / check.NAME

    def policy(self, read, deny="[]"):
        self.config.write_text("version = 1\n[files]\nread = %s\ndeny = %s\n" % (read, deny))
        self.config.chmod(0o600)

    def run_action(self, action):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check.main([action])
        return code, out.getvalue()

    def test_prepare_builds_the_project_privately_and_refuses_a_second_run(self):
        code, _ = self.run_action("prepare")
        self.assertEqual(code, 0)
        self.assertEqual(oct(self.root.stat().st_mode & 0o777), "0o700")
        self.assertGreater((self.root / "big/data.log").stat().st_size, 5 * 1024 * 1024)
        self.assertTrue((self.root / "outside-link").is_symlink())
        self.assertEqual((self.root / "src/hl.txt").stat().st_nlink, 2)
        self.assertEqual(self.run_action("prepare")[0], 2)
        self.assertEqual((self.home / "hello.py").read_text(), "mine\n")

    def test_scan_passes_under_the_real_policy(self):
        self.run_action("prepare")
        code, output = self.run_action("scan")
        self.assertEqual(code, 0, output)
        for line in ("POLICY=PASS", "count denied", "count unsafe", "multi-module markers found", "SCAN=PASS"):
            self.assertIn(line, output)
        self.assertNotIn("FAIL", output)

    def test_a_policy_without_the_directory_or_without_the_deny_rule_is_reported_not_edited(self):
        self.run_action("prepare")
        for read, deny, hint in (('["~/other/**"]', "[]", "Add to files.read"), (POLICY, "[]", "secret/ is not denied")):
            self.policy(read, deny)
            before = self.config.read_text()
            code, output = self.run_action("policy")
            self.assertEqual(code, 3)
            self.assertIn(hint, output)
            self.assertEqual(self.config.read_text(), before)

    def test_check_unchanged_detects_a_modified_added_or_removed_file_and_cleanup_removes_everything(self):
        self.run_action("prepare")
        self.assertEqual(self.run_action("check-unchanged")[0], 0)
        (self.root / "docs/readme.txt").write_text("changed\n")
        (self.root / "extra.txt").write_text("new\n")
        (self.root / "src/pkg00/mod00.py").unlink()
        code, output = self.run_action("check-unchanged")
        self.assertEqual(code, 1)
        for name in ("docs/readme.txt", "extra.txt", "src/pkg00/mod00.py"):
            self.assertIn("PROBLEM: " + name, output)
        self.assertEqual(self.run_action("cleanup")[0], 0)
        self.assertFalse(self.root.exists())
        self.assertTrue((self.home / "hello.py").exists())


if __name__ == "__main__":
    unittest.main()
