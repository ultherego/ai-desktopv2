import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .skills import ROOT, inspect_skill

DEST = "/home/sandbox/.claude/skills"
PROBE = chr(10).join([
    "import hashlib, os, sys",
    "from pathlib import Path",
    "skill = Path(\"/home/sandbox/.claude/skills/cachyos-diagnostics/SKILL.md\")",
    "checks = {",
    "  \"skill widoczny\": skill.is_file(),",
    "  \"skills tylko do odczytu\": bool(os.statvfs(\"/home/sandbox/.claude/skills\").f_flag & os.ST_RDONLY),",
    "  \"katalog hosta ukryty\": not Path(os.environ[\"HOST_HOME\"]).exists(),",
    "  \"odrebna siec\": os.readlink(\"/proc/self/ns/net\") != os.environ[\"HOST_NETNS\"],",
    "}",
    "checks[\"zawartosc identyczna\"] = skill.is_file() and hashlib.sha256(skill.read_bytes()).hexdigest() == os.environ[\"EXPECTED_HASH\"]",
    "for name, ok in checks.items():",
    "    result = \"PASS\" if ok else \"FAIL\"",
    "    print(f\"{name}: {result}\")",
    "sys.exit(0 if all(checks.values()) else 1)",
])


def source_hash(root: Path):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Brak prawidlowego katalogu skills")
    entries = list(root.iterdir())
    if not entries:
        raise ValueError("Katalog skills jest pusty")
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            raise ValueError("Niedozwolony element katalogu skills")
        inspect_skill(root, entry.name)
        if any(child.is_symlink() for child in entry.rglob("*")):
            raise ValueError("Symlink wewnatrz skilla")
    return hashlib.sha256(
        (root / "cachyos-diagnostics/SKILL.md").read_bytes()
    ).hexdigest()


def build_command(root, host_home, netns, digest):
    return [
        "bwrap", "--unshare-user", "--unshare-pid", "--unshare-net",
        "--unshare-ipc", "--unshare-uts", "--die-with-parent", "--new-session",
        "--clearenv", "--setenv", "HOME", "/home/sandbox",
        "--setenv", "CLAUDE_CONFIG_DIR", "/home/sandbox/.claude",
        "--setenv", "PATH", "/usr/bin",
        "--setenv", "HOST_HOME", str(host_home),
        "--setenv", "HOST_NETNS", netns,
        "--setenv", "EXPECTED_HASH", digest,
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib", "/lib64",
        "--dev", "/dev", "--proc", "/proc",
        "--tmpfs", "/tmp", "--tmpfs", "/home",
        "--dir", "/home/sandbox",
        "--dir", "/home/sandbox/.claude",
        "--dir", DEST,
        "--ro-bind", str(root), DEST,
        "--dir", "/work", "--chdir", "/work",
        "/usr/bin/python3", "-I", "-B", "-c", PROBE,
    ]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["self-test"]:
        print("Uzycie: python3 -B -m ai_desktop.skill_mount self-test")
        return 2
    if not shutil.which("bwrap"):
        print("Brak bwrap")
        return 2
    try:
        digest = source_hash(ROOT)
        cmd = build_command(
            ROOT, Path.home(), os.readlink("/proc/self/ns/net"), digest
        )
        result = subprocess.run(cmd, timeout=20, check=False)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"SKILL MOUNT: ERROR ({type(exc).__name__})")
        return 1
    print("SKILL MOUNT: PASS" if result.returncode == 0 else "SKILL MOUNT: FAIL")
    return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
