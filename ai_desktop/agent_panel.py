"""Offline status and interactive shortcuts. Never opens a client or reads a credential."""
import argparse
from contextlib import contextmanager
import json
import os
import shutil
import sys

from . import agent_profiles, analysis_session, permissions, session_presence, system_clients
from .account_cli import print_usage
from .terminal_style import paint
from .analysis_contract import terminal_text


def snapshot():
    from .account_cli import panel_rows
    from .local_provider import endpoint
    value = {"schema": 1, "selected": None, "role": None, "policy": None,
             "sessions": session_presence.live(), "accounts": [], "errors": []}
    try:
        config = agent_profiles.Store().load().config
        provider = config.selected_provider
        profile = config.with_provider(provider).profiles[config.active]
        value.update(selected=provider, role=config.active, model=profile.model, effort=profile.effort,
                     access=profile.access, skills=list(profile.skills))
    except (ValueError, OSError, KeyError):
        value["errors"].append("agent settings unavailable")
    policy = None
    try:
        policy = permissions.load_policy(optional=True)
        if policy is not None:
            access = policy.access()
            value["policy"] = {"read_scopes": len(access["files"]), "write_scopes": len(access.get("write", [])),
                               "read_commands": len(policy.commands()), "change_commands": len(policy.changes()),
                               "systemd": access["systemd"]}
    except (ValueError, OSError):
        value["errors"].append("policy unavailable")
    providers = []
    for name in ("claude", "codex"):
        client = system_clients.inspect(name)
        providers.append({"id": name, "client_present": client.status == "present",
                          "client_source": client.source, "client_status": client.status,
                          "version_checked": False, "protocol_checked": False,
                          "allowed": policy.provider_allowed(name) if policy else None})
    providers.append({"id": "local", "endpoint": endpoint(policy.local_port if policy else 8080),
                      "allowed": policy.provider_allowed("local") if policy else None})
    value["providers"] = providers
    try:
        value["accounts"] = panel_rows()
    except (OSError, ValueError):
        value["errors"].append("account metadata unavailable")
    return value


def _summary(data, *, interactive=False):
    selected = data["selected"] or "unknown"
    print(paint("AI Desktop", "accent", "bold") + "  " +
          " · ".join((paint(selected, "bold"), data["role"] or "unknown", data.get("model", "unknown"))))
    if not interactive:
        for item in data["providers"]:
            mark = "*" if selected == item["id"] else " "
            access = {True: "allowed", False: "blocked", None: "no explicit policy"}[item["allowed"]]
            details = item.get("endpoint") or (item["client_source"] + " client: " + item["client_status"]
                                              + ", compatibility unchecked")
            tone = {True: "green", False: "yellow", None: "muted"}[item["allowed"]]
            print("%s %-7s %s · %s" % (mark, item["id"], details, paint(access, tone)))
    if data["policy"]:
        rules = data["policy"]
        print(paint("Policy", "muted") + "  %d read / %d write scopes · %d read / %d change commands" %
              (rules["read_scopes"], rules["write_scopes"], rules["read_commands"], rules["change_commands"]))
    for row in data["sessions"]:
        print("Conversation %s · %s · %s exchanges · %s" %
              (terminal_text(row["id"]), terminal_text(row["provider"]), row["exchanges"],
               json.dumps(row["directory"], ensure_ascii=True)))
    if data["accounts"]:
        print()
        print(paint("Subscription limits", "violet", "bold"))
        accounts = data["accounts"]
        if interactive:
            # Show the account used for the next task. The limits page includes all accounts.
            accounts = [r for r in accounts if r["provider"] == selected and r["selected"]]
        if accounts:
            print_usage(accounts)
        else:
            print("  Local inference has no subscription limit." if selected == "local" else "  No selected account.")
    for error in data["errors"]:
        print(paint(error, "yellow"))


def status(as_json=False):
    data = snapshot()
    if as_json:
        print(json.dumps(data, ensure_ascii=True, sort_keys=True, allow_nan=False))
        return 2 if data["errors"] else 0
    _summary(data)
    if data["selected"] == "local":
        print("Local endpoint health: unchecked offline. No subscription login is required.")
        return 2 if data["errors"] else 0
    result = analysis_session.status(data["selected"])
    return result or (2 if data["errors"] else 0)


def services(argv):
    from . import management_cli
    parser = argparse.ArgumentParser(prog="ai-desktop agent")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="List available services without contacting them")
    use = actions.add_parser("use", help="Choose the default service for future conversations")
    use.add_argument("provider", choices=("claude", "codex", "local"))
    args = parser.parse_args(argv)
    if args.action == "list":
        data = snapshot()
        _summary(data)
        return 2 if data["errors"] else 0
    return management_cli.main(["set", "--provider", args.provider], prog="ai-desktop settings", provider_neutral=True)


@contextmanager
def _panel_screen():
    # Use the terminal's separate screen so inspecting the panel never erases shell scrollback.
    sys.stdout.write("\x1b[?1049h")
    try:
        sys.stdout.flush()
        yield
    finally:
        sys.stdout.write("\x1b[?1049l")
        sys.stdout.flush()


def _limits_page(rows, choose):
    page = 0
    while True:
        cols, lines = shutil.get_terminal_size()
        page_size = max(1, (lines - 7) // (10 if cols < 72 else 7))
        last = max(0, (len(rows) - 1) // page_size)
        page = min(page, last)
        sys.stdout.write("\x1b[2J\x1b[H")
        print(paint("Subscription limits", "violet", "bold"))
        print_usage(rows[page * page_size:(page + 1) * page_size])
        options = ["Back"]
        if page < last:
            options.append("Next page")
        if page:
            options.append("Previous page")
        title = "Actions" if not last else "Page %d of %d" % (page + 1, last + 1)
        choice = choose(title, options)
        if choice in (None, 0):
            return
        page += 1 if options[choice] == "Next page" else -1


def _panel_command(dispatch, choose):
    actions = (("Start a conversation", ["ask"]), ("Service and role", ["settings"]),
               ("Choose an account", ["account", "use"]), ("Subscription limits", ["usage"]),
               ("Saved conversations", ["sessions"]), ("Close", None))
    while True:
        sys.stdout.write("\x1b[2J\x1b[H")
        data = snapshot()
        _summary(data, interactive=True)
        print()
        choice = choose("Actions", tuple(title for title, _ in actions))
        command = None if choice is None else actions[choice][1]
        if command is None:
            return None
        if command == ["usage"]:
            _limits_page(data["accounts"], choose)
            continue
        if command == ["account", "use"]:
            from .ask_commands import _pick
            accounts = [r for r in data["accounts"] if r.get("status") != "retired"]
            if accounts:
                labels = [r["provider"] + " / " + r["account"] + (" (selected)" if r["selected"] else "")
                          for r in accounts]
                index = _pick("Account for new requests", labels)
                if index is not None and not accounts[index]["selected"]:
                    row = accounts[index]
                    dispatch([*command, row["provider"], row["account"]])
            continue
        return command


def panel(dispatch):
    from .terminal_menu import MenuInputError, choose
    if not (os.isatty(0) and os.isatty(1)) or os.environ.get("TERM", "") in ("", "dumb", "unknown"):
        _summary(snapshot())
        return 0
    try:
        while True:
            with _panel_screen():
                command = _panel_command(dispatch, choose)
            if command is None:
                return 0
            # A conversation or settings command owns its normal terminal and scrollback.
            if command == ["ask"]:
                return dispatch(command)
            dispatch(command)
    except (KeyboardInterrupt, MenuInputError):
        return 0
