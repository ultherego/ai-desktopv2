"""Sealed payload and update-race tests; no client is ever executed."""
import errno
import fcntl
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import client_binding as binding, system_clients as clients
from ai_desktop.analysis_contract import AnalysisDenied


def elf():
    return struct.pack("<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01" + b"\0" * 9,
                       3, 62, 1, 0, 0, 0, 0, 64, 56, 0, 64, 0, 0)


class ClientBindingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path.home())))
        self.usr, self.opt = self.root / "usr", self.root / "opt"
        (self.usr / "bin").mkdir(parents=True)
        (self.usr / "lib").mkdir()
        self.opt.mkdir()
        self.native = self.usr / "bin/codex"
        self.payload = self.usr / "lib/openai-codex"
        self.cache = self.opt / "registered"
        self.enterContext(patch.object(clients, "_CANDIDATES", {
            "claude": str(self.usr / "bin/claude"), "codex": str(self.native)}))
        self.enterContext(patch.object(clients, "_NATIVE_ALTERNATES", {
            "claude": str(self.opt / "claude-code/bin/claude")}))
        self.enterContext(patch.object(clients, "_REGISTERED_ROOTS", {
            provider: str(self.cache / provider) for provider in ("claude", "codex")}))
        self.enterContext(patch.object(clients, "_PACKAGE_ROOTS", {
            "claude": (str(self.opt / "claude-code"), str(self.cache / "claude/payload")),
            "codex": (str(self.payload), str(self.cache / "codex/payload"))}))
        self.enterContext(patch.object(clients, "_TRUSTED_ROOTS", (str(self.usr), str(self.opt))))
        trusted = clients._trusted
        self.enterContext(patch.object(clients, "_trusted", lambda info: trusted(SimpleNamespace(
            st_mode=info.st_mode, st_uid=0 if info.st_uid == os.getuid() else info.st_uid))))
        self.write(self.native, elf(), 0o755)

    def write(self, path, data=b"resource", mode=0o644):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def package(self):
        native = self.write(self.payload / "bin/codex", elf(), 0o755)
        self.native.unlink()
        self.native.symlink_to(native)
        self.write(self.payload / "codex-package.json", b'{"fixture":true}')
        self.write(self.payload / "codex-path/rg", b"helper", 0o755)
        (self.payload / "resource-link").symlink_to("codex-package.json")
        return native

    def denied(self, expected, operation):
        with self.assertRaises(AnalysisDenied) as raised:
            operation()
        self.assertEqual(raised.exception.reason, expected)

    def test_flat_native_snapshot_is_sealed_reused_readonly_and_closed(self):
        with binding.bind("codex") as bound:
            self.assertEqual(bound.binary, binding.binary("codex"))
            descriptors = bound.pass_fds
            self.assertEqual(len(descriptors), 1)
            fd = descriptors[0]
            self.assertFalse(os.get_inheritable(fd))
            self.assertEqual(os.pread(fd, 100, 0), elf())
            self.assertEqual(fcntl.fcntl(fd, binding._GET_SEALS), binding._SEALS)
            for operation in (lambda: os.write(fd, b"x"), lambda: os.ftruncate(fd, 0)):
                with self.assertRaises(OSError):
                    operation()
            first = bound.mount_args()
            self.assertEqual(first, bound.mount_args())
            self.assertIn(str(fd), first)
            self.assertIn("--file", first)
            self.assertNotIn("--ro-bind", first)
            self.assertFalse(any(argument.startswith("/proc/self/fd/") for argument in first))
            self.assertIn(binding.INNER_ROOT + "/payload/codex", first)
            self.assertNotIn(str(self.native), first)
            self.assertNotIn("--preserve-fds", first)
        with self.assertRaises(OSError):
            os.fstat(fd)
        self.denied("client-changed", bound.mount_args)
        self.denied("client-changed", lambda: bound.pass_fds)

    def test_complete_private_layout_preserves_resources_symlinks_and_executable_location(self):
        native = self.package()
        with binding.bind("codex") as bound:
            args = bound.mount_args()
            self.assertIn(str(self.payload), args)
            self.assertIn(str(native), args)
            self.assertIn(str(self.payload / "codex-path/rg"), args)
            self.assertIn(str(self.payload / "resource-link"), args)
            self.assertIn("--tmpfs", args)
            self.assertEqual(len(bound.pass_fds), 3)
            self.assertEqual({stat.S_IMODE(os.fstat(fd).st_mode) for fd in bound.pass_fds}, {0o444, 0o555})
            self.assertIn(b'{"fixture":true}', {os.pread(fd, 1000, 0) for fd in bound.pass_fds})

    def test_linked_tmpfs_copy_rewinds_every_payload_for_repeated_sequential_phases(self):
        self.package()
        with binding.bind("codex") as bound:
            copies = []
            previous = None
            for phase in range(2):
                args = bound.mount_args()
                if previous is not None:
                    self.assertEqual(args, previous)
                previous = args
                copied = {}
                last_file = 0
                for index, value in enumerate(args):
                    if value != "--file":
                        continue
                    self.assertEqual(args[index - 2], "--perms")
                    mode = int(args[index - 1], 8)
                    descriptor, destination = int(args[index + 1]), args[index + 2]
                    self.assertIn(descriptor, bound.pass_fds)
                    self.assertEqual(os.lseek(descriptor, 0, os.SEEK_CUR), 0)
                    data = os.read(descriptor, 4096)
                    self.assertTrue(data)
                    self.assertEqual(os.read(descriptor, 1), b"")
                    self.assertEqual(mode, 0o555 if destination.endswith(("/codex", "/rg")) else 0o444)
                    copied[destination] = data
                    last_file = index
                self.assertEqual(len(copied), 3)
                self.assertGreater(args.index("--remount-ro"), last_file)
                self.assertEqual(args[-2:], ["--remount-ro", binding.INNER_ROOT])
                copies.append(copied)
            self.assertEqual(copies[0], copies[1])

    def test_registered_payload_binds_native_and_resources_without_source_manifest(self):
        self.package()
        native = self.write(self.cache / "codex/payload/bin/codex", elf(), 0o755)
        self.write(self.cache / "codex/payload/codex-package.json", b"imported resources")
        future_helper = self.write(self.cache / "codex/payload/bin/new-upstream-helper", b"future helper", 0o755)
        manifest_bytes = json.dumps({"schema": 1, "provider": "codex", "source_uid": 1001,
            "source": "/home/synthetic/PRIVATE-SOURCE-LOCATION", "layout": "codex-bundle",
            "digest": "1" * 64, "bytes": 95, "files_count": 3}).encode()
        manifest = self.write(self.cache / "codex/manifest.json", manifest_bytes)
        with binding.bind("codex") as bound:
            args = bound.mount_args()
            self.assertIn(str(native), args)
            self.assertIn(str(future_helper), args)
            self.assertNotIn(str(manifest), args)
            self.assertNotIn(str(self.native), args)
            self.assertNotIn(str(self.payload), args)
            self.assertNotIn(manifest_bytes, {os.pread(fd, 1000, 0) for fd in bound.pass_fds})
            self.assertIn(b"future helper", {os.pread(fd, 1000, 0) for fd in bound.pass_fds})
            (self.payload / "codex-package.json").chmod(0o777)
            bound.recheck()  # The overridden system installation is not used.
            old = self.cache / "old-codex"
            (self.cache / "codex").rename(old)
            self.write(native, elf(), 0o755)
            self.denied("client-changed", bound.recheck)

    def test_source_update_is_refused_but_already_bound_bytes_stay_unchanged(self):
        with binding.bind("codex") as bound:
            fd = bound.pass_fds[0]
            args = bound.mount_args()
            self.write(self.native, elf() + b"replacement", 0o755)
            self.assertEqual(os.pread(fd, 1000, 0), elf())
            self.assertIn(str(fd), args)
            self.denied("client-changed", bound.recheck)
            self.denied("client-changed", bound.mount_args)

    def test_resource_change_while_snapshot_is_read_refuses_before_yield(self):
        self.package()
        resource = self.payload / "codex-package.json"
        inode = resource.stat().st_ino
        real_read = os.read
        changed = False
        def changing_read(fd, size):
            nonlocal changed
            data = real_read(fd, size)
            if not changed and os.fstat(fd).st_ino == inode:
                changed = True
                resource.write_bytes(b"changed while snapshotting")
            return data
        with patch.object(binding.os, "read", side_effect=changing_read):
            self.denied("client-changed", lambda: binding.bind("codex").__enter__())
        self.assertTrue(changed)

    def test_package_resource_replacement_addition_removal_and_link_retarget_are_refused(self):
        self.package()
        future_helper = self.write(self.payload / "bin/new-upstream-helper", b"future helper", 0o755)
        for change in (
            lambda: self.write(self.payload / "codex-package.json", b"updated"),
            lambda: self.write(future_helper, b"updated helper", 0o755),
            lambda: self.write(self.payload / "new-file", b"added"),
            lambda: (self.payload / "codex-path/rg").unlink(),
            lambda: ((self.payload / "resource-link").unlink(),
                     (self.payload / "resource-link").symlink_to("bin/codex")),
        ):
            with binding.bind("codex") as bound:
                change()
                self.denied("client-changed", bound.recheck)

    def test_new_package_root_and_replaced_ancestor_are_refused(self):
        with binding.bind("codex") as bound:
            self.payload.mkdir()
            self.denied("client-changed", bound.recheck)
        with binding.bind("codex") as bound:
            (self.usr / "bin").rename(self.usr / "old-bin")
            self.write(self.native, elf(), 0o755)
            self.denied("client-changed", bound.recheck)

    def test_missing_unsafe_unknown_layout_and_special_resource_refusals(self):
        self.native.unlink()
        self.denied("client-missing", lambda: binding.bind("codex").__enter__())
        self.write(self.native, elf(), 0o777)
        self.denied("client-unsafe", lambda: binding.bind("codex").__enter__())
        self.native.unlink()
        self.native.symlink_to(self.write(self.opt / "unknown-package", elf(), 0o755))
        self.denied("client-layout", lambda: binding.bind("codex").__enter__())
        self.native.unlink()
        self.write(self.native, elf(), 0o755)
        self.payload.mkdir()
        os.mkfifo(self.payload / "fifo", 0o600)
        self.denied("client-layout", lambda: binding.bind("codex").__enter__())

    def test_package_outside_links_dangling_links_and_loops_are_refused(self):
        self.payload.mkdir()
        link = self.payload / "link"
        for target in (str(self.native), "missing", "link"):
            link.symlink_to(target)
            self.denied("client-layout", lambda: binding.bind("codex").__enter__())
            link.unlink()

    def test_bounded_file_total_entry_and_depth_limits(self):
        self.package()
        for name, ceiling in (("MAX_FILE_BYTES", 63), ("MAX_TOTAL_BYTES", 64),
                              ("MAX_ENTRIES", 1), ("MAX_DEPTH", 0)):
            with self.subTest(name=name), patch.object(binding, name, ceiling):
                self.denied("client-layout", lambda: binding.bind("codex").__enter__())

    def test_directory_enumeration_stops_at_remaining_entry_budget(self):
        self.payload.mkdir()
        yielded = []
        class Scan:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                for name in ("first", "second"):
                    yielded.append(name)
                    yield SimpleNamespace(name=name)
                raise AssertionError("directory iterator exceeded its entry budget")
        with patch.object(binding, "MAX_ENTRIES", 2), patch.object(binding.os, "scandir", return_value=Scan()):
            self.denied("client-layout", lambda: binding.bind("codex").__enter__())
        self.assertEqual(yielded, ["first", "second"])

    def test_partial_snapshot_failure_closes_all_memfds(self):
        self.package()
        real_create, real_write = os.memfd_create, os.write
        opened = []
        def create(*args, **kwargs):
            fd = real_create(*args, **kwargs)
            opened.append(fd)
            return fd
        def write(fd, data):
            if len(opened) == 2:
                raise OSError(errno.ENOSPC, "synthetic full")
            return real_write(fd, data)
        with patch.object(binding.os, "memfd_create", side_effect=create), \
                patch.object(binding.os, "write", side_effect=write):
            self.denied("client-unavailable", lambda: binding.bind("codex").__enter__())
        self.assertGreaterEqual(len(opened), 2)
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_failed_sealing_and_prohibited_executable_memfds_refuse_without_fallback(self):
        for operation in (patch.object(binding.fcntl, "fcntl", side_effect=OSError(errno.EPERM, "seal")),
                          patch.object(binding.os, "memfd_create", side_effect=OSError(errno.EPERM, "noexec"))):
            with operation:
                self.denied("client-unavailable", lambda: binding.bind("codex").__enter__())

    def test_legacy_kernel_exec_flag_fallback_is_limited_to_einval(self):
        create = os.memfd_create
        calls = []
        def old_kernel(name, flags):
            calls.append(flags)
            if flags & 0x10:
                raise OSError(errno.EINVAL, "old kernel")
            return create(name, flags)
        with patch.object(binding.os, "memfd_create", side_effect=old_kernel):
            with binding.bind("codex") as bound:
                self.assertTrue(bound.pass_fds)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0] & 0x10)
        self.assertFalse(calls[1] & 0x10)

    def test_snapshot_ignores_environment_and_never_executes_or_connects(self):
        with patch.dict(os.environ, {"PATH": "/untrusted", "HOME": "/private"}), \
                patch("subprocess.Popen", side_effect=AssertionError("execution")), \
                patch("subprocess.run", side_effect=AssertionError("execution")), \
                patch.object(socket, "socket", side_effect=AssertionError("network")):
            with binding.bind("codex") as bound:
                self.assertTrue(bound.mount_args())

    def test_caller_exception_is_not_reclassified_and_handles_are_closed(self):
        with self.assertRaisesRegex(ValueError, "caller"):
            with binding.bind("codex") as bound:
                fd = bound.pass_fds[0]
                raise ValueError("caller")
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_kernel_fixture_requires_explicit_flag_and_disposable_runner(self):
        import ci_client_binding as fixture
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(fixture.subprocess, "run") as launch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fixture.main([]), 2)
            self.assertEqual(fixture.main(["--allow-disposable-client-binding-test"]), 2)
            launch.assert_not_called()

    def test_kernel_fixture_diagnostics_are_closed_without_paths_or_process_prose(self):
        import ci_client_binding as fixture
        failure = fixture.FixtureFailure("first-exec", code=3,
            errors=b"FIXTURE: executable=expected-deleted\n")
        self.assertIn("stderr=exe-expected-deleted", failure.diagnostic())
        self.assertIn("exit=3", failure.diagnostic())
        with self.assertRaises(fixture.FixtureFailure):
            fixture.checked_result("first-exec", (3, b"", b"FIXTURE: executable=expected-deleted\n"))
        output = io.StringIO()
        failure = fixture.FixtureFailure("snapshot", reason="PRIVATE", code=999999, site="/home/PRIVATE",
            errors=b"bwrap: PRIVATE /home/secret\nPermission denied")
        with patch.object(fixture, "run", side_effect=failure), contextlib.redirect_stdout(output):
            self.assertEqual(fixture.main(["--allow-disposable-client-binding-test"]), 2)
        self.assertIn("stderr=bwrap-permission", output.getvalue())
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertNotIn("/home", output.getvalue())

    def test_kernel_fixture_refuses_untrusted_ancestors_without_changing_them(self):
        import ci_client_binding as fixture
        safe = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
        for unsafe, reason in (
            (SimpleNamespace(st_uid=2345, st_mode=stat.S_IFDIR | 0o755), "owner"),
            (SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o775), "permissions"),
            (SimpleNamespace(st_uid=0, st_mode=stat.S_IFLNK | 0o777), "fixture-parent"),
        ):
            with self.subTest(reason=reason), \
                    patch.object(fixture.os, "stat", side_effect=(safe, safe, unsafe)), \
                    patch.object(fixture.subprocess, "run") as launch:
                with self.assertRaises(fixture.FixtureFailure) as raised:
                    fixture.require_parent_trust()
                self.assertEqual((raised.exception.stage, raised.exception.site, raised.exception.reason),
                                 ("preflight", "usr-lib", reason))
                launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
