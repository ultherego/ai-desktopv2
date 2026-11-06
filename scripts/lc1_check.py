#!/usr/bin/env python3
"""LC1 acceptance helper: synthetic files in ~/ai-lc1-check and file-based checks.

prepare          creates the fixtures (small module, ~200 KB log, exactly 1 MiB, 64 MiB + 1, ANSI, bad UTF-8, empty)
refusals         runs the INSTALLED loader under the owner's REAL policy on every fixture and
                 compares the outcome with the expected closed refusal (no terminal, no model)
check-unchanged  proves the fixtures are byte-identical and nothing else appeared
cleanup          removes the directory
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys

NAME = "ai-lc1-check"
SIZE = 1024 * 1024
CEILING = 64 * 1024 * 1024               # hard ceiling of the owner's file_size setting: always refused
MARK = "ERROR-MARKER"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def good_log():
    lines, total, number = [], 0, 0
    while True:
        number += 1
        text = "2026-10-31T12:00:00 INFO line %06d heartbeat ok\n" % number
        if number in (4, 7, 9000):
            text = "2026-10-31T12:00:00 %s line %06d something failed\n" % (MARK, number)
        if total + len(text) > SIZE - 1:
            break
        lines.append(text)
        total += len(text)
    return ("".join(lines) + "x" * (SIZE - total - 1) + "\n").encode()


SMALL = """\"\"\"Small synthetic module for the whole-file question.\"\"\"


def average(values):
    total = 0
    for value in values:
        total += value
    return total / len(values)


def clamp(value, low, high):
    if value < low:
        return low
    if value > high:
        return high
    return value


def parse_port(text):
    port = int(text)
    if port < 0 or port > 65536:
        raise ValueError("port out of range")
    return port


def first_word(text):
    return text.split()[0]
"""

def medium_log():
    lines = ["2026-10-31T12:00:00 INFO row %06d filler text to make this log long enough to need parts" % i
             for i in range(1, 3001)]
    lines[99] = "2026-10-31T12:00:00 ERROR-MARKER row 000100 first failure code=E-100"
    lines[2899] = "2026-10-31T12:00:00 ERROR-MARKER row 002900 last failure code=E-2900"
    return ("\n".join(lines) + "\n").encode()


FIXTURES = {
    "small.py": (lambda: SMALL.encode(), None),
    "medium.log": (medium_log, None),
    "log1m.txt": (good_log, None),
    "over.txt": (lambda: b"x" * (CEILING + 1), "is over"),
    "ansi.txt": (lambda: b"\x1b[31mred\x1b[0m failed\n", "control"),
    "bad-utf8.txt": (lambda: b"valid start\n\xff\xfe broken\n", "UTF-8"),
    "empty.txt": (lambda: b"", "empty"),
}


def base():
    path = Path.home() / NAME
    if os.geteuid() == 0 or path.is_symlink():
        raise SystemExit("REFUSED")
    return path


def manifest(root):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        result[str(path.relative_to(root))] = [stat.S_IMODE(info.st_mode), info.st_size,
                                               sha(path.read_bytes()) if stat.S_ISREG(info.st_mode) else None]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "refusals", "check-unchanged", "cleanup"))
    parser.add_argument("--lib", help="package directory to test (installed code)")
    args = parser.parse_args(argv)
    if args.lib:
        sys.path.insert(0, args.lib)
    root = base()
    if args.action == "prepare":
        if root.exists():
            print("REFUSED: " + str(root) + " exists; run cleanup first")
            return 2
        root.mkdir(mode=0o700)
        for name, (build, _) in FIXTURES.items():
            path = root / name
            path.write_bytes(build())
            path.chmod(0o600)
        (root / ".manifest.json").write_text(json.dumps(manifest(root), sort_keys=True))
        print("PREPARED " + str(root) + " (log1m.txt = %d bytes)" % (root / "log1m.txt").stat().st_size)
        return 0
    if args.action == "cleanup":
        shutil.rmtree(root)
        print("CLEANED")
        return 0
    expected = json.loads((root / ".manifest.json").read_text())
    if args.action == "check-unchanged":
        current = manifest(root)
        current.pop(".manifest.json", None)
        expected.pop(".manifest.json", None)
        problems = sorted(k for k in set(current) | set(expected) if current.get(k) != expected.get(k))
        for key in problems:
            print("PROBLEM: " + key)
        print("CHECK-UNCHANGED=" + ("PASS" if not problems else "FAIL"))
        return 0 if not problems else 1
    from ai_desktop import material_cli, permissions
    try:
        policy = permissions.load_policy()
    except ValueError as exc:
        print("POLICY: " + str(exc))
        return 3
    failures = 0
    limit = policy.file_size()
    for name, (_, phrase) in FIXTURES.items():
        path = str(root / name)
        try:
            policy.select([path])
        except ValueError:
            print("POLICY: DENIED for %s (add ~/%s/* to files.read to run this acceptance)" % (name, NAME))
            return 3
        try:
            selected, material = material_cli.load_material(path, policy)
            outcome = "ACCEPTED %d bytes sha256 %s" % (material.size, material.sha256[:12])
            ok = (phrase is None and (root / name).stat().st_size <= limit and material.sha256 == sha((root / name).read_bytes())
                  and {"log1m.txt": lambda n: n == SIZE, "medium.log": lambda n: 150000 < n < 300000}.get(
                      name, lambda n: 0 < n < 4096)(material.size))
        except ValueError as exc:
            outcome = "REFUSED: " + str(exc)
            ok = (phrase is not None and phrase in str(exc)) or (
                phrase is None and (root / name).stat().st_size > limit and "is over" in str(exc))      # a smaller file_size is the owner's choice
        print("%-13s %s %s" % (name, "OK  " if ok else "FAIL", outcome))
        failures += not ok
    print("REFUSALS=" + ("PASS" if not failures else "FAIL"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
