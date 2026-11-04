"""Opt-in CachyOS proof after approved 0.1.0-5 install and socket start.

Never invokes sudo, pacman installation, systemctl start/enable, a shell, or
Claude. Only synthetic data. The operator separately authorizes activation.
"""
import hashlib
import os
import pwd
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai_desktop import e2_account, e2_ipc, e2_socket_activation as activation
from ai_desktop.transfer_consent import ConsentDenied, ConsentSession
from test_report_policy import report_fixture


def _command(argv):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=5,
                          env={"PATH": "/usr/bin", "LANG": "C"}, check=False)
    if done.returncode:
        raise ValueError("command")
    return done.stdout.strip()


def _installed_matches():
    for name in ("/usr", "/usr/lib", "/usr/lib/ai-desktop",
                 "/usr/lib/ai-desktop/ai_desktop", "/usr/lib/systemd",
                 "/usr/lib/systemd/system"):
        info = os.lstat(name)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("installed-path")
    mappings = [(ROOT / "ai_desktop" / name,
                 Path("/usr/lib/ai-desktop/ai_desktop") / name)
                for name in ("__init__.py", "e2_socket_activation.py", "e2_ipc.py",
                             "e2_worker_once.py", "e2_account.py", "report_policy.py",
                             "transfer_consent.py")]
    for name in ("ai-desktop-handoff@.socket", "ai-desktop-handoff@.service"):
        mappings.append((ROOT / "packaging/systemd" / name,
                         Path("/usr/lib/systemd/system") / name))
    mappings.append((ROOT / "packaging/systemd/activated-worker.py",
                     Path("/usr/lib/ai-desktop/activated-worker.py")))
    for source, installed in mappings:
        info = installed.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or hashlib.sha256(source.read_bytes()).digest() !=
                hashlib.sha256(installed.read_bytes()).digest()):
            raise ValueError("installed-code")


def _property(unit, name):
    return _command(["/usr/bin/systemctl", "show", unit, "--value", "--property=" + name])


def _wait_exit(unit, expected):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        active = _property(unit, "ActiveState")
        if active in ("inactive", "failed"):
            if (_property(unit, "ExecMainCode") == "1"
                    and _property(unit, "ExecMainStatus") == str(expected)):
                return
            raise ValueError("worker-exit")
        time.sleep(0.1)
    raise ValueError("worker-timeout")


def _run_checks():
    """Shared real IO proof; caller first establishes its installation method.

    CachyOS main additionally requires the native package below. The separate
    disposable Ubuntu CI driver installs the exact root-owned files instead.
    Both paths retain all identity, installed hash, unit and kernel checks.
    """
    stage = "preflight"
    try:
        uid = os.geteuid()
        if uid <= 0 or not e2_account.account_ready():
            raise ValueError("identity")
        _installed_matches()
        base = "ai-desktop-handoff@" + str(uid)
        service, endpoint = base + ".service", base + ".socket"
        for unit, suffix in ((service, ".service"), (endpoint, ".socket")):
            if (_property(unit, "FragmentPath") !=
                    "/usr/lib/systemd/system/ai-desktop-handoff@" + suffix
                    or _property(unit, "DropInPaths")
                    or _property(unit, "NeedDaemonReload") != "no"):
                raise ValueError("unit-policy")
        if (_property(endpoint, "ActiveState") != "active"
                or _property(service, "ActiveState") not in ("inactive", "failed")):
            raise ValueError("socket-not-ready")
        stage = "approved-transfer"
        report = report_fixture()
        gate = ConsentSession()
        preview = gate.preview(report)
        auth = gate.authorize(report, confirmed_sha256=preview.sha256, nonce=preview.nonce)
        if activation.send_once(auth) != auth.sha256:
            raise ValueError("digest")
        _wait_exit(service, 0)
        stage = "malformed-refusal"
        activation._trusted_endpoint(uid)
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as conn:
            conn.settimeout(8)
            conn.connect(activation.socket_path(uid))
            activation._manager_peer(conn, uid)
            activation._expect_packet(conn, activation.READY)
            # A report cannot select a UID or supply a command. No ACK allowed.
            conn.send(b'{"desktop_uid":0,"command":"forbidden"}')
            try:
                reply = conn.recv(128)
            except ConnectionResetError:
                reply = b""
            if reply:
                raise ValueError("unexpected-ack")
        _wait_exit(service, 2)
        stage = "consent-replay"
        try:
            gate.authorize(report, confirmed_sha256=preview.sha256, nonce=preview.nonce)
        except ConsentDenied:
            pass
        else:
            raise ValueError("reused-consent")
        print("HOST ACTIVATION: PASS manager_pid=1 worker_uid=" +
              str(pwd.getpwnam(e2_account.ACCOUNT).pw_uid) +
              " sender_uid=" + str(uid) + " success=1 refusal=2 confinement=PASS")
        return 0
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired, e2_ipc.HandoffDenied):
        print("HOST ACTIVATION: REFUSED stage=" + stage)
        return 2


def main(argv=None):
    if (sys.argv[1:] if argv is None else argv) != ["--allow-activated-worker-test"]:
        print("HOST ACTIVATION: REFUSED (explicit consent flag required)")
        return 2
    try:
        if (os.geteuid() <= 0 or
                _command(["/usr/bin/pacman", "-Q", "ai-desktop"]) != "ai-desktop 0.1.0-5"):
            raise ValueError("package")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print("HOST ACTIVATION: REFUSED stage=preflight")
        return 2
    return _run_checks()


if __name__ == "__main__":
    raise SystemExit(main())
