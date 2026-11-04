"""Side-by-side update contracts using synthetic private installation trees."""
import contextlib
import io
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

OLD = (("ai_desktop/__init__.py", b'"""SYNTHETIC OLD"""\n'),
       ("ai_desktop/__main__.py", b'print("SYNTHETIC OLD")\n'))
NEW = (("ai_desktop/__init__.py", b'"""SYNTHETIC NEW"""\n'),
       ("ai_desktop/__main__.py", b'print("SYNTHETIC NEW")\n'))


class Interrupted(BaseException):
    pass


class E7UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aid-e7-upgrade-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old, self.new = self.root / "previous", self.root / "candidate"
        for prefix in (self.old, self.new):
            prefix.mkdir(mode=0o700)
        with patch.object(installer, "manifest", return_value=OLD):
            installer.install(str(self.old), installer.preview()["sha256"])
        self.before = self.state(self.old)

    def state(self, prefix):
        return {str(p.relative_to(prefix)): (p.lstat().st_ino, p.lstat().st_mode,
                p.read_bytes() if p.is_file() else None) for p in prefix.rglob("*")}

    def plan(self, action="upgrade", files=NEW):
        with patch.object(installer, "manifest", return_value=files):
            return upgrade.preview(str(self.old), str(self.new), action)

    def apply(self, plan=None, action="upgrade", files=NEW):
        plan = self.plan(action, files) if plan is None else plan
        with patch.object(installer, "manifest", return_value=files):
            return upgrade.apply(str(self.old), str(self.new), plan["sha256"], action)

    def test_stage_new_launcher_and_rollback_preserve_previous(self):
        plan = self.plan()
        self.assertEqual(plan["previous"]["access"], "read-only")
        self.assertEqual(plan["candidate"]["prefix"], str(self.new))
        self.assertEqual(self.state(self.old), self.before)
        self.assertEqual(list(self.new.iterdir()), [])
        result = self.apply(plan)
        self.assertEqual(result["status"], "STAGED")
        self.assertIs(result["previous_retained"], True)
        for prefix, expected in ((self.old, b"SYNTHETIC OLD"), (self.new, b"SYNTHETIC NEW")):
            command = subprocess.run([str(prefix / "bin/ai-desktop"), "--version"],
                                     env={"PATH": "/usr/bin"}, capture_output=True, timeout=5, check=False)
            self.assertEqual(command.returncode, 0)
            self.assertEqual(command.stdout.strip(), expected)
        rolled = self.apply(action="rollback-upgrade")
        self.assertEqual(rolled["status"], "ROLLED_BACK")
        self.assertEqual(self.state(self.old), self.before)
        self.assertEqual(list(self.new.iterdir()), [])

    def test_new_launcher_ignores_cwd_and_python_environment(self):
        shadow = self.root / "shadow"
        package = shadow / "ai_desktop"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "__main__.py").write_text('print("UNAPPROVED SHADOW CODE")')
        result = subprocess.run([str(self.old / "bin/ai-desktop"), "--version"], cwd=shadow,
                                env={"PATH": "/usr/bin", "PYTHONPATH": str(shadow),
                                     "PYTHONHOME": str(shadow)},
                                capture_output=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b"SYNTHETIC OLD")
        self.assertEqual(result.stderr, b"")

    def test_exact_historic_v2_launcher_is_accepted_without_rewriting_old(self):
        import hashlib
        wrapper = installer._wrapper(str(self.old), isolated=False)
        (self.old / "bin/ai-desktop").write_bytes(wrapper)
        manifest = self.old / lifecycle.MANIFEST
        receipt = json.loads(manifest.read_bytes())
        entry = next(e for e in receipt["files"] if e["path"] == "bin/ai-desktop")
        entry["sha256"], entry["size"] = hashlib.sha256(wrapper).hexdigest(), len(wrapper)
        manifest.write_text(json.dumps(receipt))
        before = self.state(self.old)
        result = self.apply()
        self.assertIs(result["rollback_supported"], False)
        candidate = self.state(self.new)
        with self.assertRaises(installer.InstallDenied):
            self.plan("rollback-upgrade")
        self.assertEqual(self.state(self.new), candidate)
        self.assertEqual(self.state(self.old), before)
        self.assertEqual((self.old / "bin/ai-desktop").read_bytes(), wrapper)

    def test_same_source_nonempty_and_nested_prefixes_are_refused(self):
        with self.assertRaises(installer.InstallDenied):
            self.plan(files=OLD)
        (self.new / "user.txt").write_text("USER DATA")
        with self.assertRaises(installer.InstallDenied):
            self.plan()
        for previous, candidate in ((str(self.old), str(self.old)),
                                    (str(self.old), str(self.old / "nested")),
                                    (str(self.old), str(self.root))):
            with self.assertRaises(installer.InstallDenied):
                upgrade.preview(previous, candidate)
        self.assertEqual((self.new / "user.txt").read_text(), "USER DATA")
        self.assertEqual(self.state(self.old), self.before)

    def test_wrong_hash_and_changed_source_never_write_candidate(self):
        plan = self.plan()
        with patch.object(installer, "manifest", return_value=NEW):
            for value in ("0"*64, "PRIVATE", None, True):
                with self.assertRaises(installer.InstallDenied):
                    upgrade.apply(str(self.old), str(self.new), value)
        changed = (("ai_desktop/__init__.py", b"CHANGED SOURCE"), NEW[1])
        with self.assertRaises(installer.InstallDenied):
            self.apply(plan, files=changed)
        self.assertEqual(list(self.new.iterdir()), [])
        self.assertEqual(self.state(self.old), self.before)

    def test_stale_candidate_identity_before_apply_is_refused(self):
        plan = self.plan()
        self.new.rename(self.root / "reserved")
        self.new.mkdir(mode=0o700)
        with self.assertRaises(installer.InstallDenied):
            self.apply(plan)
        self.assertEqual(list(self.new.iterdir()), [])
        self.assertEqual(self.state(self.old), self.before)

    def test_directory_substitution_inside_installer_is_refused_on_actual_fd(self):
        plan = self.plan()
        original = installer.install
        def substitute(prefix, source_sha, **kwargs):
            self.new.rename(self.root / "reserved")
            self.new.mkdir(mode=0o700)
            return original(prefix, source_sha, **kwargs)
        with patch.object(installer, "install", side_effect=substitute):
            with self.assertRaises(installer.InstallDenied):
                self.apply(plan)
        self.assertEqual(list(self.new.iterdir()), [])
        self.assertEqual(list((self.root / "reserved").iterdir()), [])
        self.assertEqual(self.state(self.old), self.before)

    def test_changed_or_pending_previous_refuses_stage_and_rollback(self):
        plan = self.plan()
        self.apply(plan)
        candidate_before = self.state(self.new)
        target = self.old / "lib/ai_desktop/__init__.py"
        data = target.read_bytes()
        target.write_bytes(b"USER MODIFIED OLD")
        for action in ("upgrade", "rollback-upgrade"):
            with self.assertRaises(installer.InstallDenied):
                self.plan(action)
        self.assertEqual(self.state(self.new), candidate_before)
        target.write_bytes(data)
        os.link(self.old / lifecycle.MANIFEST, self.old / lifecycle.REMOVE_JOURNAL)
        with self.assertRaises(installer.InstallDenied):
            self.plan("rollback-upgrade")
        self.assertEqual(self.state(self.new), candidate_before)

    def test_partial_candidate_write_keeps_old_and_can_recover(self):
        original = installer._write_new
        calls = 0
        def interrupted(*args, **kwargs):
            nonlocal calls
            original(*args, **kwargs)
            calls += 1
            if calls == 2:
                raise Interrupted()
        with patch.object(installer, "_write_new", side_effect=interrupted):
            with self.assertRaises(Interrupted):
                self.apply()
        self.assertEqual(self.state(self.old), self.before)
        with self.assertRaises(installer.InstallDenied):
            self.plan("rollback-upgrade")
        recovery = lifecycle.preview_removal(str(self.new), "recover")
        lifecycle.remove(str(self.new), recovery["sha256"], "recover")
        self.assertEqual(list(self.new.iterdir()), [])
        self.assertEqual(self.state(self.old), self.before)

    def test_partial_rollback_requires_new_plan_and_preserves_old(self):
        self.apply()
        plan = self.plan("rollback-upgrade")
        original = os.unlink
        calls = 0
        def interrupted(name, *args, **kwargs):
            nonlocal calls
            original(name, *args, **kwargs)
            calls += 1
            if calls == 1:
                raise Interrupted()
        with patch.object(lifecycle.os, "unlink", side_effect=interrupted):
            with self.assertRaises(Interrupted):
                self.apply(plan, "rollback-upgrade")
        self.assertEqual(self.state(self.old), self.before)
        with self.assertRaises(installer.InstallDenied):
            self.apply(plan, "rollback-upgrade")
        self.apply(action="rollback-upgrade")
        self.assertEqual(list(self.new.iterdir()), [])
        self.assertEqual(self.state(self.old), self.before)

    def test_modified_candidate_blocks_rollback_without_deletion(self):
        self.apply()
        plan = self.plan("rollback-upgrade")
        target = self.new / "lib/ai_desktop/__init__.py"
        target.write_bytes(b"USER MODIFIED NEW")
        before = self.state(self.new)
        with self.assertRaises(installer.InstallDenied):
            self.apply(plan, "rollback-upgrade")
        self.assertEqual(self.state(self.new), before)
        self.assertEqual(self.state(self.old), self.before)

    def test_symlink_candidate_and_unconfirmed_cli_are_refused(self):
        self.new.rmdir()
        self.new.symlink_to(self.old, target_is_directory=True)
        with self.assertRaises(installer.InstallDenied):
            self.plan()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(installer.main(["upgrade", str(self.old), str(self.new)]), 2)
            self.assertEqual(installer.main(["rollback-upgrade", str(self.old), str(self.new)]), 2)
            self.assertEqual(installer.main(["upgrade", str(self.old), str(self.new), "PRIVATE"]), 2)
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertEqual(self.state(self.old), self.before)

    def test_installer_identity_types_are_checked_before_journal(self):
        with patch.object(installer, "manifest", return_value=NEW):
            source_sha = installer.preview()["sha256"]
            for identity in ((True, 1), [1, 2], (), (1,), (1, -1), (1, "2"), (0, 0)):
                with self.assertRaises(installer.InstallDenied):
                    installer.install(str(self.new), source_sha, expected_prefix_identity=identity)
                self.assertEqual(list(self.new.iterdir()), [])
        self.assertEqual(self.state(self.old), self.before)


class E7UpgradeArgumentTests(unittest.TestCase):
    def test_unconfirmed_cli_never_inspects_or_changes_either_prefix(self):
        with patch.object(upgrade, "preview") as preview, patch.object(upgrade, "apply") as apply:
            for action in ("upgrade", "rollback-upgrade"):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(installer.main([action, "/tmp/previous", "/tmp/candidate"]), 2)
            preview.assert_not_called()
            apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
