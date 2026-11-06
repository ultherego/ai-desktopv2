"""Match references to request-local observations from the command broker.

This module never executes a command or treats model/user prose as a version
probe. Its caller must supply an actual completed broker result. Package epochs
and release suffixes describe packaging; git/post-release suffixes remain part
of the upstream version and must not silently match a release manual.
"""
import re

_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+_.-]{0,127}\Z")
_VERSION = re.compile(r"(?:[0-9]+:)?([A-Za-z0-9][A-Za-z0-9.+_~]{0,127})-[0-9]+(?:\.[0-9]+)?\Z")


def trusted_version_observation(argv, result):
    """Accept one explicit pacman -Q PACKAGE result; otherwise return no fact."""
    if (type(argv) not in (list, tuple) or len(argv) != 3
            or argv[0] not in ("pacman", "/usr/bin/pacman") or argv[1] != "-Q"
            or type(argv[2]) is not str or not _PACKAGE.fullmatch(argv[2])
            or type(result) is not dict or result.get("op") != "run"
            or result.get("argv") != list(argv) or result.get("role", "read") != "read"
            or result.get("status") != "ok" or type(result.get("exit")) is not int
            or result["exit"] != 0 or result.get("truncated") is not False):
        return {}
    output = result.get("output")
    if type(output) is not str:
        return {}
    try:
        if len(output.encode("utf-8")) > 512:
            return {}
    except UnicodeError:
        return {}
    # No warnings, additional package rows, terminal escapes or embedded lines.
    match = re.fullmatch(re.escape(argv[2]) + r" ([^\s]+)\n?", output)
    if not match:
        return {}
    version = _VERSION.fullmatch(match.group(1))
    return {argv[2]: version.group(1)} if version else {}


def reference_compatibility(metadata, versions):
    """Return matched/mismatch/unknown/rolling without guessing compatibility."""
    if type(metadata) is not dict or type(versions) is not dict:
        return "unknown"
    expected, names = metadata.get("version"), metadata.get("tools")
    if expected == "rolling":
        return "rolling"
    if (type(expected) is not str or not expected or type(names) is not list
            or not names or any(type(name) is not str for name in names)):
        return "unknown"
    observed = [versions.get(name) for name in names]
    if any(type(value) is str and value != expected for value in observed):
        return "mismatch"
    if all(type(value) is str and value == expected for value in observed):
        return "matched"
    return "unknown"
