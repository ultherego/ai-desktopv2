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
    name = "Codex" if selected is not None and selected["provider"] == "codex" else "Claude"
    if text is not None and type(text) is not str:
        raise ValueError("recipient text")
    return name if text is None else text.replace("Claude", name)


def wait_message(*, config=execution.UNSET):
    return "Czekam na odpowiedź od %s… Ctrl+C przerywa oczekiwanie." % recipient(config=config)


def network_hint(*, short=False):
    selected = execution.current()
    provider = selected["provider"] if selected is not None else "claude"
    key = "network." + provider + "=false"
    return ("Uwaga: " + key + " — tylko polecenia lokalne." if short else
            key + " w permissions.toml: wysyłka do " + recipient() + " jest zablokowana. Polecenia lokalne działają.")


def provider_allowed(policy):
    config = execution.current()
    provider = "claude" if config is None else config["provider"]
    if provider == "claude":
        return policy.sources().get("claude") is True
    return policy.provider_allowed(provider)


def failure_hint(reason, fallback, *, config=execution.UNSET, diagnostic=None):
    selected = recipient_config(config)
    if selected is not None and selected["provider"] == "codex":
        from .analysis_diagnostics import codex_diagnostic
        code = codex_diagnostic(diagnostic, reason)
        specific = {
            "codex-launch": "Nie udało się uruchomić klienta Codex.",
            "codex-native-exit": "Klient Codex zakończył sesję z błędem.",
            "codex-native-exit-2": "Klient Codex zakończył sesję z błędem (kod 2).",
            "codex-native-io": "Przerwano komunikację z klientem Codex.",
            "codex-input-write": "Nie udało się przekazać żądania do klienta Codex.",
            "codex-rpc-initialize": "Nie udało się zainicjować sesji Codex.",
            "codex-rpc-thread": "Codex nie rozpoczął rozmowy.",
            "codex-rpc-turn": "Codex nie przyjął pytania do analizy.",
            "codex-stream": "Odpowiedź Codex nie przeszła kontroli protokołu.",
            "codex-turn-failed": "Codex przerwał analizę pytania.",
            "codex-server-error": "Codex zgłosił błąd podczas analizy.",
            "codex-model-unavailable": "Wybrany model Codex jest niedostępny.",
            "codex-http-forbidden": "Usługa odmówiła dostępu do modelu Codex.",
        }
        if code in specific:
            return specific[code]
        codex = {
            "runtime": "Brak zaufanego klienta Codex 0.166.0 lub zależności w pakiecie ai-desktop.",
            "version": "Klient Codex nie przeszedł kontroli wersji 0.166.0.",
            "auth-required": "Połącz konto: ai-desktop login (wybierz ChatGPT / Codex). Potem zatwierdź nowe zadanie.",
            "auth-method": "Wymagane osobne logowanie Sign in with ChatGPT dla AI Desktop.",
        }
        if reason in codex:
            return codex[reason]
    return recipient(fallback, config=selected)


def add_arguments(parser, *, session=False):
    parser.add_argument("--agent", metavar="NAZWA", help="Profil agenta zapisany w ai-desktop agents")
    parser.add_argument("--model", type=execution.model_argument, metavar="MODEL",
                        help="Model dla tego uruchomienia: alias (ai-desktop settings models) albo identyfikator modelu; default = wybór klienta")
    parser.add_argument("--effort", choices=execution.EFFORTS,
                        help="Poziom rozumowania dla tego uruchomienia")
    if session:
        parser.add_argument("--session", metavar="new|ID", help="Jawnie zapisz rozmowę lub wznów lokalną sesję")


def label(config=execution.UNSET):
    config = recipient_config(config)
    if config is None:
        return "Claude · profil: default · model i rozumowanie: domyślne"
    return "%s · profil: %s · model: %s · rozumowanie: %s%s" % (
        "Claude" if config["provider"] == "claude" else "Codex",
        config["profile"], config["model"], config["effort"],
        " · tylko odczyt" if config["access"] == "read-only" else "")


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
            config = resolve(selected.agent, selected.model, selected.effort)
            config = execution.validate(config)
            # Preserve the accepted wire for an unchanged default installation.
            vanilla = (config == {"provider": "claude", "model": "default", "effort": "default",
                                  "instructions": "", "profile": "default", "access": "changes"})
            with execution.using(None if vanilla else config):
                if not vanilla:
                    print(label())
                return function(remaining, **kwargs)
        except (OSError, ValueError, TypeError, KeyError):
            print("Niepoprawna konfiguracja agenta. Sprawdź ai-desktop settings show lub ai-desktop agents list.")
            return 2
    return wrapped


def conversation_command(line, *, saved=False):
    """Idle-only conversation controls; never called from a model response."""
    command, _, value = line.partition(" ")
    if command not in ("/ustawienia", "/model", "/sila", "/effort", "/agent"):
        return False
    value = value.strip()
    if command == "/ustawienia" or not value:
        print(label())
        selected = execution.current()
        provider = selected["provider"] if selected is not None else "claude"
        models = execution.MODELS if provider == "claude" else ("default", "ID")
        print("/model %s · /sila %s · /agent NAZWA" % (
            "|".join(models), "|".join(execution.efforts_for(provider))))
        return True
    if saved:
        print("Zapisana rozmowa jest związana z konfiguracją agenta. Zakończ ją i otwórz --session new z nowymi ustawieniami.")
        return True
    try:
        from .agent_profiles import resolve
        before = execution.current()
        if command == "/agent":
            after = resolve(profile=value)
        else:
            after = dict(before or resolve())
            after["model" if command == "/model" else "effort"] = value
        after = execution.validate(after)
        if after["provider"] != (before["provider"] if before is not None else "claude"):
            print("Zmiana dostawcy wymaga nowej rozmowy z osobną zgodą na dane.")
            return True
        execution.select(after)
        print(label())
    except (ValueError, TypeError, KeyError, OSError):
        print("Nie wybrano ustawienia. Sprawdź nazwę agenta, model lub poziom rozumowania.")
    return True
