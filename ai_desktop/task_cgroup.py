"""Trusted preflight for multiprocess test suites, outside the test namespace.

systemd-run moves this bootstrap into its own transient user scope before it
execs Bubblewrap. Check the kernel limits themselves: successful unit creation
alone does not prove that a controller is delegated or enforcing its limits.
"""
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

PIDS = 48
MEMORY = 768 * 1024 * 1024
RUNTIME = 90
MULTIPROCESS = ("go-test", "npm-test")


def scope_name():
    return "ai-desktop-test-" + secrets.token_hex(16) + ".scope"


def scope_command(unit, sandbox):
    helper = Path(__file__).absolute()
    return ["/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect", "--no-ask-password",
            "--unit=" + unit, "-p", "TasksMax=%d" % PIDS, "-p", "MemoryMax=%d" % MEMORY,
            "-p", "MemorySwapMax=0", "-p", "CPUQuota=200%", "-p", "RuntimeMaxSec=%ds" % RUNTIME,
            "-p", "TimeoutStopSec=2s", "--", "/usr/bin/python3", "-I", "-B", str(helper), unit, *sandbox]


def verify_limits(unit, membership, root):
    if not re.fullmatch(r"ai-desktop-test-[0-9a-f]{32}\.scope", unit):
        raise ValueError("unit")
    groups = [line[3:] for line in membership.splitlines() if line.startswith("0::/")]
    if len(groups) != 1 or Path(groups[0]).name != unit or ".." in Path(groups[0]).parts:
        raise ValueError("scope membership")
    path = root / groups[0].lstrip("/")
    for key, ceiling in (("pids.max", PIDS), ("memory.max", MEMORY), ("memory.swap.max", 0)):
        raw = (path / key).read_text().strip()
        if not raw.isascii() or not raw.isdecimal() or not (0 if key == "memory.swap.max" else 1) <= int(raw) <= ceiling:
            raise ValueError(key)
    quota, period = (path / "cpu.max").read_text().split()
    if not quota.isdecimal() or not period.isdecimal() or not 0 < int(quota) <= 2 * int(period):
        raise ValueError("cpu.max")


def cleanup(unit, env):
    # An empty, collected scope is ordinary. The kernel PID namespace and the
    # scope's own lifetime limit also apply if the parent or manager disappears.
    try:
        subprocess.run(["/usr/bin/systemctl", "--user", "--no-ask-password", "kill",
                        "--signal=KILL", unit], env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


if __name__ == "__main__":
    try:
        if os.geteuid() == 0 or len(sys.argv) < 3 or sys.argv[2] != "/usr/bin/bwrap":
            raise ValueError("launcher")
        verify_limits(sys.argv[1], Path("/proc/self/cgroup").read_text(), Path("/sys/fs/cgroup"))
        os.execv("/usr/bin/bwrap", sys.argv[2:])
    except (OSError, ValueError):
        print("Test refused: the transient user scope does not enforce the required cgroup v2 limits.", file=sys.stderr)
        raise SystemExit(2)
