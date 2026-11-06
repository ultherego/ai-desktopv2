"""Offline checks in tmpfs copies, with no writable host mounts or host stdin."""
import json
import os
from pathlib import Path

from .analysis_contract import AnalysisDenied, canonical, terminal_text
from .e2_subscription_boundary import base_command
from .process_io import bounded_process
from . import copy_contract, task_contract as contract, task_cgroup

CHECKS = contract.CHECKS
MAX_PAYLOAD = copy_contract.MAX_PAYLOAD


def command(suite=None):
    if suite is not None and suite not in task_cgroup.MULTIPROCESS:
        raise ValueError("suite")
    child = Path(__file__).with_name("task_child.py")
    if child.is_symlink() or not child.is_file():
        raise ValueError("bootstrap")
    metadata = {n: os.readlink("/proc/self/ns/" + n) for n in ("user", "mnt", "pid", "net", "ipc", "uts")}
    args = base_command()
    # Bound every writable tmpfs; root and /dev become read-only after setup.
    for name, limit in (("/tmp", (384 if suite else 16) * 1024 * 1024), ("/home", 1024 * 1024)):
        pos = args.index(name, args.index("--tmpfs"))
        args[pos - 1:pos - 1] = ["--size", str(limit)]
    return args + ["--size", str(copy_contract.WORK_BYTES), "--tmpfs", "/work",
                   "--dir", "/home/client", "--disable-userns", "--assert-userns-disabled",
                   "--ro-bind", str(child), "/opt/task_child.py", "--remount-ro", "/proc",
                   "--remount-ro", "/dev", "--remount-ro", "/", "--chdir", "/work",
                   "/usr/bin/python3", "-I", "-B", "/opt/task_child.py", json.dumps(metadata)] + ([suite] if suite else [])


def run(source, replacements, check, *, originals=None):
    if os.geteuid() == 0 or os.getuid() != os.geteuid() or check not in CHECKS:
        raise ValueError("test identity or check")
    contract.request(source)
    proposed = contract.proposal(canonical({"summary": "test", "files": replacements}), source)
    # A test cannot bless bytes which the durable write/undo format would later refuse.
    contract.formatted_replacements(proposed["files"], originals or {})
    files = {f["path"]: f["content"] for f in source["files"] if f["content"] is not None}
    files.update({f["path"]: f["content"] for f in proposed["files"]})
    if originals is not None:
        files = {name: contract.in_file_format(content, originals.get(name)) for name, content in files.items()}
    return run_copy(files, check)


def run_copy(files, check, *, executable=()):
    """Accept only a bounded in-memory selection, never a host mount or a caller-supplied command."""
    if os.geteuid() == 0 or os.getuid() != os.geteuid() or check not in CHECKS:
        raise ValueError("test identity or check")
    copy_contract.validate_files(files)
    items = [{"path": name, "content": content} for name, content in files.items()]
    if any(name not in files for name in executable):
        raise ValueError("executable outside test selection")
    for item in items:
        if item["path"] in executable:
            item["executable"] = True
    payload = canonical({"files": items, "check": check}).encode()
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("test size")
    env = {"PATH": "/usr/bin", "LANG": "C.UTF-8"}
    unit = None
    if check in task_cgroup.MULTIPROCESS:
        unit = task_cgroup.scope_name()
        env["XDG_RUNTIME_DIR"] = "/run/user/%d" % os.getuid()
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + env["XDG_RUNTIME_DIR"] + "/bus"
        argv = task_cgroup.scope_command(unit, command(check))
    else:
        argv = command()
    try:
        code, out, err = bounded_process(argv, env=env, input_bytes=payload, input_limit=MAX_PAYLOAD, copy_input=True,
                                         timeout=task_cgroup.RUNTIME + 5 if unit else 20, limit=32768)
        if check == "go-test" and code == 0:
            # go test also exits 0 when every package contains zero tests.
            if not any(_go_test_passed(line) for line in out.splitlines()):
                code, err = 2, err + b"\nNo passing Go test was reported."
        return {"passed": code == 0, "exit_code": code,
                "output": terminal_text((out + b"\n" + err).decode("utf-8", errors="replace")), "check": check}
    except AnalysisDenied as exc:
        return {"passed": False, "exit_code": 2, "output": "CHECK: " + exc.reason, "check": check}
    except OSError:
        return {"passed": False, "exit_code": 2, "output": "Test launcher unavailable.", "check": check}
    finally:
        if unit is not None:
            task_cgroup.cleanup(unit, env)


def _go_test_passed(line):
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):
        return False
    return type(event) is dict and event.get("Action") == "pass" and bool(event.get("Test"))
