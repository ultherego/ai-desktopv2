"""CI-only offline version diagnosis with the known synthetic ELF, no profile.

Installed only by the guarded disposable VM driver, never packaged. This may
print fixture assertions/bwrap setup errors, never a real native auth response.
"""
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, "/usr/lib/ai-desktop")
from ai_desktop.analysis_sandbox import namespace_command
from ai_desktop.process_io import bounded_process

with tempfile.TemporaryDirectory(dir="/tmp") as temp:
    root = Path(temp)
    (root / "passwd").write_text("client:x:%d:%d::/home/client:/usr/bin/false\n" % (os.geteuid(), os.getegid()))
    (root / "group").write_text("client:x:%d:\n" % os.getegid())
    code, out, err = bounded_process(namespace_command("version", root, online=False),
                                    env={"PATH": "/usr/bin", "LANG": "C.UTF-8"}, timeout=10, limit=4096)
    print("CI SYNTHETIC OFFLINE VERSION exit=" + str(code))
    print(out.decode("utf-8", errors="replace"))
    print(err.decode("utf-8", errors="replace"))
