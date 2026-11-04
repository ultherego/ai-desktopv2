#!/usr/bin/env python3
"""E6 full-cycle checks that read the filesystem, never trust a PASS message.

Used by tests/test_e6_cycle.py (offline, fake model) and by the native
acceptance (scripts/accept_e6_cycle.fish) on a fresh synthetic project in
~/ai-e6-check. Each check returns a list of problems; an empty list is a pass.

Commands (native): prepare | check-unchanged | check-written | check-rolled-back | cleanup
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys

NAME = "ai-e6-check"
STATE = ".ai-desktop-tasks"
TARGET = "calc.py"
FILES = {
    "calc.py": ("def add(a, b):\n    return a + b\n", 0o640),
    "README.txt": ("Synthetic README, not selected for the task.\n", 0o600),
    "docs/notes.txt": ("Synthetic notes, not selected for the task.\n", 0o640),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def manifest(root, *, skip_state=False):
    """Every object below root: kind, mode, content hash or link target."""
    result = {}
    for path in sorted(Path(root).rglob("*")):
        rel = str(path.relative_to(root))
        if skip_state and (rel == STATE or rel.startswith(STATE + "/")):
            continue
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            result[rel] = ["link", mode, os.readlink(path)]
        elif stat.S_ISDIR(info.st_mode):
            result[rel] = ["dir", mode, None]
        elif stat.S_ISREG(info.st_mode):
            result[rel] = ["file", mode, sha(path.read_bytes())]
        else:
            result[rel] = ["other", mode, None]
    return result


def create_project(root):
    root = Path(root)
    for rel, (text, mode) in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
    return manifest(root)


def _stages(root):
    return [p.name for p in Path(root).rglob(".ai-task-*")]


def _journals(root):
    state = Path(root) / STATE
    return sorted(p for p in state.glob("*.json")) if state.is_dir() else []


def check_unchanged(root, expected):
    """Nothing was written: identical tree, no history, no leftovers."""
    problems = []
    if manifest(root) != expected:
        problems.append("tree differs from the recorded state")
    if (Path(root) / STATE).exists():
        problems.append("history directory exists although nothing was approved")
    if _stages(root):
        problems.append("stage files left behind")
    return problems


def check_written(root, expected, target=TARGET):
    """Only the target changed; the journal describes exactly what is on disk."""
    problems = []
    now = manifest(root, skip_state=True)
    changed = sorted(k for k in set(now) | set(expected) if now.get(k) != expected.get(k))
    if changed != [target]:
        problems.append("changed objects differ from only " + target + ": " + ",".join(changed))
        return problems
    if now[target][2] == expected[target][2]:
        problems.append("target bytes unchanged")
    if now[target][1] != expected[target][1]:
        problems.append("target mode changed")
    journals = _journals(root)
    if len(journals) != 1:
        return problems + ["expected exactly one journal"]
    value = json.loads(journals[0].read_text())
    entry = value["entries"][0] if len(value.get("entries", [])) == 1 else None
    data = (Path(root) / target).read_bytes()
    if value.get("status") != "applied" or entry is None:
        problems.append("journal is not a single applied entry")
    else:
        if entry["after"] is None or entry["after"]["sha256"] != sha(data):
            problems.append("journal 'after' does not match the bytes on disk")
        original = value["before"]["files"][target]["file"]
        if original is None or original["sha256"] != expected[target][2]:
            problems.append("journal original does not match the recorded original")
    if _stages(root):
        problems.append("stage files left behind")
    return problems


def check_rolled_back(root, expected, target=TARGET):
    """Rollback restored the exact original bytes and mode, nothing else moved."""
    problems = []
    if manifest(root, skip_state=True) != expected:
        problems.append("tree differs from the original (bytes, modes or names)")
    journals = _journals(root)
    if len(journals) != 1 or json.loads(journals[0].read_text()).get("status") != "rolled-back":
        problems.append("journal is not rolled-back")
    if _stages(root):
        problems.append("stage files left behind")
    return problems


def _base():
    base = Path.home() / NAME
    if os.geteuid() == 0 or base.is_symlink():
        raise SystemExit("REFUSED")
    return base


def _evidence(base):
    return json.loads((base / "evidence.json").read_text())


def main(argv=None):
    parser = argparse.ArgumentParser(prog="e6_cycle")
    parser.add_argument("action", choices=("prepare", "check-unchanged", "check-written",
                                           "check-rolled-back", "cleanup"))
    args = parser.parse_args(argv)
    base = _base()
    ws = base / "ws"
    if args.action == "prepare":
        if base.exists():
            print("REFUSED: " + str(base) + " exists; run cleanup first")
            return 2
        base.mkdir(mode=0o700)
        ws.mkdir(mode=0o700)
        expected = create_project(ws)
        (base / "evidence.json").write_text(json.dumps({"expected": expected}, sort_keys=True))
        print("PREPARED " + str(ws))
        return 0
    if args.action == "cleanup":
        if (ws / STATE).exists() and not any(
                json.loads(p.read_text()).get("status") == "rolled-back" for p in _journals(ws)):
            print("REFUSED: an applied task is not rolled back; nothing removed")
            return 2
        shutil.rmtree(base)
        print("CLEANED")
        return 0
    expected = _evidence(base)["expected"]
    check = {"check-unchanged": check_unchanged, "check-written": check_written,
             "check-rolled-back": check_rolled_back}[args.action]
    problems = check(ws, expected)
    for problem in problems:
        print("PROBLEM: " + problem)
    print(args.action.upper() + ("=PASS" if not problems else "=FAIL"))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
