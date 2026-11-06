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
        raise ValueError("Operation not allowed")

    base = build_command(workspace, binary, action)
    if base[-2:] != ["/opt/claude", ALLOWED[action]]:
        raise ValueError("Unexpected launcher format")

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
    """The old probe command now shares production client discovery/compatibility."""
    from .launcher import main as launch
    return launch(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
