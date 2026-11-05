"""Explicitly attach/detach a native quota session. PID 1 owns the read-only socket."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import secrets
import stat
import subprocess

from .account_store import named_account
from .analysis_admin import login_command, login_unit_stopped
from .analysis_contract import AnalysisDenied
from .analysis_sandbox import INSTALL, service_identity, trusted_file
from .analysis_session_admin import ENV, command, desktop_uid, properties

ENTRY = INSTALL / "codex-quota.py"
LOCKS = Path("/run/ai-desktop-limits-admin")
LAST_LINK_REMOVED = 91


def valid_action(action, alias):
    return action in ("connect", "disconnect") and (alias == "default" or named_account(alias))


@contextmanager
def _admin_lock(uid):
    # The root-owned parent prevents a desktop or service user swapping our lock.
    LOCKS.mkdir(mode=0o700, exist_ok=True)
    info = LOCKS.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise AnalysisDenied("identity")
    fd = os.open(LOCKS / str(uid), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise AnalysisDenied("identity")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _policy(uid):
    trusted_file(ENTRY)
    template = "ai-desktop-codex-limits@"
    socket_unit = template + str(uid) + ".socket"
    states = {}
    for suffix in ("service", "socket"):
        fragment = f"/usr/lib/systemd/system/{template}.{suffix}"
        trusted_file(fragment)
        state = properties(f"{template}{uid}.{suffix}")
        if (state.get("LoadState") != "loaded" or state.get("FragmentPath") != fragment
                or state.get("DropInPaths") != "" or state.get("NeedDaemonReload") != "no"):
            raise AnalysisDenied("runtime")
        states[suffix] = state
    prior = states["socket"]
    if (prior.get("UnitFileState") not in ("enabled", "disabled")
            or prior.get("ActiveState") not in ("active", "inactive", "failed")):
        raise AnalysisDenied("runtime")
    return socket_unit, prior


def _interactive_worker(action, uid, alias):
    transient = "ai-desktop-quota-account-" + secrets.token_hex(8)
    argv = login_command()
    argv = ["--unit=" + transient if arg.startswith("--unit=") else arg for arg in argv]
    argv[-2:] = [str(ENTRY), "worker", action, str(uid), alias]
    try:
        proc = subprocess.run(argv, env=ENV, close_fds=True, timeout=610, check=False)
    finally:
        unit = transient + ".service"
        subprocess.run(["/usr/bin/systemctl", "stop", unit], env=ENV, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        status = subprocess.run(["/usr/bin/systemctl", "show", unit, "--property=LoadState,ActiveState,MainPID"],
                                env=ENV, capture_output=True, text=True, timeout=5, check=False)
        if not login_unit_stopped(status):
            raise AnalysisDenied("cleanup")
    return proc.returncode


def administer(action, alias):
    if (not valid_action(action, alias) or os.getuid() != 0 or os.geteuid() != 0
            or not os.isatty(0) or not os.isatty(1)):
        return 2
    try:
        uid = desktop_uid(os.environ.get("SUDO_UID", ""))
        with _admin_lock(uid):
            unit, previous = _policy(uid)
            if action == "disconnect":
                result = _interactive_worker(action, uid, alias)
                if result == LAST_LINK_REMOVED:
                    command("/usr/bin/systemctl", "disable", "--now", unit)
                    return 0
                return 0 if result == 0 else 2

            from .codex_quota_ipc import endpoint
            completed = False
            try:
                command("/usr/bin/systemctl", "enable", "--now", unit)
                endpoint(uid)
                completed = _interactive_worker(action, uid, alias) == 0
            finally:
                if not completed:
                    # Restore any listener used by the owner's other connected slots.
                    try:
                        if previous["ActiveState"] != "active":
                            command("/usr/bin/systemctl", "stop", unit)
                    finally:
                        if previous["UnitFileState"] != "enabled":
                            command("/usr/bin/systemctl", "disable", unit)
            return 0 if completed else 2
    except (AnalysisDenied, OSError, ValueError, KeyError, subprocess.SubprocessError, KeyboardInterrupt):
        print("Codex limits: the local connection setup or cleanup could not be confirmed.")
        return 2


def _challenge(url, code):
    from .codex_account_protocol import validate_challenge
    validate_challenge(url, code)
    print("Open the official Codex device login and choose the account and workspace for these limits.",
          url, "Enter code: " + code, sep="\n", flush=True)


def worker(action, uid, alias):
    from . import codex_quota_runtime as runtime
    if not valid_action(action, alias) or not os.isatty(0) or not os.isatty(1):
        return 2
    try:
        service_identity()
        runtime.run(action, uid, alias, challenge=_challenge if action == "connect" else None)
        if action == "disconnect" and not runtime.has_links(uid):
            return LAST_LINK_REMOVED
        return 0
    except AnalysisDenied as exc:
        print("Codex limits: operation refused (" + exc.reason + ").")
        return 2
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, KeyboardInterrupt):
        print("Codex limits: operation did not finish.")
        return 2
