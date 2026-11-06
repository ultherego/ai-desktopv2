"""CI-only offline compatibility diagnosis with the known synthetic ELF.

Installed only by the guarded disposable VM driver, never packaged. This may
print fixture assertions/bwrap setup errors, never a real native auth response.
"""
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, "/usr/lib/ai-desktop")
from ai_desktop import client_binding, client_compat
from ai_desktop.analysis_contract import AnalysisDenied
from ai_desktop.analysis_sandbox import namespace_command
from ai_desktop.process_io import bounded_process


def main():
    action = "binding"
    try:
        with client_binding.bind("claude") as bound, tempfile.TemporaryDirectory(dir="/tmp") as temp:
            root = Path(temp)
            (root / "passwd").write_text("client:x:%d:%d::/home/client:/usr/bin/false\n" % (os.geteuid(), os.getegid()))
            (root / "group").write_text("client:x:%d:\n" % os.getegid())

            def run(phase):
                nonlocal action
                action = phase
                command = namespace_command(phase, root, online=False, bound=bound)
                code, out, err = bounded_process(command, pass_fds=bound.pass_fds,
                    env={"PATH": "/usr/bin", "LANG": "C.UTF-8"}, timeout=10, limit=4096)
                print("CI SYNTHETIC OFFLINE phase=" + phase + " exit=" + str(code), flush=True)
                # The driver installs our synthetic fixture before this script;
                # no real client, request, account profile or uplink is used.
                print(out.decode("utf-8", errors="replace"), flush=True)
                print(err.decode("utf-8", errors="replace"), flush=True)
                bound.recheck()
                return code, out, err

            client_compat.check("claude", run)
        print("CI SYNTHETIC COMPATIBILITY: PASS", flush=True)
        return 0
    except (AnalysisDenied, OSError, ValueError) as exc:
        reason = exc.reason if isinstance(exc, AnalysisDenied) else "runtime"
        print("CI SYNTHETIC COMPATIBILITY: FAIL phase=" + action + " reason=" + reason, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
