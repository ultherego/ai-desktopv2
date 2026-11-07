"""Installed source discovery reads native bytes, never wrappers or credentials."""
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import client_sources as sources


def elf(extra=b""):
    return struct.pack("<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01" + b"\0" * 9,
                       3, 62, 1, 0, 0, 0, 0, 64, 56, 0, 64, 0, 0) + extra


class ClientSourceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path.home())))

    def write(self, path, data=b"resource", mode=0o644):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def bundle(self, root, *, extra=b""):
        binary = self.write(root / "bin/codex", elf(extra), 0o755)
        self.write(root / "codex-package.json", b'{"version":"fixture"}')
        self.write(root / "codex-path/rg", b"synthetic rg", 0o755)
        self.write(root / "codex-resources/bwrap", b"synthetic bwrap", 0o755)
        self.write(root / "bin/codex-code-mode-host", b"helper", 0o755)
        return binary

    def test_claude_native_curl_link_retains_updatable_source_spelling(self):
        native = self.write(self.root / ".local/share/claude/versions/2.1.318", elf(), 0o755)
        source = self.root / ".local/bin/claude"
        source.parent.mkdir(parents=True)
        source.symlink_to("../share/claude/versions/2.1.318")
        result = sources.discover("claude", str(source))
        self.assertEqual(result.status, "present")
        self.assertEqual(result.path, str(source))
        self.assertEqual(result.native_path, str(native))
        self.assertEqual(result.layout, "native-file")
        self.assertEqual(result.files_count, 1)
        self.assertEqual(result.bytes, len(elf()))

    def test_codex_curl_captures_bundle_not_parent_auth_store(self):
        release = self.root / ".codex/packages/standalone/releases/0.167-linux"
        native = self.bundle(release)
        current = release.parent.parent / "current"
        current.symlink_to("releases/0.167-linux")
        source = self.root / ".local/bin/codex"
        source.parent.mkdir(parents=True)
        source.symlink_to(current / "bin/codex")
        secret = self.write(self.root / ".codex/auth.json", b"SECRET-OAUTH")
        seen = {}
        result = sources.capture("codex", str(source), writer=lambda entry, fd: seen.update(
            {entry.path: os.pread(fd, entry.size, 0) if fd is not None else None}))
        self.assertEqual(result.status, "present")
        self.assertEqual(result.native_path, str(native))
        self.assertEqual(result.layout, "codex-bundle")
        self.assertEqual(result.files_count, 5)
        self.assertEqual(set(seen), {"bin", "bin/codex", "codex-package.json", "codex-path", "codex-path/rg",
                                    "codex-resources", "codex-resources/bwrap", "bin/codex-code-mode-host"})
        self.assertNotIn(b"SECRET-OAUTH", seen.values())
        self.assertEqual(secret.read_bytes(), b"SECRET-OAUTH")

    def test_npm_nvm_wrapper_is_data_and_native_optional_package_is_selected(self):
        prefix = self.root / ".nvm/versions/node/v24.0"
        package = prefix / "lib/node_modules/@openai/codex"
        wrapper = self.write(package / "bin/codex.js", b"throw new Error('MUST NEVER RUN')", 0o755)
        self.write(package / "package.json", b'{"name":"@openai/codex"}')
        native = self.bundle(package.parent / "codex-linux-x64/vendor/x86_64-unknown-linux-musl")
        source = prefix / "bin/codex"
        source.parent.mkdir(parents=True)
        source.symlink_to(wrapper)
        with patch("subprocess.run", side_effect=AssertionError("execution")), \
                patch("subprocess.Popen", side_effect=AssertionError("execution")):
            result = sources.discover("codex", environ={"PATH": str(source.parent)})
        self.assertEqual(result.status, "present")
        self.assertEqual(result.path, str(source))
        self.assertEqual(result.native_path, str(native))

    def test_current_claude_npm_postinstall_native_link_needs_no_node(self):
        package = self.root / "node_modules/@anthropic-ai/claude-code"
        native = self.write(package.parent / "claude-code-linux-x64/claude", elf(), 0o755)
        source = package / "bin/claude.exe"
        source.parent.mkdir(parents=True)
        source.symlink_to(native)
        self.assertEqual(sources.discover("claude", str(source)).status, "present")

    def test_legacy_codex_resources_are_refused_before_any_payload_is_copied(self):
        native = self.write(self.root / "legacy/codex", elf(), 0o755)
        self.write(self.root / "legacy/codex-resources/rg", b"legacy rg", 0o755)
        copied = []
        with self.assertRaises(sources.SourceError) as raised:
            sources.capture("codex", str(native), writer=lambda *args: copied.append(args))
        self.assertEqual(raised.exception.detail, "legacy-layout")
        self.assertEqual(copied, [])
        package = self.root / "node_modules/@openai/codex"
        wrapper = self.write(package / "bin/codex.js", b"never execute", 0o755)
        self.write(package / "package.json", b'{"name":"@openai/codex"}')
        vendor = package / "vendor/x86_64-unknown-linux-musl"
        self.write(vendor / "codex/codex", elf(), 0o755)
        self.write(vendor / "path/rg", b"legacy rg", 0o755)
        self.assertEqual(sources.discover("codex", str(wrapper)).detail, "legacy-layout")

    def test_unknown_wrapper_missing_native_or_wrong_architecture_is_refused(self):
        source = self.write(self.root / "bin/codex", b"#!/bin/sh\nexit 0\n", 0o755)
        self.assertEqual(sources.discover("codex", str(source)).status, "unsupported")
        self.write(self.root / "package.json", b'{"name":"@openai/codex"}')
        self.assertEqual(sources.discover("codex", str(source)).detail, "native-package-missing")
        bad = bytearray(elf()); bad[18:20] = (183).to_bytes(2, "little")
        source.write_bytes(bad)
        self.assertEqual(sources.discover("codex", str(source)).status, "unsupported")

    def test_native_resources_affect_whole_review_digest(self):
        native = self.bundle(self.root / "release")
        before = sources.discover("codex", str(native))
        (self.root / "release/codex-path/rg").write_bytes(b"updated helper")
        after = sources.discover("codex", str(native))
        self.assertNotEqual(before.digest, after.digest)
        self.assertEqual(after.digest, sources.discover("codex", str(native)).digest)

    def test_new_native_bundle_helpers_are_included_and_bound_to_review(self):
        native = self.bundle(self.root / "release")
        first = sources.discover("codex", str(native))
        helper = self.write(self.root / "release/bin/new-upstream-helper", b"new helper", 0o755)
        captured = {}
        second = sources.capture("codex", str(native), writer=lambda entry, fd: captured.update(
            {entry.path: os.pread(fd, entry.size, 0) if fd is not None else None}))
        self.assertEqual(captured["bin/new-upstream-helper"], b"new helper")
        self.assertNotEqual(first.digest, second.digest)
        helper.write_bytes(b"updated helper")
        self.assertNotEqual(second.digest, sources.discover("codex", str(native)).digest)

    def test_identical_bytes_at_replaced_source_object_require_new_review(self):
        native = self.write(self.root / "claude", elf(), 0o755)
        before = sources.discover("claude", str(native))
        replacement = self.write(self.root / "replacement", elf(), 0o755)
        replacement.replace(native)
        after = sources.discover("claude", str(native))
        self.assertEqual(before.total_bytes, after.total_bytes)
        self.assertNotEqual(before.digest, after.digest)

    def test_writable_parent_hardlink_capabilities_special_files_are_refused(self):
        source = self.write(self.root / "bin/claude", elf(), 0o755)
        source.parent.chmod(0o777)
        self.assertEqual(sources.discover("claude", str(source)).status, "unsafe")
        source.parent.chmod(0o755)
        os.link(source, self.root / "second")
        self.assertEqual(sources.discover("claude", str(source)).detail, "hardlink")
        (self.root / "second").unlink()
        with patch.object(sources.os, "getxattr", return_value=b"capabilities"):
            self.assertEqual(sources.discover("claude", str(source)).detail, "capabilities")
        source.unlink(); os.mkfifo(source)
        self.assertEqual(sources.discover("claude", str(source)).status, "unsupported")

    def test_root_private_or_other_user_source_is_never_read_for_caller(self):
        for mode, uid in ((stat.S_IFDIR | 0o700, 0), (stat.S_IFREG | 0o600, 0),
                          (stat.S_IFREG | 0o644, 2002)):
            with self.subTest(mode=mode, uid=uid), self.assertRaises(sources.SourceError):
                sources._trusted(SimpleNamespace(st_mode=mode, st_uid=uid), 1001)

    def test_source_or_alias_change_during_capture_is_refused(self):
        native = self.write(self.root / "one", elf(b"one"), 0o755)
        next_native = self.write(self.root / "two", elf(b"two"), 0o755)
        source = self.root / "claude"; source.symlink_to(native)
        def mutate(entry, fd):
            if entry.kind == "file":
                source.unlink(); source.symlink_to(next_native)
        with self.assertRaises(sources.SourceError) as raised:
            sources.capture("claude", str(source), writer=mutate)
        self.assertEqual(raised.exception.status, "changed")
        def overwrite(entry, fd):
            if entry.kind == "file":
                next_native.write_bytes(elf(b"new bytes"))
        with self.assertRaises(sources.SourceError) as raised:
            sources.capture("claude", str(source), writer=overwrite)
        self.assertEqual(raised.exception.status, "changed")

    def test_resource_external_symlink_loop_and_new_entry_are_refused(self):
        native = self.bundle(self.root / "release")
        outside = self.write(self.root / "secret", b"DO NOT IMPORT")
        link = self.root / "release/codex-resources/escape"
        link.symlink_to(outside)
        self.assertEqual(sources.discover("codex", str(native)).detail, "resource-link")
        link.unlink(); link.symlink_to("escape")
        self.assertEqual(sources.discover("codex", str(native)).status, "unsafe")
        link.unlink()
        original = sources._Scan.recheck
        def add_then_check(scanner):
            self.write(self.root / "release/codex-resources/late", b"changed")
            return original(scanner)
        with patch.object(sources._Scan, "recheck", add_then_check):
            self.assertEqual(sources.discover("codex", str(native)).status, "changed")

    def test_top_level_resources_that_link_outside_the_release_are_refused(self):
        native = self.bundle(self.root / "release")
        outside = self.write(self.root / "synthetic-auth.json", b'{"synthetic": "DO NOT IMPORT"}')
        package = self.root / "release/codex-package.json"
        if package.exists():
            package.unlink()
        package.symlink_to(outside)
        # Layout detection may refuse it before the import check does; either way nothing outside is imported.
        found = sources.discover("codex", str(native))
        self.assertEqual(found.status, "unsupported")
        self.assertIn(found.detail, ("resource-link", "legacy-layout"))
        imported = []
        with self.assertRaises(sources.SourceError):
            sources.capture("codex", str(native), writer=lambda entry, fd: imported.append(entry.path))
        self.assertNotIn("codex-package.json", imported)
        package.unlink()
        # A whole resource directory that is a link out of the release is refused the same way.
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.write(elsewhere / "secret", b"DO NOT IMPORT")
        resources = self.root / "release/codex-resources"
        moved = self.root / "release-resources-original"
        resources.rename(moved)
        resources.symlink_to(elsewhere)
        found = sources.discover("codex", str(native))
        self.assertEqual(found.status, "unsupported")
        self.assertIn(found.detail, ("resource-link", "legacy-layout"))
        resources.unlink()
        moved.rename(resources)

    def test_top_level_links_inside_the_release_still_work(self):
        native = self.bundle(self.root / "release")
        package = self.root / "release/codex-package.json"
        real = self.root / "release/package-real.json"
        if package.exists():
            package.rename(real)
        else:
            self.write(real, b"{}")
        package.symlink_to("package-real.json")
        self.assertEqual(sources.discover("codex", str(native)).status, "present")

    def test_byte_entry_and_depth_budgets_fail_closed(self):
        native = self.bundle(self.root / "release")
        for name, limit in (("MAX_FILE_BYTES", 16), ("MAX_TOTAL_BYTES", 64), ("MAX_ENTRIES", 2), ("MAX_DEPTH", -1)):
            with self.subTest(name=name), patch.object(sources, name, limit):
                self.assertEqual(sources.discover("codex", str(native)).status, "unsupported")

    def test_directory_inventory_stops_at_budget_without_materializing_tree(self):
        class Listing:
            consumed = 0
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                for index in range(100):
                    self.consumed += 1
                    yield SimpleNamespace(name=str(index))
        listing = Listing()
        with patch.object(sources.os, "scandir", return_value=listing), self.assertRaises(sources.SourceError):
            sources._names(123, 3)
        self.assertEqual(listing.consumed, 4)

    def test_invalid_paths_and_first_unsafe_path_candidate_do_not_fall_back(self):
        for path in ("relative", str(self.root) + "/../claude", str(self.root) + "/bad\nname"):
            self.assertEqual(sources.discover("claude", path).status, "unsafe")
        first = self.write(self.root / "first/claude", elf(), 0o777)
        second = self.write(self.root / "second/claude", elf(), 0o755)
        result = sources.discover("claude", environ={"PATH": str(first.parent) + ":" + str(second.parent)})
        self.assertEqual(result.status, "unsafe")
        self.assertEqual(result.path, str(first))


if __name__ == "__main__":
    unittest.main()
