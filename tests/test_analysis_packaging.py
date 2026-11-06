"""Real application staging excludes legacy clients and never executes them."""
import hashlib
import re
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AnalysisPackagingTests(unittest.TestCase):
    def test_readme_and_recipe_use_independent_optional_clients_without_download_assets(self):
        recipe = (ROOT / "packaging/arch/PKGBUILD").read_text()
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("independently installed", readme)
        self.assertIn("ai-desktop clients register claude", readme)
        self.assertIn("ai-desktop clients register codex", readme)
        self.assertNotIn("_claude_code_sha256", recipe)
        self.assertNotIn("./claude-code --version", readme)
        # Evaluate the real recipe's metadata, without calling package() or a
        # package manager: no source URL, checksum pin or mandatory provider.
        result = subprocess.run(["/usr/bin/bash", "-c", 'set -e; source "$1"; '
                                 'test "${#source[@]}" -eq 0; test "${#sha256sums[@]}" -eq 0; '
                                 'printf "DEP=%s\\n" "${depends[@]}"; printf "OPT=%s\\n" "${optdepends[@]}"',
                                 "bash", str(ROOT / "packaging/arch/PKGBUILD")],
                                env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for provider in ("claude-code", "openai-codex"):
            self.assertIn("OPT=" + provider + ":", result.stdout)
            self.assertNotRegex(result.stdout, r"(?m)^DEP=" + re.escape(provider) + r"(?:[<>=:]|$)")

    def test_legacy_native_assets_are_ignored_unchanged_and_never_executed_by_real_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tree"
            for name in ("ai_desktop", "skills", "packaging"):
                shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns(
                    "__pycache__", "claude-code", "codex", "pkg", "src", "*.pkg.tar.*"))
            staging = root / "packaging/arch"
            marker = Path(temp) / "native-was-executed"
            env = {"PATH": "/usr/bin:/bin", "startdir": str(staging),
                   "AI_DESKTOP_CHECK_CLIENTS": "1", "AI_DESKTOP_EXPECT_CLAUDE": "wrong-old-pin",
                   "AI_DESKTOP_EXPECT_CODEX": "wrong-old-pin", "AI_DESKTOP_NATIVE_EXEC_MARKER": str(marker)}
            outputs = []
            def stage():
                target = Path(temp) / ("package-" + str(len(outputs)))
                outputs.append(target)
                result = subprocess.run(["/usr/bin/bash", "-c", 'set -e; source "$1"; package',
                                         "bash", str(staging / "PKGBUILD")], env=dict(env, pkgdir=str(target)),
                                        capture_output=True, timeout=15, check=False)
                return result, target

            def manifest(target):
                return {str(path.relative_to(target)): (hashlib.sha256(path.read_bytes()).hexdigest(),
                                                        stat.S_IMODE(path.stat().st_mode))
                        for path in target.rglob("*") if path.is_file()}

            result, target = stage()
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = manifest(target)
            for provider in ("claude-code", "codex"):
                self.assertNotIn("usr/lib/ai-desktop/" + provider, expected)
            self.assertNotIn("usr/bin/claude", expected)
            self.assertNotIn("usr/bin/codex", expected)
            fake = b"\x7fELF" + b"SYNTHETIC NONEXECUTABLE RUNTIME" * 50
            script = b'#!/bin/sh\nprintf executed > "$AI_DESKTOP_NATIVE_EXEC_MARKER"\nexit 97\n'
            outside = Path(temp) / "sentinel-client"
            outside.write_bytes(script)
            outside.chmod(0o755)
            for kind in ("wrong-elf", "executable-script", "symlink", "fifo"):
                with self.subTest(kind=kind):
                    for provider in ("claude-code", "codex"):
                        asset = staging / provider
                        if os.path.lexists(asset):
                            asset.unlink()
                        if kind == "symlink":
                            asset.symlink_to(outside)
                        elif kind == "fifo":
                            os.mkfifo(asset, 0o600)
                        else:
                            asset.write_bytes(fake if kind == "wrong-elf" else script)
                            asset.chmod(0o755)
                    before = {provider: (staging / provider).lstat() for provider in ("claude-code", "codex")}
                    result, target = stage()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(manifest(target), expected)
                    self.assertFalse(marker.exists())
                    for provider, info in before.items():
                        asset = staging / provider
                        after = asset.lstat()
                        self.assertEqual((after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                                         (info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
                        if kind in ("wrong-elf", "executable-script"):
                            self.assertEqual(asset.read_bytes(), fake if kind == "wrong-elf" else script)
                    self.assertEqual(outside.read_bytes(), script)
            for name in ("analysis-client.py", "analysis-worker.py", "analysis-session.py", "client-admin.py"):
                self.assertEqual((target / "usr/lib/ai-desktop" / name).read_bytes(),
                                 (root / "packaging/systemd" / name).read_bytes())
            for name in ("ai-desktop-analysis@.socket", "ai-desktop-analysis@.service"):
                self.assertEqual((target / "usr/lib/systemd/system" / name).read_bytes(),
                                 (root / "packaging/systemd" / name).read_bytes())
            # Ignored vendor names do not relax the application-code boundary.
            helper = root / "packaging/systemd/client-admin.py"
            helper.unlink()
            helper.symlink_to(outside)
            result, _ = stage()
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())

    def test_new_units_parse_and_remain_dormant(self):
        units = ROOT / "packaging/systemd"
        result = subprocess.run(["/usr/bin/systemd-analyze", "verify",
                                 str(units / "ai-desktop-analysis@.socket"),
                                 str(units / "ai-desktop-analysis@.service")],
                                capture_output=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertNotIn("[Install]", (units / "ai-desktop-analysis@.socket").read_text())
        self.assertNotIn("[Install]", (units / "ai-desktop-analysis@.service").read_text())
