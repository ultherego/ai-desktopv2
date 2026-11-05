"""Owner-facing named subscriptions and an offline display of provider-reported limit observations."""
import argparse
from datetime import datetime, timezone
import json
import os
import subprocess
import time

from .account_store import AccountStore, CLOUD, named_account
from .agent_profiles import ProfileError


def _window_text(observed, samples, now):
    if not samples:
        return "not reported"
    measured = datetime.fromtimestamp(observed, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    details = []
    for name, title in (("five_hour", "5h"), ("seven_day", "7d")):
        value = samples.get(name)
        if value is not None:
            reset = datetime.fromtimestamp(value["reset"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            details.append("%s %g%% used, resets %s%s" % (title, value["percent"], reset,
                           " (window expired)" if now >= value["reset"] else ""))
    return ", ".join(details) + "; observed " + measured


def panel_rows(*, store=None, now=None):
    state = (store or AccountStore()).load().config
    now = int(time.time()) if now is None else now
    values = {(p, n): (t, s) for p, n, _, t, s in state.rows}
    rows = []
    for provider in CLOUD:
        for name in state.items(provider):
            observed, samples = values.get((provider, name), (0, None))
            rows.append({"provider": provider, "account": name, "selected": name == state.active(provider),
                         "usage": _window_text(observed, samples, now), "observed_at": observed or None,
                         "windows": samples or {}})
    return rows


def usage_main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-desktop usage", description="Last quota windows reported by approved subscriptions")
    parser.add_argument("--provider", choices=CLOUD)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        rows = [r for r in panel_rows() if args.provider in (None, r["provider"])]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            for row in rows:
                print("{provider}/{account}   {usage}".format(**row))
        return 0
    except (OSError, ValueError):
        print("Could not read account settings.")
        return 2


def remember(provider, name, samples, *, store=None, now=None):
    if provider not in CLOUD or not samples:
        return
    try:
        observed = int(time.time()) if now is None else now
        (store or AccountStore()).mutate(lambda current: current.note(provider, name, observed, samples))
    except (OSError, ValueError):
        # Quota telemetry is optional. A busy or unreadable cache must not lose a completed response.
        pass


def _login(provider, name):
    from .analysis_sandbox import INSTALL, trusted_file
    from .analysis_contract import AnalysisDenied
    if os.getuid() < 1000 or os.getuid() != os.geteuid() or not os.isatty(0) or not os.isatty(1):
        print("A named login requires your normal user's terminal.")
        return 2
    try:
        entry = trusted_file(INSTALL / "codex-auth.py")
        return subprocess.run(["/usr/bin/sudo", "--", "/usr/bin/python3", "-I", "-B", str(entry),
                               "account-login", provider, name], check=False, close_fds=True,
                              env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"}, timeout=650).returncode
    except (AnalysisDenied, OSError, subprocess.SubprocessError, KeyboardInterrupt):
        print("The account login did not complete.")
        return 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-desktop account", description="Separate subscription logins by provider")
    actions = parser.add_subparsers(dest="action")
    actions.add_parser("list")
    for action in ("add", "use", "login"):
        cmd = actions.add_parser(action)
        cmd.add_argument("provider", choices=CLOUD)
        cmd.add_argument("name", help="Local name (letters, digits and hyphens)")
        if action == "add":
            cmd.add_argument("--login", action="store_true", help="Also start an explicit, isolated browser login")
    args = parser.parse_args(argv)
    try:
        store = AccountStore()
        if args.action in (None, "list"):
            for row in panel_rows(store=store):
                print("%s %s/%s" % ("*" if row["selected"] else "-", row["provider"], row["account"]))
            return 0
        if args.action == "use":
            store.mutate(lambda state: state.select(args.provider, args.name))
            print("Selected %s/%s for new tasks." % (args.provider, args.name))
            return 0
        if not named_account(args.name):
            raise ProfileError("account-name")
        if args.action == "add":
            store.mutate(lambda state: state.add(args.provider, args.name))
            print("Added %s/%s." % (args.provider, args.name))
            if not args.login:
                print("Connect with: ai-desktop account login %s %s" % (args.provider, args.name))
                return 0
        elif args.name not in store.load().config.items(args.provider):
            raise ProfileError("account-missing")
        return _login(args.provider, args.name)
    except (OSError, ValueError):
        print("Account operation refused. Check the account name and ai-desktop account list.")
        return 2
