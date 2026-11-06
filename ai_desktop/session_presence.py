"""Private, transient conversation counters for the local status panel."""
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import time

from . import execution


def _runtime():
    uid = os.getuid()
    if uid != os.geteuid() or uid < 1000:
        raise OSError("runtime identity")
    return Path("/run/user") / str(uid)


def _directory(create):
    root = _runtime()
    flags = os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent = os.open(root, flags)
    try:
        def private(fd):
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise OSError("runtime permissions")
        private(parent)
        if create:
            try:
                os.mkdir("ai-desktop-presence", 0o700, dir_fd=parent)
            except FileExistsError:
                pass
        fd = os.open("ai-desktop-presence", flags, dir_fd=parent)
        try:
            private(fd)
        except OSError:
            os.close(fd)
            raise
        return fd
    finally:
        os.close(parent)


class Presence:
    def __init__(self):
        self.fd = self.directory = None
        self.name = secrets.token_hex(12)
        self.started = int(time.time())

    def start(self):
        try:
            self.directory = _directory(True)
            self.fd = os.open(self.name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600,
                              dir_fd=self.directory)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            self.update()
        except OSError:
            self.close()

    def update(self, exchanges=0, saved_id=None):
        if self.fd is None:
            return
        try:
            settings = execution.current() or {}
            value = {"id": saved_id or self.name, "provider": settings.get("provider", "claude"),
                     "role": settings.get("profile", "default"), "model": settings.get("model", "default"),
                     "account": settings.get("account", "default"), "exchanges": exchanges,
                     "started": self.started, "directory": os.getcwd()}
            raw = json.dumps(value, ensure_ascii=True).encode("ascii")
            if len(raw) <= 32768:
                os.pwrite(self.fd, raw, 0)
                os.ftruncate(self.fd, len(raw))
        except (OSError, UnicodeError):
            pass  # A status display is optional and must never interrupt the conversation.

    def close(self):
        if self.fd is not None:
            try:
                os.unlink(self.name, dir_fd=self.directory)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None
        if self.directory is not None:
            os.close(self.directory)
            self.directory = None


def live():
    """A held lock proves liveness. Ignore entries left by killed processes until the user runtime directory expires."""
    try:
        directory = _directory(False)
    except OSError:
        return []
    rows = []
    try:
        for name in os.listdir(directory):
            if len(name) != 24 or any(c not in "0123456789abcdef" for c in name):
                continue
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    info = os.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                            or info.st_nlink != 1 or not 0 < info.st_size <= 32768):
                        continue
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        value = json.loads(os.read(fd, 32768))
                        if (type(value) is dict and all(type(value.get(k)) is str for k in
                                ("id", "role", "provider", "model", "account", "directory"))
                                and all(type(value.get(k)) is int for k in ("started", "exchanges"))):
                            rows.append(value)
                finally:
                    os.close(fd)
            except (OSError, ValueError, RecursionError):
                continue
    finally:
        os.close(directory)
    return sorted(rows, key=lambda row: row["started"], reverse=True)
