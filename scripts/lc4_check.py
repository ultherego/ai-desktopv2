#!/usr/bin/env python3
"""LC4 acceptance helper: a synthetic project in ~/ai-lc4-check and file-based checks (no model, no terminal).

prepare          creates the project (200 modules, a 5 MiB log, docs, and hostile entries: a denied directory,
                 a symlink, a FIFO, a hard-linked pair, a .git directory)
policy           tells whether the owner's REAL policy allows the root and denies secret/ (it never edits the policy)
scan             runs the INSTALLED scanner/search/reader under the real policy and compares with the expectations
check-unchanged  proves the project is byte-identical and nothing else appeared
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

NAME = "ai-lc4-check"
MODULES = 200
BIG_LINES = 110000
BIG_MARKER_LINE = 90000
MARKER = "LC4-BIG-MARKER code=E-7421"
DENIED = "LC4-DENIED-CONTENT"
EXPECTED = {"indexed": MODULES + 3, "denied": 1, "links": 1, "special": 1, "unsafe": 2, "vcs": 1, "other": 0}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def base():
    path = Path.home() / NAME
    if os.geteuid() == 0 or path.is_symlink():
        raise SystemExit("REFUSED")
    return path


def manifest(root):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        regular = stat.S_ISREG(info.st_mode)
        result[str(path.relative_to(root))] = [stat.S_IMODE(info.st_mode), info.st_size if regular else 0,
                                               sha(path.read_bytes()) if regular else None, info.st_nlink if regular else 0]
    return result


def prepare(root):
    root.mkdir(mode=0o700)
    for index in range(MODULES):
        package, number = divmod(index, 20)
        target = root / ("src/pkg%02d/mod%02d.py" % (package, number))
        target.parent.mkdir(parents=True, exist_ok=True)
        body = "def func_%d():\n    return %d\n" % (index, index)
        if (package, number) == (3, 7):
            body += "CONNECT_TIMEOUT = 5  # seconds, defined here (LC4-CONFIG-A)\n"
        if (package, number) == (7, 11):
            body += "def wait():\n    sleep(CONNECT_TIMEOUT / 1000)  # treats the value as milliseconds (LC4-CONFIG-B)\n"
        target.write_text(body)
    (root / "docs").mkdir()
    (root / "docs/readme.txt").write_text("Synthetic LC4 project for acceptance.\n")
    (root / "big").mkdir()
    lines = ["2026-10-31T12:00:%02d INFO heartbeat ok row %06d" % (i % 60, i) for i in range(1, BIG_LINES + 1)]
    lines[BIG_MARKER_LINE - 1] = "2026-10-31T12:00:00 ERROR %s something failed" % MARKER
    (root / "big/data.log").write_text("\n".join(lines) + "\n")
    (root / "secret").mkdir()
    (root / "secret/passwords.txt").write_text(DENIED + "\n")
    (root / "src/hl.txt").write_text("hard link source\n")
    os.link(root / "src/hl.txt", root / "src/hl-copy.txt")
    os.symlink("/etc/hostname", root / "outside-link")
    os.mkfifo(root / "pipe")
    (root / ".git").mkdir()
    (root / ".git/config").write_text("[remote]\n\turl = https://token-LC4@example.invalid/x.git\n")
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            path.chmod(0o600)
    (root / ".manifest.json").write_text(json.dumps(manifest(root), sort_keys=True))
    print("PREPARED %s (%d indexed files expected incl. the manifest, big/data.log = %d bytes)" % (
        root, EXPECTED["indexed"], (root / "big/data.log").stat().st_size))
    return 0


def policy_check(root):
    from ai_desktop import permissions
    try:
        policy = permissions.load_policy()
    except ValueError as exc:
        print("POLICY: " + str(exc))
        return 3
    if not policy.allows(str(root) + "/_") or policy.excludes(str(root) + "/_"):
        print("POLICY: DENIED. Add to files.read of permissions.toml: \"~/%s/**\" (nothing was changed)." % NAME)
        return 3
    if not policy.excludes(str(root) + "/secret/x"):
        print("POLICY: secret/ is not denied. Add to files.deny: \"~/%s/secret/**\" to test the deny rule (nothing was changed)." % NAME)
        return 3
    print("POLICY=PASS")
    return 0


def scan_check(root):
    from ai_desktop import permissions, project_scope as ps
    if policy_check(root):
        return 3
    policy = permissions.load_policy()
    project = ps.Project(policy, str(root)).scan()
    problems = []

    def check(label, ok, shown=""):
        print("%-34s %s %s" % (label, "OK  " if ok else "FAIL", shown))
        if not ok:
            problems.append(label)
    stats = project.stats
    check("complete scan", project.complete, str(project.reason))
    for key, want in EXPECTED.items():
        check("count " + key, stats[key] == want, "%d (expected %d)" % (stats[key], want))
    names = [e.path for e in project.files]
    check("no denied/link/pipe names", not any(n.startswith(("secret", ".git")) or n in ("pipe", "outside-link", "src/hl.txt", "src/hl-copy.txt") for n in names))
    check("find 'secret' is empty", not project.find_names("secret")[0])
    check("denied content not searchable", not project.search_text(DENIED).hits)
    check(".git content not searchable", not project.search_text("token-LC4").hits)
    found = project.search_text("LC4-CONFIG-")
    check("multi-module markers found", sorted(h.path for h in found.hits) == ["src/pkg03/mod07.py", "src/pkg07/mod11.py"])
    loaded = project.read_file("big/data.log")
    check("5 MiB file read as material", loaded.material.size > 5 * 1024 * 1024 and not loaded.changed, str(loaded.material.size))
    big = project.search_text(MARKER)
    check("marker in big log at line %d" % BIG_MARKER_LINE, [(h.path, h.line) for h in big.hits] == [("big/data.log", BIG_MARKER_LINE)])
    check("outside-link not readable", _refused(project, "outside-link"))
    check("denied file not readable", _refused(project, "secret/passwords.txt"))
    print("SCAN=" + ("PASS" if not problems else "FAIL"))
    return 0 if not problems else 1


def _refused(project, path):
    from ai_desktop import project_scope as ps
    try:
        project.read_file(path)
    except ps.ProjectError as exc:
        return exc.code == "PROJECT_PATH"
    return False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "policy", "scan", "check-unchanged", "cleanup"))
    parser.add_argument("--lib", help="package directory to test (installed code)")
    args = parser.parse_args(argv)
    if args.lib:
        sys.path.insert(0, args.lib)
    root = base()
    if args.action == "prepare":
        if root.exists():
            print("REFUSED: " + str(root) + " exists; run cleanup first")
            return 2
        return prepare(root)
    if args.action == "cleanup":
        shutil.rmtree(root)
        print("CLEANED")
        return 0
    if args.action == "policy":
        return policy_check(root)
    if args.action == "scan":
        return scan_check(root)
    expected = json.loads((root / ".manifest.json").read_text())
    current = manifest(root)
    current.pop(".manifest.json", None)
    expected.pop(".manifest.json", None)
    problems = sorted(k for k in set(current) | set(expected) if current.get(k) != expected.get(k))
    for key in problems:
        print("PROBLEM: " + key)
    print("CHECK-UNCHANGED=" + ("PASS" if not problems else "FAIL"))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
