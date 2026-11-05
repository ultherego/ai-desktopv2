"""Service-side account slots. Only the authenticated peer UID selects its namespace."""
from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import stat

from .account_store import CLOUD, named_account
from .analysis_contract import AnalysisDenied


_current = ContextVar("service_account", default=None)


def identity(uid, provider, name):
    if type(uid) is not int or not 1000 <= uid < 2**32 - 1 or provider not in CLOUD or not named_account(name):
        raise AnalysisDenied("identity")
    return uid, provider, name


@contextmanager
def selected(uid, provider, name):
    token = _current.set(identity(uid, provider, name))
    try:
        yield
    finally:
        _current.reset(token)


def _private_directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_gid != os.getegid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise AnalysisDenied("identity")


def directory(legacy, *, login=False):
    slot = _current.get()
    if slot is None:
        return legacy
    uid, provider, name = slot
    if legacy.name != provider:
        raise AnalysisDenied("identity")
    parent = legacy.parent
    _private_directory(parent)
    root = parent / ("account-%d-%s-%s" % (uid, provider, name))
    try:
        _private_directory(root, create=login)
        path = root / "profile"
        _private_directory(path, create=login)
        try:
            info = (root / "connected").lstat()
        except FileNotFoundError:
            if login:
                return path
            raise AnalysisDenied("auth-required") from None
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_gid != os.getegid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size):
            raise AnalysisDenied("identity")
        if login:  # A connected named slot never silently changes subscription identity.
            raise AnalysisDenied("busy")
        return path
    except FileNotFoundError:
        raise AnalysisDenied("auth-required") from None


def connected(path):
    if _current.get() is not None:
        fd = os.open(path.parent / "connected", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
