"""Compare two validated diagnostic snapshots without executing commands."""
import json
import os
import stat
import sys

from .report_policy import MAX_REPORT_BYTES, serialize_report


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _decode_snapshot(data):
    """Apply the same fail-closed JSON policy to files and standard input."""
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("too large")
    try:
        report = json.loads(data.decode("utf-8"), object_pairs_hook=_unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JSON") from exc
    serialize_report(report)
    return report


def read_snapshot(path):
    if path == "-":
        # Read at most one byte above the limit; never close the caller's stdin.
        return _decode_snapshot(sys.stdin.buffer.read(MAX_REPORT_BYTES + 1))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError("not a regular file")
        data = file.read(MAX_REPORT_BYTES + 1)
    return _decode_snapshot(data)


def compare(before, after):
    serialize_report(before)
    serialize_report(after)
    changes = []
    for field in ("kernel", "desktop", "session", "shell", "root_fs", "home_fs"):
        if before[field] != after[field]:
            changes.append({"field": field, "before": before[field], "after": after[field]})
    for section in ("systemd_health", "btrfs_health"):
        for key in sorted(before[section]):
            old, new = before[section][key], after[section][key]
            if old != new:
                changes.append({"field": section + "." + key, "before": old, "after": new})
    return {"changed": bool(changes), "change_count": len(changes), "changes": changes}


def summarize(before, after):
    """Provide an allowlisted diff without disclosing field values."""
    diff = compare(before, after)
    return {
        "changed": diff["changed"],
        "change_count": diff["change_count"],
        "fields": [item["field"] for item in diff["changes"]],
    }


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    usage = ("Usage: python -m ai_desktop.compare "
             "[--summary] [--exit-on-change] OLD.json NEW.json")
    options = set()
    while args and args[0].startswith("--"):
        option = args.pop(0)
        if option not in ("--summary", "--exit-on-change") or option in options:
            print(usage)
            return 2
        options.add(option)
    if len(args) != 2 or args == ["-", "-"]:
        print(usage)
        return 2
    try:
        before, after = read_snapshot(args[0]), read_snapshot(args[1])
        summary_only = "--summary" in options
        result = summarize(before, after) if summary_only else compare(before, after)
    except (OSError, ValueError, TypeError):
        print("COMPARE: REJECTED")
        return 1
    print(json.dumps(result, indent=2))
    # The distinct code is opt-in; default CLI/API semantics stay unchanged.
    return 3 if "--exit-on-change" in options and result["changed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
