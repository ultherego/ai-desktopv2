"""E2 native-client compatibility against a secret-free sandbox-local fixture.

Explicit opt-in only. No host credential mounts, provider connections or model.
This experiment does not implement or certify production OAuth integration.
"""
import json
import os
from pathlib import Path
import shutil
import sys

from .launcher import claude_binary
from .e2_compat_fixture import (API_STATUS_SHAPES, RESULT_FLAGS, FixtureDenied,
                              bounded_run, _unique, REASONS, SCENARIOS)

SOURCE = Path(__file__).resolve().parent
FILES = ("__init__.py", "e2_compat_fixture.py", "provider_errors.py", "e2_ipc.py",
         "report_policy.py", "transfer_consent.py", "runtime_health.py")
CLIENT_VERSION = "2.1.312"
RUNNER = ("import sys; sys.path.insert(0, '/opt'); "
          "from ai_desktop.e2_compat_fixture import main; raise SystemExit(main())")


class CompatibilityDenied(RuntimeError):
    pass


def build_command(binary, source, host_home, host_netns, scenario):
    if scenario not in SCENARIOS:
        raise CompatibilityDenied("COMPAT: REFUSED")
    binary, source = Path(binary), Path(source)
    if (not binary.is_absolute() or binary.is_symlink() or not binary.is_file() or
            not os.access(binary, os.X_OK) or not source.is_absolute() or
            source.resolve() != source or not source.is_dir()):
        raise CompatibilityDenied("COMPAT: REFUSED")
    with binary.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise CompatibilityDenied("COMPAT: REFUSED")
    for name in FILES:
        if (source / name).is_symlink() or not (source / name).is_file():
            raise CompatibilityDenied("COMPAT: REFUSED")
    command = ["/usr/bin/bwrap", "--unshare-user", "--unshare-pid", "--unshare-net",
               "--unshare-ipc", "--unshare-uts", "--die-with-parent", "--new-session",
               "--cap-drop", "ALL", "--clearenv", "--setenv", "HOME", "/home/model",
               "--setenv", "PATH", "/usr/bin", "--setenv", "HOST_HOME", str(host_home),
               "--setenv", "HOST_NETNS", host_netns,
               "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
               "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib", "/lib64",
               "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
               "--tmpfs", "/home", "--dir", "/home/model", "--dir", "/work",
               "--dir", "/opt", "--dir", "/opt/ai_desktop",
               "--ro-bind", str(binary), "/opt/claude"]
    for name in FILES:
        command += ["--ro-bind", str(source / name), "/opt/ai_desktop/" + name]
    return command + ["--chdir", "/work", "/usr/bin/python3", "-I", "-B", "-c", RUNNER, scenario]


def parse_failure(raw, scenario):
    """Accept only the fixed, non-sensitive failure schema from the sandbox."""
    result = json.loads(raw, object_pairs_hook=_unique)
    if (scenario not in SCENARIOS or type(result) is not dict or set(result) != {"error", "phase", "reason", "client_exit",
            "requests", "messages", "input_messages", "text_blocks", "marker", "exact",
            "result_type", "success_subtype", "is_error", "api_status"} or
            result["error"] != "COMPAT REFUSED" or result["phase"] != scenario or
            type(result["reason"]) is not str or result["reason"] not in REASONS):
        raise ValueError()
    code = result["client_exit"]
    if code is not None and (type(code) is not int or not -128 <= code <= 255):
        raise ValueError()
    if any(type(result[k]) is not int or not 0 <= result[k] <= 32
           for k in ("requests", "messages", "input_messages", "text_blocks")):
        raise ValueError()
    if any(type(result[k]) is not bool for k in ("marker", "exact", *RESULT_FLAGS)):
        raise ValueError()
    if type(result["api_status"]) is not str or result["api_status"] not in API_STATUS_SHAPES:
        raise ValueError()
    return result


def execute(*, allow_claude_compat=False):
    if allow_claude_compat is not True or os.geteuid() == 0 or shutil.which("bwrap", path="/usr/bin") is None:
        raise CompatibilityDenied("COMPAT: REFUSED")
    scenario = "setup"
    details = None
    try:
        binary = claude_binary()
        identity = binary.stat()
        version = None
        for scenario in SCENARIOS:
            # Each native-client phase has a separate disposable PID/net namespace;
            # no descendant from one phase can survive into the next phase.
            current = binary.stat()
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                    identity.st_dev, identity.st_ino, identity.st_size, identity.st_mtime_ns):
                raise ValueError()
            command = build_command(binary, SOURCE, Path.home(),
                                    os.readlink("/proc/self/ns/net"), scenario)
            code, stdout, _ = bounded_run(command, env={"PATH": "/usr/bin"}, timeout=55, limit=4096)
            if code != 0:
                details = parse_failure(stdout, scenario)
                raise ValueError()
            result = json.loads(stdout, object_pairs_hook=_unique)
            if (type(result) is not dict or set(result) != {"schema", "version", "scenario",
                    "boundary", "result", "real_auth", "upstream"} or
                    type(result["schema"]) is not int or result["schema"] != 1 or
                    result["scenario"] != scenario or result["boundary"] != "PASS" or
                    result["result"] != "PASS" or result["real_auth"] is not False or
                    result["upstream"] is not False):
                raise ValueError()
            if scenario == "version":
                version = result["version"]
                if type(version) is not str or version != CLIENT_VERSION:
                    raise ValueError()
            elif result["version"] is not None:
                raise ValueError()
        return {"schema": 1, "version": version, "boundary": "PASS", "reply": "PASS",
                "denial": "PASS", "rate-limit": "PASS", "unavailable": "PASS",
                "disconnect": "PASS", "timeout-stop": "PASS", "recovery": "PASS",
                "real_auth": False, "upstream": False}

    except (OSError, ValueError, TypeError, RecursionError, FixtureDenied, CompatibilityDenied):
        error = CompatibilityDenied("COMPAT: REFUSED")
        error.phase = scenario  # Fixed local vocabulary, never native output.
        error.details = details
        raise error from None


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args == ["status"]:
        print("COMPAT: OFFLINE; real client requires explicit synthetic-fixture opt-in")
        return 0
    if args != ["self-test", "--allow-claude-compat"]:
        print("COMPAT: REFUSED")
        return 2
    try:
        result = execute(allow_claude_compat=True)
    except CompatibilityDenied as exc:
        line = "COMPAT: REFUSED phase=" + getattr(exc, "phase", "setup")
        details = getattr(exc, "details", None)
        if details is not None:
            line += (" reason=" + details["reason"] + " exit=" +
                     ("none" if details["client_exit"] is None else str(details["client_exit"])) +
                     " requests=" + str(details["requests"]) + " messages=" + str(details["messages"]) +
                     " input_messages=" + str(details["input_messages"]) +
                     " text_blocks=" + str(details["text_blocks"]) +
                     " marker=" + str(int(details["marker"])) + " exact=" + str(int(details["exact"])) +
                     " result_type=" + str(int(details["result_type"])) +
                     " success_subtype=" + str(int(details["success_subtype"])) +
                     " is_error=" + str(int(details["is_error"])) + " api_status=" + details["api_status"])
        print(line)
        return 1
    print("COMPAT: PASS version=" + result["version"] +
          " isolated-loopback reply=PASS denial=PASS rate-limit=PASS unavailable=PASS"
          " disconnect=PASS timeout-stop=PASS recovery=5/5 real_auth=false upstream=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
