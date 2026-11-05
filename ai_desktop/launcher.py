
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ALLOWED = {"version": "--version", "help": "--help"}


def claude_binary(home=None):
    home = Path.home() if home is None else Path(home)
    base = (home / ".local/share/claude/versions").resolve(strict=True)
    target = (home / ".local/bin/claude").resolve(strict=True)
    if not target.is_file() or not os.access(target, os.X_OK):
        raise ValueError("Claude is not an executable file")
    if not target.is_relative_to(base):
        raise ValueError("Claude points outside the versions directory")
    return target


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
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in ALLOWED:
        print("Usage: python3 -B -m ai_desktop.launcher version|help")
        return 2
    if shutil.which("bwrap") is None:
        print("bwrap is missing")
        return 2
    try:
        binary = claude_binary()
    except (OSError, ValueError) as exc:
        print(f"Claude: {exc}")
        return 2
    workspace = Path(tempfile.mkdtemp(prefix="ai-desktop-launch-", dir="/tmp"))
    workspace.chmod(0o700)
    print(f"Test workspace: {workspace}", flush=True)
    try:
        completed = subprocess.run(
            build_command(workspace, binary, argv[0]),
            check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Could not start: {type(exc).__name__}")
        return 1
    print("LAUNCHER: PASS" if completed.returncode == 0 else "LAUNCHER: FAIL")
    return 0 if completed.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
