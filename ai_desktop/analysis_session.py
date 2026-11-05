"""Unprivileged daily lifecycle. The helper pipe carries no report or OAuth."""
from contextlib import contextmanager
import os
from pathlib import Path
import select
import signal
import subprocess
import time

from .analysis_contract import AnalysisDenied, REASONS
from .analysis_sandbox import trusted_file
from .analysis_session_admin import BOOTSTRAP, ENV, READY, STOPPED, REFUSED, unit_policy, properties, manager_unit
from . import e2_account


def identity(*, terminal=False):
    uid = os.geteuid()
    if uid < 1000 or uid != os.getuid():
        raise AnalysisDenied("identity")
    if terminal and (not os.isatty(0) or not os.isatty(1)):
        raise AnalysisDenied("consent")
    return uid


def installation_ready():
    uid = identity()
    if not e2_account.account_ready():
        raise AnalysisDenied("identity")
    if Path("/proc/1/comm").read_text().strip() != "systemd":
        raise AnalysisDenied("endpoint")
    unit_policy(uid)


def status():
    try:
        installation_ready()
        values = properties(manager_unit(identity()))
        active = values.get("ActiveState") not in ("inactive", "failed")
        print("Files and units: ready. Analysis session: " + ("running." if active else "stopped."))
        print("Client version and login: not checked offline; they are checked when you approve an analysis.")
        print("Ask: ai-desktop ask")
        print("Your ask permissions: ~/.config/ai-desktop/permissions.toml")
        print("Preview: ai-desktop ask --preview")
        print("Conversation: ai-desktop ask (consent to the conversation, then sudo for the session)")
        print("Change files and test them: ai-desktop task run --help")
        print("Login: ai-desktop login")
        print("Stop or recover a session: ai-desktop stop")
        print("STATUS: PASS auth=unchecked network=unused")
        return 0
    except (AnalysisDenied, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        reason = exc.reason if isinstance(exc, AnalysisDenied) else "runtime"
        print("STATUS: REFUSED reason=" + reason)
        print("Check the package installation and that systemd is available. Status does not start the client or a login.")
        return 2


def helper_command(action):
    if action not in ("open", "stop", "login", "login-verbose"):
        raise AnalysisDenied("input")
    trusted_file(BOOTSTRAP)
    return ["/usr/bin/sudo", "-n", "--", "/usr/bin/python3", "-I", "-B", str(BOOTSTRAP), action]


def authenticate(*, quiet=False):
    if type(quiet) is not bool:
        raise AnalysisDenied("input")
    identity(terminal=True)
    trusted_file(BOOTSTRAP)
    if not quiet:
        print("Administrator permissions are needed only to manage the separate AI Desktop session.", flush=True)
    try:
        result = subprocess.run(["/usr/bin/sudo", "-v"], env=ENV, check=False, close_fds=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        raise AnalysisDenied("authorization") from None
    if result.returncode != 0:
        raise AnalysisDenied("authorization")


class Session:
    def __init__(self, process):
        self.process, self.buffer, self.ready = process, bytearray(), False

    def line(self, seconds):
        deadline = time.monotonic() + seconds
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise AnalysisDenied("timeout")
            raw = os.read(self.process.stdout.fileno(), 512)
            if not raw or len(self.buffer) + len(raw) > 512:
                raise AnalysisDenied("endpoint")
            self.buffer.extend(raw)
        index = self.buffer.index(b"\n") + 1
        result = bytes(self.buffer[:index])
        del self.buffer[:index]
        return result

    def start(self):
        frame = self.line(20)
        if frame.startswith(REFUSED):
            reason = frame[len(REFUSED):-1].decode("ascii", "strict")
            raise AnalysisDenied(reason if reason in REASONS else "endpoint")
        if frame != READY:
            raise AnalysisDenied("endpoint")
        self.ready = True

    def close(self):
        self.process.stdin.close()  # EOF is the only control message.
        try:
            if self.ready and self.line(35) != STOPPED:
                raise AnalysisDenied("cleanup")
            code = self.process.wait(timeout=35)
            if self.ready:
                if (code != 0 or self.buffer or not select.select([self.process.stdout], [], [], 3)[0]
                        or os.read(self.process.stdout.fileno(), 1)):
                    raise AnalysisDenied("cleanup")
        except (OSError, ValueError, subprocess.SubprocessError, AnalysisDenied):
            raise AnalysisDenied("cleanup") from None
        finally:
            self.process.stdout.close()


@contextmanager
def managed_session(*, quiet=False):
    if type(quiet) is not bool:
        raise AnalysisDenied("input")
    authenticate(quiet=True) if quiet else authenticate()
    process = subprocess.Popen(helper_command("open"), env=ENV, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               close_fds=True)
    session = Session(process)
    try:
        session.start()
        yield
    finally:
        # A second Ctrl+C must not interrupt the cleanup acknowledgment.
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            session.close()
        finally:
            signal.signal(signal.SIGINT, previous)


def administrative(action, *, allow=False, details=False):
    if type(details) is not bool:
        print("SESSION: REFUSED reason=input")
        return 2
    if action == "login" and allow is not True:
        print("To choose a login, run: ai-desktop login")
        return 2
    try:
        authenticate(quiet=True) if action == "login" and not details else authenticate()
        if action == "login":
            # Direct TTY: OAuth URL/code never passes through a capture or pipe.
            return subprocess.run(helper_command("login-verbose" if details else "login"), env=ENV, close_fds=True,
                                  check=False, timeout=650).returncode
        if action != "stop":
            raise AnalysisDenied("input")
        result = subprocess.run(helper_command("stop"), env=ENV, close_fds=True,
                                check=False, capture_output=True, timeout=40)
        if result.returncode != 0 or result.stdout != STOPPED:
            raise AnalysisDenied("cleanup")
        print("SESSION: STOPPED")
        return 0
    except (AnalysisDenied, OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        reason = exc.reason if isinstance(exc, AnalysisDenied) else "cancelled" if isinstance(exc, KeyboardInterrupt) else "runtime"
        print("SESSION: REFUSED reason=" + reason)
        return 2
