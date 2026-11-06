"""Pinned client preparation; synthetic archives only, no download/exec/OAuth."""
import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import urllib.request


_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fetch_codex_client.py"
_SPEC = importlib.util.spec_from_file_location("aid_codex_asset", _PATH)
asset = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(asset)


def archive(entries=None):
    entries = entries if entries is not None else [(asset.MEMBER_NAME, b"\x7fELFsynthetic", tarfile.REGTYPE, "")]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as handle:
        for name, content, kind, link in entries:
            member = tarfile.TarInfo(name)
            member.type, member.linkname = kind, link
            member.size = len(content)
            handle.addfile(member, io.BytesIO(content))
    return output.getvalue()


class _Response(io.BytesIO):
    status = 200

    def __init__(self, content, *, url=asset.ARCHIVE_URL, length=None):
        super().__init__(content)
        self.url = url
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def geturl(self):
        return self.url


class _Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        return self.response


class CodexClientAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "packaging" / "arch"
        self.directory.mkdir(parents=True)
        self.target = self.directory / "codex"

    def install(self, raw):
        return asset.install_archive(raw, root=self.root, expected_sha=hashlib.sha256(raw).hexdigest())

    def test_pins_the_verified_official_release_and_digest(self):
        self.assertEqual(asset.VERSION, "0.166.0")
        self.assertEqual(asset.ARCHIVE_URL, "https://github.com/openai/codex/releases/download/rust-v0.166.0/codex-x86_64-unknown-linux-musl.tar.gz")
        self.assertEqual(asset.ARCHIVE_SHA256, "a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978")
        self.assertEqual(asset.MEMBER_NAME, "codex-x86_64-unknown-linux-musl")
        self.assertEqual(asset.PUBLISHED_ARCHIVE_BYTES, 121764138)
        self.assertLessEqual(asset.PUBLISHED_ARCHIVE_BYTES, asset.MAX_ARCHIVE_BYTES)

    def test_valid_archive_writes_elf_with_final_mode_without_execution(self):
        raw = archive()
        with patch("subprocess.Popen", side_effect=AssertionError("must not execute")):
            result = self.install(raw)
        self.assertEqual(result, self.target)
        self.assertEqual(result.read_bytes(), b"\x7fELFsynthetic")
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o755)

    def test_wrong_digest_refuses_before_any_target_write(self):
        with self.assertRaises(asset.AssetDenied) as failure:
            asset.install_archive(archive(), root=self.root)
        self.assertEqual(failure.exception.reason, "hash")
        self.assertFalse(self.target.exists())

    def test_path_traversal_symlinks_hardlinks_and_extra_entries_are_refused(self):
        entries = [
            [("../escape", b"\x7fELFx", tarfile.REGTYPE, "")],
            [("/tmp/escape", b"\x7fELFx", tarfile.REGTYPE, "")],
            [("./" + asset.MEMBER_NAME, b"\x7fELFx", tarfile.REGTYPE, "")],
            [(asset.MEMBER_NAME, b"", tarfile.SYMTYPE, "/etc/passwd")],
            [(asset.MEMBER_NAME, b"", tarfile.LNKTYPE, "/etc/passwd")],
            [(asset.MEMBER_NAME, b"", tarfile.DIRTYPE, "")],
            [(asset.MEMBER_NAME, b"\x7fELFx", tarfile.REGTYPE, ""),
             ("license", b"unexpected", tarfile.REGTYPE, "")],
        ]
        for values in entries:
            with self.subTest(values=values), self.assertRaises(asset.AssetDenied):
                self.install(archive(values))
            self.assertFalse(self.target.exists())
        self.assertFalse((self.root.parent / "escape").exists())

    def test_not_elf_corrupt_or_empty_archive_is_refused(self):
        for raw in (archive([(asset.MEMBER_NAME, b"shell script", tarfile.REGTYPE, "")]), b"bad gzip", archive([])):
            with self.subTest(raw=raw[:8]), self.assertRaises(asset.AssetDenied):
                self.install(raw)
            self.assertFalse(self.target.exists())

    def test_existing_regular_or_symlink_target_is_never_overwritten(self):
        self.target.write_bytes(b"keep original")
        with self.assertRaises(asset.AssetDenied):
            self.install(archive())
        self.assertEqual(self.target.read_bytes(), b"keep original")
        self.target.unlink()
        other = self.root / "other"
        other.write_bytes(b"keep other")
        self.target.symlink_to(other)
        with self.assertRaises(asset.AssetDenied):
            self.install(archive())
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(other.read_bytes(), b"keep other")

    def test_symlink_directory_and_writable_parent_are_refused(self):
        actual = self.root / "actual"
        self.directory.rename(actual)
        self.directory.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(asset.AssetDenied):
            self.install(archive())
        self.assertFalse((actual / "codex").exists())
        self.directory.unlink()
        actual.rename(self.directory)
        self.directory.chmod(0o777)
        with self.assertRaises(asset.AssetDenied):
            self.install(archive())

    def test_partial_failure_removes_only_our_created_file(self):
        real_write = os.write
        writes = []

        def interrupted(descriptor, data):
            writes.append(data)
            if len(writes) > 1:
                raise OSError("private path and details")
            return real_write(descriptor, data)

        with patch.object(asset.os, "write", side_effect=interrupted):
            with self.assertRaises(asset.AssetDenied) as failure:
                self.install(archive())
        self.assertEqual(failure.exception.reason, "target")
        self.assertNotIn("private", str(failure.exception))
        self.assertFalse(self.target.exists())

    def test_parallel_creation_wins_and_is_not_overwritten(self):
        original = asset._absent

        def competing(directory):
            original(directory)
            self.target.write_bytes(b"parallel version")

        with patch.object(asset, "_absent", side_effect=competing):
            with self.assertRaises(asset.AssetDenied):
                self.install(archive())
        self.assertEqual(self.target.read_bytes(), b"parallel version")

    def test_prepare_refuses_existing_target_before_download(self):
        self.target.write_bytes(b"old")
        with patch.object(asset, "fetch_archive", side_effect=AssertionError("no network")):
            with self.assertRaises(asset.AssetDenied):
                asset.prepare(root=self.root)

    def test_bounded_download_uses_only_fixed_url_and_timeout(self):
        opener = _Opener(_Response(b"archive", length=7))
        self.assertEqual(asset.fetch_archive(opener=opener), b"archive")
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, asset.ARCHIVE_URL)
        self.assertEqual(timeout, 30)
        for response in (_Response(b"x", url="https://evil.invalid/file"),
                         _Response(b"x", length=asset.MAX_ARCHIVE_BYTES + 1),
                         _Response(b"x", length=2)):
            with self.subTest(response=response), self.assertRaises(asset.AssetDenied):
                asset.fetch_archive(opener=_Opener(response))
        times = iter([0, 31])
        with self.assertRaises(asset.AssetDenied):
            asset.fetch_archive(opener=_Opener(_Response(b"x")), clock=lambda: next(times))

    def test_redirects_require_https_exact_canonical_hosts(self):
        handler = asset._ReleaseRedirect()
        request = urllib.request.Request(asset.ARCHIVE_URL)
        for url in ("http://github.com/file", "https://github.com.evil.invalid/file",
                    "https://user@github.com/file", "https://127.0.0.1/file",
                    "https://release-assets.githubusercontent.com:444/file", "https://github.com/file#fragment"):
            with self.subTest(url=url), self.assertRaises(asset.AssetDenied):
                handler.redirect_request(request, None, 302, "", {}, url)
        self.assertTrue(asset._url_allowed("https://release-assets.githubusercontent.com/asset?sig=opaque"))

    def test_cli_has_no_override_or_execution_flags_and_errors_are_sanitized(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(asset.main(["--url", "https://example.invalid"]), 2)
        self.assertNotIn("example.invalid", stderr.getvalue())
        with patch.object(asset, "prepare", side_effect=AssertionError("retired CLI must never download")) as prepare:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(asset.main([]), 2)
            prepare.assert_not_called()
            self.assertIn("standalone preparation retired", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
