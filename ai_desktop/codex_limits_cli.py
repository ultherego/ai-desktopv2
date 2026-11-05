"""Explicit desktop controls for a separately authorized native Codex quota session."""
import os
import signal
import subprocess
import time

from .account_store import named_account
from .agent_profiles import ProfileError
from .login_flow import login_status
from .permissions import load_policy
from .terminal_consent import fresh_input


ERRORS = {
    "account": "Choose an existing Codex account.",
    "selection-changed": "The selected Codex account changed. Refresh the widget and try again.",
    "check-sign-in": "Check the local Codex sign-in for this account before connecting live limits.",
    "not-connected": "Connect live limits for this Codex account first.",
    "auth-required": "The native limits session needs to be reconnected.",
    "busy": "An account operation is in progress. Try again when it finishes.",
    "policy": "The current permissions policy must allow the Codex network before reading live limits.",
    "network": "Codex could not be reached. The previous reading is still available.",
    "timeout": "Codex did not return a reading in time. The previous reading is still available.",
    "unavailable": "Could not read live limits. Check the quota connection and try again.",
}


def error_reason(error):
    if isinstance(error, ProfileError) and error.args and type(error.args[0]) is str and error.args[0] in ERRORS:
        return error.args[0]
    return "unavailable"


def _allowed():
    try:
        policy = load_policy()
        if policy.provider_allowed("codex"):
            return policy
    except (ValueError, OSError):
        pass
    raise ProfileError("policy")


def _target(state, alias, *, selected=True):
    if (alias != "default" and not named_account(alias)) or alias not in state.items("codex"):
        raise ProfileError("account")
    if selected:
        if alias != state.active("codex"):
            raise ProfileError("selection-changed")
        if ("codex", alias) in state.retired or state.auth_status("codex", alias) != "signed_in":
            raise ProfileError("check-sign-in")


def refresh_limits(store, alias):
    """Refresh only the named, still-selected local slot; no fallback to a different account."""
    from .codex_quota_ipc import QuotaError, read_limits
    if alias is None:
        alias = store.load().config.active("codex")
    _target(store.load().config, alias)
    with store.lifecycle("codex", alias):
        state = store.load().config
        _target(state, alias)
        if not any(record[0] == alias for record in state.native_limits):
            raise ProfileError("not-connected")
        policy = _allowed()
        try:
            reply = read_limits(alias)
        except QuotaError as error:
            if error.reason == "auth-required":
                store.mutate(lambda current: current.invalidate_native_limits(alias))
            elif error.reason == "not-connected":
                store.mutate(lambda current: current.without_native_limits(alias))
            raise ProfileError(error.reason if error.reason in ERRORS else "unavailable") from None
        try:
            policy.recheck()
        except (OSError, ValueError):
            raise ProfileError("policy") from None

        def save(current):
            _target(current, alias)
            return current.with_native_limits(alias, observed=int(time.time()),
                                              windows=reply["windows"], plan=reply["plan"])
        store.mutate(save)
    return alias


def _admin(action, alias):
    from .analysis_contract import AnalysisDenied
    from .analysis_sandbox import INSTALL, trusted_file
    try:
        entry = trusted_file(INSTALL / "codex-quota.py")
        return subprocess.run(["/usr/bin/sudo", "--", "/usr/bin/python3", "-I", "-B", str(entry), action, alias],
                              close_fds=True, check=False, timeout=650,
                              env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb"}).returncode
    except (AnalysisDenied, OSError, subprocess.SubprocessError):
        return 2


def manage_limits(store, alias, *, connect):
    if os.getuid() < 1000 or os.geteuid() != os.getuid() or not os.isatty(0) or not os.isatty(1):
        print("Open this action in your regular user's terminal.")
        return 2
    _target(store.load().config, alias, selected=connect)
    if connect:
        _allowed()
        print("Sign in to the native Codex account and workspace whose limits you want to display here.\n"
              "This enables a quota-only service for your desktop user, including after reboot.\n"
              "Refresh contacts Codex for usage and reset times without sending a model request.")
    else:
        print("This disconnects the separate limits session for this local slot. Your AI Desktop chat sign-in stays active.")
    action = "connect" if connect else "disconnect"
    try:
        answer = fresh_input(f"{action.capitalize()} live limits for Codex/{alias}? [y/N]: ")
        if answer.strip().casefold() not in ("y", "yes"):
            login_status("Limits action cancelled", "yellow", "codex", alias)
            return 0
        with store.lifecycle("codex", alias):
            _target(store.load().config, alias, selected=connect)
            if connect:
                _allowed()
            login_status("Connecting live limits" if connect else "Disconnecting live limits", "accent", "codex", alias)
            code = _admin(action, alias)
            if code in (130, -signal.SIGINT):
                login_status("Limits action cancelled", "yellow", "codex", alias)
                return 2
            if code:
                login_status("Limits connection failed" if connect else "Limits disconnection failed", "red", "codex", alias)
                return 2
            try:
                store.mutate(lambda current: current.with_native_limits(alias) if connect else current.without_native_limits(alias))
            except (OSError, ValueError):
                login_status("Account action completed; desktop indicator could not be saved", "yellow", "codex", alias)
                print("  To remove any separate limits credentials held for this slot, run:")
                print("  ai-desktop account disconnect-limits codex " + alias)
                return 3
    except (EOFError, KeyboardInterrupt):
        login_status("Account action interrupted", "yellow", "codex", alias)
        return 2
    login_status("Live limits connected" if connect else "Live limits disconnected", "green", "codex", alias)
    if connect:
        try:
            refresh_limits(store, alias)
        except (OSError, ValueError) as error:
            print("  " + ERRORS[error_reason(error)])
            return 3
    return 0
