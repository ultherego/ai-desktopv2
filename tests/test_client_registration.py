"""Explicit hash-bound publication, update and removal; synthetic bytes only."""
import errno
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import client_registration as registration, client_sources as sources
from test_client_sources import elf


class ClientRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path.home())))
        self.cache = self.root / "registry"
        self.source = self.root / "installed/claude"
        self.source.parent.mkdir()
        self.source.write_bytes(elf(b"first")); self.source.chmod(0o755)
        self.uid = os.getuid() or 1001
        self.enterContext(patch.object(registration, "CACHE", self.cache))
        # CI runs without root: represent the fixture owner as the root cache
        # owner, but exercise real descriptor traversal, modes, files and rename.
        trusted = registration._trusted
        def cache_owner(info, **kwargs):
            return trusted(SimpleNamespace(st_mode=info.st_mode, st_uid=0 if info.st_uid in (0, os.getuid()) else info.st_uid,
                                           st_nlink=info.st_nlink), **kwargs)
        self.enterContext(patch.object(registration, "_trusted", cache_owner))
        source_trust = sources._trusted
        self.enterContext(patch.object(sources, "_trusted", lambda info, uid: source_trust(info, os.getuid())))

    def digest(self, provider="claude", source=None):
        return sources.capture(provider, str(source or self.source), uid=self.uid).digest

    def register(self, provider="claude", source=None):
        path = source or self.source
        return registration.apply("register", provider, source=str(path), expected_digest=self.digest(provider, path), uid=self.uid)

    def test_registration_copies_exact_bytes_readonly_and_preserves_original(self):
        before = self.source.read_bytes()
        result = self.register()
        native = self.cache / "claude/payload/bin/claude"
        self.assertEqual(result["status"], "registered")
        self.assertEqual(native.read_bytes(), before)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(native.stat().st_mode), 0o555)
        manifest = self.cache / "claude/manifest.json"
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o644)
        self.assertFalse((self.cache / "claude/payload/manifest.json").exists())
        status = registration.registration_status("claude", uid=self.uid)
        self.assertEqual(status["path"], str(self.source))
        self.assertEqual(status["digest"], result["digest"])
        self.assertEqual(set(self.cache.iterdir()), {self.cache / ".lock", self.cache / "claude"})

    def test_private_umask_does_not_make_registered_payload_inaccessible(self):
        previous = os.umask(0o077)
        try:
            self.register()
        finally:
            os.umask(previous)
        for path in (self.cache, self.cache / "claude", self.cache / "claude/payload", self.cache / "claude/payload/bin"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    def test_registration_manifest_hides_other_users_source_path(self):
        self.register()
        result = registration.registration_status("claude", uid=self.uid + 1)
        self.assertEqual(result["status"], "registered")
        self.assertIsNone(result["path"])
        self.assertEqual(result["source_uid"], self.uid)

    def test_register_never_implicitly_replaces_existing_copy(self):
        first = self.register()
        self.source.write_bytes(elf(b"new"))
        with self.assertRaisesRegex(registration.RegistrationError, "already-registered"):
            self.register()
        self.assertEqual(registration.registration_status("claude", uid=self.uid)["digest"], first["digest"])
        self.assertEqual((self.cache / "claude/payload/bin/claude").read_bytes(), elf(b"first"))

    def test_refresh_requires_both_reviewed_source_and_previous_registration(self):
        first = self.register()
        self.source.write_bytes(elf(b"second"))
        updated = self.digest()
        for old, expected, reason in (("0" * 64, updated, "registration-changed"),
                                      (first["digest"], "0" * 64, "source-changed")):
            with self.subTest(reason=reason), self.assertRaisesRegex(registration.RegistrationError, reason):
                registration.apply("refresh", "claude", source=str(self.source), expected_digest=expected,
                                   previous_digest=old, uid=self.uid)
            self.assertEqual((self.cache / "claude/payload/bin/claude").read_bytes(), elf(b"first"))
        result = registration.apply("refresh", "claude", source=str(self.source), expected_digest=updated,
                                    previous_digest=first["digest"], uid=self.uid)
        self.assertEqual(result["digest"], updated)
        self.assertEqual((self.cache / "claude/payload/bin/claude").read_bytes(), elf(b"second"))
        self.assertEqual(set(self.cache.iterdir()), {self.cache / ".lock", self.cache / "claude"})

    def test_curl_alias_retarget_refresh_uses_new_release_and_preserves_alias(self):
        releases = self.root / ".codex/packages/standalone/releases"
        for version in ("one", "two"):
            release = releases / version
            (release / "bin").mkdir(parents=True)
            (release / "bin/codex").write_bytes(elf(version.encode()))
            (release / "bin/codex").chmod(0o755)
            (release / "codex-package.json").write_text('{"fixture":true}')
        current = releases.parent / "current"
        current.symlink_to("releases/one")
        source = self.root / "bin/codex"
        source.parent.mkdir(); source.symlink_to(current / "bin/codex")
        first = self.register("codex", source)
        current.unlink(); current.symlink_to("releases/two")
        saved = registration.registration_status("codex", uid=self.uid)
        self.assertEqual(saved["path"], str(source))
        self.assertEqual(saved["status"], "registered")
        new = sources.discover("codex", saved["path"], uid=self.uid)
        self.assertNotEqual(new.digest, first["digest"])
        registration.apply("refresh", "codex", source=saved["path"], expected_digest=new.digest,
                           previous_digest=first["digest"], uid=self.uid)
        self.assertEqual((self.cache / "codex/payload/bin/codex").read_bytes(), elf(b"two"))
        self.assertTrue(current.is_symlink())
        self.assertEqual(registration.registration_status("codex", uid=self.uid)["path"], str(source))

    def test_registered_codex_keeps_new_helpers_in_complete_program_tree(self):
        release = self.root / "release"
        (release / "bin").mkdir(parents=True)
        native = release / "bin/codex"
        native.write_bytes(elf()); native.chmod(0o755)
        (release / "codex-package.json").write_text('{"fixture":true}')
        helper = release / "bin/upstream-new-helper"
        helper.write_bytes(b"helper"); helper.chmod(0o755)
        self.register("codex", native)
        cached = self.cache / "codex/payload/bin/upstream-new-helper"
        self.assertEqual(cached.read_bytes(), b"helper")
        self.assertEqual(stat.S_IMODE(cached.stat().st_mode), 0o555)

    def test_publication_failure_keeps_previous_copy_and_cleans_staging(self):
        first = self.register()
        self.source.write_bytes(elf(b"new"))
        with patch.object(registration, "_rename", side_effect=OSError(errno.EIO, "SECRET")), \
                self.assertRaisesRegex(registration.RegistrationError, "storage"):
            registration.apply("refresh", "claude", source=str(self.source), expected_digest=self.digest(),
                               previous_digest=first["digest"], uid=self.uid)
        self.assertEqual((self.cache / "claude/payload/bin/claude").read_bytes(), elf(b"first"))
        self.assertEqual(set(self.cache.iterdir()), {self.cache / ".lock", self.cache / "claude"})

    def test_post_publication_cleanup_failure_reports_committed_state(self):
        first = self.register()
        self.source.write_bytes(elf(b"new"))
        with patch.object(registration, "_remove", side_effect=registration.RegistrationError("cleanup-incomplete")), \
                self.assertRaisesRegex(registration.RegistrationError, "published-cleanup-incomplete"):
            registration.apply("refresh", "claude", source=str(self.source), expected_digest=self.digest(),
                               previous_digest=first["digest"], uid=self.uid)
        self.assertEqual((self.cache / "claude/payload/bin/claude").read_bytes(), elf(b"new"))
        self.assertEqual(registration.registration_status("claude", uid=self.uid)["status"], "registered")

    def test_damaged_cache_cleanup_stops_at_entry_budget(self):
        damaged = self.root / "damaged"
        damaged.mkdir()
        for index in range(20):
            (damaged / str(index)).write_bytes(b"fixture")
        fd = os.open(self.root, registration._DIR)
        try:
            with patch.object(sources, "MAX_ENTRIES", 1), self.assertRaisesRegex(registration.RegistrationError, "cleanup-incomplete"):
                registration._remove(fd, damaged.name)
        finally:
            os.close(fd)
        self.assertEqual(len(list(damaged.iterdir())), 12)

    def test_source_changed_after_review_never_publishes(self):
        approved = self.digest()
        self.source.write_bytes(elf(b"changed"))
        with self.assertRaisesRegex(registration.RegistrationError, "source-changed"):
            registration.apply("register", "claude", source=str(self.source), expected_digest=approved, uid=self.uid)
        self.assertFalse((self.cache / "claude").exists())
        self.assertEqual(set(self.cache.iterdir()), {self.cache / ".lock"})

    def test_root_copy_checks_bytes_a_second_time_after_source_hash(self):
        original = registration._writer
        def changing_writer(payload):
            write = original(payload)
            def changed(entry, descriptor):
                if entry.kind == "file":
                    self.source.write_bytes(elf(b"malicious"))
                return write(entry, descriptor)
            return changed
        with patch.object(registration, "_writer", changing_writer), self.assertRaises(registration.RegistrationError):
            self.register()
        self.assertFalse((self.cache / "claude").exists())

    def test_remove_has_cas_guard_and_never_follows_payload_symlinks(self):
        first = self.register()
        sentinel = self.root / "outside"
        sentinel.write_bytes(b"PRESERVE")
        (self.cache / "claude/payload/escape").symlink_to(sentinel)
        with self.assertRaisesRegex(registration.RegistrationError, "registration-changed"):
            registration.apply("remove", "claude", expected_digest="0" * 64, uid=self.uid)
        result = registration.apply("remove", "claude", expected_digest=first["digest"], uid=self.uid)
        self.assertEqual(result["status"], "removed")
        self.assertEqual(sentinel.read_bytes(), b"PRESERVE")
        self.assertEqual(self.source.read_bytes(), elf(b"first"))
        self.assertEqual(registration.registration_status("claude", uid=self.uid)["status"], "missing")

    def test_corrupt_or_symlinked_cache_cannot_be_overwritten(self):
        self.cache.mkdir()
        (self.cache / "claude").mkdir()
        self.assertEqual(registration.registration_status("claude", uid=self.uid)["status"], "unsafe")
        with self.assertRaisesRegex(registration.RegistrationError, "unsafe-cache"):
            self.register()
        (self.cache / "claude").rmdir()
        (self.cache / "claude").symlink_to(self.source.parent)
        with self.assertRaises((registration.RegistrationError, OSError)):
            self.register()
        self.assertEqual(self.source.read_bytes(), elf(b"first"))

    def test_unsafe_cache_permissions_and_manifest_duplicates_are_refused(self):
        self.register()
        self.cache.chmod(0o777)
        self.assertEqual(registration.registration_status("claude", uid=self.uid)["status"], "unsafe")
        self.cache.chmod(0o755)
        manifest = self.cache / "claude/manifest.json"
        manifest.write_text('{"schema":1,"schema":1}')
        self.assertEqual(registration.registration_status("claude", uid=self.uid)["status"], "unsafe")

    def test_nonblocking_lock_refuses_parallel_registration(self):
        self.register()
        import fcntl
        fd = os.open(self.cache / ".lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(registration.RegistrationError, "busy"):
                self.register()
        finally:
            os.close(fd)

    def test_admin_requires_root_sudo_caller_tty_and_explicit_flag_before_writes(self):
        arguments = ["register", "--provider", "claude", "--source", str(self.source),
                     "--expected-digest", self.digest()]
        for uid, sudo_uid, tty, flag in ((1001, "1001", True, True), (0, "", True, True),
                                        (0, "0", True, True), (0, "1001", False, True),
                                        (0, "1001", True, False), (0, "9999999999", True, True)):
            output = io.StringIO()
            with patch.object(registration.os, "getuid", return_value=uid), \
                    patch.object(registration.os, "geteuid", return_value=uid), \
                    patch.object(registration.os, "isatty", return_value=tty), \
                    patch.dict(os.environ, {"SUDO_UID": sudo_uid}), patch.object(registration, "apply") as apply, \
                    patch("sys.stdout", output):
                code = registration.admin_main((["--allow-client-registration"] if flag else []) + arguments)
            self.assertEqual(code, 2)
            apply.assert_not_called()
            self.assertNotIn(str(self.source), output.getvalue())

    def test_admin_success_uses_original_authenticated_uid_and_redacts_failures(self):
        args = ["--allow-client-registration", "register", "--provider", "claude", "--source", str(self.source),
                "--expected-digest", self.digest()]
        with patch.object(registration.os, "getuid", return_value=0), \
                patch.object(registration.os, "geteuid", return_value=0), \
                patch.object(registration.os, "isatty", return_value=True), \
                patch.dict(os.environ, {"SUDO_UID": "1001"}), \
                patch.object(registration.pwd, "getpwuid", return_value=SimpleNamespace(pw_uid=1001, pw_name="fixture")), \
                patch.object(registration, "apply", return_value={"provider": "claude", "status": "registered"}) as apply, \
                patch("sys.stdout", io.StringIO()):
            self.assertEqual(registration.admin_main(args), 0)
        self.assertEqual(apply.call_args.kwargs["uid"], 1001)


if __name__ == "__main__":
    unittest.main()
