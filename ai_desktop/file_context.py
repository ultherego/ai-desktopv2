"""Collect configured file scopes under question or standalone read consent.

Scope IDs and relative selectors are untrusted suggestions, never read grants.
The captured policy is the only authority for mapping them to local rules.
"""
import json

from . import conversation_state, permissions
from .analysis_contract import AnalysisDenied, terminal_text
from .assistant_contract import file_selections, selected_file_ids
from .file_context_contract import validate_context
from .terminal_consent import confirm
from .question_consent import QuestionConsent
from .run_controls import recipient, recipient_config


class FileReadDenied(AnalysisDenied):
    """Local read refusal already explained to the user, not a Claude failure.

    `code` is set only on a deferred refusal (collect(defer=True)): nothing was printed and the caller decides whether the
    finite code is a recoverable selection problem or a stop.
    """
    code = None


_READ_HINTS = {
    "files-size": f"Co najmniej jeden plik przekracza limit {permissions.MAX_FILE_BYTES} bajtów na plik.",
    "files-total": f"Łączna treść plików przekracza limit {permissions.MAX_TOTAL_BYTES} bajtów.",
    "files-count": f"Wybrany zakres zawiera więcej niż {permissions.MAX_FILES} plików.",
    "discovery-required": "Wybrane ścieżki nie zostały znalezione ani wskazane przez użytkownika.",
    "files-entries": (f"Przekroczono limit przeglądania katalogów: {permissions.MAX_DIRECTORY_ENTRIES} "
                      f"wpisów na katalog lub {permissions.MAX_TOTAL_ENTRIES} wpisów łącznie."),
    "files-depth": f"Przekroczono limit zagnieżdżenia katalogów: {permissions.MAX_DEPTH} poziomów.",
    "files-empty": "W wybranym zakresie nie ma plików dopuszczonych do odczytu.",
    "files-missing": "Wybrany plik lub katalog nie istnieje.",
    "files-symlink": "Wybrany zakres zawiera dowiązanie symboliczne poza obsługiwanym odczytem aliasu katalogu do dozwolonego sąsiada.",
    "files-type": "Wybrany obiekt nie jest zwykłym plikiem.",
    "files-owner": "Właścicielem wybranego pliku nie jest bieżący użytkownik.",
    "files-links": "Wybrany plik ma niedozwoloną liczbę dowiązań twardych; wymagane jest dokładnie jedno.",
    "files-mode": "Wybrany plik ma niedozwolony bit setuid lub setgid.",
    "files-directory": "Element ścieżki nie jest zwykłym katalogiem.",
    "files-access": "Nie udało się zweryfikować dostępu do pliku lub katalogu.",
    "files-permission": "System odmówił dostępu do wybranego pliku lub katalogu.",
    "files-denied": "Wybrany zakres nie jest dozwolony przez politykę dostępu.",
    "files-text": "Treść pliku nie jest poprawnym UTF-8 lub zawiera niedozwolone znaki sterujące lub formatujące.",
    "files-private-key": "Treść pliku zawiera rozpoznany nagłówek klucza prywatnego.",
    "files-changed": "Plik lub element jego ścieżki zmienił się podczas sprawdzania odczytu.",
    "policy-changed": "Konfiguracja dostępu lub jej stan zmieniły się podczas pytania.",
    "policy-missing": "Plik permissions.toml nie jest już dostępny pod ustaloną ścieżką.",
    "policy-directory": "Katalog konfiguracji dostępu lub jego przodek nie spełnia wymagań ochrony.",
    "policy-file": "Plik permissions.toml nie spełnia wymagań typu, właściciela, dowiązań lub trybu 0600.",
    "policy-size": f"Plik permissions.toml przekracza limit {permissions.MAX_POLICY_BYTES} bajtów.",
    "policy-access": "Nie można otworzyć konfiguracji dostępu zgodnie z regułami odczytu.",
    "policy-identity": "Nie można potwierdzić tożsamości użytkownika dla odczytu.",
}
_SETTING_HINTS = {
    "file_size": "Niepoprawne file_size w [files] permissions.toml: podaj od 4KiB do 16MiB, np. \"5MiB\".",
    "part_size": "Niepoprawne part_size w [files] permissions.toml: podaj od 30KiB do 512KiB, np. \"240KiB\".",
}
for _name, (_kind, _low, _high) in permissions.LIMIT_SPECS.items():
    _unit = " bajtów (lub np. \"24KiB\")" if _kind == "size" else ""
    _SETTING_HINTS[_name] = (f"Niepoprawne {_name} w [files] permissions.toml: podaj od {_low} do {_high}{_unit}"
                             + ("; read_file_size nie może przekraczać read_total_size." if _name == "read_file_size" else "."))
_READ_FALLBACK = "Nie udało się zweryfikować dostępu lub odczytać wybranych plików."


def _limit_hints(policy):
    """Hints naming the owner's own permissions.toml keys and values; defaults when no policy is at hand."""
    if type(policy) is not permissions.Policy:
        return {}
    limits = policy.limits()
    return {
        "files-size": f"Co najmniej jeden plik przekracza read_file_size={limits.read_file_size} bajtów (permissions.toml, [files]).",
        "files-total": f"Łączna treść plików przekracza read_total_size={limits.read_total_size} bajtów (permissions.toml, [files]).",
        "files-count": f"Wybrany zakres zawiera więcej niż read_files={limits.read_files} plików (permissions.toml, [files]).",
    }


def _read_hint(error, policy=None):
    # Only exact, closed codes select text; paths, contents and arbitrary
    # exception messages never become user output, including in --details.
    code = error.args[0] if isinstance(error, ValueError) and len(error.args) == 1 else None
    setting = getattr(error, "setting", None)
    if code == "policy-invalid" and setting in _SETTING_HINTS:
        return _SETTING_HINTS[setting]
    if type(code) is not str:
        return _READ_FALLBACK
    return _limit_hints(policy).get(code) or _READ_HINTS.get(code, _READ_FALLBACK)


def _selected(policy, scope_ids):
    """Resolve IDs against captured metadata without stat, expansion or reads."""
    scope_ids = selected_file_ids(scope_ids)
    if not scope_ids:
        raise ValueError("files-scopes")
    allowed = policy.access()["files"]
    if type(allowed) is not list or len(allowed) > permissions.MAX_RULES:
        raise ValueError("files-access")
    scopes = policy.scopes()
    mapped = {}
    for scope in scopes:
        if (type(scope.index) is not int or not 0 <= scope.index < permissions.MAX_RULES
                or type(scope.pattern) is not str or "f" + str(scope.index) in mapped):
            raise ValueError("files-scope")
        mapped["f" + str(scope.index)] = scope
    seen = set()
    for item in allowed:
        if (type(item) is not dict or set(item) != {"id", "pattern"}
                or type(item["id"]) is not str or item["id"] not in mapped
                or item["id"] in seen or item["pattern"] != mapped[item["id"]].pattern):
            raise ValueError("files-access")
        seen.add(item["id"])
    if any(value not in seen for value in scope_ids):
        raise ValueError("files-denied")
    return tuple(mapped[value] for value in scope_ids)


def resolve_paths(policy, selections):
    """Validate captured scope identities, then expand only proposed subsets.

    Callers must hold active read consent before entering this I/O boundary.
    Whole-scope legacy selectors preserve their previous expansion path.
    """
    selections = file_selections(selections)
    selected = _selected(policy, selections)
    if all(type(item) is str for item in selections):
        return policy.expand(tuple(scope.index for scope in selected))
    mapped = {"f" + str(scope.index): scope.index for scope in selected}
    narrowed = tuple((mapped[item], None) if type(item) is str else
                     (mapped[item["id"]], item["path"]) for item in selections)
    return policy.expand_selected(narrowed)


def _label(value):
    return terminal_text(json.dumps(value, ensure_ascii=False))


def collect(policy, scope_ids, args, reader=input, *, question_consent=None, defer=()):
    """Read under the active question consent, or a fresh standalone read yes."""
    selected_execution = recipient_config()
    selected_labels = ()
    quiet = False
    def check_question():
        if question_consent is not None:
            if type(question_consent) is not QuestionConsent:
                raise AnalysisDenied("consent")
            question_consent.check()
    try:
        check_question()
        if getattr(args, "preview", False) or getattr(args, "no_context", False):
            raise ValueError("files-disabled")
        if question_consent is not None:
            question_consent.require_files(policy, scope_ids)
        scope_ids = file_selections(scope_ids)
        selected = _selected(policy, scope_ids)
        quiet = question_consent is not None and not getattr(args, "details", False)
        patterns = {"f" + str(scope.index): scope.pattern for scope in selected}
        selected_labels = tuple(_label(patterns[value] if type(value) is str else
                                       patterns[value["id"]] + " → " + value["path"])
                                for value in scope_ids)
        if not quiet:
            print("Odczyt lokalny — proponowane reguły dostępu, bez zmian w plikach:")
            for scope in selected:
                print("  " + _label(scope.pattern))
            for selection in scope_ids:
                if type(selection) is dict:
                    print("  Zawężenie " + _label(selection["id"]) + ": " + _label(selection["path"]))
        if question_consent is None and not confirm("files-read", reader=reader):
            raise AnalysisDenied("consent")
        policy.recheck()
        check_question()
        paths = resolve_paths(policy, scope_ids)
        if question_consent is not None:
            question_consent.bind_file_selection(policy, paths)
        check_question()
        item = validate_context(permissions.read_selected(policy, paths))
        check_question()
        policy.recheck()
        check_question()
        if not quiet:
            print("Do pytania: " + str(len(item["files"])) + " plików. Pełna treść w --details.")
            for file in item["files"]:
                print("  " + _label(file["path"]))
        permissions.recheck_selection(policy, paths)
        conversation_state.note_sources([file["path"] for file in item["files"]])
        return item
    except AnalysisDenied:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
        code = exc.args[0] if isinstance(exc, ValueError) and len(exc.args) == 1 else None
        if type(code) is str and code in defer:
            denied = FileReadDenied("input")
            denied.code = code
            raise denied from None
        print("Odmowa odczytu plików: " + _read_hint(exc, policy))
        if quiet and selected_labels:
            print("Wybrano: " + "; ".join(selected_labels[:3])
                  + ("; pozostałe zakresy: " + str(len(selected_labels) - 3) if len(selected_labels) > 3 else ""))
        print(recipient("Treść plików nie została wysłana do Claude.", config=selected_execution))
        raise FileReadDenied("input") from None
