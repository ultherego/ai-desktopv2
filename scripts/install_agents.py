#!/usr/bin/env python3
"""Check independently installed clients and optionally install AI Desktop only.

The historical entry point remains usable, but never downloads, installs,
updates, removes or executes a provider client. Metadata presence is not a
successful version/protocol or login check; the isolated runtime does those.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_desktop import system_clients

CLIENTS = ("claude", "codex")
PACKAGES = {"claude": "claude-code", "codex": "openai-codex"}


class InstallError(ValueError):
    pass


def discover(provider):
    from ai_desktop.client_sources import discover as inspect_source
    return inspect_source(provider)


def prepare(root, selected, *, installing=False):
    """Inspect only selected runtime metadata; app installation needs no client."""
    if (type(selected) is not tuple or any(type(p) is not str or p not in CLIENTS for p in selected)
            or len(selected) != len(set(selected))):
        raise InstallError("Invalid provider selection.")
    results = {}
    for provider in selected:
        result = system_clients.inspect(provider)
        results[provider] = result
    return results


def main(argv=None, *, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clients", choices=("claude", "codex", "both", "local"))
    parser.add_argument("--install", action="store_true", help="confirm, build and install AI Desktop; never provider packages")
    args = parser.parse_args(argv)
    if os.getuid() == 0 or os.geteuid() != os.getuid():
        print("Run as the regular user who owns this checkout.", file=sys.stderr)
        return 2
    selected = CLIENTS if args.clients == "both" else () if args.clients == "local" else (args.clients,)
    try:
        results = prepare(root, selected, installing=args.install)
        steps = []
        if selected:
            for provider, result in results.items():
                print(provider + ": isolated runtime metadata=" + result.status + ".", flush=True)
                if result.status != "present":
                    source = discover(provider)
                    if source.status == "present":
                        steps.append("ai-desktop clients register " + provider)
                        print("An independently installed source is available; explicit registration is required.", flush=True)
                    else:
                        print("Install " + provider + " independently (native installer, npm or " + PACKAGES[provider]
                              + "), then inspect it with ai-desktop clients list.", flush=True)
            print("Version, protocol and login not checked. AI Desktop installation does not require a registered client.", flush=True)
        else:
            print("Local inference selected; no cloud client is required.", flush=True)
        if args.install:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise InstallError("Installation requires an interactive terminal.")
            if not Path("/usr/bin/makepkg").is_file():
                raise InstallError("Install Arch base-devel first.")
            answer = input("Build and install AI Desktop only with makepkg -si? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return 2
            with tempfile.TemporaryDirectory(prefix="ai-desktop-package-") as pkgdest:
                env = dict(os.environ, PKGDEST=pkgdest)
                for name in ("AI_DESKTOP_PUBLIC_ARTIFACT", "AI_DESKTOP_CHECK_CLIENTS", "AI_DESKTOP_EXPECT_CLAUDE", "AI_DESKTOP_EXPECT_CODEX"):
                    env.pop(name, None)
                subprocess.run(["/usr/bin/makepkg", "--cleanbuild", "-si"], cwd=root / "packaging/arch",
                               env=env, check=True)
        for step in steps:
            print("After AI Desktop is installed, review and register the source explicitly: " + step, flush=True)
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
        return 2
    except (OSError, InstallError, subprocess.CalledProcessError) as error:
        print("Could not check clients or install AI Desktop:", error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
