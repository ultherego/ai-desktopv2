"""Service-side account slots. Only the authenticated peer UID selects its namespace."""
from contextlib import contextmanager
from contextvars import ContextVar
import os
import shutil
import stat

from .account_store import CLOUD, named_account
from .analysis_contract import AnalysisDenied


_current = ContextVar("service_account", default=None)


def identity(uid, provider, name, *, allow_default=False):
    if (type(uid) is not int or not 1000 <= uid < 2**32 - 1 or provider not in CLOUD
            or not (named_account(name) or allow_default and name == "default")):
        raise AnalysisDenied("identity")
    return uid, provider, name


@contextmanager
def selected(uid, provider, name, *, allow_default=False):
    slot = identity(uid, provider, name, allow_default=allow_default)
    token = _current.set(None if name == "default" else slot)
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


def _root(legacy):
    slot = _current.get()
    if slot is None or legacy.name != slot[1]:
        raise AnalysisDenied("identity")
    _private_directory(legacy.parent)
    return legacy.parent / ("account-%d-%s-%s" % slot)


def _marker(root, name):
    try:
        info = (root / name).lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_gid != os.getegid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size):
        raise AnalysisDenied("identity")
    return True


def _mark(root, name):
    fd = os.open(root / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    os.close(fd)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def directory(legacy, *, login=False):
    if _current.get() is None:
        return legacy
    root = _root(legacy)
    try:
        _private_directory(root, create=login)
        if _marker(root, "retired"):
            raise AnalysisDenied("auth-required")
        path = root / "profile"
        _private_directory(path, create=login)
        connected_before = _marker(root, "connected")
        if _marker(root, "signed-out"):
            # Only SIWC can reauthenticate an immutable slot against its retained subject.
            if connected_before and login and legacy.name == "codex":
                return path
            raise AnalysisDenied("auth-required")
        if not connected_before:
            if login:
                return path
            raise AnalysisDenied("auth-required")
        if login:  # A connected named slot never silently changes subscription identity.
            raise AnalysisDenied("busy")
        return path
    except FileNotFoundError:
        raise AnalysisDenied("auth-required") from None


def reconnecting(path):
    return _current.get() is not None and _marker(path.parent, "signed-out")


def connected(path):
    if _current.get() is None:
        return
    if reconnecting(path):
        if _current.get()[1] != "codex" or not _marker(path.parent, "connected"):
            raise AnalysisDenied("identity")
        (path.parent / "signed-out").unlink()
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    else:
        _mark(path.parent, "connected")


def lifecycle_profile(legacy):
    """Locate an existing profile under the caller's service lock. Never create a slot."""
    path = legacy
    connected_before = False
    try:
        if _current.get() is not None:
            root = _root(legacy)
            _private_directory(root)
            if _marker(root, "retired"):
                raise AnalysisDenied("auth-required")
            connected_before = _marker(root, "connected")
            if _marker(root, "signed-out") and not connected_before:
                raise AnalysisDenied("identity")
            path = root / "profile"
        _private_directory(path)
    except FileNotFoundError:
        if connected_before:
            raise AnalysisDenied("identity") from None
        return None, False
    return path, connected_before


def disconnected(path):
    # Leave the original connected identity fence in place after a sign-out.
    if not _marker(path.parent, "signed-out"):
        _mark(path.parent, "signed-out")


def validate_claude_path(profile):
    """Validate the fixed local credential name without opening or parsing it."""
    try:
        info = (profile / ".credentials.json").lstat()
    except FileNotFoundError:
        return
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
            or info.st_gid != os.getegid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise AnalysisDenied("identity")


def retire(legacy):
    """Caller owns the service lock. Keep a permanent tombstone even if removing local credentials is interrupted."""
    root = _root(legacy)
    _private_directory(root, create=True)
    if not _marker(root, "retired"):
        _mark(root, "retired")
    path = root / "profile"
    try:
        _private_directory(path)
    except FileNotFoundError:
        return
    if not shutil.rmtree.avoids_symlink_attacks:
        raise AnalysisDenied("identity")
    shutil.rmtree(path)
