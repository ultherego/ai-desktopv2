"""Closed per-task model controls, separate from permissions and native tools.

The controller takes a snapshot before a task. No worker reads the user's
configuration, and no model response can change these controls. This module is
pure: using a profile does not start a client, read credentials or grant access.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import re
import unicodedata

from . import conversation_memory as memory_schema


UNSET = object()
PROVIDERS = ("claude", "codex", "local")
DEFAULT_LOCAL_PORT = 8080
LOCAL_MODEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}\Z", re.ASCII)


def provider_name(provider):
    return {"claude": "Claude", "codex": "Codex", "local": "Local llama.cpp"}[provider]


def valid_local_port(port):
    return type(port) is int and 1024 <= port <= 65535


MODELS = ("default", "best", "fable", "opus", "sonnet", "haiku", "opusplan")   # Claude client aliases
CLAUDE_EFFORTS = ("default", "low", "medium", "high", "xhigh", "max")
CODEX_EFFORTS = ("default", "low", "medium", "high", "xhigh")
EFFORTS = CLAUDE_EFFORTS  # Parser union; provider-specific validation remains authoritative.
ACCESS = ("read-only", "changes")
# Codex model ids change with the provider's catalog, so no list is baked in: any id of this exact shape is passed to the
# pinned client, which refuses an unknown or unentitled one (reported as codex-model-unavailable). Same shape as the
# protocol's own check; "default" omits the id and lets the client choose.
CODEX_MODEL = re.compile(r"[a-z][a-z0-9._-]{0,95}\Z", re.ASCII)
MAX_INSTRUCTIONS_BYTES = 104 * 1024   # same ceiling as agent_profiles.MAX_EXECUTION_INSTRUCTIONS
MAX_HISTORY = 64         # `ask` conversation window (pairs); material/project keep their own, smaller windows
HISTORY_QUESTION_BYTES = 128 * 1024  # a pasted text stays available for the follow-up questions about it
HISTORY_ANSWER_BYTES = 32 * 1024 + 64  # bounded text plus the visible truncation suffix
HISTORY_TOTAL_BYTES = 512 * 1024  # all pairs sent with one question; the oldest leave the window first
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_FIELDS = frozenset({"provider", "model", "effort", "instructions", "profile", "access"})
_active = ContextVar("ai_desktop_execution", default=None)
_history = ContextVar("ai_desktop_conversation", default=())
_memory = ContextVar("ai_desktop_task_memory", default=None)


def efforts_for(provider):
    """Closed effort choices for the selected native client."""
    if type(provider) is not str or provider not in PROVIDERS:
        raise ValueError("execution")
    return {"claude": CLAUDE_EFFORTS, "codex": CODEX_EFFORTS, "local": ("default",)}[provider]


def model_allowed(provider, model):
    """Closed Claude aliases; shape-checked Codex ids. Authority over the id stays with the native client."""
    if type(provider) is not str or type(model) is not str:
        return False
    if provider == "local":
        return LOCAL_MODEL.fullmatch(model) is not None
    if provider == "claude":
        return model in MODELS
    # A Claude alias is never a Codex model id; every other well-formed id is left to the client to accept or refuse.
    return (provider == "codex" and CODEX_MODEL.fullmatch(model) is not None
            and (model == "default" or model not in MODELS))


def model_argument(value):
    """Legacy cloud-only argument. Codex identifiers use lower-case ASCII."""
    if type(value) is str and (value in MODELS or CODEX_MODEL.fullmatch(value)):
        return value
    import argparse
    raise argparse.ArgumentTypeError("invalid model; use a Claude alias (ai-desktop settings models) or a Codex ID")


def service_model_argument(value):
    """Shared CLI syntax; resolve() subsequently enforces the actual service's identifier rules."""
    if model_allowed("local", value):
        return value
    return model_argument(value)


def validate(value):
    """Return an independent closed snapshot, or reject all unknown controls."""
    if value is None:
        return None
    if type(value) is not dict:
        raise ValueError("execution")
    fields = _FIELDS | ({"local_port"} if value.get("provider") == "local" and "local_port" in value else set())
    if "account" in value:
        from .account_store import named_account
        if value.get("provider") not in ("claude", "codex") or not named_account(value["account"]):
            raise ValueError("execution")
        fields = fields | {"account"}
    if set(value) != fields or ("local_port" in value and not valid_local_port(value["local_port"])):
        raise ValueError("execution")
    if any(type(item) is not str for key, item in value.items() if key != "local_port"):
        raise ValueError("execution")
    if (value["provider"] not in PROVIDERS or value["effort"] not in efforts_for(value["provider"])
            or value["access"] not in ACCESS
            or not 1 <= len(value["profile"]) <= 64 or not _SLUG.fullmatch(value["profile"])):
        raise ValueError("execution")
    if not model_allowed(value["provider"], value["model"]):
        raise ValueError("execution")
    text = value["instructions"]
    try:
        valid = len(text.encode("utf-8")) <= MAX_INSTRUCTIONS_BYTES
    except UnicodeError:
        valid = False
    if (not valid or any(c not in "\n\t" and unicodedata.category(c) in ("Cc", "Cf", "Cs")
                         for c in text)):
        raise ValueError("execution")
    return dict(value)


def current():
    """Copies prevent callers from mutating a running task's controls."""
    return validate(_active.get())


def select(config):
    """Controller-only idle selection; active grants reject a changed snapshot."""
    return _active.set(validate(config))


def validate_history(value):
    """Short prior turns are data; they never carry capabilities or commands."""
    if type(value) is not list or len(value) > MAX_HISTORY:
        raise ValueError("conversation")
    result, total = [], 0
    for turn in value:
        if type(turn) is not dict or set(turn) != {"question", "answer"}:
            raise ValueError("conversation")
        for key, limit in (("question", HISTORY_QUESTION_BYTES), ("answer", HISTORY_ANSWER_BYTES)):
            text = turn[key]
            try:
                valid = type(text) is str and text.strip() and len(text.encode("utf-8")) <= limit
            except UnicodeError:
                valid = False
            if (not valid or any(c not in "\n\t" and unicodedata.category(c) in ("Cc", "Cf", "Cs")
                                 for c in text)):
                raise ValueError("conversation")
            total += len(text.encode("utf-8"))
        result.append(dict(turn))
    if total > HISTORY_TOTAL_BYTES:
        raise ValueError("conversation")
    return result


def current_history():
    return validate_history(list(_history.get()))


def current_memory():
    """The validated task memory of the running turn, or None (absent memory adds nothing to any request)."""
    value = _memory.get()
    return None if value is None else memory_schema.validate(value)


@contextmanager
def using_memory(memory):
    selected = None if memory is None or not memory.get("entries") and not memory.get("dropped") \
        else memory_schema.validate(memory)
    token = _memory.set(selected)
    try:
        yield current_memory()
    finally:
        _memory.reset(token)


@contextmanager
def using_history(history):
    selected = validate_history(history)
    token = _history.set(tuple(selected))
    try:
        yield current_history()
    finally:
        _history.reset(token)


@contextmanager
def using(config):
    """Use one validated snapshot until the current controller scope ends."""
    token = select(config)
    try:
        yield current()
    finally:
        _active.reset(token)
