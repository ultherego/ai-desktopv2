"""Linux user process/permission gate with disposable synthetic E7 prefixes.

Runs offline in CI too; Ubuntu CI is not CachyOS acceptance.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import install_user as installer
from ai_desktop import install_lifecycle as lifecycle
from ai_desktop import install_upgrade as upgrade

SOURCE = str(Path(__file__).resolve().parent.parent)
CHILD = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
from ai_desktop import install_user as installer
from ai_desktop import install_lifecycle as lifecycle
prefix, scenario = sys.argv[2:]
if scenario == "install":
    original = installer._write_new
    calls = 0
    def interrupted(*args, **kwargs):
        global calls
        original(*args, **kwargs)
        calls += 1
        if calls == 3:
            os._exit(73)
    installer._write_new = interrupted
    installer.install(prefix, installer.preview()["sha256"])
else:
    original = os.unlink
    def interrupted(name, *args, **kwargs):
        original(name, *args, **kwargs)
        if name not in lifecycle.METADATA:
            os._exit(74)
    lifecycle.os.unlink = interrupted
    plan = lifecycle.preview_removal(prefix, "uninstall")
    lifecycle.remove(prefix, plan["sha256"], "uninstall")
raise SystemExit(99)
'''


class HostE7LifecycleChecks(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.fail("E7 host test requires an ordinary user; root is refused")
        self.temp = tempfile.TemporaryDirectory(prefix="aid-e7-host-")
        self.addCleanup(self.temp.cleanup)
        self.prefix = Path(self.temp.name) / "installation"
        self.prefix.mkdir(mode=0o700)

    def install(self):
        installer.install(str(self.prefix), installer.preview()["sha256"])

    def remove(self, action):
        plan = lifecycle.preview_removal(str(self.prefix), action)
        lifecycle.remove(str(self.prefix), plan["sha256"], action)
        self.assertEqual(list(self.prefix.iterdir()), [])

    def child(self, scenario):
        return subprocess.run([sys.executable, "-I", "-B", "-c", CHILD,
                               SOURCE, str(self.prefix), scenario],
                              env={"PATH": "/usr/bin"}, capture_output=True, timeout=30, check=False)

    def test_full_source_install_smoke_cli_refusal_and_uninstall(self):
        self.install()
        command = subprocess.run([str(self.prefix / "bin/ai-desktop"), "--version"],
                                 env={"PATH": "/usr/bin"}, capture_output=True, timeout=15, check=False)
        self.assertEqual(command.returncode, 0)
        refusal = subprocess.run([sys.executable, "-B", "-m", "ai_desktop.install_user",
                                  "uninstall", str(self.prefix), "0"*64],
                                 cwd=SOURCE, env={"PATH": "/usr/bin"},
                                 capture_output=True, timeout=15, check=False)
        self.assertEqual(refusal.returncode, 2)
        self.assertEqual(refusal.stdout.strip(), b"INSTALL: REFUSED")
        target = self.prefix / "lib/ai_desktop/__init__.py"
        old = target.read_bytes()
        target.write_bytes(b"USER MODIFIED SYNTHETIC DATA")
        with self.assertRaises(installer.InstallDenied):
            lifecycle.preview_removal(str(self.prefix), "uninstall")
        self.assertEqual(target.read_bytes(), b"USER MODIFIED SYNTHETIC DATA")
        target.write_bytes(old)
        plan = lifecycle.preview_removal(str(self.prefix), "uninstall")
        # Import the installed copy, then let that copy remove its own files.
        # Python and the standard library remain outside the selected prefix.
        code = ("import sys; sys.path.insert(0, sys.argv[1]); "
                "from ai_desktop.install_user import main; "
                "raise SystemExit(main(['uninstall', sys.argv[2], sys.argv[3]]))")
        removal = subprocess.run([sys.executable, "-I", "-B", "-c", code,
                                  str(self.prefix / "lib"), str(self.prefix), plan["sha256"]],
                                 env={"PATH": "/usr/bin"}, capture_output=True, timeout=15, check=False)
        self.assertEqual(removal.returncode, 0)
        self.assertEqual(removal.stdout.strip(), b"INSTALL: UNINSTALL PASS (prefix retained)")
        self.assertEqual(removal.stderr, b"")
        self.assertEqual(list(self.prefix.iterdir()), [])

    def test_real_process_termination_during_install_and_uninstall(self):
        self.assertEqual(self.child("install").returncode, 73)
        self.assertTrue((self.prefix / lifecycle.INSTALL_JOURNAL).is_file())
        self.remove("recover")
        self.install()
        self.assertEqual(self.child("uninstall").returncode, 74)
        self.assertTrue((self.prefix / lifecycle.REMOVE_JOURNAL).is_file())
        self.remove("uninstall")

    def test_actual_permission_denial_preserves_retry_marker(self):
        self.install()
        plan = lifecycle.preview_removal(str(self.prefix), "uninstall")
        original = os.unlink
        locked = self.prefix / "bin"
        injected = False
        def denied(name, *args, **kwargs):
            nonlocal injected
            if name == "ai-desktop" and not injected:
                injected = True
                locked.chmod(0o500)
            return original(name, *args, **kwargs)
        try:
            with patch.object(lifecycle.os, "unlink", side_effect=denied):
                with self.assertRaises(installer.InstallDenied):
                    lifecycle.remove(str(self.prefix), plan["sha256"], "uninstall")
        finally:
            locked.chmod(0o700)
        self.assertTrue(injected)
        self.assertTrue((self.prefix / lifecycle.REMOVE_JOURNAL).is_file())
        self.assertTrue((locked / "ai-desktop").is_file())
        self.remove("uninstall")


class HostE7UpgradeChecks(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.fail("E7 upgrade host test requires an ordinary user")
        self.temp = tempfile.TemporaryDirectory(prefix="aid-e7-upgrade-host-")
        self.addCleanup(self.temp.cleanup)
        self.old = Path(self.temp.name) / "previous"
        self.new = Path(self.temp.name) / "candidate"
        for prefix in (self.old, self.new):
            prefix.mkdir(mode=0o700)
        previous_files = tuple((path, data + b"\n# SYNTHETIC PREVIOUS REVISION\n")
                               if path == "ai_desktop/__init__.py" else (path, data)
                               for path, data in installer.manifest())
        with patch.object(installer, "manifest", return_value=previous_files):
            installer.install(str(self.old), installer.preview()["sha256"])
        self.before = self.snapshot()

    def snapshot(self):
        return {str(path.relative_to(self.old)): (path.stat().st_ino, path.stat().st_mode,
                path.read_bytes() if path.is_file() else None) for path in self.old.rglob("*")}

    def smoke(self, prefix):
        result = subprocess.run([str(prefix / "bin/ai-desktop"), "--version"],
                                env={"PATH": "/usr/bin"}, capture_output=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0)

    def test_upgrade_and_installed_copy_rollback_keep_previous_runnable(self):
        plan = upgrade.preview(str(self.old), str(self.new))
        result = upgrade.apply(str(self.old), str(self.new), plan["sha256"])
        self.assertEqual(result["status"], "STAGED")
        self.smoke(self.old)
        self.smoke(self.new)
        plan = upgrade.preview(str(self.old), str(self.new), "rollback-upgrade")
        code = ("import sys; sys.path.insert(0, sys.argv[1]); "
                "from ai_desktop.install_user import main; "
                "raise SystemExit(main(['rollback-upgrade', sys.argv[2], sys.argv[3], sys.argv[4]]))")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code,
                                 str(self.new / "lib"), str(self.old), str(self.new), plan["sha256"]],
                                env={"PATH": "/usr/bin"}, capture_output=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "ROLLED_BACK")
        self.assertEqual(result.stderr, b"")
        self.assertEqual(list(self.new.iterdir()), [])
        self.assertEqual(self.snapshot(), self.before)
        self.smoke(self.old)

    def test_real_upgrade_process_crash_keeps_previous_and_candidate_recovers(self):
        code = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
from ai_desktop import install_user as installer
from ai_desktop import install_upgrade as upgrade
original = installer._write_new
calls = 0
def interrupted(*args, **kwargs):
    global calls
    original(*args, **kwargs)
    calls += 1
    if calls == 3:
        os._exit(75)
installer._write_new = interrupted
plan = upgrade.preview(sys.argv[2], sys.argv[3])
upgrade.apply(sys.argv[2], sys.argv[3], plan["sha256"])
raise SystemExit(99)
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code,
                                 SOURCE, str(self.old), str(self.new)], env={"PATH": "/usr/bin"},
                                capture_output=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 75)
        self.assertTrue((self.new / lifecycle.INSTALL_JOURNAL).is_file())
        self.assertEqual(self.snapshot(), self.before)
        self.smoke(self.old)
        recovery = lifecycle.preview_removal(str(self.new), "recover")
        lifecycle.remove(str(self.new), recovery["sha256"], "recover")
        self.assertEqual(list(self.new.iterdir()), [])
        self.assertEqual(self.snapshot(), self.before)


if __name__ == "__main__":
    unittest.main()
