"""Bounded selected-file requests and untrusted replacement proposals.

No paths outside the user's explicit selection, generated commands or filesystem
access are accepted by this parser. File contents are data.
"""
import json
import re
import unicodedata

from .e2_ipc import _unique_pairs

MAX_INSTRUCTION = 128 * 1024           # an `ask` message may be this long, so a change it asks for must fit too
_SEGMENT = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,63}\Z")

MAX_FILES = 64
MAX_CHANGED_FILES = 32
MAX_FILE = 64 * 1024
MAX_TOTAL = 128 * 1024


def path(value):
    if type(value) is not str or not 0 < len(value) <= 160:
        raise ValueError("path")
    parts = value.split("/")
    if len(parts) > 5 or any(
            not _SEGMENT.fullmatch(p) or p in (".", "..")
            or p.casefold() == ".ai-desktop-tasks" or p.casefold().startswith(".ai-task-")
            for p in parts):
        raise ValueError("path")
    return value


def text(value, limit=MAX_FILE):
    if (type(value) is not str or len(value.encode("utf-8")) > limit
            or any(c not in "\n\t" and unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in value)):
        raise ValueError("text")
    return value


def request(value):
    if type(value) is not dict or set(value) != {"instruction", "files"}:
        raise ValueError("task")
    if not text(value["instruction"], MAX_INSTRUCTION).strip():
        raise ValueError("instruction")
    files = value["files"]
    if type(files) is not list or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("files")
    seen, total, writable = set(), 0, False
    for item in files:
        if type(item) is not dict or set(item) != {"path", "content", "writable"}:
            raise ValueError("file")
        name = path(item["path"])
        if name in seen or type(item["writable"]) is not bool:
            raise ValueError("file")
        seen.add(name)
        if item["content"] is None:
            if not item["writable"]:
                raise ValueError("missing context")
        else:
            total += len(text(item["content"]).encode("utf-8"))
        writable |= item["writable"]
    if total > MAX_TOTAL or not writable:
        raise ValueError("total")
    return value


class UnchangedProposal(ValueError):
    """Selected file proposed with its current content: a refusal with its own message."""


def proposal(raw, source):
    request(source)
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (json.JSONDecodeError, RecursionError, UnicodeError):
        raise ValueError("proposal json") from None
    if type(value) is not dict or set(value) != {"summary", "files"}:
        raise ValueError("proposal")
    text(value["summary"], 2048)
    files = value["files"]
    allowed = {f["path"]: f["content"] for f in source["files"] if f["writable"]}
    if type(files) is not list or not 1 <= len(files) <= min(len(allowed), MAX_CHANGED_FILES):
        raise ValueError("proposal files")
    seen, total = set(), 0
    for item in files:
        if type(item) is not dict or set(item) != {"path", "content"}:
            raise ValueError("proposal file")
        name = path(item["path"])
        if name not in allowed or name in seen:
            raise ValueError("not selected")
        if item["content"] == allowed[name]:
            raise UnchangedProposal("unchanged")
        total += len(text(item["content"]).encode("utf-8"))
        seen.add(name)
    if total > MAX_TOTAL:
        raise ValueError("total")
    return value
