"""Local metadata by default, with explicit isolated offline client probes.

Presence checks are not proof that a namespace or authenticated session works.
The report intentionally contains no host paths, process output or credentials.
"""
import json
import os
from pathlib import Path
import shutil
import sys

from . import e2_account, skills, system_clients

MINIMUM_PYTHON = (3, 12)
TOOLS = ("bwrap", "findmnt", "systemctl")
SKILL = "cachyos-diagnostics"
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
            "source": result.source,
            "detail": result.detail,
            "metadata_trusted": result.status == "present",
            "layout": "native-elf" if result.status == "present" else "unverified",
            "runtime_candidate": result.status == "present",
            "version_checked": False,
            "protocol_checked": False,
        }

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
        "runtime_client_source": "independent",
        "system_clients": discovered,
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
    suffix = "; compatibility unchecked." if data.get("status") == "present" else "; unavailable for execution."
    return descriptions.get(data.get("status"), "metadata unavailable") + suffix


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose_flags = ("--v", "--verbose", "-v", "--details")
    usage = "Usage: ai-desktop doctor [--clients] [--v]"
    if argv in (["--help"], ["-h"]):
        print(usage)
        print("Default: local metadata, without starting a client.")
        print("--clients: probe the selected client offline, without login or AI requests.")
        print("--v, --verbose, -v, --details: full JSON report.")
        return 0
    if (any(arg not in (*verbose_flags, "--clients") for arg in argv)
            or argv.count("--clients") > 1 or sum(arg in verbose_flags for arg in argv) > 1):
        print(usage)
        return 2
    verbose = any(arg in verbose_flags for arg in argv)
    probe_requested = "--clients" in argv
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
        if verbose:
            print(json.dumps(details, indent=2))
        else:
            print("Local inference endpoint: " + details["endpoint"])
            print("Policy: " + ("allowed." if details["authorized"] else "blocked. Set network.local=true to enable."))
            print("Server availability is checked when you approve an inference request.")
        return 0
    data = inspect()
    outcome = None
    if probe_requested:
        if provider not in ("claude", "codex"):
            print("DOCTOR: choose a service with ai-desktop agent use claude or codex first.")
            return 2
        from . import client_compat
        outcome = client_compat.inspect(provider)
        data["client_compatibility"] = outcome
    if verbose:
        print("=== AI DESKTOP READINESS (READ-ONLY) ===")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print("DOCTOR: " + ("REFUSED" if outcome is not None and not outcome["status"].startswith("compatible-")
                            else "PASS") + " (checks completed; login and AI not checked)")
    else:
        print("Offline requirements (metadata): " + (
            "met." if data.get("offline_prerequisites_present") is True else "incomplete."))
        print("Runtime sources: system packages or registered copies (ai-desktop clients list).")
        print("Isolated account metadata: " + (
            "valid." if data.get("isolated_account_metadata_valid") is True else "unconfirmed."))
        for name in ("claude", "codex"):
            item = data.get("system_clients", {}).get(name, {})
            source = item.get("source", "unknown")
            print(name.capitalize() + " (" + source + "): " + system_client_summary(item))
        if outcome is None:
            print("Compatibility: ai-desktop doctor --clients (selected service, offline).")
        else:
            version = " " + outcome["version"] if outcome.get("version") else ""
            print("Selected " + provider.capitalize() + version + ": " + outcome["status"] + ".")
            if not outcome["status"].startswith("compatible-"):
                print("Run ai-desktop clients list; check registration, source and supported layout in docs/AGENT_INSTALL.md.")
        print("Login and AI operation: not checked.")
        print("Details: ai-desktop doctor --v")
    return 2 if outcome is not None and not outcome["status"].startswith("compatible-") else 0


if __name__ == "__main__":
    raise SystemExit(main())
