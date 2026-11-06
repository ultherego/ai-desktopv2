"""Explicit root publication of reviewed independently installed native bytes.

This is an installer, not an execution broker. No provider program, wrapper,
package manager or network operation is ever run. Root ownership is granted
only to the whole payload digest the caller reviewed, under an atomic update.
"""
import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat

from . import client_sources as sources
from .e2_ipc import _unique_pairs


CACHE = Path("/var/lib/ai-desktop-clients")
_ROOT_UID = 0
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_HASH = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)
_MANIFEST_KEYS = {"schema", "provider", "source_uid", "source", "layout", "digest", "bytes", "files_count"}


class RegistrationError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _trusted(info, *, directory=False, mode=None):
    wanted = stat.S_ISDIR if directory else stat.S_ISREG
    if (not wanted(info.st_mode) or info.st_uid != _ROOT_UID or info.st_mode & 0o022
            or info.st_mode & (stat.S_ISUID | stat.S_ISGID)
            or (not directory and info.st_nlink != 1)
            or mode is not None and stat.S_IMODE(info.st_mode) != mode):
        raise RegistrationError("unsafe-cache")


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid


def _open_cache(*, create=False):
    path = str(CACHE)
    if not path.startswith("/") or os.path.normpath(path) != path:
        raise RegistrationError("unsafe-cache")
    fd = os.open("/", _DIR)
    try:
        _trusted(os.fstat(fd), directory=True)
        parts = path.split("/")[1:]
        for index, name in enumerate(parts):
            created = False
            try:
                child = os.open(name, _DIR, dir_fd=fd)
            except FileNotFoundError:
                if not create or index != len(parts) - 1:
                    raise
                try:
                    os.mkdir(name, 0o755, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(name, _DIR, dir_fd=fd)
            try:
                if created:
                    os.fchmod(child, 0o755)
                    os.fsync(child)
                    os.fsync(fd)
                _trusted(os.fstat(child), directory=True)
                linked = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if _identity(linked) != _identity(os.fstat(child)):
                    raise RegistrationError("changed-cache")
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def validate_manifest(data, provider):
    """Parse local registration metadata; callers must establish file trust."""
    try:
        if type(data) is not bytes or len(data) > 16384:
            raise RegistrationError("unsafe-cache")
        value = json.loads(data, object_pairs_hook=_unique_pairs)
        if (type(value) is not dict or set(value) != _MANIFEST_KEYS
                or type(value["schema"]) is not int or value["schema"] != 1
                or value["provider"] != provider or type(value["source_uid"]) is not int
                or not 0 < value["source_uid"] < 2**32 - 1
                or type(value["source"]) is not str
                or sources._path(value["source"]) != value["source"]
                or value["layout"] not in ("native-file", "codex-bundle")
                or type(value["digest"]) is not str or not _HASH.fullmatch(value["digest"])
                or type(value["bytes"]) is not int or not 1 <= value["bytes"] <= sources.MAX_TOTAL_BYTES
                or type(value["files_count"]) is not int or not 1 <= value["files_count"] <= sources.MAX_ENTRIES):
            raise RegistrationError("unsafe-cache")
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, RegistrationError):
            raise
        raise RegistrationError("unsafe-cache") from None


def _manifest(cache_fd, provider):
    descriptor = os.open(provider, _DIR, dir_fd=cache_fd)
    try:
        identity = os.fstat(descriptor)
        _trusted(identity, directory=True)
        manifest_fd = os.open("manifest.json", _FILE, dir_fd=descriptor)
        try:
            before = os.fstat(manifest_fd)
            _trusted(before)
            if before.st_size > 16384:
                raise RegistrationError("unsafe-cache")
            data = os.read(manifest_fd, 16385)
            after = os.fstat(manifest_fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise RegistrationError("changed-cache")
            value = validate_manifest(data, provider)
            if _identity(os.stat(provider, dir_fd=cache_fd, follow_symlinks=False)) != _identity(identity):
                raise RegistrationError("changed-cache")
            return value, _identity(identity)
        except (ValueError, UnicodeError, RecursionError) as exc:
            if isinstance(exc, RegistrationError):
                raise
            raise RegistrationError("unsafe-cache") from None
        finally:
            os.close(manifest_fd)
    finally:
        os.close(descriptor)


def registration_status(provider, *, uid=None):
    provider, uid = sources._provider(provider), sources._uid(uid)
    result = {"provider": provider, "status": "missing", "digest": None, "source_uid": None,
              "path": None, "layout": None, "bytes": 0, "files_count": 0}
    try:
        fd = _open_cache()
        try:
            try:
                value, _ = _manifest(fd, provider)
            except FileNotFoundError:
                try:
                    os.stat(provider, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    raise
                raise RegistrationError("unsafe-cache") from None
        finally:
            os.close(fd)
        result.update(status="registered", digest=value["digest"], source_uid=value["source_uid"],
                      path=value["source"] if value["source_uid"] == uid else None,
                      layout=value["layout"], bytes=value["bytes"], files_count=value["files_count"])
    except FileNotFoundError:
        pass
    except RegistrationError:
        result["status"] = "unsafe"
    except OSError:
        result["status"] = "unavailable"
    return result


def _rename(parent_fd, source, destination, flags):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent_fd, os.fsencode(source), parent_fd, os.fsencode(destination), flags) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _remove(parent_fd, name, *, expected=None, depth=0, count=None):
    if count is None:
        count = [0]
    count[0] += 1
    if depth > sources.MAX_DEPTH + 4 or count[0] > sources.MAX_ENTRIES + 8:
        raise RegistrationError("cleanup-incomplete")
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if expected is not None and _identity(info) != expected:
        raise RegistrationError("changed-cache")
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    _trusted(info, directory=True)
    fd = os.open(name, _DIR, dir_fd=parent_fd)
    try:
        if _identity(os.fstat(fd)) != _identity(info):
            raise RegistrationError("changed-cache")
        # Stream the bounded cleanup walk instead of materializing arbitrary
        # contents of a damaged cache before the counter can reject it.
        with os.scandir(fd) as entries:
            for entry in entries:
                _remove(fd, entry.name, depth=depth + 1, count=count)
        if _identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != _identity(info):
            raise RegistrationError("changed-cache")
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def _write_file(parent_fd, name, data, mode):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
    try:
        offset = 0
        while offset < len(data):
            count = os.write(fd, data[offset:])
            if count <= 0:
                raise RegistrationError("storage")
            offset += count
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _writer(payload_fd):
    def write(entry, source_fd):
        parts = entry.path.split("/")
        if any(part in ("", ".", "..") for part in parts) or len(parts) > sources.MAX_DEPTH + 2:
            raise RegistrationError("unsafe-source")
        parent = os.dup(payload_fd)
        try:
            for part in parts[:-1]:
                child = os.open(part, _DIR, dir_fd=parent)
                os.close(parent)
                parent = child
            if entry.kind == "directory":
                os.mkdir(parts[-1], 0o755, dir_fd=parent)
                descriptor = os.open(parts[-1], _DIR, dir_fd=parent)
                try:
                    os.fchmod(descriptor, 0o755)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.fsync(parent)
                return
            if entry.kind != "file":
                raise RegistrationError("unsafe-source")
            output = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=parent)
            try:
                digest, offset = hashlib.sha256(), 0
                while offset < entry.size:
                    block = os.pread(source_fd, min(1024 * 1024, entry.size - offset), offset)
                    if not block:
                        raise RegistrationError("source-changed")
                    digest.update(block)
                    written = 0
                    while written < len(block):
                        count = os.write(output, block[written:])
                        if count <= 0:
                            raise RegistrationError("storage")
                        written += count
                    offset += len(block)
                if digest.hexdigest() != entry.digest:
                    raise RegistrationError("source-changed")
                os.fchmod(output, entry.mode)
                os.fsync(output)
            finally:
                os.close(output)
            os.fsync(parent)
        finally:
            os.close(parent)
    return write


def apply(action, provider, *, source=None, expected_digest=None, previous_digest=None, uid):
    """Internal installer entrypoint; caller already passed the root CLI gate."""
    provider, uid = sources._provider(provider), sources._uid(uid)
    if (action not in ("register", "refresh", "remove") or uid <= 0
            or type(expected_digest) is not str or not _HASH.fullmatch(expected_digest)
            or action == "refresh" and (type(previous_digest) is not str or not _HASH.fullmatch(previous_digest))
            or action != "refresh" and previous_digest is not None
            or action != "remove" and (type(source) is not str or not source.startswith("/"))
            or action == "remove" and source is not None):
        raise RegistrationError("input")
    cache_fd = _open_cache(create=action != "remove")
    lock_fd = stage_fd = payload_fd = None
    stage, stage_identity, published = None, None, False
    try:
        lock_fd = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                          0o600, dir_fd=cache_fd)
        _trusted(os.fstat(lock_fd), mode=0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RegistrationError("busy") from None
        try:
            old, old_identity = _manifest(cache_fd, provider)
        except FileNotFoundError:
            # A present but incomplete provider directory is corruption, not an
            # empty slot into which a new registration may overwrite silently.
            try:
                os.stat(provider, dir_fd=cache_fd, follow_symlinks=False)
            except FileNotFoundError:
                old = old_identity = None
            else:
                raise RegistrationError("unsafe-cache") from None
        if action == "register" and old is not None:
            raise RegistrationError("already-registered")
        if action != "register" and old is None:
            raise RegistrationError("not-registered")
        if action == "refresh" and old["digest"] != previous_digest or action == "remove" and old["digest"] != expected_digest:
            raise RegistrationError("registration-changed")
        stage = ".stage-" + secrets.token_hex(16)
        if action == "remove":
            if _manifest(cache_fd, provider)[1] != old_identity:
                raise RegistrationError("registration-changed")
            _rename(cache_fd, provider, stage, 1)
            stage_identity = old_identity
            published = True
            os.fsync(cache_fd)
            _remove(cache_fd, stage, expected=stage_identity)
            stage = None
            return {"provider": provider, "status": "removed", "digest": expected_digest}
        os.mkdir(stage, 0o700, dir_fd=cache_fd)
        stage_fd = os.open(stage, _DIR, dir_fd=cache_fd)
        stage_identity = _identity(os.fstat(stage_fd))
        os.mkdir("payload", 0o755, dir_fd=stage_fd)
        payload_fd = os.open("payload", _DIR, dir_fd=stage_fd)
        os.fchmod(payload_fd, 0o755)
        result = sources.capture(provider, source, uid=uid, writer=_writer(payload_fd))
        if result.digest != expected_digest:
            raise RegistrationError("source-changed")
        manifest = {"schema": 1, "provider": provider, "source_uid": uid, "source": result.path,
                    "layout": result.layout, "digest": result.digest, "bytes": result.total_bytes,
                    "files_count": result.files_count}
        _write_file(stage_fd, "manifest.json", (json.dumps(manifest, sort_keys=True) + "\n").encode(), 0o644)
        os.fsync(payload_fd)
        os.fchmod(stage_fd, 0o755)
        stage_identity = _identity(os.fstat(stage_fd))
        os.fsync(stage_fd)
        if action == "refresh":
            current, identity = _manifest(cache_fd, provider)
            if identity != old_identity or current["digest"] != previous_digest:
                raise RegistrationError("registration-changed")
            _rename(cache_fd, stage, provider, 2)
            stage_identity = old_identity
        else:
            _rename(cache_fd, stage, provider, 1)
            stage = None
        published = True
        os.fsync(cache_fd)
        if stage is not None:
            _remove(cache_fd, stage, expected=stage_identity)
            stage = None
        return {"provider": provider, "status": "registered", "digest": result.digest}
    except sources.SourceError as exc:
        raise RegistrationError("source-" + exc.status) from None
    except RegistrationError:
        if published:
            raise RegistrationError("published-cleanup-incomplete") from None
        raise
    except OSError as exc:
        if published:
            raise RegistrationError("published-cleanup-incomplete") from None
        raise RegistrationError("storage") from None
    finally:
        if payload_fd is not None:
            os.close(payload_fd)
        if stage_fd is not None:
            os.close(stage_fd)
        if stage is not None and not published:
            try:
                _remove(cache_fd, stage, expected=stage_identity)
            except (OSError, RegistrationError):
                pass
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(cache_fd)


def admin_main(argv=None):
    parser = argparse.ArgumentParser(description="Register reviewed independent client bytes; never run a provider.")
    parser.add_argument("--allow-client-registration", action="store_true")
    parser.add_argument("action", choices=("register", "refresh", "remove"))
    parser.add_argument("--provider", choices=sources.PROVIDERS, required=True)
    parser.add_argument("--source")
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--previous-digest")
    args = parser.parse_args(argv)
    try:
        identity = os.environ.get("SUDO_UID", "")
        if (args.allow_client_registration is not True or os.getuid() != 0 or os.geteuid() != 0
                or not re.fullmatch(r"[1-9][0-9]{0,9}", identity) or not os.isatty(0) or not os.isatty(1)):
            raise RegistrationError("authorization")
        uid = sources._uid(int(identity))
        record = pwd.getpwuid(uid)
        if record.pw_uid != uid or uid == 0 or record.pw_name == "ai-desktop":
            raise RegistrationError("authorization")
        result = apply(args.action, args.provider, source=args.source, expected_digest=args.expected_digest,
                       previous_digest=args.previous_digest, uid=uid)
        print("CLIENT REGISTRATION: " + result["status"] + " provider=" + result["provider"])
        return 0
    except (RegistrationError, OSError, ValueError, KeyError, TypeError) as exc:
        reason = exc.reason if isinstance(exc, RegistrationError) else "input"
        print("CLIENT REGISTRATION: REFUSED reason=" + reason)
        return 2


if __name__ == "__main__":
    raise SystemExit(admin_main())
