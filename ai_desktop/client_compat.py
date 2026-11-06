"""Provider capability checks in a caller-owned, credential-free namespace.

Version text is only a compatibility hint. Every operation also exercises its
CLI parser and offline response contract using the same bound installation.
No probe starts inference, signs in, mounts a profile or opens an uplink.
"""
from dataclasses import dataclass, asdict
import json
import re

from .analysis_contract import AnalysisDenied
from .e2_ipc import _unique_pairs
from . import system_clients


PROBE_ACTIONS = frozenset({"version", "compat-help", "compat-options", "compat-web", "compat-reject", "compat-rpc"})
INVALID_OPTION = "--ai-desktop-invalid-compatibility-option"
FIRST_INVALID_OPTION = "--ai-desktop-invalid-precedence-option"
MAX_OUTPUT = 128 * 1024
_UNKNOWN_OPTION = re.compile(
    rb"(?:error: *)?(?:unknown|unrecognized) (?:option|argument) "
    rb"(?:(['\"])(--?[A-Za-z][A-Za-z0-9-]*)\1|(--?[A-Za-z][A-Za-z0-9-]*))", re.IGNORECASE)


@dataclass(frozen=True)
class CompatibilityRecord:
    provider: str
    status: str
    version: str
    version_checked: bool = True
    protocol_checked: bool = True
    tested: bool = False


def _result(run, action):
    value = run(action)
    if (type(value) is not tuple or len(value) != 3 or type(value[0]) is not int
            or any(type(item) is not bytes for item in value[1:])
            or sum(map(len, value[1:])) > MAX_OUTPUT):
        raise AnalysisDenied("client-protocol", stage="compatibility")
    return value


def _failure(errors, reason):
    # Preserve boundary refusals without disclosing a native diagnostic.
    from .analysis_sandbox import child_failure
    failure = child_failure(errors, reason)
    return AnalysisDenied(failure.reason, stage=("compatibility" if failure.stage == "internal" else failure.stage))


def _claude_signed_out(output):
    try:
        data = json.loads(output, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError, RecursionError):
        return False
    return (type(data) is dict and data.get("loggedIn") is False
            and data.get("authMethod") == "none"
            and data.get("configDirectory") == "/home/client/profile")


def _claude_option_rejected(code, output, errors, expected):
    """Require the parser's exact refusal, not an echoed argv or session error."""
    if code not in (1, 2) or output:
        return False
    match = _UNKNOWN_OPTION.fullmatch(errors.strip())
    return bool(match and (match.group(2) or match.group(3)) == expected.encode("ascii"))


def check(provider, run):
    """Return tested/untested compatibility after bounded offline observations.

The supplied runner owns one BoundClient and must bind it into every probe.
Newer releases within the adapter's major series are admitted only after all
checks pass. Runtime result/tool validation remains mandatory on every call.
"""
    if provider not in ("claude", "codex") or not callable(run):
        raise AnalysisDenied("input", stage="compatibility")
    code, output, errors = _result(run, "version")
    if code:
        raise _failure(errors, "client-version")
    version = system_clients.compatibility(provider, output)
    major = version.version.split(".", 1)[0] if version.version else None
    if version.status not in ("tested", "untested") or major != {"claude": "2", "codex": "0"}[provider]:
        raise AnalysisDenied("client-version", stage="compatibility")
    for action in (("compat-options", "compat-web") if provider == "claude" else ("compat-help",)):
        code, output, errors = _result(run, action)
        valid = (_claude_option_rejected(code, output, errors, INVALID_OPTION)
                 if provider == "claude" else (code == 0 and bool(output.strip())))
        if not valid:
            raise _failure(errors, "client-protocol")
    # Print mode treats auth/status as prompt text, and help may bypass option
    # validation. A final invalid option exercises the complete runtime argv
    # without entering a session. Two invalid options prove first-unknown
    # ordering: an unsupported earlier production flag cannot be hidden by the
    # final sentinel. This checks CLI recognition, not inference semantics.
    code, output, errors = _result(run, "compat-reject")
    if code not in (1, 2):
        raise AnalysisDenied("client-protocol", stage="compatibility")
    if b"CLIENT: REFUSED " in errors or b"bwrap:" in errors:
        raise _failure(errors, "client-protocol")
    if provider == "claude" and not _claude_option_rejected(code, output, errors, FIRST_INVALID_OPTION):
        raise AnalysisDenied("client-protocol", stage="compatibility")
    code, output, errors = _result(run, "compat-rpc")
    if provider == "claude":
        if code != 1 or not _claude_signed_out(output):
            raise _failure(errors, "client-protocol")
        tested = version.status == "tested"
        return CompatibilityRecord(provider, "compatible-tested" if tested else "compatible-untested",
                                   version.version, tested=tested)
    try:
        data = json.loads(output, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise _failure(errors, "client-protocol") from None
    valid = (code == 0 and type(data) is dict
             and set(data) == {"initialize", "signed_out", "models"}
             and all(value is True for value in data.values()))
    if not valid:
        raise _failure(errors, "client-protocol")
    tested = version.status == "tested"
    return CompatibilityRecord(provider, "compatible-tested" if tested else "compatible-untested",
                               version.version, tested=tested)


def inspect(provider):
    """Explicit doctor probe; no service identity, profile or authentication."""
    if provider not in ("claude", "codex"):
        raise ValueError("provider")
    try:
        if provider == "claude":
            from .analysis_sandbox import probe_client
        else:
            from .codex_runtime import probe_client
        return asdict(probe_client())
    except (AnalysisDenied, OSError, ValueError, TypeError) as exc:
        reason = exc.reason if isinstance(exc, AnalysisDenied) else "client-unavailable"
        return {"provider": provider, "status": reason, "version": None,
                "version_checked": False, "protocol_checked": False, "tested": False}
