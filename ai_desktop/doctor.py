"""Metadata-only local readiness diagnostic; never runs Claude or shell commands.

Presence checks are not proof that a namespace or authenticated session works.
The report intentionally contains no host paths, process output or credentials.
"""
import json
import os
from pathlib import Path
import shutil
import sys

from . import e2_account, launcher, skills

MINIMUM_PYTHON = (3, 12)
TOOLS = ("bwrap", "findmnt", "systemctl")
SKILL = "cachyos-diagnostics"
INSTALLED = Path("/usr/lib/ai-desktop")
# Historical skill installation presence only; NOT an approved Claude/OAuth profile.
PROFILE = Path.home() / ".local/state/ai-desktop/claude"


def inspect():
    tools = {name: shutil.which(name) is not None for name in TOOLS}
    platform_supported = sys.platform == "linux"
    python_supported = sys.version_info >= MINIMUM_PYTHON

    if INSTALLED.is_dir():
        # An installed package only ever uses its own client, whatever else is on PATH. Its exact version is checked
        # when the isolated session starts; this offline report does not run it.
        claude_binary = (INSTALLED / "claude-code").is_file()
    else:
        try:
            launcher.claude_binary()
            claude_binary = True
        except (OSError, ValueError):
            claude_binary = False

    try:
        skills.inspect_skill(skills.ROOT, SKILL)
        source_skill = True
    except (OSError, ValueError, UnicodeError):
        source_skill = False

    profile = PROFILE
    profile_dir = profile.is_dir() and not profile.is_symlink()
    installed = profile / "skills" / SKILL / "SKILL.md"
    installed_skill = False

    if profile_dir and source_skill and installed.is_file() and not installed.is_symlink():
        try:
            source = (skills.ROOT / SKILL / "SKILL.md").read_bytes()
            installed_skill = installed.read_bytes() == source
        except OSError:
            pass

    return {
        "platform_linux": platform_supported,
        "python_supported": python_supported,
        "tools_available": tools,
        "claude_binary_valid": claude_binary,
        "claude_profile_directory": bool(profile_dir),
        "skill_source_valid": source_skill,
        "skill_profile_matches": installed_skill,
        # E2: can inspect account/home metadata, NOT private profile or OAuth.
        "isolated_account_metadata_valid": e2_account.account_ready(),
        "offline_prerequisites_present": (
            platform_supported and python_supported and tools["bwrap"]
            and source_skill
        ),
        # Readiness metadata (including the separate UID) cannot authorize live Claude.
        "diagnose_prerequisites_present": False,
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose_flags = ("--v", "--verbose", "-v", "--details")
    if argv in (["--help"], ["-h"]):
        print("Użycie: ai-desktop doctor [--v]")
        print("Sprawdzenie lokalnych metadanych, bez uruchamiania AI.")
        print("--v, --verbose, -v, --details: pełny raport JSON.")
        return 0
    if argv and (len(argv) != 1 or argv[0] not in verbose_flags):
        print("Użycie: ai-desktop doctor [--v]")
        return 2
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("DOCTOR: REFUSED (root)")
        return 2
    data = inspect()
    if argv:
        print("=== AI DESKTOP READINESS (TYLKO ODCZYT) ===")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print("DOCTOR: PASS (sprawdzono wymagania; nie uruchomiono AI)")
    else:
        print("Wymagania offline (metadane): " + (
            "spełnione." if data.get("offline_prerequisites_present") is True else "niekompletne."))
        print("Klient Claude: %s; metadane izolowanego konta: %s." % (
            "znaleziony" if data.get("claude_binary_valid") is True else "brak",
            "poprawne" if data.get("isolated_account_metadata_valid") is True else "niepotwierdzone"))
        print("Logowanie i działanie AI: niesprawdzone.")
        print("Szczegóły: ai-desktop doctor --v")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
