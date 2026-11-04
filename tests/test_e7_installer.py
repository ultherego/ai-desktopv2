"""E7 fresh-prefix user installation proof: only synthetic empty tempdirs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import install_user


class E7InstallContract(unittest.TestCase):
    def test_preview_exact_digest_and_fresh_prefix_install(self):
        with tempfile.TemporaryDirectory(prefix="aid-e7-test-") as root:
            prefix = Path(root) / "new-install"
            prefix.mkdir(mode=0o700)
            plan = install_user.preview()
            self.assertEqual(len(plan["sha256"]), 64)
            self.assertTrue(any(f["path"] == "skills/cachyos-diagnostics/SKILL.md"
                                for f in plan["files"]))
            self.assertTrue(any(f["path"] == "ai_desktop/__main__.py"
                                for f in plan["files"]))
            result = install_user.install(str(prefix), plan["sha256"])
            self.assertEqual(result["status"], "INSTALLED")
            self.assertEqual(result["files"], len(plan["files"]))
            wrapper = prefix / "bin/ai-desktop"
            self.assertEqual(wrapper.stat().st_mode & 0o777, 0o700)
            self.assertTrue((prefix / "lib/skills/cachyos-diagnostics/SKILL.md").is_file())
            command = subprocess.run([str(wrapper), "--version"], capture_output=True,
                                     text=True, timeout=15, check=False)
            self.assertEqual(command.returncode, 0, command.stderr)
            self.assertIn("ai-desktop", command.stdout)
            installed = json.loads((prefix / "install-manifest.json").read_text())
            self.assertEqual(installed["sha256"], plan["sha256"])
            with self.assertRaises(install_user.InstallDenied):
                install_user.install(str(prefix), plan["sha256"])

    def test_refuse_wrong_hash_nonempty_prefix_and_symlink(self):
        with tempfile.TemporaryDirectory(prefix="aid-e7-test-") as root:
            prefix = Path(root) / "new-install"
            prefix.mkdir()
            plan = install_user.preview()
            with self.assertRaises(install_user.InstallDenied):
                install_user.install(str(prefix), "0"*64)
            self.assertEqual(list(prefix.iterdir()), [])
            (prefix / "existing.txt").write_text("USER DATA")
            with self.assertRaises(install_user.InstallDenied):
                install_user.install(str(prefix), plan["sha256"])
            self.assertEqual((prefix / "existing.txt").read_text(), "USER DATA")
            link = Path(root) / "symlink"
            link.symlink_to(prefix)
            with self.assertRaises(install_user.InstallDenied):
                install_user.install(str(link), plan["sha256"])

    def test_root_or_missing_prefix_denied_before_write(self):
        with tempfile.TemporaryDirectory(prefix="aid-e7-test-") as root:
            prefix = Path(root) / "missing"
            with self.assertRaises(install_user.InstallDenied):
                install_user.install(str(prefix), install_user.preview()["sha256"])
            prefix.mkdir()
            with patch("ai_desktop.workspace_executor.os.geteuid", return_value=0):
                with self.assertRaises(install_user.InstallDenied):
                    install_user.install(str(prefix), install_user.preview()["sha256"])
            self.assertEqual(list(prefix.iterdir()), [])

    def test_cli_denies_unconfirmed_install_and_uninstall(self):
        self.assertEqual(install_user.main([]), 2)
        self.assertEqual(install_user.main(["uninstall", "/tmp"]), 2)
        self.assertEqual(install_user.main(["install", "/tmp"]), 2)


if __name__ == "__main__":
    unittest.main()
