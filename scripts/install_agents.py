#!/usr/bin/env python3
"""Prepare pinned private clients and optionally install the local Arch package."""
import argparse
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

if __package__:
    from . import fetch_codex_client as codex_asset
else:
    import fetch_codex_client as codex_asset

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_SHA256 = "4f4178ce41d83e23341ebfbaecdbcf8ea6bf7baea0690025abe49eeec3a5d01d"
CLAUDE_URL = "https://downloads.claude.ai/claude-code-releases/2.1.312/linux-x64/claude"
MAX_CLIENT_BYTES = 512 * 1024 * 1024
CLIENTS = {"claude": "claude-code", "codex": "codex"}


class InstallError(ValueError):
    pass


def fingerprint(directory, name):
    """Hash a bounded regular file through its descriptor. Never follow links or open a device."""
    try:
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.geteuid()
            or before.st_mode & 0o022 or not 4 <= before.st_size <= MAX_CLIENT_BYTES):
        raise InstallError("Unsafe or incomplete client at " + name)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(name, flags, dir_fd=directory), "rb") as stream:
        current = os.fstat(stream.fileno())
        if not os.path.samestat(before, current) or stream.read(4) != b"\x7fELF":
            raise InstallError("Invalid client at " + name)
        stream.seek(0)
        digest = hashlib.sha256()
        remaining = current.st_size
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise InstallError("Client changed while checking " + name)
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (not os.path.samestat(current, after) or after.st_ctime_ns != current.st_ctime_ns
                or after.st_size != current.st_size):
            raise InstallError("Client changed while checking " + name)
    return digest.hexdigest()


def download_claude(target):
    # Disable .curlrc and proxies. A vendor redirect must also use HTTPS; the fixed digest authenticates the bytes.
    subprocess.run(["/usr/bin/curl", "-q", "--fail", "--silent", "--show-error", "--location",
                    "--proto", "=https", "--proto-redir", "=https", "--max-redirs", "3", "--proxy", "",
                    "--connect-timeout", "15", "--max-time", "300", "--max-filesize", str(MAX_CLIENT_BYTES),
                    "--output", str(target), CLAUDE_URL], check=True)


def prepare(root, selected, *, installing=False):
    target = root / "packaging/arch"
    with codex_asset._target_directory(root) as directory:
        existing = {}
        for provider, name in CLIENTS.items():
            if provider in selected:
                existing[provider] = fingerprint(directory, name)
            elif installing and os.path.lexists(target / name):
                raise InstallError("The build also contains " + name + ". Select both to verify and include it.")
        expected = {}
        if "claude" in selected:
            if existing["claude"] not in (None, CLAUDE_SHA256):
                raise InstallError("Existing claude-code has the wrong SHA-256.")
            expected["claude"] = CLAUDE_SHA256
        with tempfile.TemporaryDirectory(dir=target, prefix=".clients-") as tmp:
            stage_root = Path(tmp)
            stage = stage_root / "packaging/arch"
            stage.mkdir(parents=True, mode=0o700)
            if "claude" in selected and existing["claude"] is None:
                download_claude(stage / "claude-code")
                (stage / "claude-code").chmod(0o755)
            if "codex" in selected:
                # The pinned digest authenticates the archive. Verify its member even when reusing a local ELF.
                codex_asset.prepare(root=stage_root)
            with codex_asset._target_directory(stage_root) as staging:
                for provider in selected:
                    name = CLIENTS[provider]
                    if provider == "claude" and existing[provider] is not None:
                        continue
                    digest = fingerprint(staging, name)
                    if digest is None or (provider == "claude" and digest != CLAUDE_SHA256):
                        raise InstallError("Wrong SHA-256 for " + name)
                    if existing[provider] not in (None, digest):
                        raise InstallError("Existing " + name + " differs from the verified release.")
                    expected[provider] = digest
                for provider in selected:
                    if existing[provider] is None:
                        # Same filesystem, atomic publication. An occupied name, including a symlink, is never replaced.
                        name = CLIENTS[provider]
                        os.link(name, name, src_dir_fd=staging, dst_dir_fd=directory, follow_symlinks=False)
                        os.unlink(name, dir_fd=staging)
                os.fsync(directory)
        for provider in selected:
            if fingerprint(directory, CLIENTS[provider]) != expected[provider]:
                raise InstallError("Client changed after preparation.")
    return expected


def main(argv=None, *, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clients", choices=("claude", "codex", "both"))
    parser.add_argument("--install", action="store_true", help="confirm, build and install the private Arch package")
    args = parser.parse_args(argv)
    if os.getuid() == 0 or os.geteuid() != os.getuid():
        print("Run as the regular user who owns this checkout.", file=sys.stderr)
        return 2
    selected = tuple(CLIENTS) if args.clients == "both" else (args.clients,)
    try:
        if args.install:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise InstallError("Installation requires an interactive terminal.")
            if not Path("/usr/bin/makepkg").is_file():
                raise InstallError("Install Arch base-devel first.")
            answer = input("Prepare " + args.clients + " and install ai-desktop with makepkg -si? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return 2
        expected = prepare(root, selected, installing=args.install)
        print("Verified: " + ", ".join(selected), flush=True)
        if args.install:
            # A fresh package destination prevents installing an earlier build with the same pkgver/pkgrel.
            # The recipe verifies the packaged bytes once more, after staging and before installation.
            with tempfile.TemporaryDirectory(prefix="ai-desktop-package-") as pkgdest:
                env = dict(os.environ, PKGDEST=pkgdest, AI_DESKTOP_CHECK_CLIENTS="1",
                           AI_DESKTOP_EXPECT_CLAUDE=expected.get("claude", "-"),
                           AI_DESKTOP_EXPECT_CODEX=expected.get("codex", "-"))
                subprocess.run(["/usr/bin/makepkg", "--cleanbuild", "-si"], cwd=root / "packaging/arch",
                               env=env, check=True)
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
        return 2
    except (OSError, InstallError, codex_asset.AssetDenied, subprocess.CalledProcessError) as error:
        print("Could not prepare or install the clients:", error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
