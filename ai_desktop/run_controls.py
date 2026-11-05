"""Resolve local agent settings once; never grant a model host authority."""
import argparse
from functools import wraps
import sys

from . import execution


def recipient_config(config=execution.UNSET):
    """Copy an explicit task snapshot, or capture the current controller scope.

    ``None`` is the accepted legacy Claude envelope, not a request to consult
    mutable settings or account state. This helper performs no file/auth IO.
    """
    return execution.current() if config is execution.UNSET else execution.validate(config)


def recipient(text=None, *, config=execution.UNSET):
    """Name the selected recipient in fixed application-owned text only.

    Callers interpolate filenames, questions and model text AFTER this helper;
    those values must never be passed in for provider-name substitution.
    """
    selected = recipient_config(config)
    name = execution.provider_name(selected["provider"] if selected is not None else "claude")
    if selected is not None and selected["provider"] == "local":
        name += " (127.0.0.1:%d)" % selected.get("local_port", execution.DEFAULT_LOCAL_PORT)
    if selected is not None and "account" in selected:
        name += " (account: " + selected["account"] + ")"
    if text is not None and type(text) is not str:
        raise ValueError("recipient text")
    return name if text is None else text.replace("Claude", name)


def wait_message(*, config=execution.UNSET):
    return "Waiting for %s… Ctrl+C stops waiting." % recipient(config=config)


def network_hint(*, short=False):
    selected = execution.current()
    provider = selected["provider"] if selected is not None else "claude"
    key = "network." + provider + "=false"
    return ("Note: " + key + " — local commands only." if short else
            key + " in permissions.toml: sending to " + recipient() + " is blocked. Local commands still work.")


def provider_allowed(policy):
    config = execution.current()
    provider = "claude" if config is None else config["provider"]
    if provider == "claude":
        return policy.sources().get("claude") is True
    return policy.provider_allowed(provider)


def failure_hint(reason, fallback, *, config=execution.UNSET, diagnostic=None):
    selected = recipient_config(config)
    if selected is not None and selected.get("account") and reason in ("auth-required", "auth-method"):
        provider, name = selected["provider"], selected["account"]
        return ("Check the account with ai-desktop account list. For its first login, run "
                "ai-desktop account login %s %s. If its connected login has expired, use a new name with "
                "ai-desktop account add %s NEW-NAME --login, then ai-desktop account use %s NEW-NAME."
                % (provider, name, provider, provider))
    if selected is not None and selected["provider"] == "local":
        if reason == "authorization":
            return "Local inference needs network.local=true and the approved port in permissions.toml."
        if reason in {"network", "provider-unavailable", "endpoint"}:
            return "The approved local llama.cpp server is unavailable. Check its address and model alias."
    if selected is not None and selected["provider"] == "codex":
        from .analysis_diagnostics import codex_diagnostic
        code = codex_diagnostic(diagnostic, reason)
        specific = {
            "codex-launch": "Could not start the Codex client.",
            "codex-native-exit": "The Codex client ended the session with an error.",
            "codex-native-exit-2": "The Codex client ended the session with an error (code 2).",
            "codex-native-io": "Communication with the Codex client was interrupted.",
            "codex-input-write": "Could not pass the request to the Codex client.",
            "codex-rpc-initialize": "Could not start a Codex session.",
            "codex-rpc-thread": "Codex did not start the conversation.",
            "codex-rpc-turn": "Codex did not accept the question.",
            "codex-stream": "The Codex answer failed the protocol check.",
            "codex-turn-failed": "Codex stopped working on the question.",
            "codex-server-error": "Codex reported an error while working on the question.",
            "codex-model-unavailable": "The selected Codex model is not available.",
            "codex-http-forbidden": "The service refused access to the Codex model.",
        }
        if code in specific:
            return specific[code]
        codex = {
            "runtime": "The trusted Codex 0.166.0 client or its dependencies are missing from the ai-desktop package.",
            "version": "The Codex client failed the version 0.166.0 check.",
            "auth-required": "Connect your account: ai-desktop login (choose ChatGPT / Codex). Then approve a new task.",
            "auth-method": "AI Desktop needs its own Sign in with ChatGPT login.",
        }
        if reason in codex:
            return codex[reason]
    return recipient(fallback, config=selected)


def add_arguments(parser, *, session=False):
    parser.add_argument("--provider", choices=execution.PROVIDERS, help="Use this service for the current run")
    parser.add_argument("--agent", metavar="NAME", help="Agent profile saved in ai-desktop agents")
    parser.add_argument("--model", type=execution.service_model_argument, metavar="MODEL",
                        help="Model for this run: an alias (ai-desktop settings models) or a model ID; default = the client's choice")
    parser.add_argument("--effort", choices=execution.EFFORTS,
                        help="Reasoning level for this run")
    if session:
        parser.add_argument("--session", metavar="new|ID", help="Explicitly save the conversation or resume a local session")


def label(config=execution.UNSET):
    config = recipient_config(config)
    if config is None:
        return "Claude · profile: default · model and reasoning: default"
    return "%s · profile: %s · model: %s · reasoning: %s%s" % (
        recipient(config=config),
        config["profile"], config["model"], config["effort"],
        " · read-only" if config["access"] == "read-only" else "")


def require_changes():
    config = execution.current()
    if config is not None and config["access"] == "read-only":
        from .analysis_contract import AnalysisDenied
        raise AnalysisDenied("consent")


def configured(function):
    """A stable ContextVar snapshot covers the complete foreground invocation.

    Help stays completely offline. Shared flags are parsed separately so direct
    calls to each public CLI receive the same configuration as the dispatcher.
    """
    @wraps(function)
    def wrapped(argv=None, **kwargs):
        arguments = list(sys.argv[1:] if argv is None else argv)
        # Local recovery remains available even when an AI profile is broken.
        if function.__module__.endswith("task_cli") and arguments[:1] != ["run"]:
            return function(arguments, **kwargs)
        if any(flag in arguments for flag in ("--help", "-h")):
            return function(arguments, **kwargs)
        common = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        add_arguments(common)
        selected, remaining = common.parse_known_args(arguments)
        try:
            from .agent_profiles import resolve
            overrides = {"provider": selected.provider} if selected.provider is not None else {}
            if function.__module__.endswith(".ask_cli"):
                overrides["catalog"] = True
            config = resolve(selected.agent, selected.model, selected.effort, **overrides)
            config = execution.validate(config)
            # Preserve the accepted wire for an unchanged default installation.
            vanilla = (config == {"provider": "claude", "model": "default", "effort": "default",
                                  "instructions": "", "profile": "default", "access": "changes"})
            with execution.using(None if vanilla else config):
                if not vanilla:
                    print(label())
                return function(remaining, **kwargs)
        except (OSError, ValueError, TypeError, KeyError):
            print("Invalid agent configuration. Check ai-desktop settings show or ai-desktop agents list.")
            return 2
    return wrapped


def conversation_command(line, *, saved=False):
    """Idle-only conversation controls; never called from a model response."""
    command, _, value = line.partition(" ")
    if command not in ("/settings", "/ustawienia", "/model", "/sila", "/effort", "/agent"):
        return False
    value = value.strip()
    if command in ("/settings", "/ustawienia") or not value:
        print(label())
        selected = execution.current()
        provider = selected["provider"] if selected is not None else "claude"
        models = execution.MODELS if provider == "claude" else ("default", "ID")
        print("/model %s · /effort %s · /agent NAME" % (
            "|".join(models), "|".join(execution.efforts_for(provider))))
        return True
    if saved:
        print("A saved conversation is tied to its agent configuration. End it and open --session new with the new settings.")
        return True
    try:
        from .agent_profiles import resolve
        before = execution.current()
        if command == "/agent":
            after = resolve(profile=value, provider=before["provider"] if before else "claude")
        else:
            after = dict(before or resolve())
            after["model" if command == "/model" else "effort"] = value
        after = execution.validate(after)
        if (after["provider"] != (before["provider"] if before is not None else "claude")
                or after.get("account") != (before or {}).get("account")
                or after.get("local_port") != (before or {}).get("local_port")):
            print("Changing the provider needs a new conversation with separate consent for the data.")
            return True
        execution.select(after)
        print(label())
    except (ValueError, TypeError, KeyError, OSError):
        print("The setting was not changed. Check the agent name, model or reasoning level.")
    return True
