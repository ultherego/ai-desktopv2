"""Metadata-only local readiness diagnostic; never runs native clients or commands.

Presence checks are not proof that a namespace or authenticated session works.
The report intentionally contains no host paths, process output or credentials.
"""
import json
import os
from pathlib import Path
import shutil
import sys

from . import e2_account, launcher, skills, system_clients

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
    discovered = {}
    for provider in ("claude", "codex"):
        result = system_clients.inspect(provider)
        # Keep paths and filesystem identities local to the inspector. Trust
        # here covers file metadata only, never vendor or protocol verification.
        discovered[provider] = {
            "status": result.status,
            "detail": result.detail,
            "metadata_trusted": result.status == "present",
            "layout": "native-elf" if result.status == "present" else "unverified",
            "active": False,
            "version_checked": False,
            "protocol_checked": False,
        }

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
        "runtime_client_source": "bundled-package",
        "bundled_clients_present": {
            "claude": (INSTALLED / "claude-code").is_file(),
            "codex": (INSTALLED / "codex").is_file(),
        },
        "system_clients": discovered,
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


def system_client_summary(data):
    descriptions = {
        "present": "trusted file metadata, native ELF",
        "missing": "missing",
        "unsafe": "unsafe file metadata",
        "unsupported": "unsupported layout",
        "changed": "changed during inspection",
        "unavailable": "metadata unavailable",
    }
    return descriptions.get(data.get("status"), "metadata unavailable") + "; not active."


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose_flags = ("--v", "--verbose", "-v", "--details")
    if argv in (["--help"], ["-h"]):
        print("Usage: ai-desktop doctor [--v]")
        print("Checks local metadata without starting AI.")
        print("--v, --verbose, -v, --details: full JSON report.")
        return 0
    if argv and (len(argv) != 1 or argv[0] not in verbose_flags):
        print("Usage: ai-desktop doctor [--v]")
        return 2
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("DOCTOR: REFUSED (root)")
        return 2
    from .agent_profiles import Store
    try:
        provider = Store().load().config.selected_provider
    except (OSError, ValueError, KeyError):
        provider = None
    if provider == "local":
        from .permissions import load_policy
        from .local_provider import endpoint
        from .execution import DEFAULT_LOCAL_PORT
        try:
            policy = load_policy(optional=True)
        except (OSError, ValueError):
            print("DOCTOR: invalid permissions.toml; local inference is blocked.")
            return 2
        details = {"provider": "local", "endpoint": endpoint(policy.local_port if policy else DEFAULT_LOCAL_PORT),
                   "authorized": bool(policy and policy.provider_allowed("local")), "server_checked": False,
                   "python_supported": sys.version_info >= MINIMUM_PYTHON, "platform_linux": sys.platform == "linux"}
        if argv:
            print(json.dumps(details, indent=2))
        else:
            print("Local inference endpoint: " + details["endpoint"])
            print("Policy: " + ("allowed." if details["authorized"] else "blocked. Set network.local=true to enable."))
            print("Server availability is checked when you approve an inference request.")
        return 0
    data = inspect()
    if argv:
        print("=== AI DESKTOP READINESS (READ-ONLY) ===")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print("DOCTOR: PASS (requirements checked; AI not started)")
    else:
        print("Offline requirements (metadata): " + (
            "met." if data.get("offline_prerequisites_present") is True else "incomplete."))
        bundled = data.get("bundled_clients_present", {})
        print("Runtime source: bundled package clients (Claude %s; Codex %s)." % (
            "found" if bundled.get("claude") is True else "missing",
            "found" if bundled.get("codex") is True else "missing"))
        print("Isolated account metadata: " + (
            "valid." if data.get("isolated_account_metadata_valid") is True else "unconfirmed."))
        for provider in ("claude", "codex"):
            print("System " + provider.capitalize() + ": " + system_client_summary(
                data.get("system_clients", {}).get(provider, {})))
        print("System client vendor, version and protocol: not verified; runtime migration is pending.")
        print("Login and AI operation: not checked.")
        print("Details: ai-desktop doctor --v")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
