"""E7 install home: side-by-side releases plus an atomically switched pointer.

Layout of a user-chosen private (0700) directory:
  releases/<name>/   complete installs made by install_user (each self-contained)
  current, previous  relative symlinks "releases/<name>", replaced only by
                     symlink(temp) + rename + directory fsync, so after power
                     loss each pointer is the old or the new value, never torn
  bin/ai-desktop     fixed launcher that resolves `current` at run time

Nothing here touches PATH, dotfiles, systemd, state or OAuth, and no release is
modified or deleted by activation. Every change is hash-confirmed like the rest
of E7. A hostile process of the same UID racing renames remains out of scope.
"""
import hmac
import os
import re
import secrets
import shlex
import stat

from . import install_lifecycle as lifecycle
from .install_policy import InstallDenied, _prefix
from .install_upgrade import _previous_state
from .install_user import _mkdir, _write_new

RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_TEMP = re.compile(r"\.pointer-[0-9a-f]{32}\Z")
_LINK = re.compile(r"releases/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})\Z")
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _deny():
    raise InstallDenied("INSTALL: REFUSED")


def _launcher(home):
    quoted = shlex.quote(home)
    return ("#!/bin/sh\n"
            "target=$(/usr/bin/readlink " + quoted + "/current) || exit 2\n"
            "case \"$target\" in\n"
            "  releases/*/*|*..*|*[!A-Za-z0-9_./-]*) exit 2 ;;\n"
            "  releases/?*) ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n"
            "exec " + quoted + "/\"$target\"/bin/ai-desktop \"$@\"\n").encode()


def _pointer(fd, name):
    """Release name a pointer designates, None if absent; refuse anything odd."""
    try:
        target = os.readlink(name, dir_fd=fd)
    except FileNotFoundError:
        return None
    except OSError:
        _deny()
    match = _LINK.fullmatch(target)
    if match is None:
        _deny()
    return match.group(1)


def _set_pointer(fd, name, release):
    temp = ".pointer-" + secrets.token_hex(16)
    os.symlink("releases/" + release, temp, dir_fd=fd)
    try:
        os.rename(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
    except BaseException:
        try:
            os.unlink(temp, dir_fd=fd)
        except OSError:
            pass
        raise
    os.fsync(fd)


def _open_home(home):
    fd = _prefix(home)
    try:
        for name in ("releases", "bin"):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                _deny()
        return fd
    except BaseException:
        os.close(fd)
        raise


def init(home):
    """Create the empty skeleton in an existing, empty, private directory."""
    fd = _prefix(home)
    try:
        if os.listdir(fd):
            _deny()
        _mkdir(fd, "releases")
        _mkdir(fd, "bin")
        binfd = os.open("bin", _DIR, dir_fd=fd)
        try:
            _write_new(binfd, "ai-desktop", _launcher(home), 0o700)
        finally:
            os.close(binfd)
        return {"status": "INITIALIZED"}
    except OSError:
        _deny()
    finally:
        os.close(fd)


def new_release(home, release):
    """Empty private prefix inside releases/, ready for install_user.install."""
    if type(release) is not str or not RELEASE.fullmatch(release):
        _deny()
    fd = _open_home(home)
    try:
        releases = os.open("releases", _DIR, dir_fd=fd)
        try:
            _mkdir(releases, release)
        finally:
            os.close(releases)
        return {"prefix": home + "/releases/" + release}
    except OSError:
        _deny()
    finally:
        os.close(fd)


def _complete(home, release):
    """A release usable as an active target: complete, current-format launcher."""
    state = _previous_state(home + "/releases/" + release)
    if state["launcher_isolated"] is not True:
        _deny()
    return state


def preview(home, release=None, action="activate"):
    try:
        if action not in ("activate", "rollback"):
            _deny()
        fd = _open_home(home)
        try:
            current, previous = _pointer(fd, "current"), _pointer(fd, "previous")
        finally:
            os.close(fd)
        if action == "rollback":
            release = previous
        if (type(release) is not str or not RELEASE.fullmatch(release)
                or release == current):
            _deny()
        state = _complete(home, release)
        plan = {"schema": 1, "action": action, "home": home, "target": release,
                "target_source_sha256": state["source_sha256"],
                "target_root": state["root"], "current": current,
                "previous": previous}
        return {**plan, "sha256": lifecycle._digest(plan)}
    except (OSError, ValueError, TypeError, RecursionError, KeyError):
        _deny()


def apply(home, release, confirmed_sha256, action="activate"):
    try:
        if not lifecycle._hex(confirmed_sha256):
            _deny()
        plan = preview(home, release, action)
        if not hmac.compare_digest(plan["sha256"], confirmed_sha256):
            _deny()
        fd = _open_home(home)
        try:
            _clean_temp(fd)
            # Each step leaves a valid state if power is lost right after it.
            if action == "activate":
                if plan["current"] is not None:
                    _set_pointer(fd, "previous", plan["current"])
                _set_pointer(fd, "current", plan["target"])
            else:
                _set_pointer(fd, "current", plan["target"])
                _set_pointer(fd, "previous", plan["current"])
        finally:
            os.close(fd)
        return {"status": "ACTIVATED" if action == "activate" else "ROLLED_BACK",
                "current": plan["target"], "previous": (
                    plan["current"] if plan["current"] is not None else plan["previous"])}
    except (OSError, ValueError, TypeError, RecursionError, KeyError):
        _deny()


def _clean_temp(fd):
    removed = 0
    for name in os.listdir(fd):
        if _TEMP.fullmatch(name) and stat.S_ISLNK(
                os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
            os.unlink(name, dir_fd=fd)
            removed += 1
    if removed:
        os.fsync(fd)
    return removed


def status(home):
    """Pointers and whether `current` designates a complete release."""
    try:
        fd = _open_home(home)
        try:
            current, previous = _pointer(fd, "current"), _pointer(fd, "previous")
            stale = sum(1 for n in os.listdir(fd) if _TEMP.fullmatch(n))
        finally:
            os.close(fd)
        usable = None
        if current is not None:
            try:
                usable = _complete(home, current) is not None
            except InstallDenied:
                usable = False
        return {"current": current, "previous": previous,
                "current_usable": usable, "stale_temp": stale}
    except (OSError, ValueError, TypeError, RecursionError, KeyError):
        _deny()


def recover(home):
    """Drop leftovers of an interrupted switch; pointers are never rewritten."""
    try:
        fd = _open_home(home)
        try:
            cleaned = _clean_temp(fd)
        finally:
            os.close(fd)
        result = status(home)
        broken = result["current"] is not None and result["current_usable"] is False
        return {**result, "cleaned": cleaned,
                "status": "BROKEN" if broken else "OK"}
    except (OSError, ValueError, TypeError, RecursionError, KeyError):
        _deny()


def retire_preview(home, release):
    fd = _open_home(home)
    try:
        if release in (_pointer(fd, "current"), _pointer(fd, "previous")):
            _deny()  # never remove an active or fallback release
    finally:
        os.close(fd)
    if type(release) is not str or not RELEASE.fullmatch(release):
        _deny()
    return lifecycle.preview_removal(home + "/releases/" + release, "uninstall")


def retire(home, release, confirmed_sha256):
    retire_preview(home, release)
    return lifecycle.remove(home + "/releases/" + release, confirmed_sha256, "uninstall")


def remove_home(home):
    """Delete the skeleton once no release remains; keeps the empty directory."""
    try:
        fd = _open_home(home)
        try:
            releases = os.open("releases", _DIR, dir_fd=fd)
            try:
                if os.listdir(releases):
                    _deny()
            finally:
                os.close(releases)
            _clean_temp(fd)
            for name in ("current", "previous"):
                if _pointer(fd, name) is not None:
                    os.unlink(name, dir_fd=fd)
            binfd = os.open("bin", _DIR, dir_fd=fd)
            try:
                if os.listdir(binfd) not in ([], ["ai-desktop"]):
                    _deny()
                launcher = os.open("ai-desktop", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=binfd) \
                    if os.listdir(binfd) else None
                if launcher is not None:
                    try:
                        if os.read(launcher, 65536) != _launcher(home):
                            _deny()
                    finally:
                        os.close(launcher)
                    os.unlink("ai-desktop", dir_fd=binfd)
            finally:
                os.close(binfd)
            os.rmdir("bin", dir_fd=fd)
            os.rmdir("releases", dir_fd=fd)
            os.fsync(fd)
            if os.listdir(fd):
                _deny()
        finally:
            os.close(fd)
        return {"status": "HOME_REMOVED", "directory_retained": True}
    except (OSError, ValueError, TypeError, RecursionError, KeyError):
        _deny()
