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
        if item.get("executable"):
            dest.chmod(0o700)
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
            compile(item["content"].encode("utf-8"), item["path"], "exec", dont_inherit=True)
        else:
            raise ValueError("syntax check")


def lock(multiprocess=False):
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
        blocked = ["socket", "connect", "bind", "listen", "clone3", "unshare", "setns", "mount", "umount2",
                   "pivot_root", "ptrace", "process_vm_readv", "process_vm_writev", "bpf", "userfaultfd",
                   "io_uring_setup", "io_uring_enter", "io_uring_register"]
        if multiprocess:
            # Only this controller-selected path already passed the kernel cgroup preflight.
            # clone3's flags live behind a pointer, so require the inspectable clone(2) fallback.
            if os.uname().machine not in ("x86_64", "aarch64"):
                raise RuntimeError("unsupported clone ABI")
            class Compare(ctypes.Structure):
                _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_uint), ("a", ctypes.c_uint64), ("b", ctypes.c_uint64)]
            def deny_arg(syscall, comparison):
                number = lib.seccomp_syscall_resolve_name(syscall)
                if number < 0 or lib.seccomp_rule_add_array(context, 0x50000 | errno.EPERM, number, 1,
                                                            ctypes.byref(comparison)):
                    raise RuntimeError("filter")
            # CLONE_NEW{TIME,NS,CGROUP,UTS,IPC,USER,PID,NET}. SCMP_CMP_MASKED_EQ is 7.
            for flag in (0x80, 0x20000, 0x02000000, 0x04000000, 0x08000000, 0x10000000, 0x20000000, 0x40000000):
                deny_arg(b"clone", Compare(0, 7, flag, flag))
            # Runtimes may create a *private* pair of Unix sockets; no named or external socket can be opened.
            deny_arg(b"socketpair", Compare(0, 1, 1, 0))  # SCMP_CMP_NE, AF_UNIX
        else:
            blocked.extend(("socketpair", "fork", "vfork", "clone"))
        for name in blocked:
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
    suite = sys.argv[2] if len(sys.argv) == 3 else None
    if suite is not None and suite not in ("go-test", "npm-test"):
        raise ValueError("suite")
    limits = [(resource.RLIMIT_CORE, 0), (resource.RLIMIT_CPU, 75 if suite else 10),
              (resource.RLIMIT_FSIZE, (64 if suite else 1) * 1024 * 1024), (resource.RLIMIT_NOFILE, 128 if suite else 64)]
    if not suite:
        limits.append((resource.RLIMIT_AS, 512 * 1024 * 1024))
    for key, limit in limits:
        resource.setrlimit(key, (limit, limit))
    lock(multiprocess=suite is not None)
    raw = sys.stdin.buffer.read(MAX_PAYLOAD + 1)
    if not raw or len(raw) > MAX_PAYLOAD:
        raise ValueError("input size")
    data = json.loads(raw)
    materialize(data, "/work")
    null = os.open("/dev/null", os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    check = data["check"]
    if suite is not None:
        if check != suite:
            raise ValueError("suite mismatch")
        env = {"PATH": "/usr/bin", "HOME": "/home/client", "LANG": "C.UTF-8", "TMPDIR": "/tmp"}
        if suite == "go-test":
            if not Path("/work/go.mod").is_file():
                raise ValueError("go.mod missing")
            env.update({"GOPROXY": "off", "GOSUMDB": "off", "GOTOOLCHAIN": "local", "CGO_ENABLED": "0",
                        "GOMAXPROCS": "2", "GOCACHE": "/tmp/go-cache", "GOPATH": "/tmp/go", "GOWORK": "off"})
            argv = ["/usr/bin/go", "test", "-buildvcs=false", "-vet=off", "-p=1", "-count=1", "-json", "./..."]
        else:
            package = json.loads(Path("/work/package.json").read_bytes())
            if not isinstance(package.get("scripts", {}).get("test"), str) or not package["scripts"]["test"].strip():
                raise ValueError("package.json has no test script")
            env.update({"npm_config_offline": "true", "npm_config_audit": "false", "npm_config_fund": "false",
                        "npm_config_update_notifier": "false", "npm_config_cache": "/tmp/npm", "UV_THREADPOOL_SIZE": "2",
                        "NODE_OPTIONS": "--max-old-space-size=192"})
            argv = ["/usr/bin/npm", "--offline", "test"]
        if not os.access(argv[0], os.X_OK):
            print("The fixed test tool is not installed: " + argv[0], file=sys.stderr)
            return 2
        os.execve(argv[0], argv, env)
    if check in ("go-test", "npm-test"):
        raise ValueError("missing resource scope")
    if check == "text":
        print("CHECK: UTF-8 integrity only; syntax and functionality not tested")
        return 0
    if check == "pytest":
        try:
            import pytest   # trusted system installation; /work is added only afterwards
        except ImportError:
            print("pytest is unavailable in the system Python; install python-pytest to use this recipe.", file=sys.stderr)
            return 2
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        sys.path.insert(0, "/work")
        class Results:
            passed = 0
            def pytest_runtest_logreport(self, report):
                if report.when == "call" and report.passed and not getattr(report, "wasxfail", None):
                    self.passed += 1
        results = Results()
        code = pytest.main(["-q", "-p", "no:cacheprovider", "--color=no", "/work"], plugins=[results])
        if not code and not results.passed:
            print("CHECK: no test passed", file=sys.stderr)
            return 2
        return int(code)
    if check == "python-unittest":
        import unittest
        sys.path.insert(0, "/work")
        suite = unittest.defaultTestLoader.discover("/work", pattern="test*.py")
        if suite.countTestCases() == 0:
            raise ValueError("no tests")
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        if result.testsRun <= len(result.skipped) + len(result.expectedFailures):
            print("CHECK: no test passed", file=sys.stderr)
            return 2
        return 0 if result.wasSuccessful() else 1
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
