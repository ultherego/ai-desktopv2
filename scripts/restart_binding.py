#!/usr/bin/env python3
"""E5 restart acceptance helper: synthetic task before/after a natural restart.

prepare  creates ~/ai-restart-check/{ws,evidence.json}: a NEW schema-3 task on a
         synthetic file inside a private workspace on the owner's filesystem.
verify   after a restart: proves the history binding, file state and a
         controlled rollback with byte and mode comparison.
cleanup  removes the synthetic directory once verify rolled the task back.

Only the synthetic directory is touched, never the owner's files or the older
private journals. The computer is never restarted by this program, device
numbers are never edited, and a missing st_dev change is reported as such:
RESTART_CHECKED_REBOUND_NOT_OBSERVED is NOT a pass of the rebound scenario.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time

BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
NAME = "ai-restart-check"
ORIGINAL = "ORIGINAL SYNTHETIC CONTENT\n"
CHANGED = "CHANGED SYNTHETIC CONTENT\n"
MODE = 0o640
TARGET = "notes.txt"
KEYS = {"version", "base", "created", "boot_id", "dev", "root_ino", "fs", "plan", "journal",
        "journal_sha256", "target_inode", "original_sha256", "changed_sha256", "mode"}


def boot_id():
    return BOOT_ID.read_text().strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tree(root):
    """Names, types, modes and content hashes of everything below root."""
    result = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        kind = "dir" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other"
        result[str(path.relative_to(root))] = [kind, stat.S_IMODE(info.st_mode),
                                               sha(path.read_bytes()) if kind == "file" else None]
    return result


def default_base():
    return Path.home() / NAME


def _modules():
    from ai_desktop import fs_identity, task_cli, task_workspace as w
    from ai_desktop.task_plan import TaskPlan
    return fs_identity, task_cli, w, TaskPlan


def _guard_base(base):
    base = Path(base)
    home = Path.home()
    if (base.parent != home or base.name != NAME or os.geteuid() == 0
            or base.is_symlink()):
        raise SystemExit("REFUSED: base must be exactly ~/" + NAME + " for a regular user")
    return base


def _read_evidence(base):
    path = base / "evidence.json"
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
        raise ValueError("evidence file")
    value = json.loads(path.read_text())
    if type(value) is not dict or set(value) != KEYS or value["version"] != 1 or value["base"] != str(base):
        raise ValueError("evidence schema")
    return value


def prepare(base):
    fs_identity, _, w, TaskPlan = _modules()
    base = _guard_base(base)
    if base.exists() or base.is_symlink():
        print("REFUSED: " + str(base) + " already exists; run cleanup after a finished check")
        return 2
    base.mkdir(mode=0o700)
    ws = base / "ws"
    ws.mkdir(mode=0o700)
    target = ws / TARGET
    target.write_text(ORIGINAL)
    target.chmod(MODE)
    before = w.snapshot(str(ws), [TARGET])
    if before["fs"] is None:
        shutil.rmtree(base)
        print("REFUSED: no durable filesystem identity here (HOME is not on Btrfs?); nothing created")
        return 2
    source = {"instruction": "synthetic restart check",
              "files": [{"path": TARGET, "content": ORIGINAL, "writable": True}]}
    plan = TaskPlan(before, source, {"summary": "synthetic", "files": [
        {"path": TARGET, "content": CHANGED}]}, "text")
    w.apply(plan, plan.authorize("ZAPISZ " + plan.sha256))
    journal = ws / w.STATE / (plan.sha256 + ".json")
    value = json.loads(journal.read_text())
    if value["schema"] != 3 or value["before"]["fs"] != before["fs"]:
        raise SystemExit("FAIL: new journal is not schema 3 with the durable identity")
    evidence = {"version": 1, "base": str(base), "created": int(time.time()), "boot_id": boot_id(),
                "dev": os.stat(ws).st_dev, "root_ino": os.stat(ws).st_ino, "fs": before["fs"],
                "plan": plan.sha256, "journal": journal.name, "journal_sha256": sha(journal.read_bytes()),
                "target_inode": target.stat().st_ino, "original_sha256": sha(ORIGINAL.encode()),
                "changed_sha256": sha(CHANGED.encode()), "mode": MODE}
    out = base / "evidence.json"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    print("PREPARED: schema=3 st_dev=%d boot_id=%s plan=%s" % (evidence["dev"], evidence["boot_id"], evidence["plan"]))
    print("Fs: " + json.dumps(evidence["fs"], sort_keys=True))
    print("Teraz zrestartuj komputer ręcznie, potem uruchom verify. Nie ruszaj " + str(base) + ".")
    return 0


def verify(base):
    fs_identity, task_cli, w, _ = _modules()
    base = _guard_base(base)
    ws = base / "ws"
    try:
        evidence = _read_evidence(base)
    except (OSError, ValueError):
        print("FAIL: evidence missing or invalid; run prepare first")
        return 1
    if boot_id() == evidence["boot_id"]:
        print("RESULT=NO_RESTART (boot_id unchanged); nothing was changed, restart and run verify again")
        return 3
    info = os.stat(ws)
    if info.st_ino != evidence["root_ino"]:
        print("FAIL: workspace root inode differs from the recorded one")
        return 1
    dev_changed = info.st_dev != evidence["dev"]
    print("RESTART_DETECTED: boot_id changed; st_dev %s (%d -> %d)" % (
        "CHANGED" if dev_changed else "unchanged", evidence["dev"], info.st_dev))
    journal = ws / w.STATE / evidence["journal"]
    if sha(journal.read_bytes()) != evidence["journal_sha256"]:
        print("FAIL: journal changed since prepare")
        return 1
    target = ws / TARGET
    state = target.stat()
    if (sha(target.read_bytes()) != evidence["changed_sha256"] or stat.S_IMODE(state.st_mode) != evidence["mode"]
            or state.st_ino != evidence["target_inode"]):
        print("FAIL: synthetic file differs from the applied state; no rollback attempted")
        return 1
    fd = os.open(ws, os.O_RDONLY | os.O_DIRECTORY)
    try:
        current = fs_identity.identity(fd)
    finally:
        os.close(fd)
    if current != evidence["fs"]:
        print("FAIL: durable filesystem identity differs; history must NOT be bound (detached expected)")
        return 1
    records = w.recovery_records(str(ws))
    if len(records) != 1 or records[0]["plan"] != evidence["plan"] or records[0]["status"] != "applied":
        print("FAIL: unexpected history records")
        return 1
    if bool(records[0].get("rebound")) != dev_changed:
        print("FAIL: classification does not match the observed st_dev change")
        return 1
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        task_cli_code = task_cli.main(["status", "--workspace", str(ws)])
    if task_cli_code != 0 or "Stan: zapisano" not in out.getvalue() or "niepowiązana" in out.getvalue():
        print("FAIL: task status does not show a normal stored task")
        return 1
    if not w.rollback(str(ws), evidence["plan"], expected_journal=records[0]["journal_sha256"]):
        print("FAIL: controlled rollback reported a conflict; files kept")
        return 1
    state = target.stat()
    if (sha(target.read_bytes()) != evidence["original_sha256"] or stat.S_IMODE(state.st_mode) != evidence["mode"]
            or json.loads(journal.read_text())["status"] != "rolled-back"):
        print("FAIL: rollback result differs from the original bytes/mode")
        return 1
    extra = set(tree(ws)) - {TARGET, w.STATE, w.STATE + "/.gitignore", w.STATE + "/" + evidence["journal"]}
    if extra:
        print("FAIL: unexpected objects in the synthetic workspace")
        return 1
    result = "REBOUND_OBSERVED" if dev_changed else "RESTART_CHECKED_REBOUND_NOT_OBSERVED"
    print("RESULT=" + result)
    if not dev_changed:
        print("Restart sprawdzony, rebound niezaobserwowany: st_dev się nie zmienił, więc ten scenariusz NIE jest zaliczony.")
    return 0


def cleanup(base):
    _, _, w, _ = _modules()
    base = _guard_base(base)
    try:
        evidence = _read_evidence(base)
        journal = json.loads((base / "ws" / w.STATE / evidence["journal"]).read_text())
    except (OSError, ValueError):
        print("REFUSED: evidence/journal unreadable; nothing removed")
        return 2
    if journal["status"] != "rolled-back":
        print("REFUSED: task not rolled back yet; run verify first. Nothing removed")
        return 2
    shutil.rmtree(base)
    print("CLEANED")
    return 0


def main(argv=None, lib=None):
    parser = argparse.ArgumentParser(prog="restart_binding")
    parser.add_argument("action", choices=("prepare", "verify", "cleanup"))
    parser.add_argument("--lib", help="directory containing the ai_desktop package to test")
    parser.add_argument("--base", default=str(default_base()))
    args = parser.parse_args(argv)
    if args.lib:
        sys.path.insert(0, args.lib)
    return {"prepare": prepare, "verify": verify, "cleanup": cleanup}[args.action](args.base)


if __name__ == "__main__":
    if "--lib" not in sys.argv:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(main())
