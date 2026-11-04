"""E2: limited real-process sandbox for synthetic model/credential boundary probes.

No Claude, OAuth, network proxy or generic task execution exists here.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .report_policy import serialize_report, MAX_REPORT_BYTES

FILES = ("__init__.py", "e2_probe.py", "report_policy.py",
         "runtime_health.py")
SOURCE = Path(__file__).resolve().parent
RUNNER = ("import sys; sys.path.insert(0, '/opt'); "
          "from ai_desktop.e2_probe import main; raise SystemExit(main())")


def build_command(source, action, host_home, host_netns):
    if action not in ("--probe", "--inspect"):
        raise ValueError("invalid action")
    source = Path(source)
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise ValueError("invalid source")
    for name in FILES:
        file = source / name
        if file.is_symlink() or not file.is_file():
            raise ValueError("invalid source file")

    command = [
        "bwrap", "--unshare-user", "--unshare-pid", "--unshare-net",
        "--unshare-ipc", "--unshare-uts", "--die-with-parent",
        "--new-session", "--clearenv",
        "--setenv", "PATH", "/usr/bin",
        "--setenv", "HOME", "/home/model",
        "--setenv", "HOST_HOME", str(host_home),
        "--setenv", "HOST_NETNS", str(host_netns),
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib", "/lib64",
        "--dev", "/dev", "--proc", "/proc",
        "--tmpfs", "/tmp", "--tmpfs", "/home",
        "--dir", "/home/model", "--dir", "/opt",
        "--dir", "/opt/ai_desktop", "--dir", "/work",
    ]
    for name in FILES:
        command += ["--ro-bind", str(source / name), "/opt/ai_desktop/" + name]
    return command + [
        "--chdir", "/work", "/usr/bin/python3", "-I", "-B", "-c", RUNNER, action
    ]


def execute(action, report=None, runner=subprocess.run):
    """Run exactly one synthetic probe with no auth/network or host writes."""
    if action not in ("--probe", "--inspect"):
        raise ValueError("invalid action")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise RuntimeError("root denied")
    if shutil.which("bwrap") is None:
        raise RuntimeError("missing sandbox")
    try:
        netns = os.readlink("/proc/self/ns/net")
        payload = None if action == "--probe" else serialize_report(report)
        command = build_command(SOURCE, action, Path.home(), netns)
        result = runner(command, input=payload, text=True, capture_output=True,
                        check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise RuntimeError("boundary failed") from exc
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > 1024:
        raise RuntimeError("boundary refused")
    try:
        output = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid boundary output") from exc
    if action == "--probe":
        if output != {"probe": "PASS"}:
            raise RuntimeError("isolation not proven")
    else:
        import hashlib
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if type(output) is not dict or output != {
            "sha256": digest, "field_count": len(report)
        }:
            raise RuntimeError("untrusted response")
    return output


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args != ["self-test"]:
        print("Usage: python3 -B -m ai_desktop.e2_boundary self-test")
        return 2
    try:
        execute("--probe")
    except (RuntimeError, ValueError):
        print("E2 BOUNDARY: FAIL")
        return 1
    print("E2 BOUNDARY: PASS (synthetic process, no Claude or OAuth)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
