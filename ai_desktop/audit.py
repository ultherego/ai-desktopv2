import os
import platform
import shutil
import subprocess
from pathlib import Path

TOOLS = ("git", "python3", "claude", "codex", "opencode", "bwrap", "podman", "snapper")


def read_command(args):
    try:
        result = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=8
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"Unavailable: {type(exc).__name__}"
    if result.returncode != 0:
        return result.returncode, "Could not read the data"
    return 0, result.stdout.strip() or "(empty result)"


def audit_lines():
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "none")
    session = os.environ.get("XDG_SESSION_TYPE", "none")
    shell = Path(os.environ.get("SHELL", "none")).name
    output = [
        "=== AI Desktop: read-only audit ===",
        f"Kernel: {platform.release()}",
        f"Desktop: {desktop}",
        f"Session: {session}",
        f"Shell: {shell}",
        "", "=== TOOLS ==="
    ]
    for tool in TOOLS:
        present = "YES" if shutil.which(tool) else "NO"
        output.append(f"{tool}: {present}")
    output.extend(["", "=== MOUNT POINTS ==="])
    for path in ("/", str(Path.home())):
        code, value = read_command(
            ("findmnt", "-T", path, "-n", "-o", "TARGET,SOURCE,FSTYPE")
        )
        output.append(
            f"{path}: {value}" if code == 0 else f"{path}: no data"
        )
    output.extend(["", "=== SNAPPER ==="])
    if shutil.which("snapper"):
        code, value = read_command(("snapper", "list-configs"))
        output.append(
            value if code == 0 else "Could not read the configuration"
        )
    else:
        output.append("Snapper unavailable")
    output.extend([
        "", "=== NOTES ===",
        "A snapshot of / does not automatically include a separate /home subvolume.",
        "A snapshot on the same disk is not an independent backup.",
        "Having bwrap does not prove that the agent is correctly isolated.",
        "The audit does not start AI, does not save any configuration and does not need sudo."
    ])
    return output
