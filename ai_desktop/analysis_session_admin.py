"""Fixed root-only session lifecycle. Never receives reports or credentials.

sudo authenticates the caller; PID1 owns a bounded helper with ExecStopPost.
The root-owned claim precedes activation and identifies exactly our cleanup.
No user-supplied command, path, property, unit name or UID is accepted by open.
"""
import os
from pathlib import Path
import pwd
import re
import secrets
import select
import signal
import stat
import subprocess
import sys

from .analysis_contract import AnalysisDenied, SESSION_SECONDS
from .analysis_sandbox import INSTALL, cloud_transport_ready, trusted_file
from .analysis_service import endpoint, socket_path, UID
from .process_io import bounded_process

BOOTSTRAP = INSTALL / "analysis-session.py"
STATE = Path("/run/ai-desktop-sessions")
ENV = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"}
NONCE = re.compile(r"[a-f0-9]{32}\Z", re.ASCII)
READY = b"SESSION1 READY\n"
STOPPED = b"SESSION1 STOPPED\n"
REFUSED = b"SESSION1 REFUSED "
PROPERTIES = "LoadState,ActiveState,MainPID,FragmentPath,DropInPaths,NeedDaemonReload,UnitFileState"


def desktop_uid(value):
    if type(value) is not str or not UID.fullmatch(value):
        raise AnalysisDenied("identity")
    uid = int(value)
    socket_path(uid)
    user = pwd.getpwuid(uid)
    if (uid < 1000 or user.pw_name == "ai-desktop"
            or user.pw_shell in ("/usr/bin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false")):
        raise AnalysisDenied("identity")
    return uid


def units(uid):
    socket_path(uid)
    return ["ai-desktop-analysis@" + str(uid) + suffix for suffix in (".socket", ".service")]


def manager_unit(uid):
    socket_path(uid)
    return "ai-desktop-session-" + str(uid) + ".service"


def command(*args):
    code, out, _ = bounded_process(list(args), env=ENV, timeout=10, limit=16384)
    if code != 0:
        raise AnalysisDenied("endpoint")
    return out


def properties(unit):
    code, raw, _ = bounded_process(["/usr/bin/systemctl", "show", unit, "--property=" + PROPERTIES],
                                  env=ENV, timeout=10, limit=16384)
    result = {}
    for line in raw.decode("utf-8", "strict").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in result:
            raise AnalysisDenied("endpoint")
        result[key] = value
    if code != 0 and not (code == 1 and result.get("LoadState") == "not-found"
                          and result.get("ActiveState") == "inactive"):
        raise AnalysisDenied("endpoint")
    return result


def unit_policy(uid, *, inactive=False):
    """Read-only metadata, also used by the unprivileged status command."""
    cloud_transport_ready()
    trusted_file(BOOTSTRAP)
    for unit in units(uid):
        suffix = unit.rsplit(".", 1)[1]
        fragment = "/usr/lib/systemd/system/ai-desktop-analysis@." + suffix
        trusted_file(fragment)
        values = properties(unit)
        if (values.get("LoadState") != "loaded" or values.get("FragmentPath") != fragment
                or values.get("DropInPaths") != "" or values.get("NeedDaemonReload") != "no"
                or values.get("UnitFileState") != "static"):
            raise AnalysisDenied("runtime")
        if inactive and (values.get("ActiveState") not in ("inactive", "failed")
                         or (suffix == "service" and values.get("MainPID") != "0")):
            raise AnalysisDenied("busy")
    if inactive and os.path.lexists(socket_path(uid)):
        raise AnalysisDenied("busy")


def state_directory():
    for parent in (Path("/"), Path("/run")):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise AnalysisDenied("identity")
    STATE.mkdir(mode=0o700, exist_ok=True)
    info = STATE.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise AnalysisDenied("identity")


def claim_path(uid):
    socket_path(uid)
    return STATE / str(uid)


def read_claim(uid):
    try:
        fd = os.open(claim_path(uid), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise AnalysisDenied("identity")
        nonce = os.read(fd, 33).decode("ascii", "strict")
        if not NONCE.fullmatch(nonce):
            raise AnalysisDenied("identity")
        return nonce
    finally:
        os.close(fd)


def create_claim(uid, nonce):
    fd = os.open(claim_path(uid), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if os.write(fd, nonce.encode("ascii")) != 32:
            raise AnalysisDenied("cleanup")
    finally:
        os.close(fd)


def cleaned(uid):
    if os.path.lexists(socket_path(uid)):
        raise AnalysisDenied("cleanup")
    for unit in units(uid):
        values = properties(unit)
        if (values.get("ActiveState") not in ("inactive", "failed")
                or (unit.endswith(".service") and values.get("MainPID") != "0")):
            raise AnalysisDenied("cleanup")


def cleanup(uid, nonce):
    state_directory()
    owner = read_claim(uid)
    if owner is not None:
        if owner != nonce:
            raise AnalysisDenied("busy")
        # Only our root-owned claim authorizes stopping these fixed units.
        command("/usr/bin/systemctl", "stop", *units(uid))
        cleaned(uid)
        claim_path(uid).unlink()
    os.write(1, STOPPED)


def hold(uid, nonce):
    state_directory()
    unit_policy(uid, inactive=True)
    create_claim(uid, nonce)  # Before the first activation, including partial start.
    # Inactive units may be garbage-collected between show and reset-failed;
    # reset-failed then refuses an unloaded unit. Starting is sufficient and
    # must not silently reset the installed activation rate limits.
    command("/usr/bin/systemctl", "start", units(uid)[0])
    endpoint(uid)
    os.write(1, READY)
    # The UI owns the write end. EOF, including SIGKILL of the UI, releases the
    # session. PID1's RuntimeMaxSec and ExecStopPost also cover this helper dying.
    if not select.select([0], [], [], SESSION_SECONDS)[0]:
        raise AnalysisDenied("timeout")
    if os.read(0, 1) != b"":
        raise AnalysisDenied("input")


def session_command(uid, nonce):
    stop = "/usr/bin/python3 -I -B " + str(BOOTSTRAP) + " cleanup " + str(uid) + " " + nonce
    return ["/usr/bin/systemd-run", "--quiet", "--wait", "--collect", "--pipe", "--service-type=exec",
            "--unit=" + manager_unit(uid), "--property=RuntimeMaxSec=%d" % (SESSION_SECONDS + 20), "--property=TimeoutStopSec=30",
            "--property=ExecStopPost=" + stop, "--property=KillMode=control-group",
            "--property=User=root", "--property=Group=root", "--property=UMask=0077",
            "--property=NoNewPrivileges=yes", "--property=CapabilityBoundingSet=",
            "--property=AmbientCapabilities=", "--property=ProtectSystem=strict",
            "--property=ProtectHome=yes", "--property=PrivateTmp=yes", "--property=PrivateDevices=yes",
            "--property=RestrictAddressFamilies=AF_UNIX", "--property=RestrictNamespaces=yes",
            "--property=RestrictSUIDSGID=yes", "--property=MemoryMax=96M", "--property=TasksMax=16",
            "--property=ReadWritePaths=" + str(STATE),
            "/usr/bin/python3", "-I", "-B", str(BOOTSTRAP), "hold", str(uid), nonce]


def open_session(uid):
    unit_policy(uid, inactive=True)
    state_directory()
    if read_claim(uid) is not None or properties(manager_unit(uid)).get("LoadState") != "not-found":
        raise AnalysisDenied("busy")
    nonce = secrets.token_hex(16)
    try:
        # PID1 atomically rejects a competing invocation of the same unit name.
        result = subprocess.run(session_command(uid, nonce), env=ENV, check=False,
                                close_fds=True, timeout=SESSION_SECONDS + 60, stderr=subprocess.DEVNULL)
        return 0 if result.returncode == 0 else 2
    finally:
        # Never stop a competing invocation. ExecStopPost owns normal cleanup;
        # this path only handles an interrupted launcher with our own claim.
        if read_claim(uid) == nonce:
            command("/usr/bin/systemctl", "stop", manager_unit(uid))
            if read_claim(uid) is not None:
                raise AnalysisDenied("cleanup")


def stop_session(uid):
    state_directory()
    nonce = read_claim(uid)
    if nonce is None:
        # Do not stop a manually administered or someone else's active socket.
        if properties(manager_unit(uid)).get("ActiveState") not in ("inactive", "failed"):
            raise AnalysisDenied("busy")
        cleaned(uid)
        os.write(1, STOPPED)
        return
    values = properties(manager_unit(uid))
    if values.get("LoadState") != "not-found":
        command("/usr/bin/systemctl", "stop", manager_unit(uid))
    if read_claim(uid) is not None:
        # Recovery of our stale claim after an interrupted cleanup only.
        if properties(manager_unit(uid)).get("ActiveState") not in ("inactive", "failed"):
            raise AnalysisDenied("busy")
        cleanup(uid, nonce)
    else:
        cleaned(uid)
        os.write(1, STOPPED)


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        if os.getuid() != 0 or os.geteuid() != 0:
            raise AnalysisDenied("identity")
        if args in (["open"], ["stop"], ["login"], ["login-verbose"]):
            uid = desktop_uid(os.environ.get("SUDO_UID"))
            if args in (["login"], ["login-verbose"]):
                from .analysis_admin import main as login
                return login(allow=True, details=True) if args == ["login-verbose"] else login(allow=True)
            if args == ["open"]:
                # Keep sudo's controlling TTY/timestamp. The UI handles Ctrl+C
                # and closes its control pipe; this wrapper must finish cleanup.
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                return open_session(uid)
            stop_session(uid)
        elif (len(args) == 3 and args[0] in ("hold", "cleanup")
              and NONCE.fullmatch(args[2])):
            uid = desktop_uid(args[1])
            (hold if args[0] == "hold" else cleanup)(uid, args[2])
        else:
            raise AnalysisDenied("input")
        return 0
    except (AnalysisDenied, OSError, ValueError, KeyError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        reason = exc.reason if isinstance(exc, AnalysisDenied) else "cancelled" if isinstance(exc, KeyboardInterrupt) else "runtime"
        try:
            os.write(1, REFUSED + reason.encode("ascii") + b"\n")
        except OSError:
            pass
        return 2
