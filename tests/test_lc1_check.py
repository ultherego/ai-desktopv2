"""LC1 acceptance helper against a synthetic HOME and a real policy file."""
import contextlib
import io
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import permissions as p
from scripts import lc1_check as check


class Lc1CheckTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.geteuid(), 0)
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ai-lc1-home-", dir=real_home)))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}))
        self.enterContext(patch.object(p, "_identity", return_value=(os.getuid(), str(self.home))))
        self.config = self.home / ".config/ai-desktop/permissions.toml"
        self.config.parent.mkdir(parents=True, mode=0o700)
        self.policy('["~/ai-lc1-check/*"]')
        (self.home / "hello.py").write_text("mine\n")
        self.root = self.home / check.NAME

    def policy(self, read):
        self.config.write_text("version = 1\n[files]\nread = %s\ndeny = []\n" % read)
        self.config.chmod(0o600)

    def run_action(self, action):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check.main([action])
        return code, out.getvalue()

    def test_prepare_creates_exact_fixtures_privately_and_refuses_a_second_run(self):
        code, _ = self.run_action("prepare")
        self.assertEqual(code, 0)
        sizes = {name: (self.root / name).stat().st_size for name in check.FIXTURES}
        self.assertEqual(sizes["log1m.txt"], 1024 * 1024)
        self.assertEqual(check.CEILING, p.MAX_FILE_SIZE)
        self.assertEqual(sizes["over.txt"], p.MAX_FILE_SIZE + 1)
        self.assertEqual(sizes["empty.txt"], 0)
        self.assertEqual(oct(self.root.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((self.root / "log1m.txt").stat().st_mode & 0o777), "0o600")
        self.assertEqual((self.root / "log1m.txt").read_bytes().count(b"ERROR-MARKER"), 3)
        self.assertEqual(self.run_action("prepare")[0], 2)
        self.assertEqual((self.home / "hello.py").read_text(), "mine\n")

    def test_refusals_pass_under_the_real_policy_and_installed_style_loader(self):
        self.run_action("prepare")
        code, output = self.run_action("refusals")
        self.assertEqual(code, 0, output)
        for line in ("small.py      OK   ACCEPTED", "medium.log    OK   ACCEPTED", "log1m.txt     OK   ACCEPTED 1048576 bytes", "over.txt      OK   REFUSED", "ansi.txt      OK",
                     "bad-utf8.txt  OK", "empty.txt     OK", "REFUSALS=PASS"):
            self.assertIn(line, output)

    def test_a_smaller_owner_file_size_makes_the_big_log_an_expected_refusal_not_a_failure(self):
        self.config.write_text('version = 1\n[files]\nread = ["~/ai-lc1-check/*"]\ndeny = []\nfile_size = "500KiB"\n')
        self.config.chmod(0o600)
        self.run_action("prepare")
        code, output = self.run_action("refusals")
        self.assertEqual(code, 0, output)
        self.assertIn("medium.log    OK   ACCEPTED", output)
        self.assertRegex(output, r"log1m.txt\s+OK   REFUSED: The file is over 512000 bytes")
        self.assertIn("REFUSALS=PASS", output)

    def test_a_policy_that_does_not_allow_the_directory_is_reported_not_misread_as_refusal(self):
        self.run_action("prepare")
        self.policy('["~/elsewhere/*"]')
        code, output = self.run_action("refusals")
        self.assertEqual(code, 3)
        self.assertIn("POLICY: DENIED", output)

    def test_unchanged_check_detects_edits_extra_files_and_missing_files(self):
        self.run_action("prepare")
        self.assertEqual(self.run_action("check-unchanged")[0], 0)
        (self.root / "log1m.txt").write_bytes(b"changed")
        self.assertEqual(self.run_action("check-unchanged")[0], 1)
        self.run_action("cleanup")
        self.run_action("prepare")
        (self.root / "extra.txt").write_text("new")
        self.assertEqual(self.run_action("check-unchanged")[0], 1)
        (self.root / "extra.txt").unlink()
        (self.root / "empty.txt").unlink()
        self.assertEqual(self.run_action("check-unchanged")[0], 1)

    def test_cleanup_removes_only_the_fixture_directory_and_symlinked_base_is_refused(self):
        self.run_action("prepare")
        self.assertEqual(self.run_action("cleanup")[0], 0)
        self.assertFalse(self.root.exists())
        self.assertTrue((self.home / "hello.py").exists())
        (self.home / "x").mkdir()
        self.root.symlink_to(self.home / "x")
        with self.assertRaises(SystemExit):
            check.main(["prepare"])


if __name__ == "__main__":
    unittest.main()
