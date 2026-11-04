"""E7 install home: atomic active-release pointer, recovery and removal (synthetic releases)."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import install_home as home
from ai_desktop import install_user as installer
from ai_desktop.install_policy import InstallDenied

V1 = (("ai_desktop/__init__.py", b'"""V1"""\n'),
      ("ai_desktop/__main__.py", b'print("SYNTHETIC V1")\n'))
V2 = (("ai_desktop/__init__.py", b'"""V2"""\n'),
      ("ai_desktop/__main__.py", b'print("SYNTHETIC V2")\n'))


class InstallHomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aid-e7-home-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir(mode=0o700)
        self.h = str(self.home)
        home.init(self.h)

    def release(self, name, files):
        prefix = home.new_release(self.h, name)["prefix"]
        with patch.object(installer, "manifest", return_value=files):
            installer.install(prefix, installer.preview()["sha256"])
        return prefix

    def activate(self, name):
        plan = home.preview(self.h, name)
        return home.apply(self.h, name, plan["sha256"])

    def run_launcher(self):
        done = subprocess.run([str(self.home / "bin/ai-desktop")], capture_output=True,
                              text=True, timeout=30, cwd=self.temp.name)
        return done.returncode, done.stdout.strip()

    def test_init_requires_empty_private_directory(self):
        other = Path(self.temp.name) / "other"
        other.mkdir(mode=0o755)
        # mkdir is filtered by umask; establish the public mode being tested.
        other.chmod(0o755)
        self.assertEqual(stat.S_IMODE(other.stat().st_mode), 0o755)
        with self.assertRaises(InstallDenied):
            home.init(str(other))
        other.chmod(0o700)
        self.assertEqual(stat.S_IMODE(other.stat().st_mode), 0o700)
        (other / "file").write_text("x")
        with self.assertRaises(InstallDenied):
            home.init(str(other))
        with self.assertRaises(InstallDenied):
            home.init(self.h)  # second init never overwrites

    def test_launcher_fails_closed_until_a_release_is_active(self):
        self.assertEqual(self.run_launcher()[0], 2)
        self.release("r1", V1)
        self.assertEqual(self.run_launcher()[0], 2)

    def test_activate_switch_and_rollback_with_real_launcher(self):
        self.release("r1", V1)
        self.release("r2", V2)
        self.assertEqual(self.activate("r1")["status"], "ACTIVATED")
        self.assertEqual(self.run_launcher(), (0, "SYNTHETIC V1"))
        before = (self.home / "releases/r1/install-manifest.json").read_bytes()
        result = self.activate("r2")
        self.assertEqual((result["current"], result["previous"]), ("r2", "r1"))
        self.assertEqual(self.run_launcher(), (0, "SYNTHETIC V2"))
        self.assertEqual(os.readlink(self.home / "previous"), "releases/r1")
        plan = home.preview(self.h, None, "rollback")
        self.assertEqual(plan["target"], "r1")
        home.apply(self.h, None, plan["sha256"], "rollback")
        self.assertEqual(self.run_launcher(), (0, "SYNTHETIC V1"))
        self.assertEqual((self.home / "releases/r1/install-manifest.json").read_bytes(), before)

    def test_refusals_leave_pointers_unchanged(self):
        self.release("r1", V1)
        self.activate("r1")
        (self.home / "releases/r2").mkdir(mode=0o700)  # empty = not a complete release
        for release in ("r2", "r1", "missing", "../r1", "r1/x", "", ".hidden"):
            with self.subTest(release=release):
                with self.assertRaises(InstallDenied):
                    home.preview(self.h, release)
        with self.assertRaises(InstallDenied):
            home.preview(self.h, None, "rollback")  # no previous yet
        self.assertEqual(os.readlink(self.home / "current"), "releases/r1")

    def test_stale_or_wrong_hash_and_changed_state_are_refused(self):
        self.release("r1", V1)
        self.release("r2", V2)
        plan = home.preview(self.h, "r1")
        for bad in ("0" * 64, "x", plan["sha256"].upper(), None):
            with self.assertRaises(InstallDenied):
                home.apply(self.h, "r1", bad)
        self.assertFalse((self.home / "current").exists() or (self.home / "current").is_symlink())
        home.apply(self.h, "r1", plan["sha256"])
        with self.assertRaises(InstallDenied):
            home.apply(self.h, "r1", plan["sha256"])  # replay: already active
        plan2 = home.preview(self.h, "r2")
        # Approved plan is bound to the pointers at approval time.
        (self.home / "previous").symlink_to("releases/r2")
        with self.assertRaises(InstallDenied):
            home.apply(self.h, "r2", plan2["sha256"])
        self.assertEqual(os.readlink(self.home / "current"), "releases/r1")

    def test_tampered_release_cannot_be_activated(self):
        prefix = self.release("r1", V1)
        (Path(prefix) / "lib/ai_desktop/__main__.py").write_bytes(b'print("EVIL")\n')
        with self.assertRaises(InstallDenied):
            home.preview(self.h, "r1")

    def test_odd_pointer_targets_are_refused(self):
        self.release("r1", V1)
        for target in ("/etc", "../x", "releases/../x", "releases/a/b", "r1"):
            with self.subTest(target=target):
                link = self.home / "current"
                if link.is_symlink():
                    link.unlink()
                link.symlink_to(target)
                with self.assertRaises(InstallDenied):
                    home.preview(self.h, "r1")
                with self.assertRaises(InstallDenied):
                    home.status(self.h)
        link.unlink()
        link.mkdir()  # a directory in place of the pointer
        with self.assertRaises(InstallDenied):
            home.preview(self.h, "r1")

    def test_interruption_between_steps_leaves_valid_pointers(self):
        self.release("r1", V1)
        self.release("r2", V2)
        self.activate("r1")
        plan = home.preview(self.h, "r2")
        real = home._set_pointer
        calls = []

        def die_before_current(fd, name, release):
            calls.append(name)
            if name == "current":
                raise KeyboardInterrupt  # power loss after `previous` was written
            return real(fd, name, release)

        with patch.object(home, "_set_pointer", side_effect=die_before_current):
            with self.assertRaises(KeyboardInterrupt):
                home.apply(self.h, "r2", plan["sha256"])
        self.assertEqual(calls, ["previous", "current"])
        self.assertEqual(os.readlink(self.home / "current"), "releases/r1")
        self.assertEqual(self.run_launcher(), (0, "SYNTHETIC V1"))
        self.assertEqual(home.recover(self.h)["status"], "OK")
        self.activate("r2")
        self.assertEqual(self.run_launcher(), (0, "SYNTHETIC V2"))

    def test_leftover_temp_link_is_cleaned_without_touching_pointers(self):
        self.release("r1", V1)
        self.activate("r1")
        stale = self.home / (".pointer-" + "ab" * 16)
        stale.symlink_to("releases/r1")
        self.assertEqual(home.status(self.h)["stale_temp"], 1)
        result = home.recover(self.h)
        self.assertEqual((result["cleaned"], result["status"]), (1, "OK"))
        self.assertFalse(stale.is_symlink())
        self.assertEqual(os.readlink(self.home / "current"), "releases/r1")

    def test_recover_reports_broken_current_and_does_not_guess(self):
        prefix = self.release("r1", V1)
        self.activate("r1")
        os.unlink(Path(prefix) / "install-manifest.json")
        result = home.recover(self.h)
        self.assertEqual(result["status"], "BROKEN")
        self.assertEqual(os.readlink(self.home / "current"), "releases/r1")
        self.assertEqual(self.run_launcher()[0], 2)

    def test_retire_refuses_active_and_fallback_releases(self):
        self.release("r1", V1)
        self.release("r2", V2)
        self.release("r3", V2)
        self.activate("r1")
        self.activate("r2")
        for name in ("r1", "r2"):
            with self.assertRaises(InstallDenied):
                home.retire_preview(self.h, name)
        plan = home.retire_preview(self.h, "r3")
        with self.assertRaises(InstallDenied):
            home.retire(self.h, "r3", "0" * 64)
        self.assertEqual(home.retire(self.h, "r3", plan["sha256"])["status"], "UNINSTALLED")
        self.assertEqual(self.run_launcher(), (0, "SYNTHETIC V2"))

    def test_home_removal_leaves_no_trace_but_requires_empty_releases(self):
        self.release("r1", V1)
        self.activate("r1")
        with self.assertRaises(InstallDenied):
            home.remove_home(self.h)
        self.assertTrue((self.home / "current").is_symlink())
        # Retire needs r1 not to be active: drop pointers first via a second release.
        self.release("r2", V2)
        self.activate("r2")
        (self.home / "previous").unlink()
        plan = home.retire_preview(self.h, "r1")
        home.retire(self.h, "r1", plan["sha256"])
        (self.home / "releases/r1").rmdir()
        (self.home / "current").unlink()
        plan = home.retire_preview(self.h, "r2")
        home.retire(self.h, "r2", plan["sha256"])
        (self.home / "releases/r2").rmdir()
        self.assertEqual(home.remove_home(self.h)["status"], "HOME_REMOVED")
        self.assertEqual(os.listdir(self.home), [])

    def test_modified_launcher_blocks_home_removal(self):
        (self.home / "bin/ai-desktop").write_text("#!/bin/sh\necho mine\n")
        with self.assertRaises(InstallDenied):
            home.remove_home(self.h)
        self.assertEqual((self.home / "bin/ai-desktop").read_text(), "#!/bin/sh\necho mine\n")

    def test_cli_verbs_round_trip_and_refuse_wrong_arity(self):
        self.release("r1", V1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(installer.main(["activate-preview", self.h, "r1"]), 0)
            sha = json.loads(out.getvalue())["sha256"]
            self.assertEqual(installer.main(["activate", self.h, "r1", sha]), 0)
            self.assertEqual(installer.main(["home-status", self.h]), 0)
            self.assertEqual(installer.main(["activate", self.h, "r1"]), 2)
            self.assertEqual(installer.main(["home-status"]), 2)
        self.assertIn("INSTALL: REFUSED", out.getvalue())
        self.assertEqual(self.run_launcher(), (0, "SYNTHETIC V1"))

    def test_symlinked_home_is_refused(self):
        link = Path(self.temp.name) / "homelink"
        link.symlink_to(self.home)
        with self.assertRaises(InstallDenied):
            home.status(str(link))


if __name__ == "__main__":
    unittest.main()
