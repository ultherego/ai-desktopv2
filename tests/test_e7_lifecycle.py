"""E7 removal/recovery regressions; fresh synthetic private prefixes only."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import install_user as installer
from ai_desktop import install_lifecycle as lifecycle

PAYLOAD = (("ai_desktop/__init__.py", b'"""SYNTHETIC PACKAGE"""\n'),
           ("ai_desktop/__main__.py", b'print("SYNTHETIC OFFLINE")\n'),
           ("skills/synthetic/SKILL.md", b"# SYNTHETIC SKILL\n"),
           ("skills/synthetic/references/tool-guide.md", b"SYNTHETIC VERSIONED REFERENCE\n"))


class Interrupted(BaseException):
    pass


class E7LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aid-e7-lifecycle-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / "installation"
        self.prefix.mkdir(mode=0o700)
        self.target = self.prefix / "lib/ai_desktop/__init__.py"

    def install(self):
        with patch.object(installer, "manifest", return_value=PAYLOAD):
            return installer.install(str(self.prefix), installer.preview()["sha256"])

    def plan(self, action="uninstall"):
        return lifecycle.preview_removal(str(self.prefix), action)

    def remove(self, action="uninstall", plan=None):
        plan = self.plan(action) if plan is None else plan
        return lifecycle.remove(str(self.prefix), plan["sha256"], action)

    def state(self):
        result = {}
        for path in self.prefix.rglob("*"):
            info = path.lstat()
            data = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            result[str(path.relative_to(self.prefix))] = (info.st_mode, info.st_ino, info.st_nlink, data)
        return result

    def test_install_preview_uninstall_cycle_keeps_prefix_and_other_data(self):
        sentinel = self.root / "unrelated"
        sentinel.write_text("USER DATA")
        self.install()
        before = self.state()
        plan = self.plan()
        self.assertEqual(self.state(), before)
        self.assertEqual(plan["prefix"], str(self.prefix))
        self.assertEqual(plan["root"]["ino"], self.prefix.stat().st_ino)
        self.assertEqual(len(plan["sha256"]), 64)
        result = self.remove(plan=plan)
        self.assertEqual(result["status"], "UNINSTALLED")
        self.assertIs(result["prefix_retained"], True)
        self.assertEqual(list(self.prefix.iterdir()), [])
        self.assertEqual(sentinel.read_text(), "USER DATA")
        self.install()  # The retained empty prefix can be installed again.
        self.remove()

    def test_uninstall_never_depends_on_current_source(self):
        self.install()
        with patch.object(installer, "manifest", side_effect=AssertionError("must use receipt")):
            self.remove()
        self.assertEqual(list(self.prefix.iterdir()), [])

    def test_modified_unknown_symlink_hardlink_fifo_and_permissions_refused(self):
        modifications = ("changed", "extra-file", "extra-dir", "symlink", "hardlink", "fifo", "mode")
        for modification in modifications:
            with self.subTest(modification=modification):
                self.prefix = self.root / ("installation-" + modification)
                self.prefix.mkdir(mode=0o700)
                self.target = self.prefix / "lib/ai_desktop/__init__.py"
                self.install()
                if modification == "changed":
                    self.target.write_bytes(b"USER MODIFIED")
                elif modification == "extra-file":
                    (self.prefix / "user.txt").write_text("USER DATA")
                elif modification == "extra-dir":
                    (self.prefix / "unknown").mkdir(mode=0o700)
                elif modification == "symlink":
                    self.target.unlink()
                    self.target.symlink_to(self.root / "outside")
                elif modification == "hardlink":
                    os.link(self.target, self.root / "outside-link")
                elif modification == "fifo":
                    self.target.unlink()
                    os.mkfifo(self.target, mode=0o600)
                else:
                    self.target.chmod(0o644)
                before = self.state()
                with self.assertRaises(installer.InstallDenied):
                    self.plan()
                self.assertEqual(self.state(), before)

    def test_wrong_hash_stale_inode_and_missing_file_refused_before_mutation(self):
        self.install()
        plan = self.plan()
        before = self.state()
        for value in ("0"*64, "G"*64, "", None, True):
            with self.assertRaises(installer.InstallDenied):
                lifecycle.remove(str(self.prefix), value, "uninstall")
            self.assertEqual(self.state(), before)
        data = self.target.read_bytes()
        replacement = self.target.with_name("replacement")
        replacement.write_bytes(data)
        replacement.chmod(0o600)
        replacement.replace(self.target)
        before = self.state()
        with self.assertRaises(installer.InstallDenied):
            self.remove(plan=plan)
        self.assertEqual(self.state(), before)
        self.target.unlink()
        with self.assertRaises(installer.InstallDenied):
            self.plan()  # No removal journal: damage is not an approved uninstall.

    def test_recover_after_interruption_between_complete_files(self):
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
                self.install()
        self.assertTrue((self.prefix / lifecycle.INSTALL_JOURNAL).is_file())
        self.assertFalse((self.prefix / lifecycle.MANIFEST).exists())
        with self.assertRaises(installer.InstallDenied):
            self.plan()
        self.assertEqual(self.remove("recover")["status"], "RECOVERED")
        self.assertEqual(list(self.prefix.iterdir()), [])

    def test_partial_file_write_and_invalid_journal_require_manual_review(self):
        original = installer._write_new
        def broken(parent, name, data, mode):
            if name == "__init__.py":
                fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=parent)
                os.write(fd, data[:1])
                os.close(fd)
                raise OSError("SYNTHETIC short write failure")
            return original(parent, name, data, mode)
        with patch.object(installer, "_write_new", side_effect=broken):
            with self.assertRaises(installer.InstallDenied):
                self.install()
        before = self.state()
        with self.assertRaises(installer.InstallDenied):
            self.plan("recover")
        self.assertEqual(self.state(), before)
        (self.prefix / lifecycle.INSTALL_JOURNAL).write_text('{"schema":')
        with self.assertRaises(installer.InstallDenied):
            self.plan("recover")

    def test_publication_hardlink_cutpoint_and_launcher_refusal(self):
        original = os.unlink
        def interrupted(name, *args, **kwargs):
            if name == lifecycle.INSTALL_JOURNAL:
                raise Interrupted()
            return original(name, *args, **kwargs)
        with patch.object(installer.os, "unlink", side_effect=interrupted):
            with self.assertRaises(Interrupted):
                self.install()
        journal = self.prefix / lifecycle.INSTALL_JOURNAL
        final = self.prefix / lifecycle.MANIFEST
        self.assertEqual(journal.stat().st_ino, final.stat().st_ino)
        self.assertEqual(final.stat().st_nlink, 2)
        command = subprocess.run([str(self.prefix / "bin/ai-desktop"), "--version"],
                                 capture_output=True, timeout=5, check=False)
        self.assertEqual(command.returncode, 2)
        self.remove("recover")
        self.assertEqual(list(self.prefix.iterdir()), [])

    def test_external_receipt_hardlink_and_copied_root_binding_refused(self):
        self.install()
        receipt = self.prefix / lifecycle.MANIFEST
        alias = self.root / "receipt-alias"
        os.link(receipt, alias)
        with self.assertRaises(installer.InstallDenied):
            self.plan()
        alias.unlink()
        data = json.loads(receipt.read_bytes())
        data["root"][1] += 1
        receipt.write_text(json.dumps(data))
        with self.assertRaises(installer.InstallDenied):
            self.plan()

    def test_interrupted_uninstall_keeps_journal_and_requires_new_hash(self):
        self.install()
        plan = self.plan()
        original = os.unlink
        calls = 0
        def interrupted(name, *args, **kwargs):
            nonlocal calls
            result = original(name, *args, **kwargs)
            calls += 1
            if calls == 1:
                raise Interrupted()
            return result
        with patch.object(lifecycle.os, "unlink", side_effect=interrupted):
            with self.assertRaises(Interrupted):
                self.remove(plan=plan)
        self.assertTrue((self.prefix / lifecycle.REMOVE_JOURNAL).is_file())
        self.assertTrue((self.prefix / lifecycle.MANIFEST).is_file())
        with self.assertRaises(installer.InstallDenied):
            self.remove(plan=plan)
        self.remove()
        self.assertEqual(list(self.prefix.iterdir()), [])

    def test_marker_link_failure_never_deletes_payload(self):
        self.install()
        plan = self.plan()
        before = self.state()
        with patch.object(lifecycle.os, "link", side_effect=OSError("SYNTHETIC")):
            with self.assertRaises(installer.InstallDenied):
                self.remove(plan=plan)
        self.assertEqual(self.state(), before)

    def test_fsync_failure_after_marker_publication_is_resumable(self):
        self.install()
        plan = self.plan()
        with patch.object(lifecycle.os, "fsync", side_effect=OSError("SYNTHETIC")):
            with self.assertRaises(installer.InstallDenied):
                self.remove(plan=plan)
        self.assertTrue(self.target.is_file())
        self.assertTrue((self.prefix / lifecycle.REMOVE_JOURNAL).is_file())
        self.remove()
        self.assertEqual(list(self.prefix.iterdir()), [])

    def test_receipt_unlink_cutpoints_keep_retry_or_empty_prefix(self):
        for interrupted_name in (lifecycle.MANIFEST, lifecycle.REMOVE_JOURNAL):
            for after in (False, True):
                with self.subTest(name=interrupted_name, after=after):
                    self.install()
                    original = os.unlink
                    def interrupted(name, *args, **kwargs):
                        if name == interrupted_name and not after:
                            raise Interrupted()
                        result = original(name, *args, **kwargs)
                        if name == interrupted_name and after:
                            raise Interrupted()
                        return result
                    with patch.object(lifecycle.os, "unlink", side_effect=interrupted):
                        with self.assertRaises(Interrupted):
                            self.remove()
                    if list(self.prefix.iterdir()):
                        self.remove()
                    self.assertEqual(list(self.prefix.iterdir()), [])

    def test_legacy_v1_manifest_uninstall(self):
        self.install()
        (self.prefix / "bin/ai-desktop").write_bytes(installer._wrapper(str(self.prefix), legacy=True))
        legacy = installer._preview_for_files(PAYLOAD)
        (self.prefix / lifecycle.MANIFEST).write_text(json.dumps(legacy))
        self.remove()
        self.assertEqual(list(self.prefix.iterdir()), [])

    def test_receipt_duplicate_traversal_types_and_unknown_fields_refused(self):
        self.install()
        receipt = self.prefix / lifecycle.MANIFEST
        original = receipt.read_bytes()
        valid = json.loads(original)
        invalid = [b'{"schema":2,"schema":2}', b"x" * (lifecycle.MAX_RECEIPT + 1)]
        for change in ({"schema": True}, {"extra": "PRIVATE"}, {"prefix": "/tmp/other"},
                       {"files": [{"path": "lib/../../outside", "sha256": "0"*64, "size": 1, "mode": 0o600}]}):
            invalid.append(json.dumps({**valid, **change}).encode())
        for raw in invalid:
            receipt.write_bytes(raw)
            before = self.state()
            with self.assertRaises(installer.InstallDenied):
                self.plan()
            self.assertEqual(self.state(), before)
        receipt.write_bytes(original)
        self.remove()

    def test_cli_malformed_receipt_refuses_without_traceback_or_values(self):
        self.install()
        (self.prefix / lifecycle.MANIFEST).write_text("PRIVATE MALFORMED RECEIPT")
        command = subprocess.run([sys.executable, "-B", "-m", "ai_desktop.install_user",
                                  "uninstall-preview", str(self.prefix)],
                                 cwd=installer.SOURCE, env={"PATH": "/usr/bin"},
                                 capture_output=True, timeout=10, check=False)
        self.assertEqual(command.returncode, 2)
        self.assertEqual(command.stdout.strip(), b"INSTALL: REFUSED")
        self.assertEqual(command.stderr, b"")

    def test_private_prefix_root_refusal_and_cli_no_confirmation(self):
        self.install()
        with patch("ai_desktop.workspace_executor.os.geteuid", return_value=0):
            with self.assertRaises(installer.InstallDenied):
                self.plan()
        self.prefix.chmod(0o755)
        with self.assertRaises(installer.InstallDenied):
            self.plan()
        self.prefix.chmod(0o700)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(installer.main(["uninstall", str(self.prefix)]), 2)
            self.assertEqual(installer.main(["recover", str(self.prefix)]), 2)
            self.assertEqual(installer.main(["uninstall", str(self.prefix), "PRIVATE"]), 2)
        self.assertNotIn(str(self.prefix), output.getvalue())
        self.assertNotIn("PRIVATE", output.getvalue())
        self.remove()


class E7PolicyErrorTests(unittest.TestCase):
    def test_prefix_fstat_failure_closes_reserved_fd_and_refuses(self):
        from ai_desktop import install_policy
        with tempfile.TemporaryDirectory(prefix="aid-e7-fd-") as directory:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            # Internal fault injection only; no install or delete is performed.
            with patch.object(install_policy, "_root", return_value=fd), \
                 patch.object(install_policy.os, "fstat", side_effect=OSError("PRIVATE")):
                with self.assertRaises(installer.InstallDenied) as failure:
                    install_policy._prefix(directory)
            self.assertEqual(str(failure.exception), "INSTALL: REFUSED")
            with self.assertRaises(OSError):
                os.fstat(fd)


if __name__ == "__main__":
    unittest.main()
