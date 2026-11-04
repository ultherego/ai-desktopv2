"""Offline checks in tmpfs copies, with no writable host mounts or host stdin."""
import json
import os
from pathlib import Path

from .analysis_contract import AnalysisDenied, canonical, terminal_text
from .e2_subscription_boundary import base_command
from .process_io import bounded_process
from . import task_contract as contract

CHECKS = ("text", "toml", "python-unittest", "python-syntax", "json", "bash-syntax", "fish-syntax")
MAX_PAYLOAD = 640 * 1024


def command():
    child = Path(__file__).with_name("task_child.py")
    if child.is_symlink() or not child.is_file():
        raise ValueError("bootstrap")
    metadata = {n: os.readlink("/proc/self/ns/" + n) for n in ("user", "mnt", "pid", "net", "ipc", "uts")}
    args = base_command()
    # Bound every writable tmpfs; root and /dev become read-only after setup.
    for name, limit in (("/tmp", 16 * 1024 * 1024), ("/home", 1024 * 1024)):
        pos = args.index(name, args.index("--tmpfs"))
        args[pos - 1:pos - 1] = ["--size", str(limit)]
    return args + ["--size", str(64 * 1024 * 1024), "--tmpfs", "/work",
                   "--dir", "/home/client", "--disable-userns", "--assert-userns-disabled",
                   "--ro-bind", str(child), "/opt/task_child.py", "--remount-ro", "/proc",
                   "--remount-ro", "/dev", "--remount-ro", "/", "--chdir", "/work",
                   "/usr/bin/python3", "-I", "-B", "/opt/task_child.py", json.dumps(metadata)]


def run(source, replacements, check):
    if os.geteuid() == 0 or os.getuid() != os.geteuid() or check not in CHECKS:
        raise ValueError("test identity or check")
    contract.request(source)
    proposed = contract.proposal(canonical({"summary": "test", "files": replacements}), source)
    files = {f["path"]: f["content"] for f in source["files"] if f["content"] is not None}
    files.update({f["path"]: f["content"] for f in proposed["files"]})
    payload = canonical({"files": [{"path": p, "content": c} for p, c in files.items()], "check": check}).encode()
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("test size")
    try:
        code, out, err = bounded_process(command(), env={"PATH": "/usr/bin", "LANG": "C.UTF-8"},
                                         input_bytes=payload, input_limit=MAX_PAYLOAD, timeout=20, limit=32768)
        return {"passed": code == 0, "exit_code": code,
                "output": terminal_text((out + b"\n" + err).decode("utf-8", errors="replace")), "check": check}
    except AnalysisDenied as exc:
        return {"passed": False, "exit_code": 2, "output": "CHECK: " + exc.reason, "check": check}
