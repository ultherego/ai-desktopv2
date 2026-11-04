"""Install/run the exact worker units ONLY in the disposable Ubuntu CI VM.

No model, OAuth, real report, pacman, enable or changes to the developer host.
Normal verify.py never runs this driver. The explicit flag AND CI preflight
must pass before any sudo. Existing accounts/files are refused, not repaired.
"""
import grp
import os
from pathlib import Path
import pwd
import subprocess
import sys

import host_e2_activated_worker as host

ROOT = Path(__file__).resolve().parents[1]
DEST = Path("/usr/lib/ai-desktop")
UNITS = Path("/usr/lib/systemd/system")
NAMES = ("ai-desktop-handoff@.socket", "ai-desktop-handoff@.service")


def _require_ci():
    uid = os.geteuid()
    if (os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_OS") != "Linux"
            or uid <= 0 or uid != os.getuid()):
        raise ValueError("disposable-ci-only")
    release = Path("/etc/os-release").read_text().splitlines()
    if ("ID=ubuntu" not in release or 'VERSION_ID="24.04"' not in release
            or Path("/proc/1/comm").read_text().strip() != "systemd"):
        raise ValueError("ci-platform")
    for lookup in (pwd.getpwnam, grp.getgrnam):
        try:
            lookup("ai-desktop")
        except KeyError:
            pass
        else:
            raise ValueError("existing-account")
    for path in (DEST, Path("/var/lib/ai-desktop"), Path("/run/ai-desktop-handoff"),
                 *(UNITS / name for name in NAMES)):
        if os.path.lexists(path):
            raise ValueError("existing-installation")
    return uid


def _run(*args):
    subprocess.run(args, check=True, timeout=30,
                   env={"PATH": "/usr/bin", "LANG": "C.UTF-8"})


def _install():
    _run("sudo", "-n", "/usr/bin/install", "-d", "-m755", str(DEST / "ai_desktop"))
    sources = sorted((ROOT / "ai_desktop").glob("*.py"))
    if not sources or any(p.is_symlink() or not p.is_file() for p in sources):
        raise ValueError("source-code")
    _run("sudo", "-n", "/usr/bin/install", "-m644", *(str(p) for p in sources),
         str(DEST / "ai_desktop"))
    _run("sudo", "-n", "/usr/bin/install", "-m644",
         str(ROOT / "packaging/systemd/activated-worker.py"), str(DEST))
    for name in NAMES:
        _run("sudo", "-n", "/usr/bin/install", "-m644",
             str(ROOT / "packaging/systemd" / name), str(UNITS / name))
    _run("sudo", "-n", "/usr/bin/systemd-sysusers", str(ROOT / "packaging/sysusers.d/ai-desktop.conf"))
    _run("sudo", "-n", "/usr/bin/systemd-tmpfiles", "--create", str(ROOT / "packaging/tmpfiles.d/ai-desktop.conf"))
    _run("sudo", "-n", "/usr/bin/systemctl", "daemon-reload")


def _stopped(units, uid):
    return (all(host._property(unit, "ActiveState") in ("inactive", "failed") for unit in units)
            and host._property(units[1], "MainPID") == "0"
            and not os.path.lexists(host.activation.socket_path(uid)))


def main(argv=None):
    if (sys.argv[1:] if argv is None else argv) != ["--allow-disposable-systemd-test"]:
        print("CI SYSTEMD: REFUSED (explicit disposable-CI flag required)")
        return 2
    started, passed, stage = False, False, "preflight"
    try:
        uid = _require_ci()
        units = ["ai-desktop-handoff@" + str(uid) + suffix for suffix in (".socket", ".service")]
        stage = "install"
        _install()
        stage = "start"
        started = True
        _run("sudo", "-n", "/usr/bin/systemctl", "start", units[0])
        stage = "activation"
        if host._run_checks() != 0:
            raise ValueError("activation")
        passed = True
    except (OSError, ValueError, subprocess.SubprocessError):
        print("CI SYSTEMD: FAIL stage=" + stage)
    finally:
        if started:
            try:
                _run("/usr/bin/systemctl", "show", units[1],
                     "--property=ActiveState,Result,ExecMainCode,ExecMainStatus,MainPID")
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                _run("sudo", "-n", "/usr/bin/systemctl", "stop", *units)
                if not _stopped(units, uid):
                    raise ValueError("still-active")
            except (OSError, ValueError, subprocess.SubprocessError):
                passed = False
                print("CI SYSTEMD: FAIL stage=cleanup")
    print("CI SYSTEMD: " + ("PASS activation=0 cleanup=0" if passed else "FAIL"))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
