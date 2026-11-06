"""Owner-facing named subscriptions and an offline display of provider-reported limit observations."""
import argparse
from datetime import datetime, timezone
import json
import os
import shutil
import sys
import subprocess
import time

from .account_store import AccountStore, CLOUD, named_account
from .agent_profiles import ProfileError
from .login_flow import authenticate_account, login_status
from .terminal_style import paint


def _window_text(observed, samples, now):
    if not samples:
        return "not reported"
    measured = datetime.fromtimestamp(observed, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    details = []
    for name, title in (("five_hour", "5h"), ("seven_day", "7d")):
        value = samples.get(name)
        if value is not None:
            reset = datetime.fromtimestamp(value["reset"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            detail = "%s %g%% used, resets %s%s" % (title, value["percent"], reset,
                     " (window expired)" if now >= value["reset"] else "")
            if "observed_at" in value:
                earlier = datetime.fromtimestamp(value["observed_at"], timezone.utc)
                detail += earlier.strftime(", reported %Y-%m-%d %H:%M UTC")
            details.append(detail)
    return ", ".join(details) + "; observed " + measured


def panel_rows(*, store=None, now=None):
    state = (store or AccountStore()).load().config
    now = int(time.time()) if now is None else now
    values = {(p, n): (t, s) for p, n, _, t, s in state.rows}
    logins = {(p, n): t for p, n, t in state.logins}
    plans = {(p, n): (plan, t) for p, n, plan, t in state.plans}
    native_limits = {name: (t, plan, windows) for name, t, plan, windows in state.native_limits}
    rows = []
    for provider in CLOUD:
        for name in state.items(provider):
            observed, samples = values.get((provider, name), (0, None))
            plan, plan_time = plans.get((provider, name), (None, None))
            native = native_limits.get(name) if provider == "codex" else None
            source = "provider_event" if samples or plan else None
            if native is not None and native[0]:
                observed, plan, samples = native
                plan_time = observed if plan else None
                source = "codex_native"
            retired = (provider, name) in state.retired
            auth = "signed_out" if retired else state.auth_status(provider, name)
            rows.append({"live_limits_connected": native is not None, "quota_source": source,
                         "native_limits_cleanup_pending": provider == "codex" and name in state.native_stale,
                         "auth_state": "signed_out" if auth == "new_alias" else auth,
                         "requires_new_alias": auth == "new_alias", "plan": plan, "plan_observed_at": plan_time, "provider": provider, "account": name, "selected": name == state.active(provider),
                         "usage": _window_text(observed, samples, now), "observed_at": observed or None,
                         "windows": samples or {}, "last_login_at": logins.get((provider, name)),
                         "status": "retired" if retired else {
                             "signed_in": "local sign-in confirmed", "signed_out": "signed out",
                             "new_alias": "signed out (new alias required)", "unknown": "unchecked"}[auth]})
    return rows


def _local_time(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _until_reset(seconds):
    if seconds < 60:
        return "<1m"
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %dm" % (hours, minutes)
    return "%dm" % minutes


def print_usage(rows):
    """Human view of already-read quota snapshots. Never contacts a subscription."""
    if not rows:
        print("No subscription accounts to display.")
        return
    width = shutil.get_terminal_size((80, 24)).columns
    now = int(time.time())
    unicode_bar = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") == "utf8"
    full, empty = ("█", "░") if unicode_bar else ("#", "-")
    previous = None
    for row in rows:
        provider = row["provider"]
        if previous is not None:
            print()
        if provider != previous:
            print(paint({"claude": "Claude", "codex": "Codex"}[provider], "accent", "bold"))
        previous = provider
        name = paint(row["account"], "bold")
        selected = " (selected)" if row["selected"] else ""
        plan = (row.get("plan") or "unknown").replace("_", " ")
        print("  Account  " + name + selected)
        print("  Plan     " + plan)
        print("  Status   " + row.get("status", "unchecked"))
        if row.get("quota_source") == "codex_native":
            print("  Source   Native Codex limits")
        windows = row.get("windows") or {}
        if not windows:
            print("  Limits   " + paint("Not reported", "muted"))
            if provider == "codex" and row.get("status") != "retired":
                if row.get("quota_source") == "codex_native":
                    print("    The native Codex account reported no 5-hour or 7-day window.")
                    if row.get("observed_at"):
                        print("    Checked " + _local_time(row["observed_at"]))
                elif row.get("live_limits_connected"):
                    print("    Read live limits: ai-desktop usage --refresh")
                else:
                    print("    Sign-in does not return usage limits.")
                    if row.get("auth_state") == "signed_in":
                        print("    Connect live limits: ai-desktop account connect-limits codex " + row["account"])
                print("    https://chatgpt.com/settings/usage")
            continue
        for key, label in (("five_hour", "5-hour"), ("seven_day", "7-day")):
            sample = windows.get(key)
            prefix = "    %-7s" % label
            if sample is None:
                print(prefix + paint("Not reported", "muted"))
                continue
            percent, reset = sample["percent"], sample["reset"]
            expired = reset <= now
            # Keep a nearly full window distinguishable from an exhausted one.
            if 99.9 < percent < 100:
                percent_text = ">99.9%"
            elif 0 < percent < 0.1:
                percent_text = "<0.1%"
            else:
                percent_text = ("%.1f" % percent).removesuffix(".0") + "%"
            value = "%6s %s" % (percent_text, "last recorded" if expired else "used")
            tone = "muted" if expired else "red" if percent >= 90 else "yellow" if percent >= 75 else "green"
            bar = ""
            bar_width = 0
            if width >= 72:
                bar_width = 12
                if expired:
                    bar = " " * bar_width
                else:
                    count = int(percent // 10)
                    bar = paint(full * count, tone) + paint(empty * (10 - count), "muted") + "  "
            stamp = _local_time(reset)
            label = "Window expired" if expired else "Resets in " + _until_reset(reset - now)
            timing = label + " (" + stamp + ")"
            if len(prefix + value + "   " + timing) + bar_width <= width:
                print(prefix + bar + paint(value, tone, "bold") + "   " + paint(timing, "muted"))
            else:
                print(prefix + bar + paint(value, tone, "bold"))
                if len(timing) + 6 <= width:
                    print("      " + paint(timing, "muted"))
                else:
                    print("      " + paint(label, "muted"))
                    print("      " + paint(stamp, "muted"))
            if "observed_at" in sample:
                print(paint("      Reported " + _local_time(sample["observed_at"]), "muted"))
        if row.get("observed_at"):
            print(paint("    Last reported " + _local_time(row["observed_at"]), "muted"))


def usage_main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-desktop usage", description="Display local quota readings or explicitly refresh native Codex limits")
    parser.add_argument("--provider", choices=CLOUD)
    parser.add_argument("--account", help="Local account alias for this provider")
    parser.add_argument("--refresh", action="store_true", help="Read live limits for the selected connected Codex slot")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.account is not None and (args.account != "default" and not named_account(args.account)
                                     or not args.provider and not args.refresh):
        parser.error("--account requires a valid local alias and a provider or --refresh")
    if args.refresh and args.provider == "claude":
        parser.error("--refresh is available for Codex")
    provider, name = args.provider, args.account
    try:
        if args.refresh:
            from .codex_limits_cli import refresh_limits
            provider = "codex"
            name = refresh_limits(AccountStore(), name)
        rows = [r for r in panel_rows() if provider in (None, r["provider"]) and name in (None, r["account"])]
        if name and not rows:
            raise ProfileError("account")
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            print_usage(rows)
        return 0
    except (OSError, ValueError) as error:
        from .codex_limits_cli import ERRORS, error_reason
        reason = error_reason(error)
        if args.json:
            print(json.dumps({"error": reason}))
        else:
            print(ERRORS[reason] if args.refresh or name else "Could not read account settings.")
        return 2


def remember(provider, name, samples, *, plan=None, store=None, now=None):
    if provider not in CLOUD or not (samples or plan):
        return
    try:
        observed = int(time.time()) if now is None else now
        def apply(current):
            if (provider, name) in current.retired or current.auth_status(provider, name) in ("signed_out", "new_alias"):
                return current
            if samples:
                current = current.note(provider, name, observed, samples)
            return current.with_plan(provider, name, plan, observed) if plan else current
        (store or AccountStore()).mutate(apply)
    except (OSError, ValueError):
        # Quota telemetry is optional. A busy or unreadable cache must not lose a completed response.
        pass


def _login(provider, name, *, action="login"):
    from .analysis_sandbox import INSTALL, trusted_file
    from .analysis_contract import AnalysisDenied
    if os.getuid() < 1000 or os.getuid() != os.geteuid() or not os.isatty(0) or not os.isatty(1):
        print("Account operations require your regular user's terminal.")
        return 2
    try:
        entry = trusted_file(INSTALL / "codex-auth.py")
        return subprocess.run(["/usr/bin/sudo", "--", "/usr/bin/python3", "-I", "-B", str(entry),
                               "account-" + action, provider, name], check=False, close_fds=True,
                              env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"}, timeout=650).returncode
    except KeyboardInterrupt:
        if action == "login":
            raise
        print("The account operation was interrupted.")
        return 2
    except (AnalysisDenied, OSError, subprocess.SubprocessError):
        print("The account operation did not complete.")
        return 2


def _check_or_logout(store, provider, name, action, *, authorized=None):
    from .terminal_consent import fresh_input
    if os.getuid() < 1000 or os.getuid() != os.geteuid() or not os.isatty(0) or not os.isatty(1):
        print("Open this account action in your regular user's terminal.")
        return 2
    key = (provider, name)
    if key in store.load().config.retired:
        print("This account name is retired.")
        return 2
    if action == "logout" and authorized is None:
        if provider == "claude" and name != "default":
            print("Signing out closes this Claude alias. Add a new alias for a future login.")
        elif name == "default":
            print("This signs out the default profile for every desktop user of this installation. Existing default-account conversations "
                  "will use the next subscription connected here. Use a new named account to change identity.")
        try:
            answer = fresh_input("Sign out of %s/%s on this installation? [y/N]: " % key)
        except (EOFError, KeyboardInterrupt):
            login_status("Sign-out cancelled", "yellow", provider, name)
            return 2
        if answer.strip().casefold() not in ("y", "yes"):
            login_status("Sign-out cancelled", "yellow", provider, name)
            return 0
    with store.lifecycle(provider, name):
        if key in store.load().config.retired:
            return 2
        code = authorized() if authorized is not None else _login(provider, name, action=action)
        failure = "Sign-in check failed" if action == "check" else "Sign-out failed"
        if type(code) is not int or (code == 6 and name == "default"):
            login_status(failure, "red", provider, name)
            return 2
        if action == "check" and code in (0, 4, 6):
            status = {0: "signed_in", 4: "signed_out", 6: "new_alias"}[code]
        elif action == "logout" and code in (0, 5, 6):
            status = "new_alias" if code == 6 else "signed_out"
        else:
            login_status(failure, "red", provider, name)
            return code
        try:
            store.mutate(lambda state: state.checked(provider, name, status))
        except (OSError, ValueError):
            login_status("Desktop indicator could not be saved", "yellow", provider, name)
            print("  Run the local sign-in check again.")
            return 3
        if status == "signed_in":
            login_status("Local sign-in found", "green", provider, name)
        else:
            login_status("No local sign-in" if action == "check" else "Signed out locally",
                         "yellow" if action == "check" or code == 5 else "green", provider, name)
            if status == "new_alias":
                print("  Add a new alias for the next login.")
        if action == "logout" and code == 5:
            print("The provider did not confirm remote revocation.")
            return 5
        return 0


def _widget_result(result):
    """Give a desktop-launched terminal a chance to display the result before exiting."""
    from .terminal_consent import fresh_input
    if os.isatty(0) and os.isatty(1):
        try:
            fresh_input("Press Enter to close this window.")
        except (EOFError, KeyboardInterrupt, OSError, ValueError):
            pass
    return result


def _connect(provider, store, *, from_widget=False):
    """Interactive account picker used by the packaged desktop sign-in actions."""
    if os.getuid() < 1000 or os.getuid() != os.geteuid() or not os.isatty(0) or not os.isatty(1):
        print("Open this account picker in your regular user's terminal.")
        return 2
    from .ask_commands import _pick
    from .cli import main as dispatch

    label = {"claude": "Claude", "codex": "Codex"}[provider]
    while True:
        state = store.load().config
        selected = state.active(provider)
        choices = []
        if state.auth_status(provider, "default") != "signed_in":
            choices.append((f"Sign in to default and select the {label} service", ["login", "--provider", provider]))
        linked = {record[0] for record in state.native_limits}
        undisplayed = []
        for name in state.items(provider):
            if provider == "codex":
                if name in linked or name in state.native_stale:
                    choices.append(("Disconnect live limits for " + name, ["account", "disconnect-limits", "codex", name]))
                else:
                    undisplayed.append(name)
            if (provider, name) in state.retired:
                continue
            auth = state.auth_status(provider, name)
            if auth == "signed_in":
                choices.append(("Sign out of " + name, ["account", "logout", provider, name]))
            elif name != "default" and auth != "new_alias":
                choices.append(("Sign in to " + name, ["account", "login", provider, name]))
            choices.append(("Check sign-in for " + name, ["account", "check", provider, name]))
            if provider == "codex" and name == selected and auth == "signed_in" and name not in linked:
                choices.append(("Connect live limits for " + name, ["account", "connect-limits", "codex", name]))
            if name != selected and auth != "new_alias":
                choices.append(("Use " + name + " for new tasks", ["account", "use", provider, name]))
        choices.append(("Add a named account and sign in", ["account", "add", provider, "--login"]))
        if undisplayed:
            # A credential can outlive a failed desktop receipt write.
            choices.append(("Remove an unlisted native limits connection…", []))
        choices.append(("Close", None))
        pick = _pick(f"{label} accounts · selected: {selected}", [title for title, _ in choices])
        if pick is None or choices[pick][1] is None:
            return 0
        # Only the legacy default login can choose the service; named logins and account selection
        # keep their existing scope and checks. Leave the result visible before the next choice.
        command = choices[pick][1]
        if command == []:
            slot = _pick("Which Codex limits connection should be removed?", [*undisplayed, "Back"])
            if slot is None or slot == len(undisplayed):
                continue
            command = ["account", "disconnect-limits", "codex", undisplayed[slot]]
        result = dispatch(command)
        if from_widget:
            return _widget_result(result)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-desktop account", description="Separate subscription logins by provider")
    actions = parser.add_subparsers(dest="action")
    actions.add_parser("list")
    connect = actions.add_parser("connect", help="Choose an account to sign in, add or use for this service")
    connect.add_argument("provider", choices=CLOUD)
    connect.add_argument("--from-widget", action="store_true", help=argparse.SUPPRESS)
    for action, help_text in (("connect-limits", "Pair a native Codex account to read its live quota"),
                              ("disconnect-limits", "Remove a native Codex quota connection")):
        cmd = actions.add_parser(action, help=help_text)
        cmd.add_argument("--from-widget", action="store_true", help=argparse.SUPPRESS)
        cmd.add_argument("provider", choices=("codex",))
        cmd.add_argument("name", help="Exact local Codex account alias, including default")
    for action in ("check", "logout"):
        cmd = actions.add_parser(action, help="Check local credentials" if action == "check" else "Sign out of one local account")
        cmd.add_argument("--from-widget", action="store_true", help=argparse.SUPPRESS)
        cmd.add_argument("provider", choices=CLOUD)
        cmd.add_argument("name", help="Exact local alias, including default")
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
    signing_in = args.action == "login" or (args.action == "add" and args.login)
    try:
        store = AccountStore()
        if args.action in ("connect-limits", "disconnect-limits"):
            from .codex_limits_cli import ERRORS, error_reason, manage_limits
            try:
                result = manage_limits(store, args.name, connect=args.action == "connect-limits")
            except (OSError, ValueError) as error:
                print(ERRORS[error_reason(error)])
                result = 2
            return _widget_result(result) if args.from_widget else result
        if args.action == "connect":
            return _connect(args.provider, store, from_widget=args.from_widget)
        if args.action in (None, "list"):
            for row in panel_rows(store=store):
                print("%s %s/%s · %s" % ("*" if row["selected"] else "-", row["provider"], row["account"], row["status"]))
            return 0
        if not args.provider or not args.name:
            if not os.isatty(0) or not os.isatty(1):
                if signing_in:
                    login_status("Sign-in failed", "red", args.provider, args.name)
                print("A provider and local account name are required outside an interactive terminal.")
                return 2
            from .terminal_menu import choose, MenuInputError
            from .terminal_consent import fresh_input
            try:
                if not args.provider:
                    pick = choose("Provider", CLOUD)
                    if pick is None:
                        if signing_in:
                            login_status("Sign-in cancelled", "yellow")
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
                            if signing_in:
                                login_status("Sign-in failed", "red", args.provider)
                            print("No named account is registered for this provider.")
                            return 2
                        pick = choose("Account", names)
                        if pick is None:
                            if signing_in:
                                login_status("Sign-in cancelled", "yellow", args.provider)
                            return 0
                        args.name = names[pick]
            except MenuInputError:
                if signing_in:
                    login_status("Sign-in failed", "red", args.provider)
                return 2
            except (EOFError, KeyboardInterrupt):
                if signing_in:
                    login_status("Sign-in cancelled", "yellow", args.provider)
                return 2
        if args.action == "use":
            store.mutate(lambda state: state.select(args.provider, args.name))
            print("Selected %s/%s for new tasks." % (args.provider, args.name))
            return 0
        if args.action in ("check", "logout"):
            if (args.name != "default" and not named_account(args.name)) or args.name not in store.load().config.items(args.provider):
                raise ProfileError("account-name")
            result = _check_or_logout(store, args.provider, args.name, args.action)
            return _widget_result(result) if args.from_widget else result
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
        if args.action == "retire" and not args.confirm_retire:
            print("Retiring permanently blocks this name and removes its local credentials. "
                  "Remote sessions may still need revocation at the provider. To proceed, add --confirm-retire.")
            return 2
        with store.lifecycle(args.provider, args.name):
            if args.action == "retire":
                result = _login(args.provider, args.name, action="retire")
                if result == 0:
                    store.mutate(lambda state: state.retire(args.provider, args.name))
                    print("Retired %s/%s locally. Choose another account for new tasks." % (args.provider, args.name))
                return result
            state = store.load().config
            if (args.provider, args.name) in state.retired or state.auth_status(args.provider, args.name) == "new_alias":
                login_status("Sign-in failed", "red", args.provider, args.name)
                print("This name is closed. Add a fresh name for another login.")
                return 2
            result = authenticate_account(args.provider, args.name, lambda: _login(args.provider, args.name))
            if result != 0:
                return result
            try:
                store.mutate(lambda state: state.logged_in(args.provider, args.name, int(time.time())))
            except (OSError, ValueError):
                login_status("Signed in", "yellow", args.provider, args.name)
                print("  Could not save the desktop indicator. Use Check sign-in in the account picker.")
                return 3
            login_status("Signed in", "green", args.provider, args.name)
            return 0
    except (OSError, ValueError):
        if signing_in:
            login_status("Sign-in failed", "red", args.provider, args.name)
        elif args.action in ("check", "logout"):
            login_status("Sign-in check failed" if args.action == "check" else "Sign-out failed",
                         "red", args.provider, args.name)
        print("Account operation refused. Check the account name and ai-desktop account list.")
        return _widget_result(2) if getattr(args, "from_widget", False) else 2
