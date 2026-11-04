"""Strictly isolated, credential-free offline worker launcher.

This is NOT a general command executor and does not sandbox Claude itself.
Only two hardcoded worker actions are supported; there is no shell,
untrusted executable path, host HOME mount, or network access.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .report_policy import serialize_report
from .worker import ALLOWED_SIGNALS, ALLOWED_UNKNOWN

WORKER_FILES = (
    "__init__.py", "worker.py", "report_policy.py",
    "runtime_health.py",
)
PACKAGE_ROOT = Path(__file__).resolve().parent
RUNNER = (
    "import sys; sys.path.insert(0, '/opt'); "
    "from ai_desktop.worker import main; "
    "raise SystemExit(main())"
)


def build_command(source, action, host_home, host_netns):
    """Generate a fixed bwrap argv; never accept a model-supplied command."""
    if action not in ("--probe", "--check"):
        raise ValueError("Niedozwolone dzialanie wykonawcy")
    source = Path(source)
    if not source.is_absolute() or not source.is_dir() or source.is_symlink():
        raise ValueError("Nieprawidlowy katalog kodu")
    for name in WORKER_FILES:
        file = source / name
        if not file.is_file() or file.is_symlink():
            raise ValueError("Nieprawidlowy plik wykonawcy")

    argv = [
        "bwrap", "--unshare-user", "--unshare-pid", "--unshare-net",
        "--unshare-ipc", "--unshare-uts", "--die-with-parent",
        "--new-session", "--clearenv",
        "--setenv", "PATH", "/usr/bin",
        "--setenv", "HOME", "/home/worker",
        "--setenv", "HOST_HOME", str(host_home),
        "--setenv", "HOST_NETNS", str(host_netns),
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib", "/lib64",
        "--dev", "/dev", "--proc", "/proc",
        "--tmpfs", "/tmp", "--tmpfs", "/home",
        "--dir", "/home/worker",
        "--dir", "/opt", "--dir", "/opt/ai_desktop",
    ]
    for name in WORKER_FILES:
        argv.extend(["--ro-bind", str(source / name), "/opt/ai_desktop/" + name])
    return argv + [
        "--dir", "/work", "--chdir", "/work",
        "/usr/bin/python3", "-I", "-B", "-c", RUNNER, action,
    ]


def _run(action, payload=None):
    if shutil.which("bwrap") is None:
        raise RuntimeError("Brak bwrap")
    try:
        netns = os.readlink("/proc/self/ns/net")
    except OSError as exc:
        raise RuntimeError("Nie mozna odczytac przestrzeni sieci") from exc
    command = build_command(PACKAGE_ROOT, action, Path.home(), netns)
    try:
        process = subprocess.run(
            command,
            input=payload,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Wykonawca nie zakonczyl pracy") from exc
    if process.returncode != 0 or len(process.stdout) > 4096:
        raise RuntimeError("Wykonawca odrzucil zadanie")
    try:
        return json.loads(process.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Wykonawca zwrocil niepoprawna odpowiedz") from exc



def validate_assessment(response):
    """Reject any unexpected worker output before using or displaying it."""
    if type(response) is not dict or set(response) != {"signals", "unknown"}:
        raise RuntimeError("Niepoprawny schemat odpowiedzi")
    for key, allowed in (("signals", ALLOWED_SIGNALS), ("unknown", ALLOWED_UNKNOWN)):
        values = response[key]
        if type(values) is not list or len(values) > len(allowed):
            raise RuntimeError("Niepoprawna lista wynikow")
        if any(type(value) is not str or value not in allowed for value in values):
            raise RuntimeError("Niedozwolone dane z wykonawcy")
        if len(set(values)) != len(values):
            raise RuntimeError("Powtorzone dane z wykonawcy")
    return response


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args not in (["self-test"], ["inspect"]):
        print("Uzycie: python3 -B -m ai_desktop executor self-test|inspect")
        return 2
    if os.geteuid() == 0:
        print("EXECUTOR: REFUSED (root)")
        return 2
    try:
        if args == ["self-test"]:
            if _run("--probe") != {"probe": "PASS"}:
                raise RuntimeError("Izolacja nie zostala potwierdzona")
            print("EXECUTOR ISOLATION: PASS")
            return 0

        from .diagnose import collect

        payload = serialize_report(collect())
        response = validate_assessment(_run("--check", payload))
        print("=== WYNIK IZOLOWANEGO WYKONAWCY (bez AI) ===")
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, ValueError, OSError):
        print("EXECUTOR: FAIL (odmowa wykonania)")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
