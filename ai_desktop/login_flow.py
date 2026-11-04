"""Select the desktop service only after a completed account login.

The privileged account helper never writes desktop configuration. The desktop
captures its settings before authentication and publishes with the same CAS as
the settings menu; a concurrent edit must survive a long browser login.
"""
from . import agent_profiles as profiles


def login(provider, authenticate, *, store=None):
    if provider not in ("claude", "codex"):
        raise ValueError("provider")
    name = "Claude" if provider == "claude" else "Codex"
    try:
        store = store or profiles.Store()
        before = store.load()
    except (profiles.ProfileError, OSError, ValueError):
        print("Nie można wczytać ustawień. Sprawdź ai-desktop settings show; logowanie nie zostało rozpoczęte.")
        return 2

    result = authenticate()
    if type(result) is not int:
        print("Nie potwierdzono zakończenia logowania.")
        return 2
    if result != 0:
        return result
    try:
        store.mutate(lambda config: config.with_provider(provider), expected=before)
    except (profiles.ProfileError, OSError, ValueError):
        print("Zalogowano do %s. Nie przełączono usługi, ponieważ ustawienia zmieniły się lub nie można ich zapisać. "
              "Wybierz usługę w ai-desktop settings." % name)
        return 3
    print("Zalogowano. Aktywna usługa: %s." % name)
    return 0
