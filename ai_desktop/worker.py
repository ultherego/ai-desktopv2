"""Offline, deterministic report assessment executed without Claude credentials."""
import json
import os
from pathlib import Path
import sys

from .report_policy import MAX_REPORT_BYTES, validate_report

ALLOWED_SIGNALS = frozenset({
    "systemd_degraded", "failed_units_present", "btrfs_low_space",
})
ALLOWED_UNKNOWN = frozenset({
    "systemd_state", "failed_units", "btrfs_free",
})


def evaluate(report):
    """Return only predefined categories, never unfiltered report content."""
    validate_report(report)
    signals = []
    unknown = []

    systemd = report["systemd_health"]
    if systemd["state"] == "degraded":
        signals.append("systemd_degraded")
    elif systemd["state"] == "unknown":
        unknown.append("systemd_state")
    count = systemd["failed_units"]
    if count == "unknown":
        unknown.append("failed_units")
    elif count > 0:
        signals.append("failed_units_present")

    if report["root_fs"] == "btrfs":
        space = report["btrfs_health"]["free_percent"]
        if space == "unknown":
            unknown.append("btrfs_free")
        elif space <= 15:
            signals.append("btrfs_low_space")

    return {"signals": signals, "unknown": unknown}


def probe():
    """Probe actual kernel namespaces and mounted view without reading secrets."""
    home = os.environ.get("HOST_HOME")
    netns = os.environ.get("HOST_NETNS")
    if not home or not netns:
        return False
    try:
        return all((
            Path.home() == Path("/home/worker"),
            not Path(home).exists(),
            not Path("/etc/passwd").exists(),
            not Path("/home/worker/.claude").exists(),
            not Path("/home/worker/.ssh").exists(),
            os.readlink("/proc/self/ns/net") != netns,
            bool(os.statvfs("/usr").f_flag & os.ST_RDONLY),
            not any(k in os.environ for k in (
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
            )),
        ))
    except OSError:
        return False


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--probe"]:
        passed = probe()
        print(json.dumps({"probe": "PASS" if passed else "FAIL"}))
        return 0 if passed else 1
    if argv != ["--check"]:
        return 2
    try:
        raw = sys.stdin.read(MAX_REPORT_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_REPORT_BYTES:
            raise ValueError("payload too large")
        report = json.loads(raw)
        result = evaluate(report)
    except (ValueError, TypeError, UnicodeError):
        print('{"error":"rejected"}')
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
