import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .launcher import ALLOWED, build_command, claude_binary
from .skill_mount import DEST, ROOT, source_hash

PROBE = chr(10).join([
    "import hashlib, os, sys",
    "from pathlib import Path",
    "skill = Path(\"/home/sandbox/.claude/skills/cachyos-diagnostics/SKILL.md\")",
    "data = skill.read_bytes() if skill.is_file() else b\"\"",
    "checks = {",
    "  \"skill present\": skill.is_file(),",
    "  \"skill contents\": hashlib.sha256(data).hexdigest() == os.environ[\"EXPECTED_HASH\"],",
    "  \"skill read-only\": bool(os.statvfs(\"/home/sandbox/.claude/skills\").f_flag & os.ST_RDONLY),",
    "  \"host home hidden\": not Path(os.environ[\"HOST_HOME\"]).exists(),",
    "  \"network isolated\": os.readlink(\"/proc/self/ns/net\") != os.environ[\"HOST_NETNS\"],",
    "  \"separate Claude config\": os.environ[\"CLAUDE_CONFIG_DIR\"] == \"/home/sandbox/.claude\"",
    "}",
    "for label, passed in checks.items():",
    "  result = \"PASS\" if passed else \"FAIL\"",
    "  print(f\"{label}: {result}\")",
    "sys.exit(0 if all(checks.values()) else 1)",
])


def build_agent_command(workspace, binary, skills_root, action, digest, home, netns):
    if action not in ALLOWED:
        raise ValueError("Niedozwolona operacja")

    base = build_command(workspace, binary, action)
    if base[-2:] != ["/opt/claude", ALLOWED[action]]:
        raise ValueError("Nieoczekiwany format launchera")

    extra = [
        "--setenv", "CLAUDE_CONFIG_DIR", "/home/sandbox/.claude",
        "--setenv", "EXPECTED_HASH", digest,
        "--setenv", "HOST_HOME", str(home),
        "--setenv", "HOST_NETNS", netns,
        "--setenv", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1",
        "--dir", "/home/sandbox/.claude",
        "--dir", DEST,
        "--ro-bind", str(skills_root), DEST,
    ]

    return base[:-2] + extra + base[-2:]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    if len(argv) != 1 or argv[0] not in ALLOWED:
        print("Uzycie: python3 -B -m ai_desktop.agent version|help")
        return 2

    if shutil.which("bwrap") is None:
        print("Brak bwrap")
        return 2

    try:
        binary = claude_binary()
        digest = source_hash(ROOT)
        home = Path.home()
        netns = os.readlink("/proc/self/ns/net")

        with tempfile.TemporaryDirectory(
            prefix="ai-desktop-launch-", dir="/tmp"
        ) as temporary:

            command = build_agent_command(
                Path(temporary), binary, ROOT,
                argv[0], digest, home, netns
            )

            probe = command[:-2] + [
                "/usr/bin/python3", "-I", "-B", "-c", PROBE
            ]

            checked = subprocess.run(
                probe, check=False, timeout=20
            )

            if checked.returncode != 0:
                print("AGENT PROBE: FAIL")
                return 1

            print("AGENT PROBE: PASS", flush=True)

            result = subprocess.run(
                command, check=False, timeout=30
            )

    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"AGENT: ERROR ({type(exc).__name__})")
        return 1

    print("AGENT: PASS" if result.returncode == 0 else "AGENT: FAIL")
    return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
