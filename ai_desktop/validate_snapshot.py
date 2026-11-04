"""Validate an existing offline diagnostic snapshot without echoing its contents.

No host collection, subprocesses, model calls or file writes are performed.
"""
import json
import sys

from .compare import read_snapshot


def inspect(path):
    """Return only harmless validation metadata for one allowlisted JSON file."""
    report = read_snapshot(path)
    return {"valid": True, "field_count": len(report)}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or not isinstance(args[0], str) or args[0].startswith("--"):
        print("Usage: python -m ai_desktop.validate_snapshot REPORT.json")
        return 2
    try:
        result = inspect(args[0])
    except (OSError, ValueError, TypeError):
        # No exception text, host paths, JSON values or secrets in output.
        print("VALIDATE: REJECTED")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
