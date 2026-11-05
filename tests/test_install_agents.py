"""Private client preparation using synthetic ELF bytes and mocked network and package tools."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import install_agents as install


class PrivateInstallTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.target = self.root / "packaging/arch"
        self.target.mkdir(parents=True)
        self.claude = b"\x7fELFfake claude"
        self.codex = b"\x7fELFfake codex"
        self.sha = hashlib.sha256(self.claude).hexdigest()
        self.enterContext(patch.object(install, "CLAUDE_SHA256", self.sha))
        self.fetch = self.enterContext(patch.object(install, "download_claude", side_effect=lambda p: p.write_bytes(self.claude)))
        self.codex_fetch = self.enterContext(patch.object(install.codex_asset, "prepare",
            side_effect=lambda *, root: (root / "packaging/arch/codex").write_bytes(self.codex)))

    def test_pins_match_existing_package_and_archive_preparer(self):
        original = (install.ROOT / "scripts/install_agents.py").read_text()
        recipe = (install.ROOT / "packaging/arch/PKGBUILD").read_text()
        self.assertEqual(re.search(r'^CLAUDE_SHA256 = "([a-f0-9]{64})"', original, re.M)[1],
                         re.search(r'^_claude_code_sha256=([a-f0-9]{64})', recipe, re.M)[1])
        self.assertIn(install.CLAUDE_URL, (install.ROOT / "README.md").read_text())

    def test_prepare_and_reuse_verified_files_without_overwriting(self):
        expected = {"claude": self.sha, "codex": hashlib.sha256(self.codex).hexdigest()}
        self.assertEqual(install.prepare(self.root, ("claude", "codex")), expected)
        inode = (self.target / "codex").stat().st_ino
        self.assertEqual(install.prepare(self.root, ("claude", "codex")), expected)
        self.assertEqual((self.target / "codex").stat().st_ino, inode)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.codex_fetch.call_count, 2)
        self.assertFalse(list(self.target.glob(".clients-*")))

    def test_wrong_existing_client_or_bad_download_fails_before_publication(self):
        (self.target / "codex").write_bytes(b"\x7fELFwont run")
        with self.assertRaises(install.InstallError):
            install.prepare(self.root, ("claude", "codex"))
        self.assertFalse((self.target / "claude-code").exists())
        self.assertEqual((self.target / "codex").read_bytes(), b"\x7fELFwont run")
        self.fetch.side_effect = lambda path: path.write_bytes(b"\x7fELFwrong")
        with self.assertRaises(install.InstallError):
            install.prepare(self.root, ("claude",))
        self.assertFalse((self.target / "claude-code").exists())

    def test_symlink_and_unselected_sibling_for_install_are_refused_before_network(self):
        (self.target / "codex").symlink_to("missing")
        for selected in (("codex",), ("claude",)):
            with self.assertRaises(install.InstallError):
                install.prepare(self.root, selected, installing=True)
        self.fetch.assert_not_called()
        self.codex_fetch.assert_not_called()

    def test_install_consent_precedes_download_and_makepkg_errors_are_reported(self):
        with patch.object(install.os, "getuid", return_value=1000), patch.object(install.os, "geteuid", return_value=1000), \
             patch.object(Path, "is_file", return_value=True), patch.object(install, "prepare", return_value={"claude": self.sha}) as prepare, \
             patch.object(install.sys.stdin, "isatty", return_value=True), redirect_stdout(io.StringIO()) as out, \
             patch.object(out, "isatty", return_value=True), redirect_stderr(io.StringIO()), patch("builtins.input") as reader, \
             patch.object(install.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "makepkg")) as run:
            reader.return_value = "n"
            self.assertEqual(install.main(["claude", "--install"], root=self.root), 2)
            prepare.assert_not_called()
            run.assert_not_called()
            reader.return_value = "y"
            self.assertEqual(install.main(["claude", "--install"], root=self.root), 2)
            prepare.assert_called_once_with(self.root, ("claude",), installing=True)
            command, kwargs = run.call_args
            self.assertEqual(command[0], ["/usr/bin/makepkg", "--cleanbuild", "-si"])
            self.assertEqual(kwargs["env"]["AI_DESKTOP_EXPECT_CLAUDE"], self.sha)
            self.assertEqual(kwargs["env"]["AI_DESKTOP_EXPECT_CODEX"], "-")
            self.assertEqual(kwargs["cwd"], self.target)
            self.assertFalse(Path(kwargs["env"]["PKGDEST"]).exists())

    def test_recipe_checks_staged_codex_digest_and_requested_absence(self):
        for folder in ("ai_desktop", "skills", "packaging"):
            shutil.copytree(install.ROOT / folder, self.root / folder, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "claude-code", "codex", "src", "pkg", "*.pkg.tar.*"))
        (self.target / "codex").write_bytes(self.codex)
        env = {"PATH": "/usr/bin:/bin", "startdir": str(self.target), "pkgdir": str(self.root / "out"),
               "AI_DESKTOP_CHECK_CLIENTS": "1", "AI_DESKTOP_EXPECT_CLAUDE": "-"}
        for digest, success in (("-", False), ("a" * 64, False), (hashlib.sha256(self.codex).hexdigest(), True)):
            env["AI_DESKTOP_EXPECT_CODEX"] = digest
            result = subprocess.run(["/usr/bin/bash", "-c", 'set -e; source "$1"; package', "bash",
                                     str(self.target / "PKGBUILD")], env=env, capture_output=True, timeout=30)
            self.assertEqual(result.returncode == 0, success, result.stderr)
