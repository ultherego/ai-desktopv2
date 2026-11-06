"""Real temporary metadata, no native client, shell, credentials or network."""
from collections import Counter
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import system_clients as clients


def elf():
    return struct.pack("<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01" + b"\0" * 9,
                       3, 62, 1, 0, 0, 0, 0, 64, 56, 0, 64, 0, 0)


class SystemClientMetadataTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory(dir=Path.home()))
        self.root = Path(temporary)
        self.usr, self.opt = self.root / "usr", self.root / "opt"
        (self.usr / "bin").mkdir(parents=True)
        (self.usr / "lib").mkdir()
        self.opt.mkdir()
        for directory in (self.usr, self.usr / "bin", self.usr / "lib", self.opt):
            directory.chmod(0o755)
        self.path = self.usr / "bin/claude"
        self.enterContext(patch.object(clients, "_CANDIDATES", MappingProxyType({
            "claude": str(self.path), "codex": str(self.usr / "bin/codex")})))
        self.enterContext(patch.object(clients, "_TRUSTED_ROOTS", (str(self.usr), str(self.opt))))
        self.original_trusted = clients._trusted
        # Ordinary CI is unprivileged. Only ownership is represented as root;
        # real inode/mode/link/byte checks and the actual validator are retained.
        self.enterContext(patch.object(clients, "_trusted", self.trusted))

    def trusted(self, info):
        self.original_trusted(SimpleNamespace(st_mode=info.st_mode,
            st_uid=0 if info.st_uid == os.getuid() else info.st_uid))

    def executable(self, path=None, data=None):
        path = self.path if path is None else path
        path.write_bytes(elf() if data is None else data)
        path.chmod(0o755)
        return path

    def assert_result(self, status, detail):
        result = clients.inspect("claude")
        self.assertEqual((result.status, result.detail), (status, detail))
        if status != "present":
            self.assertIsNone(result.identity)
            self.assertFalse(result.recheck())
            self.assertEqual(result.path, str(self.path))
        return result

    def test_native_metadata_is_immutable_recheckable_and_not_vendor_verification(self):
        self.executable()
        result = self.assert_result("present", "vendor-unverified")
        info = self.path.stat()
        self.assertEqual(result.identity, clients.Fingerprint(info.st_dev, info.st_ino, info.st_size,
                                                            info.st_mtime_ns, info.st_ctime_ns))
        self.assertTrue(result.recheck())
        with self.assertRaises(FrozenInstanceError):
            result.status = "ready"
        with self.assertRaises(FrozenInstanceError):
            result.identity.ino = 0

    def test_absolute_relative_and_directory_symlinks_under_trusted_roots(self):
        target = self.executable(self.opt / "claude-native")
        (self.usr / "lib/native").symlink_to(target)
        self.path.symlink_to("../lib/native")
        result = clients.inspect("claude")
        self.assertEqual((result.status, result.path), ("present", str(target)))
        self.assertTrue(result.recheck())
        self.path.unlink()
        (self.usr / "lib/tools").symlink_to(self.opt, target_is_directory=True)
        self.path.symlink_to("../lib/tools/claude-native")
        self.assertEqual(clients.inspect("claude").path, str(target))

    def test_path_and_home_are_ignored_and_inspection_never_executes_or_connects(self):
        self.executable()
        with patch.dict(os.environ, {"PATH": str(self.root), "HOME": "/untrusted"}), \
                patch("shutil.which", side_effect=AssertionError("PATH search")), \
                patch("pwd.getpwuid", side_effect=AssertionError("HOME lookup")), \
                patch("subprocess.run", side_effect=AssertionError("execution")), \
                patch("subprocess.Popen", side_effect=AssertionError("execution")), \
                patch.object(socket, "socket", side_effect=AssertionError("network")):
            self.assert_result("present", "vendor-unverified")
            self.assertEqual(clients.inspect("codex").status, "missing")

    def test_missing_and_inaccessible_have_fixed_redacted_details(self):
        self.assert_result("missing", "missing")
        with patch.object(clients.os, "open", side_effect=PermissionError("PRIVATE PATH")):
            result = self.assert_result("unavailable", "metadata-unavailable")
        self.assertNotIn("PRIVATE", repr(result))

    def test_owner_permissions_special_modes_and_hardlinks_are_refused(self):
        self.executable()
        for mode, detail in ((0o775, "permissions"), (0o757, "permissions"),
                             (0o4755, "special-mode"), (0o2755, "special-mode")):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                self.assert_result("unsafe", detail)
        self.path.chmod(0o755)
        for path in (self.path, self.usr / "bin"):
            inode = path.stat().st_ino
            def wrong_owner(info):
                if info.st_ino == inode:
                    self.original_trusted(SimpleNamespace(st_mode=info.st_mode, st_uid=999999))
                else:
                    self.trusted(info)
            with self.subTest(path=path.name), patch.object(clients, "_trusted", wrong_owner):
                self.assert_result("unsafe", "owner")
        (self.usr / "bin").chmod(0o777)
        self.assert_result("unsafe", "permissions")
        (self.usr / "bin").chmod(0o755)
        os.link(self.path, self.usr / "bin/other")
        self.assert_result("unsafe", "hardlink")

    def test_wrappers_wrong_arch_truncated_elf_and_nonexecutable_files_are_refused(self):
        for data in (b"#!/usr/bin/node\n", b"\x7fELF", elf()[:18] + b"\xb7\0" + elf()[20:],
                     elf()[:4] + b"\1" + elf()[5:]):
            with self.subTest(data=data[:20]):
                self.executable(data=data)
                self.assert_result("unsupported", "not-native-elf")
        self.executable().chmod(0o644)
        self.assert_result("unsupported", "not-executable")

    def test_fifo_socket_and_directory_are_rejected_without_opening_them(self):
        real_open = os.open
        def guarded_open(path, *args, **kwargs):
            self.assertNotEqual(path, "claude", "special file must not be opened")
            return real_open(path, *args, **kwargs)
        os.mkfifo(self.path, 0o600)
        with patch.object(clients.os, "open", side_effect=guarded_open):
            self.assert_result("unsupported", "not-regular")
        self.path.unlink()
        # A socket inode is sufficient: never create a listener or connection.
        os.mknod(self.path, stat.S_IFSOCK | 0o600)
        with patch.object(clients.os, "open", side_effect=guarded_open):
            self.assert_result("unsupported", "not-regular")
        self.path.unlink()
        self.path.mkdir(mode=0o755)
        with patch.object(clients.os, "open", side_effect=guarded_open):
            self.assert_result("unsupported", "not-regular")

    def test_link_ownership_outside_roots_and_noncanonical_targets_are_refused(self):
        outside = self.executable(self.root / "private-client")
        for target in (str(outside), str(self.usr) + "-other/client", "../../private-client",
                       "../lib/a/../client", str(self.opt / "client") + "/", str(self.opt / "client") + "/."):
            with self.subTest(target=target):
                self.path.symlink_to(target)
                self.assert_result("unsafe", "symlink-target")
                self.path.unlink()
        self.path.symlink_to(self.executable(self.opt / "client"))
        def wrong_link_owner(info):
            if stat.S_ISLNK(info.st_mode):
                self.original_trusted(SimpleNamespace(st_mode=info.st_mode, st_uid=999999))
            else:
                self.trusted(info)
        with patch.object(clients, "_trusted", wrong_link_owner):
            self.assert_result("unsafe", "owner")

    def test_symlink_loops_and_path_lengths_are_bounded(self):
        self.path.symlink_to("loop")
        (self.usr / "bin/loop").symlink_to("claude")
        self.assert_result("unsafe", "symlink-limit")
        with patch.object(clients.os, "readlink", return_value="/usr/" + "x" * clients.MAX_PATH_BYTES):
            self.assert_result("unsafe", "symlink-target")

    def test_updates_and_replaced_ancestors_invalidate_saved_identity(self):
        self.executable()
        original = clients.inspect("claude")
        self.path.write_bytes(elf() + b"updated")
        self.assertFalse(original.recheck())
        original = clients.inspect("claude")
        old = self.usr / "old-bin"
        (self.usr / "bin").rename(old)
        (self.usr / "bin").mkdir(mode=0o755)
        self.executable()
        self.assertFalse(original.recheck())

    def test_unrelated_directory_contents_do_not_invalidate_identity(self):
        self.executable()
        original = clients.inspect("claude")
        (self.usr / "another-package").mkdir()
        (self.usr / "bin/another-tool").write_bytes(b"unrelated")
        self.assertTrue(original.recheck())

    def test_file_update_during_read_and_parent_swap_during_second_pass_are_refused(self):
        self.executable()
        real_read = os.read
        def changed_file(fd, size):
            data = real_read(fd, size)
            self.path.write_bytes(elf() + b"changed")
            return data
        with patch.object(clients.os, "read", side_effect=changed_file):
            self.assert_result("changed", "metadata-changed")
        self.executable()
        reads = 0
        def changed_parent(fd, size):
            nonlocal reads
            data = real_read(fd, size)
            reads += 1
            if reads == 2:
                (self.usr / "bin").rename(self.usr / "old-bin")
                (self.usr / "bin").mkdir(mode=0o755)
                self.executable()
            return data
        with patch.object(clients.os, "read", side_effect=changed_parent):
            self.assert_result("changed", "metadata-changed")

    def test_replacing_regular_file_with_fifo_before_open_cannot_block(self):
        self.executable()
        real_open = os.open
        def replace(path, flags, *args, **kwargs):
            if path == "claude":
                self.assertTrue(flags & os.O_NONBLOCK)
                self.path.unlink()
                os.mkfifo(self.path, 0o600)
            return real_open(path, flags, *args, **kwargs)
        with patch.object(clients.os, "open", side_effect=replace):
            self.assert_result("changed", "metadata-changed")

    def test_all_opened_descriptors_close_on_success_and_refusal(self):
        real_open, real_close = os.open, os.close
        for data in (elf(), b"invalid"):
            self.executable(data=data)
            opened, closed = [], []
            def opening(*args, **kwargs):
                descriptor = real_open(*args, **kwargs)
                opened.append(descriptor)
                return descriptor
            def closing(descriptor):
                closed.append(descriptor)
                real_close(descriptor)
            with self.subTest(data=data[:4]), patch.object(clients.os, "open", side_effect=opening), \
                    patch.object(clients.os, "close", side_effect=closing):
                clients.inspect("claude")
            self.assertTrue(opened)
            self.assertEqual(Counter(opened), Counter(closed))


class SystemClientVersionTests(unittest.TestCase):
    def test_tested_baseline_older_and_future_are_distinct(self):
        for provider, native, old, future in (
            ("claude", "2.1.312 (Claude Code)", "2.1.311 (Claude Code)", "2.1.313 (Claude Code)"),
            ("codex", "codex-cli 0.166.0", "codex-cli 0.165.0", "codex-cli 1.0.0"),
        ):
            for output, status in ((native, "tested"), (old, "unsupported"), (future, "untested")):
                with self.subTest(provider=provider, output=output):
                    result = clients.compatibility(provider, output)
                    self.assertEqual(result.status, status)
                    self.assertIsNotNone(result.version)
                    self.assertEqual(clients.compatibility(provider, (output + "\n").encode()), result)
                    with self.assertRaises(FrozenInstanceError):
                        result.status = "tested"

    def test_malformed_or_unbounded_output_is_rejected_without_echo(self):
        for output in (None, True, 1, [], {}, "", b"", "PRIVATE\n2.1.312 (Claude Code)",
                       "2.1.312 (Claude Code)\nPRIVATE", "2.1.312-beta (Claude Code)",
                       "02.1.312 (Claude Code)", "codex-cli 0.166.0", "\ud800", b"\xff",
                       "x" * (1024 * 1024), b"x" * (1024 * 1024)):
            with self.subTest(kind=type(output).__name__):
                result = clients.compatibility("claude", output)
                self.assertEqual((result.status, result.version, result.detail),
                                 ("unsupported", None, "invalid-version-output"))
                self.assertNotIn("PRIVATE", repr(result))

    def test_unknown_or_malformed_provider_is_refused_before_filesystem_work(self):
        with patch.object(clients.os, "open", side_effect=AssertionError("filesystem")):
            for provider in (None, True, [], "", "local", "../claude", "Claude"):
                with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "provider"):
                    clients.inspect(provider)
                with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "provider"):
                    clients.compatibility(provider, "2.1.312 (Claude Code)")


if __name__ == "__main__":
    unittest.main()
