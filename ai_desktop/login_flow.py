"""Publish non-secret default login receipts and select the authenticated recipient.

The privileged account helper never writes desktop configuration. A separate
per-slot desktop lock orders desktop login, status checks and logout. The role
and account settings still allow concurrent edits while a browser is open.
"""
import signal
import subprocess
import time

from . import agent_profiles as profiles
from .account_store import AccountStore, named_account
from .terminal_style import paint


def login_status(message, tone, provider=None, account=None):
    """Only the controller's own labels and validated local aliases go into a status line."""
    line = paint(message, tone, "bold")
    if provider is not None:
        target = {"claude": "Claude", "codex": "Codex"}[provider]
        if account == "default" or named_account(account):
            target += " / " + account
        line += "  " + target
    print(line, flush=True)


def authenticate_account(provider, account, authenticate):
    """Keep the provider's live TTY; never capture, parse or repeat its sign-in URL."""
    login_status("Signing in", "accent", provider, account)
    try:
        result = authenticate()
    except KeyboardInterrupt:
        login_status("Sign-in cancelled", "yellow", provider, account)
        return 2
    except (OSError, ValueError, subprocess.SubprocessError):
        result = 2
    if type(result) is not int:
        result = 2
    if result in (130, -signal.SIGINT):
        login_status("Sign-in cancelled", "yellow", provider, account)
    elif result != 0:
        login_status("Sign-in failed", "red", provider, account)
    return result


def login(provider, authenticate, *, store=None):
    if provider not in ("claude", "codex"):
        raise ValueError("provider")
    authenticated = False
    try:
        store = store or profiles.Store()
        before = store.load()
        accounts = AccountStore(store.home)
        with accounts.lifecycle(provider, "default"):
            previous_name = accounts.load().config.active(provider)
            result = authenticate_account(provider, "default", authenticate)
            if result != 0:
                return result
            authenticated = True
            settings_unchanged = store.load() == before
            def record(current):
                updated = current.logged_in(provider, "default", int(time.time()))
                if settings_unchanged and current.active(provider) == previous_name:
                    return updated.select(provider, "default")
                return updated
            account = accounts.mutate(record)
            if not settings_unchanged or account.config.active(provider) != "default":
                raise profiles.ProfileError("changed")
            try:
                store.mutate(lambda config: config.with_provider(provider), expected=before)
            except (profiles.ProfileError, OSError, ValueError):
                if previous_name != "default":
                    # Undo only our slot selection, retaining the receipt. A later desktop edit wins.
                    accounts.mutate(lambda current: current.select(provider, previous_name), expected=account)
                raise
    except (profiles.ProfileError, OSError, ValueError):
        if authenticated:
            login_status("Signed in", "yellow", provider, "default")
            print("  Desktop settings changed or could not be saved. Check the sign-in and selection in the account picker.")
            return 3
        login_status("Sign-in failed", "red", provider, "default")
        print("  Could not open the settings or the account is busy.")
        return 2
    login_status("Signed in", "green", provider, "default")
    print("  Selected for new tasks.")
    return 0
