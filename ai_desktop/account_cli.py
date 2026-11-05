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
    logins = {(p, n): t for p, n, t in state.logins}
    rows = []
    for provider in CLOUD:
        for name in state.items(provider):
            observed, samples = values.get((provider, name), (0, None))
            rows.append({"provider": provider, "account": name, "selected": name == state.active(provider),
                         "usage": _window_text(observed, samples, now), "observed_at": observed or None,
                         "windows": samples or {}, "last_login_at": logins.get((provider, name)),
                         "status": ("retired" if (provider, name) in state.retired else
                                    "login previously confirmed" if (provider, name) in logins else "unchecked")})
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


def _login(provider, name, *, action="login"):
    from .analysis_sandbox import INSTALL, trusted_file
    from .analysis_contract import AnalysisDenied
    if os.getuid() < 1000 or os.getuid() != os.geteuid() or not os.isatty(0) or not os.isatty(1):
        print("A named login requires your normal user's terminal.")
        return 2
    try:
        entry = trusted_file(INSTALL / "codex-auth.py")
        return subprocess.run(["/usr/bin/sudo", "--", "/usr/bin/python3", "-I", "-B", str(entry),
                               "account-" + action, provider, name], check=False, close_fds=True,
                              env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"}, timeout=650).returncode
    except (AnalysisDenied, OSError, subprocess.SubprocessError, KeyboardInterrupt):
        print("The account login did not complete.")
        return 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-desktop account", description="Separate subscription logins by provider")
    actions = parser.add_subparsers(dest="action")
    actions.add_parser("list")
    for action in ("add", "use", "login", "retire"):
        cmd = actions.add_parser(action)
        cmd.add_argument("provider", nargs="?", choices=CLOUD)
        cmd.add_argument("name", nargs="?", help="Local name (letters, digits and hyphens)")
        if action == "retire":
            cmd.add_argument("--confirm-retire", action="store_true",
                             help="Permanently disable this name and delete its isolated local credentials")
        if action == "add":
            cmd.add_argument("--login", action="store_true", help="Also start an explicit, isolated browser login")
    args = parser.parse_args(argv)
    try:
        store = AccountStore()
        if args.action in (None, "list"):
            for row in panel_rows(store=store):
                print("%s %s/%s · %s" % ("*" if row["selected"] else "-", row["provider"], row["account"], row["status"]))
            return 0
        if not args.provider or not args.name:
            if not os.isatty(0) or not os.isatty(1):
                print("A provider and local account name are required outside an interactive terminal.")
                return 2
            from .terminal_menu import choose, MenuInputError
            from .terminal_consent import fresh_input
            try:
                if not args.provider:
                    pick = choose("Provider", CLOUD)
                    if pick is None:
                        return 0
                    args.provider = CLOUD[pick]
                if not args.name:
                    if args.action == "add":
                        args.name = fresh_input("New local account name: ", max_bytes=32).strip()
                    else:
                        state = store.load().config
                        names = tuple(name for name in state.items(args.provider)
                                      if (args.provider, name) not in state.retired
                                      and (args.action == "use" or name != "default"))
                        if not names:
                            print("No named account is registered for this provider.")
                            return 2
                        pick = choose("Account", names)
                        if pick is None:
                            return 0
                        args.name = names[pick]
            except (MenuInputError, EOFError, KeyboardInterrupt):
                return 2
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
        if args.action == "retire":
            if not args.confirm_retire:
                print("Retiring permanently blocks this name and removes its local credentials. "
                      "Remote sessions may still need revocation at the provider. To proceed, add --confirm-retire.")
                return 2
            result = _login(args.provider, args.name, action="retire")
            if result == 0:
                store.mutate(lambda state: state.retire(args.provider, args.name))
                print("Retired %s/%s locally. Choose another account for new tasks." % (args.provider, args.name))
            return result
        if (args.provider, args.name) in store.load().config.retired:
            raise ProfileError("account-retired")
        result = _login(args.provider, args.name)
        if result == 0:
            store.mutate(lambda state: state.logged_in(args.provider, args.name, int(time.time())))
        return result
    except (OSError, ValueError):
        print("Account operation refused. Check the account name and ai-desktop account list.")
        return 2
