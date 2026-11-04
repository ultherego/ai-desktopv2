"""One-shot, local-only, cross-UID diagnostic receipt for an isolated worker.

No network, no shell invocation, no listener on a filesystem path, and no
access to Claude or OAuth. This is a future worker entrypoint; no program
starts it automatically. Its abstract AF_UNIX address is per-session.
"""
import hashlib
import hmac
import os
import pwd
import re
import socket
import stat

from . import e2_account, e2_ipc
from .transfer_consent import TransferAuthorization

PREFIX = "\0ai-desktop-e2-"
TOKEN = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
ACK_PREFIX = b"ACK1:"
ACK_SIZE = len(ACK_PREFIX) + 64
SESSION_TIMEOUT = 180
IO_TIMEOUT = 5


def _address(token):
    if type(token) is not str or not TOKEN.fullmatch(token):
        raise e2_ipc.HandoffDenied("HANDOFF: REFUSED")
    return PREFIX + token


def _worker_ready():
    """Validate actual worker UID and home directory metadata, not tokens."""
    try:
        user = pwd.getpwnam(e2_account.ACCOUNT)
        home = os.lstat(e2_account.HOME)
        return (
            type(user.pw_uid) is int and user.pw_uid > 0
            and user.pw_gid > 0
            and os.getuid() == user.pw_uid == os.geteuid()
            and os.getgid() == user.pw_gid == os.getegid()
            and set(os.getgroups()) <= {user.pw_gid}
            and user.pw_dir == e2_account.HOME
            and user.pw_shell in e2_account._NOLOGIN
            and stat.S_ISDIR(home.st_mode)
            and stat.S_IMODE(home.st_mode) == 0o700
            and home.st_uid == user.pw_uid and home.st_gid == user.pw_gid
        )
    except (KeyError, OSError, TypeError, ValueError, AttributeError):
        return False


def _with_timeout(channel, action):
    initial = channel.gettimeout()
    try:
        channel.settimeout(IO_TIMEOUT)
        return action()
    finally:
        channel.settimeout(initial)


def serve_once(token, *, desktop_uid, on_ready=None):
    """Receive exactly one approved report and acknowledge only its digest.

    Only a trusted launcher may supply desktop_uid and start this process as
    ai-desktop. A malicious or wrong-UID connection terminates the session.
    """
    address = _address(token)
    if (type(desktop_uid) is not int or desktop_uid <= 0
            or not _worker_ready() or desktop_uid == os.geteuid()):
        raise e2_ipc.HandoffDenied("HANDOFF: REFUSED")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
            listener.settimeout(SESSION_TIMEOUT)
            listener.bind(address)
            listener.listen(1)
            if on_ready is not None:
                on_ready()
            with listener.accept()[0] as conn:
                payload = e2_ipc.receive_approved(conn, desktop_uid=desktop_uid)
                digest = hashlib.sha256(payload.encode("utf-8")).hexdigest().encode("ascii")
                ack = ACK_PREFIX + digest
                if _with_timeout(conn, lambda: conn.send(ack)) != len(ack):
                    raise OSError("short reply")
                return digest.decode("ascii")
    except (OSError, TimeoutError, ValueError, e2_ipc.HandoffDenied):
        raise e2_ipc.HandoffDenied("HANDOFF: REFUSED") from None


def send_once(token, authorization):
    """Trusted desktop UI side; reject if dedicated account is not ready."""
    address = _address(token)
    if (type(authorization) is not TransferAuthorization
            or not e2_account.account_ready()):
        raise e2_ipc.HandoffDenied("HANDOFF: REFUSED")
    try:
        worker_uid = pwd.getpwnam(e2_account.ACCOUNT).pw_uid
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as conn:
            conn.settimeout(IO_TIMEOUT)
            conn.connect(address)
            e2_ipc.send_approved(conn, authorization, worker_uid=worker_uid)
            reply = conn.recv(ACK_SIZE + 1)
            expected = ACK_PREFIX + authorization.sha256.encode("ascii")
            if not hmac.compare_digest(reply, expected):
                raise ValueError("wrong acknowledgement")
            return authorization.sha256
    except (OSError, TimeoutError, TypeError, ValueError, UnicodeError, KeyError):
        raise e2_ipc.HandoffDenied("HANDOFF: REFUSED") from None
