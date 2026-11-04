#!/usr/bin/env python3
"""Prepare one pinned official Codex ELF for a private local package build.

No login, OAuth, AI request, binary execution, package installation or overwrite.
The SHA-256 below was read from OpenAI's official rust-v0.166.0 release asset
metadata on 2026-11-02. It is a pinned digest, not a separate signature claim.
https://github.com/openai/codex/releases/tag/rust-v0.166.0
"""
from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import ssl
import stat
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request


VERSION = "0.166.0"
ARCHIVE_URL = ("https://github.com/openai/codex/releases/download/rust-v0.166.0/"
               "codex-x86_64-unknown-linux-musl.tar.gz")
ARCHIVE_SHA256 = "a0bf572d2f93f056c6712a6da348e96078dc84f3885fd7be67807ef389cb3978"
MEMBER_NAME = "codex-x86_64-unknown-linux-musl"
TARGET_NAME = "codex"
# Official release metadata lists 121,764,138 bytes (2026-11-02); a 100 MiB
# cap would reject the pinned artifact before its digest could be checked.
PUBLISHED_ARCHIVE_BYTES = 121764138
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_BINARY_BYTES = 512 * 1024 * 1024
TIMEOUT_SECONDS = 30
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
_DOWNLOAD_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com"})
ROOT = Path(__file__).absolute().parent.parent


class AssetDenied(RuntimeError):
    def __init__(self, reason):
        self.reason = reason if reason in {"download", "hash", "archive", "target", "elf", "limit", "cancelled"} else "download"
        super().__init__("CODEX ASSET: REFUSED reason=" + self.reason)


def _url_allowed(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in _DOWNLOAD_HOSTS
                and parsed.netloc in _DOWNLOAD_HOSTS and parsed.port in (None, 443)
                and parsed.username is None and parsed.password is None and not parsed.fragment
                and not any(ord(c) < 33 for c in url))
    except (ValueError, TypeError):
        return False


class _ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 1

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _url_allowed(newurl):
            raise AssetDenied("download")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_archive(*, opener=None, clock=time.monotonic):
    """Fixed URL, HTTPS, bounded size and time; no inherited proxy or custom CA.

    The optional opener/clock are trusted test seams, never CLI options. Signed
    release-asset URLs and errors are not printed or persisted.
    """
    if opener is None:
        context = ssl.create_default_context(cafile=CA_BUNDLE)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _ReleaseRedirect(),
                                            urllib.request.HTTPSHandler(context=context))
    deadline = clock() + TIMEOUT_SECONDS
    request = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "AI-Desktop-Codex-asset/" + VERSION})
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            if not _url_allowed(response.geturl()) or response.status != 200:
                raise AssetDenied("download")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or not 0 < int(length) <= MAX_ARCHIVE_BYTES):
                raise AssetDenied("limit")
            result = bytearray()
            while True:
                if clock() >= deadline:
                    raise AssetDenied("download")
                block = response.read(min(65536, MAX_ARCHIVE_BYTES + 1 - len(result)))
                if not block:
                    break
                result.extend(block)
                if len(result) > MAX_ARCHIVE_BYTES:
                    raise AssetDenied("limit")
            if not result or (length is not None and len(result) != int(length)):
                raise AssetDenied("download")
            return bytes(result)
    except (OSError, ValueError, urllib.error.URLError, ssl.SSLError):
        raise AssetDenied("download") from None


@contextmanager
def _target_directory(root):
    """Open known checkout directories without traversing any symlink."""
    root = Path(root)
    if not root.is_absolute() or root != Path(os.path.normpath(root)) or ".." in root.parts:
        raise AssetDenied("target")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        for component in (*root.parts[1:], "packaging", "arch"):
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise AssetDenied("target")
        yield descriptor
    except OSError:
        raise AssetDenied("target") from None
    finally:
        os.close(descriptor)


def _absent(directory):
    try:
        os.stat(TARGET_NAME, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise AssetDenied("target")


def install_archive(raw, *, root=ROOT, expected_sha=ARCHIVE_SHA256):
    """Validate one archive and publish its ELF with O_EXCL, never extractall.

    ``root``/``expected_sha`` are trusted test seams. The command line accepts no
    endpoint, digest, member name or destination override.
    """
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_ARCHIVE_BYTES:
        raise AssetDenied("limit")
    if (type(expected_sha) is not str or len(expected_sha) != 64
            or hashlib.sha256(raw).hexdigest() != expected_sha):
        raise AssetDenied("hash")
    descriptor, created, complete = None, None, False
    try:
        with _target_directory(root) as directory:
            _absent(directory)
            try:
                archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
            except (tarfile.TarError, OSError, ValueError):
                raise AssetDenied("archive") from None
            with archive:
                member = archive.next()
                if (member is None or member.name != MEMBER_NAME or not member.isreg() or member.issparse()
                        or member.linkname or not 4 <= member.size <= MAX_BINARY_BYTES or archive.next() is not None):
                    raise AssetDenied("archive")
                source = archive.extractfile(member)
                if source is None:
                    raise AssetDenied("archive")
                with source:
                    magic = source.read(4)
                    if magic != b"\x7fELF":
                        raise AssetDenied("elf")
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
                    try:
                        descriptor = os.open(TARGET_NAME, flags, 0o600, dir_fd=directory)
                    except OSError:
                        raise AssetDenied("target") from None
                    created = os.fstat(descriptor)
                    try:
                        pending, remaining = magic, member.size - 4
                        while pending:
                            count = os.write(descriptor, pending)
                            if count <= 0:
                                raise AssetDenied("target")
                            pending = pending[count:]
                        while remaining:
                            block = source.read(min(65536, remaining))
                            if not block:
                                raise AssetDenied("archive")
                            remaining -= len(block)
                            pending = block
                            while pending:
                                count = os.write(descriptor, pending)
                                if count <= 0:
                                    raise AssetDenied("target")
                                pending = pending[count:]
                        if source.read(1):
                            raise AssetDenied("archive")
                        os.fsync(descriptor)
                        os.fchmod(descriptor, 0o755)
                        os.fsync(descriptor)
                        current = os.stat(TARGET_NAME, dir_fd=directory, follow_symlinks=False)
                        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                                or (current.st_dev, current.st_ino) != (created.st_dev, created.st_ino)):
                            raise AssetDenied("target")
                        os.fsync(directory)
                        complete = True
                    finally:
                        os.close(descriptor)
                        descriptor = None
                        if not complete:
                            # Never remove a concurrent replacement at the name.
                            try:
                                current = os.stat(TARGET_NAME, dir_fd=directory, follow_symlinks=False)
                                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                                    os.unlink(TARGET_NAME, dir_fd=directory)
                                    os.fsync(directory)
                            except FileNotFoundError:
                                pass
    except (tarfile.TarError, EOFError):
        raise AssetDenied("archive") from None
    except OSError:
        raise AssetDenied("target") from None
    return Path(root) / "packaging" / "arch" / TARGET_NAME


def prepare(*, root=ROOT, fetch=None):
    # Refuse an occupied destination before doing any network I/O.
    with _target_directory(root) as directory:
        _absent(directory)
    raw = (fetch or fetch_archive)()
    return install_archive(raw, root=root)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--help"]:
        print("Przygotuj oficjalny Codex " + VERSION + " do lokalnej budowy pakietu; bez logowania i uruchamiania AI.")
        return 0
    if argv:
        print("CODEX ASSET: REFUSED reason=target", file=sys.stderr)
        return 2
    try:
        prepare()
    except KeyboardInterrupt:
        print("CODEX ASSET: REFUSED reason=cancelled", file=sys.stderr)
        return 2
    except AssetDenied as failure:
        print(str(failure), file=sys.stderr)
        return 2
    print("Codex " + VERSION + ": przygotowano klienta do budowy pakietu.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
