
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

from .report_policy import serialize_report
from .runtime_health import collect_btrfs_health, collect_systemd_health

FILESYSTEMS = {"btrfs", "ext4", "xfs", "f2fs", "zfs", "tmpfs"}
STATES = {"active", "inactive", "failed", "activating", "deactivating"}


def label(value):
    return re.sub(r"[^A-Za-z0-9_.+:-]", "?", value)[:48]


def query(command, allowed):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"

    value = result.stdout.strip()
    return value if value in allowed else "unknown"


def collect():
    root_fs = query(
        ["findmnt", "-T", "/", "-n", "-o", "FSTYPE"],
        FILESYSTEMS,
    )
    return {
        "kernel": label(platform.release()),
        "desktop": label(os.getenv("XDG_CURRENT_DESKTOP", "unknown")),
        "session": label(os.getenv("XDG_SESSION_TYPE", "unknown")),
        "shell": label(Path(os.getenv("SHELL", "unknown")).name),
        "root_fs": root_fs,
        "home_fs": query(
            ["findmnt", "-T", str(Path.home()), "-n", "-o", "FSTYPE"],
            FILESYSTEMS,
        ),
        "systemd_health": collect_systemd_health(),
        "btrfs_health": collect_btrfs_health(root_fs),
    }


def ask(report):
    """Fail closed until the A+ boundary and real-client consent are accepted."""
    serialize_report(report)
    print("AI E2: BLOCKED (no approved Claude/OAuth isolation)")
    return 2


def save_payload(payload, destination):
    """Create a private snapshot exclusively, without overwriting user files."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(destination, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(payload + "\n")


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv

    saving = (len(args) == 2 and args[0] == "save"
              and type(args[1]) is str and bool(args[1])
              and args[1] != "-" and not args[1].startswith("--"))
    if args not in (["preview"], ["snapshot"], ["ask"]) and not saving:
        print("Usage: python3 -B -m ai_desktop.diagnose preview|snapshot|save FILE|ask")
        return 2

    if os.geteuid() == 0:
        print("Do not run AI Desktop as root")
        return 2

    try:
        report = collect()
        payload = serialize_report(report)
    except ValueError:
        print("REPORT: REJECTED (ValueError)")
        return 1

    if args == ["snapshot"]:
        # stdout contains exactly one validated JSON object, suitable for compare.
        # No Claude invocation and no write to disk.
        print(payload, flush=True)
        return 0

    if saving:
        try:
            save_payload(payload, args[1])
        except OSError:
            # Never echo the filename, filesystem error details or JSON values.
            print("SNAPSHOT SAVE: REFUSED")
            return 2
        print("SNAPSHOT SAVE: PASS")
        return 0

    print("=== DIAGNOSTIC DATA ===", flush=True)
    print(payload, flush=True)

    if args == ["preview"]:
        print("PREVIEW: PASS (no data sent to AI)")
        return 0

    try:
        return ask(report)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print("AI: ERROR (" + type(exc).__name__ + ")")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
