"""Read-only, bounded summaries of systemd and the root Btrfs mount.

Collector output is intentionally small: no raw unit names, journal entries,
mount paths, device identifiers or command output may enter the AI report.
"""

import os
import re
import subprocess

SYSTEM_STATES = frozenset({
    "running", "degraded", "starting", "maintenance", "stopping",
    "initializing", "offline", "unknown",
})
COMPRESSION = frozenset({"zstd", "zlib", "lzo", "none", "unknown"})
UNIT_LINE = re.compile(
    r"^(?:[●*]\s+)?\S+\.(?:service|socket|timer|target|mount|automount|"
    r"device|swap|path|scope|slice)\s"
)
COMPRESS_OPTION = re.compile(
    r"(?:^|,)compress(?:-force)?=(zstd|zlib|lzo)(?::[0-9]+)?(?:,|$)"
)
FAILED_LIMIT = 512


def _read(args):
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if len(result.stdout) > 32768:
        return None
    return result


def collect_systemd_health():
    """Return summary states, not sensitive unit names or log contents."""
    result = {"state": "unknown", "failed_units": "unknown"}

    state = _read(["systemctl", "is-system-running"])
    if state is not None and state.returncode in (0, 1):
        candidate = state.stdout.strip()
        if candidate in SYSTEM_STATES:
            result["state"] = candidate

    failed = _read([
        "systemctl", "list-units", "--failed", "--all",
        "--no-legend", "--plain", "--no-pager",
    ])
    if failed is None or failed.returncode != 0:
        return result

    lines = [line.strip() for line in failed.stdout.splitlines() if line.strip()]
    if len(lines) <= FAILED_LIMIT and all(UNIT_LINE.match(line) for line in lines):
        result["failed_units"] = len(lines)
    return result


def collect_btrfs_health(root_fs):
    """Summarize only the root mount, and only if it is known to be Btrfs."""
    result = {"free_percent": "unknown", "compression": "unknown"}
    if root_fs != "btrfs":
        return result

    try:
        stats = os.statvfs("/")
        if stats.f_blocks > 0:
            percent = round(100 * stats.f_bavail / stats.f_blocks)
            if 0 <= percent <= 100:
                result["free_percent"] = percent
    except OSError:
        pass

    options = _read(["findmnt", "-T", "/", "-n", "-o", "OPTIONS"])
    if options is None or options.returncode != 0:
        return result

    raw = options.stdout.strip()
    if not raw or len(raw) > 4096 or "\n" in raw:
        return result

    match = COMPRESS_OPTION.search(raw)
    if match:
        result["compression"] = match.group(1)
    elif not re.search(r"(?:^|,)compress(?:-force)?(?:=|,|$)", raw):
        result["compression"] = "none"
    return result
