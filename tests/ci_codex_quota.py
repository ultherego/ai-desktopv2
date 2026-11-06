"""Disposable systemd/namespace acceptance for the Codex account reader, with a synthetic offline ELF."""
import json
import os
from pathlib import Path
import socket
import subprocess

import ci_e2_systemd_activation as prior
from ai_desktop import codex_quota_ipc as ipc
from ai_desktop.siwc_auth import PLAN_SCOPES, _account_id


ALIAS = "ci-live-limits"
ENTRIES = ("codex-client.py", "codex-quota.py")
UNITS = ("ai-desktop-codex-limits@.socket", "ai-desktop-codex-limits@.service")
FIXTURE = "quota-fixture-client.py"


def require_absent(paths):
    for path in paths:
        # Some service-owned paths are inaccessible to this regular UID.
        for flag in ("-e", "-L"):
            prior._run("sudo", "-n", "/usr/bin/test", "!", flag, str(path))


def install_siwc_fixture(temp, slot):
    # An existing SIWC login pre-dates the separate native connection. Use only
    # this synthetic slot. The two access tokens deliberately have different values.
    key = _account_id("oaiapp_ci", "ci-subject")
    fixture = temp / "ci-quota-accounts.json"
    fixture.write_text(json.dumps({
        "schema": 1, "host_id": "urn:uuid:00000000-0000-4000-8000-000000000001", "active": key,
        "accounts": {key: {"client_id": "oaiapp_ci", "subject": "ci-subject", "email": "",
                           "id_token": "", "access_token": "synthetic-siwc-access",
                           "refresh_token": "synthetic-siwc-refresh", "expires_at": 2000000000,
                           "scopes": sorted(PLAN_SCOPES)}},
    }))
    install = ["sudo", "-n", "/usr/bin/install", "-o", "ai-desktop", "-g", "ai-desktop"]
    prior._run(*install, "-d", "-m700", str(slot), str(slot / "profile"))
    prior._run(*install, "-m600", "/dev/null", str(slot / "connected"))
    prior._run(*install, "-m600", str(fixture), str(slot / "profile/accounts.json"))


def rejected(alias, reasons):
    try:
        ipc.read_limits(alias)
    except ipc.QuotaError as exc:
        if exc.reason in reasons:
            return
    raise ValueError("quota-refusal")


def check_socket(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as channel:
        channel.settimeout(8)
        channel.connect(path)
        if channel.recv(1024) != ipc.READY:
            raise ValueError("quota-handshake")
        channel.sendall(b"READ ../../somewhere")
        if not channel.recv(1024).startswith(ipc.REFUSED):
            raise ValueError("quota-malformed")
    # Root may connect past mode 0600. The worker must check the peer UID
    # *before* sending READY; a filesystem-mode assertion alone cannot prove it.
    foreign = """
import socket, sys
with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
    sock.settimeout(8)
    sock.connect(sys.argv[1])
    if sock.recv(1024):
        raise SystemExit(1)
"""
    prior._run("sudo", "-n", "/usr/bin/python3", "-I", "-B", "-c", foreign, str(path))


def run_cycle(temp, shim_source, terminal):
    # Only called after ci_analysis_systemd.require_ci and the guarded throwaway
    # installation. Never run against a user's existing native binary or grants.
    uid = os.getuid()
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux"
            or Path("/proc/1/comm").read_text().strip() != "systemd" or uid < 1000 or uid != os.geteuid()):
        raise ValueError("quota-ci-only")
    socket_unit = f"ai-desktop-codex-limits@{uid}.socket"
    service_unit = f"ai-desktop-codex-limits@{uid}.service"
    path = ipc.socket_path(uid)
    siwc = Path("/var/lib/ai-desktop") / f"account-{uid}-codex-{ALIAS}"
    native = Path("/var/lib/ai-desktop") / f"quota-codex-{uid}"
    locks = Path("/run/ai-desktop-limits-admin")
    installed = [Path("/usr/bin/codex"), *(prior.DEST / name for name in (FIXTURE, *ENTRIES)),
                 *(prior.UNITS / name for name in UNITS)]
    require_absent([Path("/var/lib/ai-desktop-clients/codex"), *installed, siwc, native, locks, ipc.DIRECTORY,
                    Path("/etc/systemd/system/sockets.target.wants") / socket_unit])

    admin = ["sudo", "-n"]
    daemon = [*admin, "/usr/bin/systemctl"]
    command = [*admin, "/usr/bin/python3", "-I", "-B", str(prior.DEST / "codex-quota.py")]
    unit_installed = False
    try:
        source = temp / "quota-client.c"
        source.write_text(shim_source.replace("fixture-client.py", FIXTURE))
        binary = temp / "quota-client"
        prior._run("/usr/bin/cc", "-Wall", "-Werror", "-O2", str(source), "-o", str(binary))
        prior._run(*admin, "/usr/bin/install", "-m755", str(binary), "/usr/bin/codex")
        prior._run(*admin, "/usr/bin/install", "-m644", str(prior.ROOT / "tests/quota_fixture_client.py"),
                   str(prior.DEST / FIXTURE))
        for name in ENTRIES:
            prior._run(*admin, "/usr/bin/install", "-m644", str(prior.ROOT / "packaging/systemd" / name), str(prior.DEST))
        for name in UNITS:
            prior._run(*admin, "/usr/bin/install", "-m644", str(prior.ROOT / "packaging/systemd" / name), str(prior.UNITS))
        prior._run(*daemon, "daemon-reload")
        unit_installed = True
        install_siwc_fixture(temp, siwc)

        rejected(ALIAS, {"unavailable"})
        terminal([*command, "connect", ALIAS], 0, "quota-connect")
        prior._run("/usr/bin/systemctl", "is-enabled", "--quiet", socket_unit)
        prior._run("/usr/bin/systemctl", "is-active", "--quiet", socket_unit)
        ipc.endpoint(uid)
        check_socket(path)
        rejected("another-account", {"auth-required", "not-connected"})
        sample = ipc.read_limits(ALIAS)
        expected = {"plan": "pro", "windows": {"five_hour": {"percent": 31, "reset": 2000000000},
                                              "seven_day": {"percent": 47, "reset": 2000100000}}}
        if sample != expected:
            raise ValueError("quota-values")

        terminal([*command, "disconnect", ALIAS], 0, "quota-disconnect")
        if os.path.lexists(path) or _unit_state(socket_unit) != "disabled":
            raise ValueError("quota-disconnect")
        rejected(ALIAS, {"unavailable"})
        print("CI CODEX LIMITS: PASS bound-uid socket-activation native-namespace two-windows disconnect", flush=True)
    finally:
        if unit_installed:
            prior._run(*daemon, "disable", "--now", socket_unit)
            prior._run(*daemon, "stop", service_unit)
        prior._run(*admin, "/usr/bin/rm", "-f", "--", *(str(p) for p in installed))
        # Fresh, CI-only trees checked absent before installing any fixture.
        prior._run(*admin, "/usr/bin/rm", "-rf", "--one-file-system", "--",
                   str(siwc), str(native), str(locks), ipc.DIRECTORY)
        prior._run(*daemon, "daemon-reload")


def _unit_state(name):
    result = subprocess.run(["/usr/bin/systemctl", "show", name, "--property=UnitFileState", "--value"],
                            capture_output=True, text=True, check=True, timeout=8)
    return result.stdout.strip()
