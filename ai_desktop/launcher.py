
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ALLOWED = {"version": "--version", "help": "--help"}


def claude_binary(home=None):
    """Legacy metadata accessor; HOME is never a client source."""
    from .system_clients import inspect
    found = inspect("claude")
    if found.status != "present":
        raise ValueError("System Claude client unavailable: " + found.status)
    return Path(found.path)


def build_command(workspace, binary, operation):
    if operation not in ALLOWED:
        raise ValueError("Only version and help are allowed")
    workspace = Path(workspace)
    if (not workspace.is_absolute() or workspace.is_symlink()
            or not workspace.is_dir()
            or workspace.parent != Path("/tmp")
            or not workspace.name.startswith("ai-desktop-launch-")):
        raise ValueError("Working directory not allowed")
    return [
        "bwrap",
        "--unshare-user", "--unshare-pid", "--unshare-net",
        "--unshare-ipc", "--unshare-uts", "--die-with-parent",
        "--new-session", "--clearenv",
        "--setenv", "HOME", "/home/sandbox",
        "--setenv", "PATH", "/usr/bin",
        "--setenv", "DISABLE_AUTOUPDATER", "1",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib", "/lib64",
        "--dev", "/dev", "--proc", "/proc",
        "--tmpfs", "/tmp", "--tmpfs", "/home",
        "--dir", "/home/sandbox", "--dir", "/opt",
        "--ro-bind", str(binary), "/opt/claude",
        "--dir", "/work",
        "--bind", str(workspace), "/work",
        "--chdir", "/work",
        "/opt/claude", ALLOWED[operation],
    ]


def main(argv=None):
    """Retained command spelling; use the same offline adapter as production."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in ALLOWED:
        print("Usage: python3 -B -m ai_desktop.launcher version|help")
        return 2
    from .client_compat import inspect
    result = inspect("claude")
    if result["protocol_checked"] is not True:
        print("System Claude: " + result["status"] + ". Run ai-desktop doctor --clients.")
        return 2
    print("System Claude " + result["version"] + ": " + result["status"] + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
