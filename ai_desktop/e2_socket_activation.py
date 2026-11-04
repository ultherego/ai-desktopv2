"""Systemd-activated, one-report worker; no Claude, OAuth or privilege change.

The desktop authenticates PID 1's root-owned listener, NOT a worker UID via
SO_PEERCRED (the listener was created by systemd). Root-owned unit policy
selects the worker UID. The worker authenticates the connecting desktop UID.
"""
import hashlib
import hmac
import os
from pathlib import Path
import pwd
import re
import select
import socket
import stat
import errno

from . import e2_account, e2_ipc
from .e2_worker_once import ACK_PREFIX

SOCKET_DIRECTORY = "/run/ai-desktop-handoff"
TIMEOUT = 5
READY = b"READY1"
_UID = re.compile(r"[1-9][0-9]{0,9}\Z", re.ASCII)


def _uid(value):
    if (type(value) is not int or not 0 < value < 2**32 - 1):
        raise e2_ipc.HandoffDenied("HANDOFF: REFUSED")
    return value


def socket_path(uid):
    return SOCKET_DIRECTORY + "/" + str(_uid(uid)) + ".sock"


def _trusted_endpoint(uid):
    """A desktop-owned socket inode in an exclusively root-controlled path.

    A desktop UID can chmod its socket inode, but cannot replace the name or
    listener in this directory. Worker peer checks still reject another UID.
    """
    for parent in ("/", "/run", SOCKET_DIRECTORY):
        info = os.lstat(parent)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise e2_ipc.HandoffDenied("HANDOFF: REFUSED")
    info = os.lstat(socket_path(uid))
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise e2_ipc.HandoffDenied("HANDOFF: REFUSED")
    return info.st_dev, info.st_ino


def _manager_peer(channel, uid):
    if (channel.family != socket.AF_UNIX
            or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
            or channel.getpeername() != socket_path(uid)
            or e2_ipc._kernel_peer(channel) != (1, 0, 0)):
        raise e2_ipc.HandoffDenied("HANDOFF: REFUSED")


def _expect_packet(channel, expected):
    reply, ancillary, flags, _ = channel.recvmsg(len(expected) + 1, 0)
    if (ancillary or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
            or not hmac.compare_digest(reply, expected)):
        raise e2_ipc.HandoffDenied("HANDOFF: REFUSED")


def send_once(authorization):
    """Connect only after consent; connection activates the fixed system unit.

    No systemctl, sudo, shell, custom endpoint or caller-supplied target UID.
    """
    try:
        uid = _uid(os.geteuid())
        if os.getuid() != uid or not e2_account.account_ready():
            raise e2_ipc.HandoffDenied("HANDOFF: REFUSED")
        body = e2_ipc.encode_approved(authorization)
        before = _trusted_endpoint(uid)
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as conn:
            conn.settimeout(TIMEOUT)
            conn.connect(socket_path(uid))
            _manager_peer(conn, uid)
            if _trusted_endpoint(uid) != before:
                raise e2_ipc.HandoffDenied("HANDOFF: REFUSED")
            # No report bytes until the installed worker has checked its
            # actual identity, confinement, activation descriptor and peer.
            _expect_packet(conn, READY)
            if conn.send(body) != len(body):
                raise OSError("partial send")
            _expect_packet(conn, ACK_PREFIX + authorization.sha256.encode("ascii"))
        return authorization.sha256
    except (OSError, ValueError, TypeError, UnicodeError, KeyError):
        raise e2_ipc.HandoffDenied("HANDOFF: REFUSED") from None


def _worker_identity():
    # /var is hidden by the service, including the entire OAuth home. Only
    # account database metadata is needed; there is no attempt to open HOME.
    user = pwd.getpwnam(e2_account.ACCOUNT)
    return (user.pw_name == e2_account.ACCOUNT
            and user.pw_uid > 0 and user.pw_gid > 0
            and os.getuid() == user.pw_uid == os.geteuid()
            and os.getgid() == user.pw_gid == os.getegid()
            and set(os.getgroups()) <= {user.pw_gid}
            and user.pw_dir == e2_account.HOME
            and user.pw_shell in e2_account._NOLOGIN)


def _confinement_ready():
    """Fail closed on missing effective restrictions, before reading a report.

    Reads only this process's kernel metadata. Socket probes never bind or
    connect; no filesystem write probe, OAuth access, or external traffic.
    """
    status = dict(line.split(":", 1) for line in
                  Path("/proc/self/status").read_text().splitlines() if ":" in line)
    if (status.get("NoNewPrivs", "").strip() != "1"
            or status.get("Seccomp", "").strip() != "2"
            or int(status.get("CapEff", "1").strip(), 16) != 0
            or int(status.get("CapBnd", "1").strip(), 16) != 0):
        return False
    mounts = {}
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields, fs = line.split(" - ", 1)
        parts = fields.split()
        mounts[parts[4]] = (set(parts[5].split(",")), fs.split()[0])
    for path in ("/etc", "/var", "/run", "/tmp"):
        flags, kind = mounts.get(path, (set(), ""))
        if kind != "tmpfs" or "ro" not in flags:
            return False
    for path in ("/", "/usr"):
        flags, _ = mounts.get(path, mounts.get("/", (set(), "")))
        if "ro" not in flags:
            return False
    if (os.path.lexists(e2_account.HOME)
            or os.access("/home", os.R_OK) or os.access("/root", os.R_OK)):
        return False
    for family in (socket.AF_UNIX, socket.AF_INET, socket.AF_INET6):
        try:
            probe = socket.socket(family, socket.SOCK_STREAM)
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EACCES, errno.EAFNOSUPPORT):
                return False
        else:
            probe.close()
            return False
    return True


def _activation_listener(desktop_uid):
    """Consume sd_listen_fds metadata once and take ownership of fd 3."""
    env = {key: os.environ.pop(key, None) for key in (
        "LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES", "LISTEN_PIDFDID")}
    if (env["LISTEN_PID"] != str(os.getpid()) or env["LISTEN_FDS"] != "1"
            or env["LISTEN_FDNAMES"] != "handoff"):
        raise e2_ipc.HandoffDenied("HANDOFF: REFUSED")
    listener = socket.socket(fileno=3)
    try:
        os.set_inheritable(3, False)
        if (listener.family != socket.AF_UNIX
                or listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
                or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1
                or listener.getsockname() != socket_path(desktop_uid)):
            raise e2_ipc.HandoffDenied("HANDOFF: REFUSED")
        return listener
    except BaseException:
        listener.close()
        raise


def receive_once(desktop_uid):
    """Only the installed system unit supplies the authorized numeric UID.

    LISTEN_* alone is not a credential: an attacker already running as the
    worker UID is within that UID's trust boundary. The desktop separately
    authenticates PID 1 and a protected pathname before disclosing anything.
    """
    try:
        _uid(desktop_uid)
        if (desktop_uid == os.geteuid() or not _worker_identity()
                or not _confinement_ready()):
            raise e2_ipc.HandoffDenied("HANDOFF: REFUSED")
        with _activation_listener(desktop_uid) as listener:
            # Do not set O_NONBLOCK on systemd's shared open file description.
            if not select.select([listener], [], [], TIMEOUT)[0]:
                raise e2_ipc.HandoffDenied("HANDOFF: REFUSED")
            with listener.accept()[0] as conn:
                e2_ipc._peer_uid(conn, desktop_uid)
                conn.settimeout(TIMEOUT)
                if conn.send(READY) != len(READY):
                    raise OSError("partial readiness")
                payload = e2_ipc.receive_approved(conn, desktop_uid=desktop_uid)
                digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                conn.settimeout(TIMEOUT)
                ack = ACK_PREFIX + digest.encode("ascii")
                if conn.send(ack) != len(ack):
                    raise OSError("partial acknowledgement")
                return digest
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        raise e2_ipc.HandoffDenied("HANDOFF: REFUSED") from None


def worker_main(argv):
    # Internal entrypoint only, fixed in the root-owned service. No arbitrary
    # path, command, environment file, UID switch, log or AI execution.
    try:
        if len(argv) != 1 or not _UID.fullmatch(argv[0]):
            raise e2_ipc.HandoffDenied("HANDOFF: REFUSED")
        receive_once(int(argv[0]))
        return 0
    except (e2_ipc.HandoffDenied, KeyboardInterrupt):
        return 2
