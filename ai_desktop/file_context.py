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
    "files-size": f"At least one file is over the limit of {permissions.MAX_FILE_BYTES} bytes per file.",
    "files-total": f"The total file contents are over the limit of {permissions.MAX_TOTAL_BYTES} bytes.",
    "files-count": f"The selected scope has more than {permissions.MAX_FILES} files.",
    "discovery-required": "The selected paths were neither found nor named by the user.",
    "files-entries": (f"Over the directory listing limit: {permissions.MAX_DIRECTORY_ENTRIES} "
                      f"entries per directory or {permissions.MAX_TOTAL_ENTRIES} entries in total."),
    "files-depth": f"Over the directory nesting limit: {permissions.MAX_DEPTH} levels.",
    "files-empty": "The selected scope has no files that may be read.",
    "files-missing": "The selected file or directory does not exist.",
    "files-symlink": "The selected scope has a symbolic link other than the supported directory alias to an allowed sibling.",
    "files-type": "The selected item is not a regular file.",
    "files-owner": "The selected file is not owned by the current user.",
    "files-links": "The selected file has a disallowed number of hard links; exactly one is required.",
    "files-mode": "The selected file has a disallowed setuid or setgid bit.",
    "files-directory": "A part of the path is not a regular directory.",
    "files-access": "Could not verify access to the file or directory.",
    "files-permission": "The system refused access to the selected file or directory.",
    "files-denied": "The selected scope is not allowed by the access policy.",
    "files-text": "The file contents are not valid UTF-8 or contain disallowed control or formatting characters.",
    "files-private-key": "The file contents include a recognized private key header.",
    "files-changed": "The file or a part of its path changed while the read was being checked.",
    "policy-changed": "The access settings or their state changed during the question.",
    "policy-missing": "The permissions.toml file is no longer available at its expected path.",
    "policy-directory": "The access settings directory or a parent directory does not meet the protection requirements.",
    "policy-file": "The permissions.toml file does not meet the requirements for type, owner, links or mode 0600.",
    "policy-size": f"The permissions.toml file is over the limit of {permissions.MAX_POLICY_BYTES} bytes.",
    "policy-access": "Could not open the access settings under the read rules.",
    "policy-identity": "Could not confirm the user's identity for the read.",
}
_SETTING_HINTS = {
    "file_size": "Invalid file_size in [files] of permissions.toml: use 4KiB to 64MiB, for example \"32MiB\".",
    "part_size": "Invalid part_size in [files] of permissions.toml: use 30KiB to 512KiB, for example \"240KiB\".",
}
for _name, (_kind, _low, _high) in permissions.LIMIT_SPECS.items():
    _unit = " bytes (or for example \"24KiB\")" if _kind == "size" else ""
    _SETTING_HINTS[_name] = (f"Invalid {_name} in [files] of permissions.toml: use {_low} to {_high}{_unit}"
                             + ("; read_file_size cannot be larger than read_total_size." if _name == "read_file_size" else
                                "; test_copy_file_size cannot be larger than test_copy_total_size."
                                if _name == "test_copy_file_size" else "."))
_READ_FALLBACK = "Could not verify access to, or read, the selected files."


def _limit_hints(policy):
    """Hints naming the owner's own permissions.toml keys and values; defaults when no policy is at hand."""
    if type(policy) is not permissions.Policy:
        return {}
    limits = policy.limits()
    return {
        "files-size": f"At least one file is over read_file_size={limits.read_file_size} bytes (permissions.toml, [files]).",
        "files-total": f"The total file contents are over read_total_size={limits.read_total_size} bytes (permissions.toml, [files]).",
        "files-count": f"The selected scope has more than read_files={limits.read_files} files (permissions.toml, [files]).",
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
            print("Local read: proposed access rules, files are not changed:")
            for scope in selected:
                print("  " + _label(scope.pattern))
            for selection in scope_ids:
                if type(selection) is dict:
                    print("  Narrowed " + _label(selection["id"]) + ": " + _label(selection["path"]))
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
            print("For the question: " + str(len(item["files"])) + (" file" if len(item["files"]) == 1 else " files") + ". Full contents with --details.")
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
        print("File read refused: " + _read_hint(exc, policy))
        if quiet and selected_labels:
            print("Selected: " + "; ".join(selected_labels[:3])
                  + ("; other scopes: " + str(len(selected_labels) - 3) if len(selected_labels) > 3 else ""))
        print(recipient("The file contents were not sent to Claude.", config=selected_execution))
        raise FileReadDenied("input") from None
