"""Offline status and interactive shortcuts. Never opens a client or reads a credential."""
import argparse
import json
import os

from . import agent_profiles, analysis_session, permissions, session_presence
from .analysis_contract import CLIENT_VERSION, terminal_text
from .analysis_sandbox import INSTALL
from .codex_protocol import CLIENT_VERSION as CODEX_VERSION


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
    for name, binary, version in (("claude", "claude-code", CLIENT_VERSION), ("codex", "codex", CODEX_VERSION)):
        path = INSTALL / binary
        providers.append({"id": name, "client_present": path.is_file() and not path.is_symlink(),
                          "pinned_version": version, "allowed": policy.provider_allowed(name) if policy else None})
    providers.append({"id": "local", "endpoint": endpoint(policy.local_port if policy else 8080),
                      "allowed": policy.provider_allowed("local") if policy else None})
    value["providers"] = providers
    try:
        value["accounts"] = panel_rows()
    except (OSError, ValueError):
        value["errors"].append("account metadata unavailable")
    return value


def _summary(data):
    selected = data["selected"] or "unknown"
    print("AI Desktop")
    print("Service: %s    Role: %s    Model: %s" % (selected, data["role"] or "unknown", data.get("model", "unknown")))
    for item in data["providers"]:
        mark = "*" if selected == item["id"] else " "
        access = {True: "allowed", False: "blocked", None: "no explicit policy"}[item["allowed"]]
        details = item.get("endpoint") or ("installed" if item["client_present"] else "missing") + ", pinned " + item["pinned_version"]
        print("%s %-7s %s · %s" % (mark, item["id"], details, access))
    if data["policy"]:
        rules = data["policy"]
        print("Policy: %d read / %d write scopes, %d read / %d change commands" %
              (rules["read_scopes"], rules["write_scopes"], rules["read_commands"], rules["change_commands"]))
    for row in data["sessions"]:
        print("Conversation %s · %s · %s exchanges · %s" %
              (terminal_text(row["id"]), terminal_text(row["provider"]), row["exchanges"], terminal_text(row["directory"])))
    for row in data["accounts"]:
        print("Account: %s / %s%s" % (terminal_text(row["provider"]), terminal_text(row["account"]),
                                      " (selected)" if row["selected"] else ""))
    for error in data["errors"]:
        print(error)


def status(as_json=False):
    data = snapshot()
    if as_json:
        print(json.dumps(data, ensure_ascii=True, sort_keys=True, allow_nan=False))
        return 2 if data["errors"] else 0
    _summary(data)
    if data["selected"] == "local":
        print("Local endpoint health: unchecked offline. No subscription login is required.")
        return 2 if data["errors"] else 0
    return analysis_session.status()


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


def panel(dispatch):
    from .terminal_menu import MenuInputError, choose
    actions = (("Start a conversation", ["ask"]), ("Service and role", ["settings"]),
               ("Accounts", ["account", "list"]), ("Subscription limits", ["usage"]),
               ("Saved conversations", ["sessions"]), ("Close", None))
    try:
        while True:
            _summary(snapshot())
            if not os.isatty(0) or not os.isatty(1):
                return 0
            choice = choose("AI Desktop", tuple(title for title, _ in actions))
            command = None if choice is None else actions[choice][1]
            if command is None:
                return 0
            if command == ["ask"]:
                return dispatch(command)
            dispatch(command)
    except (KeyboardInterrupt, MenuInputError):
        return 0
