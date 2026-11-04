"""Trusted test bootstrap, entered only inside the task's empty namespaces.

Standalone stdlib file: never imports project modules before confinement.
The tested source is untrusted even when its tests report success.
"""
import ctypes
import errno
import json
import os
from pathlib import Path
import resource
import sys

MAX_PAYLOAD = 640 * 1024


def materialize(data, root):
    """Verify the exact UTF-8 bytes written to the isolated copy."""
    for item in data["files"]:
        dest = Path(root) / item["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        expected = item["content"].encode("utf-8")
        with dest.open("xb") as output:
            output.write(expected)
        actual = dest.read_bytes()
        if actual != expected or actual.decode("utf-8").encode("utf-8") != expected:
            raise ValueError("text integrity")


def syntax_check(selected, check):
    for item in selected:
        if check == "json":
            json.loads(item["content"])
        elif check == "toml":
            import tomllib
            tomllib.loads(item["content"])
        elif check == "python-syntax":
            compile(item["content"], item["path"], "exec", dont_inherit=True)
        else:
            raise ValueError("syntax check")


def lock():
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    ptr = ctypes.c_void_p
    lib.seccomp_init.argtypes, lib.seccomp_init.restype = [ctypes.c_uint32], ptr
    lib.seccomp_release.argtypes = [ptr]
    lib.seccomp_load.argtypes, lib.seccomp_load.restype = [ptr], ctypes.c_int
    lib.seccomp_attr_set.argtypes = [ptr, ctypes.c_int, ctypes.c_uint32]
    lib.seccomp_attr_set.restype = ctypes.c_int
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [ptr, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ptr]
    lib.seccomp_rule_add_array.restype = ctypes.c_int
    context = lib.seccomp_init(0x7fff0000)
    if not context:
        raise RuntimeError("filter")
    try:
        if lib.seccomp_attr_set(context, 2, 0x80000000) or lib.seccomp_attr_set(context, 4, 1):
            raise RuntimeError("filter")
        # No sockets, children, threads, namespace/mount changes or ptrace.
        # clone3 returns ENOSYS so shell runtimes can fall back, then fork/clone
        # are refused too. Fixed syntax-only shell checks don't require forks.
        for name in ("socket", "socketpair", "connect", "bind", "listen", "fork", "vfork", "clone",
                     "clone3", "unshare", "setns", "mount", "umount2", "pivot_root", "ptrace",
                     "process_vm_readv", "process_vm_writev", "bpf", "userfaultfd"):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            error = errno.ENOSYS if name == "clone3" else errno.EPERM
            if number < 0 or lib.seccomp_rule_add_array(context, 0x50000 | error, number, 0, None):
                raise RuntimeError("filter")
        if lib.seccomp_load(context):
            raise RuntimeError("filter")
    finally:
        lib.seccomp_release(context)


def main():
    metadata = json.loads(sys.argv[1])
    for name, outside in metadata.items():
        if os.readlink("/proc/self/ns/" + name) == outside:
            raise RuntimeError("namespace")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    if status["NoNewPrivs"].strip() != "1" or any(int(status[n], 16) for n in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")):
        raise RuntimeError("privileges")
    for key, limit in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_CPU, 10),
                       (resource.RLIMIT_AS, 512 * 1024 * 1024), (resource.RLIMIT_FSIZE, 1024 * 1024),
                       (resource.RLIMIT_NOFILE, 64)):
        resource.setrlimit(key, (limit, limit))
    lock()
    raw = sys.stdin.buffer.read(MAX_PAYLOAD + 1)
    if not raw or len(raw) > MAX_PAYLOAD:
        raise ValueError("input size")
    data = json.loads(raw)
    materialize(data, "/work")
    null = os.open("/dev/null", os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    check = data["check"]
    if check == "text":
        print("CHECK: UTF-8 integrity only; syntax and functionality not tested")
        return 0
    if check == "python-unittest":
        import unittest
        sys.path.insert(0, "/work")
        suite = unittest.defaultTestLoader.discover("/work", pattern="test*.py")
        if suite.countTestCases() == 0:
            raise ValueError("no tests")
        return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1
    suffix = {"python-syntax": ".py", "json": ".json", "toml": ".toml", "bash-syntax": ".sh", "fish-syntax": ".fish"}[check]
    selected = [f for f in data["files"] if f["path"].endswith(suffix)]
    if not selected:
        raise ValueError("no matching files")
    if check in ("bash-syntax", "fish-syntax"):
        # Parent validates one file, so a shell cannot treat others as args.
        if len(selected) != 1:
            raise ValueError("one shell file required")
        binary = "/usr/bin/bash" if check == "bash-syntax" else "/usr/bin/fish"
        args = [binary, "--noprofile", "--norc", "-n"] if check == "bash-syntax" else [binary, "--no-config", "--no-execute"]
        os.execve(binary, args + ["/work/" + selected[0]["path"]],
                  {"PATH": "/usr/bin", "HOME": "/home/client", "LANG": "C.UTF-8"})
    syntax_check(selected, check)
    print("CHECK: completed files=" + str(len(selected)))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        print("CHECK: failed", file=sys.stderr)
        code = 2
    raise SystemExit(code)
