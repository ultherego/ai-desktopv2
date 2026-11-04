"""E7 fresh-prefix user-space installer proof, no sudo or overwrites.

Requires an existing, empty, private directory explicitly selected by user.
Preview never writes. Does not install into PATH or modify dotfiles/systemd.
Hash-confirmed uninstall/recovery and side-by-side updates are explicit operations.
In-place replacement and automatic activation are not implemented.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys

from .install_policy import InstallDenied, _prefix, _wrapper

SOURCE = Path(__file__).resolve().parent.parent
SOURCE_PACKAGE = SOURCE / "ai_desktop"
SOURCE_SKILLS = SOURCE / "skills"


def manifest():
    """Deterministic list of source bytes to copy. No secrets or unlisted files."""
    try:
        paths = sorted(SOURCE_PACKAGE.glob("*.py"))
        paths += sorted(SOURCE_SKILLS.glob("*/SKILL.md"))
        if not paths or not (SOURCE_PACKAGE / "__init__.py").is_file():
            raise InstallDenied("INSTALL: REFUSED")
        out = []
        for file in paths:
            if file.is_symlink() or not file.is_file():
                raise InstallDenied("INSTALL: REFUSED")
            relative = file.relative_to(SOURCE).as_posix()
            data = file.read_bytes()
            if not data or len(data) > 128 * 1024:
                raise InstallDenied("INSTALL: REFUSED")
            out.append((relative, data))
        return tuple(out)
    except (OSError, ValueError):
        raise InstallDenied("INSTALL: REFUSED") from None


def _preview_for_files(files):
    entries = [{"path": path, "sha256": hashlib.sha256(data).hexdigest()}
               for path, data in files]
    digest = hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"schema": 1, "files": tuple(entries), "sha256": digest}


def preview():
    return _preview_for_files(manifest())


def _mkdir(parent, name):
    os.mkdir(name, mode=0o700, dir_fd=parent)
    os.fsync(parent)


def install(prefix, confirmed_sha256, *, expected_prefix_identity=None):
    from .install_lifecycle import INSTALL_JOURNAL, MANIFEST, receipt, _entries
    if type(prefix) is not str or type(confirmed_sha256) is not str:
        raise InstallDenied("INSTALL: REFUSED")
    files = manifest()
    expected = _preview_for_files(files)
    if (len(confirmed_sha256) != 64 or
            not hmac.compare_digest(expected["sha256"], confirmed_sha256)):
        raise InstallDenied("INSTALL: REFUSED")
    fd = _prefix(prefix)
    opened = []
    try:
        if expected_prefix_identity is not None:
            identity = os.fstat(fd)
            if (type(expected_prefix_identity) is not tuple or len(expected_prefix_identity) != 2 or
                    any(type(value) is not int or value < 0 for value in expected_prefix_identity) or
                    expected_prefix_identity != (identity.st_dev, identity.st_ino)):
                raise InstallDenied("INSTALL: REFUSED")
        if os.listdir(fd):
            raise InstallDenied("INSTALL: REFUSED")
        intent = receipt(prefix, os.fstat(fd), files, expected["sha256"])
        _entries(intent, prefix, os.fstat(fd))  # Validate before any mutation.
        raw = (json.dumps(intent, sort_keys=True) + "\n").encode()
        if len(raw) > 64 * 1024:
            raise InstallDenied("INSTALL: REFUSED")
        _write_new(fd, INSTALL_JOURNAL, raw, 0o600)
        _mkdir(fd, "lib")
        _mkdir(fd, "bin")
        lib = os.open("lib", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        opened.append(lib)
        binfd = os.open("bin", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        opened.append(binfd)
        for source, data in files:
            parts = source.split("/")
            current = os.dup(lib)
            try:
                for directory in parts[:-1]:
                    try:
                        _mkdir(current, directory)
                    except FileExistsError:
                        pass
                    nxt = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                    os.close(current)
                    current = nxt
                _write_new(current, parts[-1], data, 0o600)
            finally:
                os.close(current)
        _write_new(binfd, "ai-desktop", _wrapper(prefix), 0o700)
        # Publish the complete receipt without replacing an existing name.
        # Recovery recognizes both names at this deliberate hardlink cutpoint.
        os.link(INSTALL_JOURNAL, MANIFEST, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.fsync(fd)
        os.unlink(INSTALL_JOURNAL, dir_fd=fd)
        os.fsync(fd)
        return {"status": "INSTALLED", "files": len(expected["files"]),
                "sha256": expected["sha256"]}
    except (OSError, ValueError):
        raise InstallDenied("INSTALL: REFUSED") from None
    finally:
        for child in reversed(opened):
            os.close(child)
        os.close(fd)


def _write_new(parent, name, data, mode):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(name, flags, mode, dir_fd=parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(data)
            output.flush()
            os.fsync(fd)
        os.fsync(parent)
    finally:
        os.close(fd)


_HOME_VERBS = ("home-init", "home-new-release", "home-status", "home-recover",
               "activate-preview", "activate", "rollback-active-preview",
               "rollback-active", "retire-preview", "retire", "home-remove")


def _home_command(args):
    """Install-home verbs (releases + atomic pointer); see install_home."""
    from . import install_home as home
    verb, rest = args[0], args[1:]
    table = {
        "home-init": (1, lambda h: home.init(h)),
        "home-new-release": (2, lambda h, r: home.new_release(h, r)),
        "home-status": (1, lambda h: home.status(h)),
        "home-recover": (1, lambda h: home.recover(h)),
        "activate-preview": (2, lambda h, r: home.preview(h, r, "activate")),
        "activate": (3, lambda h, r, sha: home.apply(h, r, sha, "activate")),
        "rollback-active-preview": (1, lambda h: home.preview(h, None, "rollback")),
        "rollback-active": (2, lambda h, sha: home.apply(h, None, sha, "rollback")),
        "retire-preview": (2, lambda h, r: home.retire_preview(h, r)),
        "retire": (3, lambda h, r, sha: home.retire(h, r, sha)),
        "home-remove": (1, lambda h: home.remove_home(h)),
    }
    count, action = table[verb]
    if len(rest) != count:
        raise InstallDenied("INSTALL: REFUSED")
    print(json.dumps(action(*rest), sort_keys=True))
    return 0


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if args == ["preview"]:
            print(json.dumps(preview(), sort_keys=True))
            return 0
        if len(args) == 2 and args[0] in ("uninstall-preview", "recover-preview"):
            from .install_lifecycle import preview_removal
            result = preview_removal(args[1], args[0].split("-")[0])
            print(json.dumps(result, sort_keys=True))
            return 0
        if len(args) == 3 and args[0] in ("upgrade-preview", "rollback-upgrade-preview"):
            from .install_upgrade import preview as preview_upgrade
            action = args[0].removesuffix("-preview")
            print(json.dumps(preview_upgrade(args[1], args[2], action), sort_keys=True))
            return 0
        if len(args) == 4 and args[0] in ("upgrade", "rollback-upgrade"):
            from .install_upgrade import apply as apply_upgrade
            result = apply_upgrade(args[1], args[2], args[3], args[0])
            print(json.dumps(result, sort_keys=True))
            return 0
        if args and args[0] in _HOME_VERBS:
            return _home_command(args)
        if len(args) == 3 and args[0] == "install":
            install(args[1], args[2])
            print("INSTALL: PASS (fresh prefix only)")
            return 0
        if len(args) == 3 and args[0] in ("uninstall", "recover"):
            from .install_lifecycle import remove
            remove(args[1], args[2], args[0])
            print("INSTALL: " + args[0].upper() + " PASS (prefix retained)")
            return 0
    except InstallDenied:
        pass
    print("INSTALL: REFUSED")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
