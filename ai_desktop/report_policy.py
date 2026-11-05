"""Fail-closed policy for data passed to external AI providers.

This allowlist defines the entire diagnostic data contract. Adding a new collector
must not silently expand the information disclosed to Claude.
"""

import json
import re

from .runtime_health import COMPRESSION, FAILED_LIMIT, SYSTEM_STATES

FILESYSTEMS = frozenset({"btrfs", "ext4", "xfs", "f2fs", "zfs", "tmpfs", "unknown"})
FIELDS = frozenset({
    "kernel", "desktop", "session", "shell",
    "root_fs", "home_fs", "systemd_health", "btrfs_health",
})
SYSTEMD_FIELDS = frozenset({"state", "failed_units"})
BTRFS_FIELDS = frozenset({"free_percent", "compression"})
SAFE_LABEL = re.compile(r"[A-Za-z0-9_.+?:-]{1,48}\Z")
MAX_REPORT_BYTES = 4096


def _exact_fields(data, expected, path):
    if type(data) is not dict or set(data) != set(expected):
        raise ValueError(f"Invalid set of fields: {path}")


def _choice(value, allowed, path):
    if type(value) is not str or value not in allowed:
        raise ValueError(f"Value not allowed: {path}")


def _bounded_number(value, upper, path):
    if value == "unknown" and type(value) is str:
        return
    if type(value) is not int or not 0 <= value <= upper:
        raise ValueError(f"Number not allowed: {path}")


def validate_report(report):
    """Reject unexpected fields, binary data, arbitrary text and noncanonical values."""
    _exact_fields(report, FIELDS, "report")

    for key in ("kernel", "desktop", "shell"):
        value = report[key]
        if type(value) is not str or not SAFE_LABEL.fullmatch(value):
            raise ValueError(f"Label not allowed: {key}")

    _choice(report["session"], {"wayland", "x11", "tty", "unknown"}, "session")
    for key in ("root_fs", "home_fs"):
        _choice(report[key], FILESYSTEMS, key)
    systemd = report["systemd_health"]
    _exact_fields(systemd, SYSTEMD_FIELDS, "systemd_health")
    _choice(systemd["state"], SYSTEM_STATES, "systemd_health.state")
    _bounded_number(systemd["failed_units"], FAILED_LIMIT, "systemd_health.failed_units")

    btrfs = report["btrfs_health"]
    _exact_fields(btrfs, BTRFS_FIELDS, "btrfs_health")
    _choice(btrfs["compression"], COMPRESSION, "btrfs_health.compression")
    _bounded_number(btrfs["free_percent"], 100, "btrfs_health.free_percent")
    if report["root_fs"] != "btrfs":
        if btrfs != {"compression": "unknown", "free_percent": "unknown"}:
            raise ValueError("Btrfs data for a different file system")

    return report


def serialize_report(report):
    """Return a deterministic, small JSON payload, or refuse to send anything."""
    validate_report(report)
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if len(payload.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("The report exceeds the size limit")
    return payload
