"""Finite validation diagnostics; no native values may escape.

These codes identify the failing check, not a provider message or a root cause.
They cannot turn a refusal into success. The wire binds each code to its
request provider, mode and refusal reason.
"""
from types import MappingProxyType

WEB_DIAGNOSTICS = frozenset({
    'web-stream',
    'web-init-event',
    'web-init-tools',
    'web-init-mcp',
    'web-session',
    'web-assistant-message',
    'web-assistant-tool',
    'web-tool-call',
    'web-query',
    'web-tool-result-message',
    'web-tool-result-pair',
    'web-search-shape',
    'web-search-query',
    'web-search-results',
    'web-search-duration',
    'web-search-count',
    'web-search-source',
    'web-final-order',
    'web-final-status',
    'web-final-turns',
    'web-final-permissions',
    'web-final-pending',
    'web-rate-metadata',
    'web-telemetry',
    'web-event-type',
    'web-missing-result',
    'web-answer-json',
    'web-answer-shape',
    'web-answer-text',
    'web-answer-sources',
    'web-answer-provenance',
    'web-answer-references',
    'web-answer-render',
})


def web_diagnostic(value):
    """Return one trusted code or None; do not stringify untrusted values."""
    return value if type(value) is str and value in WEB_DIAGNOSTICS else None


_PROTOCOL_REASONS = frozenset({"client", "result", "authorization", "timeout"})
CODEX_DIAGNOSTIC_REASONS = MappingProxyType({
    "codex-launch": frozenset({"client"}),
    "codex-native-exit": frozenset({"client"}),
    "codex-native-exit-2": frozenset({"client"}),
    "codex-native-io": frozenset({"client"}),
    "codex-input-write": frozenset({"client"}),
    "codex-rpc-initialize": _PROTOCOL_REASONS,
    "codex-rpc-thread": _PROTOCOL_REASONS,
    "codex-rpc-turn": _PROTOCOL_REASONS,
    "codex-stream": _PROTOCOL_REASONS,
    "codex-turn-failed": frozenset({"client"}),
    "codex-server-error": frozenset({"client"}),
    "codex-usage-limit": frozenset({"rate-limit"}),
    "codex-rate-limit": frozenset({"rate-limit"}),
    "codex-unauthorized": frozenset({"auth-required"}),
    "codex-model-unavailable": frozenset({"client"}),
    "codex-http-unauthorized": frozenset({"auth-required"}),
    "codex-http-forbidden": frozenset({"authorization"}),
    "codex-http-rate-limit": frozenset({"rate-limit"}),
    "codex-http-unavailable": frozenset({"provider-unavailable"}),
    "codex-http-connection": frozenset({"network"}),
    "codex-stream-connection": frozenset({"network"}),
    "codex-stream-disconnected": frozenset({"network"}),
    "codex-stream-attempts": frozenset({"network"}),
    "codex-server-overloaded": frozenset({"provider-unavailable"}),
})
CODEX_DIAGNOSTICS = frozenset(CODEX_DIAGNOSTIC_REASONS)
_CODEX_MODES = frozenset({"analysis", "question", "assistant", "material", "project", "workspace"})


def codex_diagnostic(value, reason):
    """Accept a closed code/reason pair without formatting native data."""
    return (value if type(value) is str and type(reason) is str
            and reason in CODEX_DIAGNOSTIC_REASONS.get(value, ()) else None)


def failure_diagnostic(value, reason, *, mode, provider):
    """The exact validated request selects the only accepted code vocabulary."""
    if provider == "claude" and mode == "web" and reason == "result":
        return web_diagnostic(value)
    if provider == "codex" and mode in _CODEX_MODES:
        return codex_diagnostic(value, reason)
    return None
