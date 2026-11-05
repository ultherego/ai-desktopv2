"""Real staging of the optional runtime; never install or execute Claude."""
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
    def test_the_readme_and_the_recipe_pin_the_same_client_and_a_wrong_sum_stops_before_the_file_runs(self):
        recipe = (ROOT / "packaging/arch/PKGBUILD").read_text()
        pinned = re.search(r"^_claude_code_sha256=([0-9a-f]{64})$", recipe, re.M).group(1)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("echo '" + pinned + "  claude-code' | sha256sum -c -\nand chmod 755 claude-code\nand ./claude-code --version", readme)

    def test_optional_native_asset_is_unchanged_or_refused_without_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tree"
            for name in ("ai_desktop", "skills", "packaging"):
                shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns(
                    "__pycache__", "claude-code", "pkg", "src", "*.pkg.tar.*"))
            asset = root / "packaging/arch/claude-code"
            env = {"PATH": "/usr/bin:/bin", "startdir": str(asset.parent), "pkgdir": str(Path(temp) / "package")}
            def stage():
                return subprocess.run(["/usr/bin/bash", "-c", 'set -e; source "$1"; package',
                                       "bash", str(asset.parent / "PKGBUILD")], env=env,
                                      capture_output=True, timeout=15, check=False)
            self.assertEqual(stage().returncode, 0)
            target = Path(env["pkgdir"]) / "usr/lib/ai-desktop/claude-code"
            self.assertFalse(target.exists())
            fake = b"\x7fELF" + b"SYNTHETIC NONEXECUTABLE RUNTIME" * 50
            asset.write_bytes(fake)
            refused = stage()                                                # the real pinned sum rejects another file
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn(b"wrong SHA-256", refused.stderr)
            self.assertFalse(target.exists())
            recipe = asset.parent / "PKGBUILD"
            recipe.write_text(re.sub(r"_claude_code_sha256=[0-9a-f]{64}",
                                     "_claude_code_sha256=" + hashlib.sha256(fake).hexdigest(), recipe.read_text()))
            self.assertEqual(stage().returncode, 0)
            self.assertEqual(hashlib.sha256(target.read_bytes()).digest(), hashlib.sha256(fake).digest())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            for name in ("analysis-client.py", "analysis-worker.py", "analysis-session.py"):
                self.assertEqual((target.parent / name).read_bytes(), (root / "packaging/systemd" / name).read_bytes())
            for name in ("ai-desktop-analysis@.socket", "ai-desktop-analysis@.service"):
                self.assertEqual((Path(env["pkgdir"]) / "usr/lib/systemd/system" / name).read_bytes(),
                                 (root / "packaging/systemd" / name).read_bytes())
            asset.write_text("#!/bin/sh\nexit 0\n")
            self.assertNotEqual(stage().returncode, 0)
            asset.unlink()
            asset.symlink_to("/usr/bin/true")
            self.assertNotEqual(stage().returncode, 0)

    def test_new_units_parse_and_remain_dormant(self):
        units = ROOT / "packaging/systemd"
        result = subprocess.run(["/usr/bin/systemd-analyze", "verify",
                                 str(units / "ai-desktop-analysis@.socket"),
                                 str(units / "ai-desktop-analysis@.service")],
                                capture_output=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertNotIn("[Install]", (units / "ai-desktop-analysis@.socket").read_text())
        self.assertNotIn("[Install]", (units / "ai-desktop-analysis@.service").read_text())
