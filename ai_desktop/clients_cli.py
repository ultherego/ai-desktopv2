"""Explicit registration of independently installed clients, separate from login.

The regular-user command previews immutable payload metadata. Only the fixed
installed administrative helper can publish/remove a root-owned runtime copy;
no native client, installer, shell script or package manager is executed here.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .analysis_contract import AnalysisDenied
from .analysis_sandbox import INSTALL, trusted_file
from .terminal_consent import fresh_input

PROVIDERS = ("claude", "codex")
ENV = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"}
DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def discover(provider, explicit_path=None):
    from .client_sources import discover as inspect_source
    return inspect_source(provider, explicit_path=explicit_path)


def registration_status(provider):
    from .client_registration import registration_status as status
    return status(provider)


def _text(value):
    return json.dumps(value, ensure_ascii=True)


def _digest(value):
    if type(value) is not str or not DIGEST.fullmatch(value):
        raise ValueError("registration digest")
    return value


def _identity():
    if os.getuid() <= 0 or os.getuid() != os.geteuid() or os.getgid() != os.getegid():
        raise ValueError("regular user required")


def _approve(action):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    while True:
        answer = fresh_input(action + " this system-wide isolated client copy for all users? [y/n]: ").strip().casefold()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Answer y or n.")


def _admin(arguments):
    entry = trusted_file(INSTALL / "client-admin.py")
    result = subprocess.run(["/usr/bin/sudo", "--", "/usr/bin/python3", "-I", "-B", str(entry),
                             "--allow-client-registration", *arguments],
                            env=ENV, close_fds=True, check=False, timeout=180)
    return 0 if result.returncode == 0 else 2


def list_clients():
    from . import system_clients
    for provider in PROVIDERS:
        registered = registration_status(provider)
        active = system_clients.inspect(provider)
        source = discover(provider, registered.get("path") if registered["status"] == "registered" else None)
        print(provider.capitalize() + ": runtime=" + active.status + "; registration=" + registered["status"]
              + "; installed-source=" + source.status)
        if source.status == "present":
            print("  Source: " + _text(source.path))
            print("  Layout: " + _text(source.layout) + "; files=" + str(source.files_count)
                  + "; bytes=" + str(source.total_bytes))
        if registered["status"] == "registered":
            print("  Registered digest: " + _digest(registered["digest"]))
            if registered.get("path") is not None:
                print("  Registered source: " + _text(registered["path"]))
            if registered.get("path") is not None and source.status == "present" and source.digest != registered["digest"]:
                print("  Source differs from the registered copy: ai-desktop clients refresh " + provider)
        elif source.status == "present" and active.status != "present":
            print("  Register for isolated use: ai-desktop clients register " + provider)
    print("Metadata only; version, protocol and login have not been checked.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-desktop clients", description=__doc__)
    commands = parser.add_subparsers(dest="action")
    commands.add_parser("list", help="Show installed sources and registered runtime copies without running clients")
    for action in ("register", "refresh"):
        command = commands.add_parser(action, help=action.capitalize() + " a reviewed copy of your installed native payload")
        command.add_argument("provider", choices=PROVIDERS)
        command.add_argument("--path", help="Explicit installed executable or launcher; inspected as data, never run")
    remove = commands.add_parser("remove", help="Remove the registered isolated copy; preserve the original client and login data")
    remove.add_argument("provider", choices=PROVIDERS)
    args = parser.parse_args(argv)
    try:
        _identity()
        if args.action in (None, "list"):
            return list_clients()
        registered = registration_status(args.provider)
        state = registered["status"]
        if state not in ("registered", "missing"):
            print("Registration metadata is unavailable or unsafe; no change was made.")
            return 2
        if args.action == "register" and state == "registered":
            print("A copy is already registered. Use ai-desktop clients refresh " + args.provider + ".")
            return 2
        if args.action == "refresh" and state == "missing":
            print("No copy is registered. Use ai-desktop clients register " + args.provider + ".")
            return 2
        previous = _digest(registered["digest"]) if state == "registered" else None
        if args.action == "remove":
            if state == "missing":
                print("No registered " + args.provider + " copy to remove.")
                return 0
            expected = previous
            print("Remove registered " + args.provider + " payload with digest " + expected + ".")
            print("The independently installed client and isolated login data remain in place.")
            if not _approve("Remove"):
                print("Cancelled.")
                return 2
            return _admin(["remove", "--provider", args.provider, "--expected-digest", expected])
        requested = args.path
        if requested is None and args.action == "refresh":
            requested = registered.get("path")
        source = discover(args.provider, requested)
        if source.status != "present":
            if getattr(source, "detail", None) == "legacy-layout":
                print("Client source unavailable: unsupported legacy Codex layout.")
                print("Update the independently installed Codex with its original installer, then retry registration.")
            else:
                print("Client source unavailable: " + source.status + ". Run ai-desktop clients list or select --path.")
            return 2
        expected = _digest(source.digest)
        if type(source.path) is not str or not source.path.startswith("/"):
            raise ValueError("absolute source")
        print(args.action.capitalize() + " " + args.provider + " for the system-wide isolated runtime (all users).")
        print("  Source: " + _text(source.path))
        print("  Layout: " + _text(source.layout) + "; files=" + str(source.files_count)
              + "; bytes=" + str(source.total_bytes))
        print("  Payload digest: " + expected)
        if previous is not None:
            print("  Replacing registered digest: " + previous)
        print("The source stays unchanged. Registration does not run the client or access login data.")
        if not _approve(args.action.capitalize()):
            print("Cancelled.")
            return 2
        arguments = [args.action, "--provider", args.provider, "--source", source.path, "--expected-digest", expected]
        if args.action == "refresh":
            arguments += ["--previous-digest", previous]
        return _admin(arguments)
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.")
        return 2
    except (AnalysisDenied, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("Client registration failed; no automatic retry. Check ai-desktop clients list.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
