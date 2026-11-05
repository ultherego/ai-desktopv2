"""Strict schemas for the model-driven read loop over ONE read-only project (LC4).

Request side (`validate_input`): project metadata and the controller's observations
(name pages, search hits, file fragments), checked again by the worker. Response side
(`parse_decision`): exactly one action from a closed set; anything else ends the task, no
"repair" loop. Names, hits and fragments are untrusted data and never commands; a decision
is a proposal the controller may refuse.
"""
import json
import re

from . import material_step as base
from .assistant_contract import EDIT_CHECKS
from . import materials
from . import project_scope as scope

MAX_STEPS = base.MAX_STEPS
MAX_OBSERVATIONS = 24
MAX_HISTORY = base.MAX_HISTORY
MAX_NAMES = 100
MAX_HITS = base.MAX_HITS
MAX_RANGES = base.MAX_RANGES
MAX_SOURCES = 8
MAX_EDIT_FILES = 8
PROGRESS_BYTES = 2048
MAX_STAGES = 5
MAX_VISITED = 60
MAX_PASTES = 9
PASTE_BYTES = 32 * 1024
PASTES_TOTAL = 32 * 1024
DATA_BYTES = base.DATA_BYTES
TOTAL_BYTES = base.TOTAL_BYTES
MAX_LINES = 1 << 24
MAX_CURSOR = scope.MAX_FILES
REFUSAL_ACTIONS = ("list", "find", "search", "read")
REFUSAL_CODES = frozenset({"NEEDLE_INVALID", "RANGE_INVALID", "TOO_LARGE", "PATH", "CHANGED", "LIMIT", "FILE"})
SKIPPED_KEYS = ("denied", "links", "special", "unsafe", "vcs", "other")
SEARCH_SKIPPED_KEYS = ("large", "binary", "unreadable")
_ID = re.compile(r"[0-9a-f]{16}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")

_int, _text, _data_text = base._int, base._text, base._data_text


def _path(value):
    if not scope._valid_relative(value):
        raise ValueError("project step")
    return value


def _counts(value, keys, high):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError("project step")
    for key in keys:
        _int(value[key], 0, high)
    return value


def _entries(items, limit):
    """Directory-level entries: {name, dir, files, size}; returns the bytes of names (for the data budget)."""
    if type(items) is not list or len(items) > limit:
        raise ValueError("project step")
    total = 0
    for entry in items:
        if type(entry) is not dict or set(entry) != {"name", "dir", "files", "size"} or type(entry["dir"]) is not bool:
            raise ValueError("project step")
        name = _path(entry["name"])
        if "/" in name:
            raise ValueError("project step")
        _int(entry["files"], 1, scope.MAX_FILES)
        _int(entry["size"], 0, scope.FILE_READ_BYTES * scope.MAX_FILES)
        total += len(name.encode("utf-8"))
    return total


def validate_input(value):
    if type(value) is not dict or set(value) != {"project", "task", "observations", "history"}:
        raise ValueError("project step")
    meta, task, observations, history = value["project"], value["task"], value["observations"], value["history"]
    if (type(meta) is not dict or set(meta) != {"id", "files", "complete", "skipped", "progress", "stage", "visited"}
            or type(meta["id"]) is not str or not _ID.fullmatch(meta["id"]) or type(meta["complete"]) is not bool):
        raise ValueError("project step")
    _int(meta["files"], 0, scope.MAX_FILES)
    _counts(meta["skipped"], SKIPPED_KEYS, 10 ** 7)
    _int(meta["stage"], 1, MAX_STAGES)
    visited = meta["visited"]
    if type(visited) is not list or len(visited) > MAX_VISITED:
        raise ValueError("project step")
    visited_bytes = sum(len(_path(path).encode("utf-8")) for path in visited)
    progress = meta["progress"]
    if type(progress) is not str or len(progress.encode("utf-8")) > PROGRESS_BYTES:
        raise ValueError("project step")
    if progress:
        _text(progress, PROGRESS_BYTES, multiline=True)
    if type(task) is not dict or set(task) != {"step", "max_steps", "bytes_left"}:
        raise ValueError("project step")
    maximum = _int(task["max_steps"], 1, MAX_STEPS)
    _int(task["step"], 1, maximum)
    _int(task["bytes_left"], 0, TOTAL_BYTES)
    if type(history) is not list or len(history) > MAX_HISTORY:
        raise ValueError("project step")
    data = len(progress.encode("utf-8")) + visited_bytes
    for turn in history:
        if type(turn) is not dict or set(turn) != {"question", "answer"}:
            raise ValueError("project step")
        data += len(_text(turn["question"], 2048, multiline=True).encode("utf-8"))
        data += len(_text(turn["answer"], base.HISTORY_TEXT + 64, multiline=True).encode("utf-8"))
    if type(observations) is not list or len(observations) > MAX_OBSERVATIONS:
        raise ValueError("project step")
    for item in observations:
        if type(item) is not dict or type(item.get("kind")) is not str:
            raise ValueError("project step")
        kind = item["kind"]
        if kind == "names":
            if (set(item) != {"kind", "prefix", "query", "items", "total", "next"}
                    or type(item["prefix"]) is not str or type(item["query"]) is not str):
                raise ValueError("project step")
            if item["prefix"]:
                _path(item["prefix"].rstrip("/"))
            if item["query"]:
                _text(item["query"], materials.MAX_NEEDLE_BYTES)
            _int(item["total"], 0, scope.MAX_FILES)
            if item["next"] is not None:
                _int(item["next"], 1, MAX_CURSOR)
            if type(item["items"]) is not list or len(item["items"]) > MAX_NAMES:
                raise ValueError("project step")
            for entry in item["items"]:
                if type(entry) is not dict or set(entry) != {"path", "size"}:
                    raise ValueError("project step")
                data += len(_path(entry["path"]).encode("utf-8"))
                _int(entry["size"], 0, scope.FILE_READ_BYTES * 64)
            data += len(item["prefix"].encode("utf-8")) + len(item["query"].encode("utf-8"))
        elif kind == "tree":
            if set(item) != {"kind", "prefix", "items", "total", "next"} or type(item["prefix"]) is not str:
                raise ValueError("project step")
            if item["prefix"]:
                _path(item["prefix"].rstrip("/"))
            _int(item["total"], 0, scope.MAX_FILES)
            if item["next"] is not None:
                _int(item["next"], 1, MAX_CURSOR)
            data += len(item["prefix"].encode("utf-8")) + _entries(item["items"], MAX_NAMES)
        elif kind == "overview":
            if set(item) != {"kind", "files", "size", "top", "extensions", "key_files"}:
                raise ValueError("project step")
            _int(item["files"], 0, scope.MAX_FILES)
            _int(item["size"], 0, scope.FILE_READ_BYTES * scope.MAX_FILES)
            data += _entries(item["top"], scope.OVERVIEW_TOP)
            extensions, key_files = item["extensions"], item["key_files"]
            if (type(extensions) is not list or len(extensions) > scope.OVERVIEW_EXTENSIONS
                    or type(key_files) is not list or len(key_files) > scope.OVERVIEW_KEY_FILES):
                raise ValueError("project step")
            for ext in extensions:
                if type(ext) is not dict or set(ext) != {"ext", "files"}:
                    raise ValueError("project step")
                data += len(_text(ext["ext"], 32).encode("utf-8"))
                _int(ext["files"], 1, scope.MAX_FILES)
            for path in key_files:
                data += len(_path(path).encode("utf-8"))
        elif kind == "pasted":
            if set(item) != {"kind", "id", "lines", "text"}:
                raise ValueError("project step")
            _int(item["id"], 1, MAX_PASTES)
            text = _text(item["text"], PASTE_BYTES, multiline=True)
            if _int(item["lines"], 1, 10 ** 6) != text.count("\n") + 1:
                raise ValueError("project step")
            data += len(text.encode("utf-8"))
        elif kind == "search":
            if (set(item) != {"kind", "needle", "complete", "more", "hits", "skipped"}
                    or type(item["complete"]) is not bool or type(item["more"]) is not bool):
                raise ValueError("project step")
            _text(item["needle"], materials.MAX_NEEDLE_BYTES, multiline=True)
            _counts(item["skipped"], SEARCH_SKIPPED_KEYS, scope.MAX_FILES)
            hits = item["hits"]
            if type(hits) is not list or len(hits) > MAX_HITS:
                raise ValueError("project step")
            data += len(item["needle"].encode("utf-8"))
            for hit in hits:
                if type(hit) is not dict or set(hit) != {"path", "line", "text"}:
                    raise ValueError("project step")
                data += len(_path(hit["path"]).encode("utf-8"))
                _int(hit["line"], 1, MAX_LINES)
                data += len(_data_text(hit["text"], base.MAX_HIT_TEXT * 4).encode("utf-8"))
        elif kind == "read":
            if set(item) != {"kind", "path", "first", "last", "lines", "ref", "text"}:
                raise ValueError("project step")
            _path(item["path"])
            lines = _int(item["lines"], 1, MAX_LINES)
            first, last = _int(item["first"], 1, lines), _int(item["last"], 1, lines)
            try:
                ref = materials.parse_ref(item["ref"])
            except materials.MaterialError:
                raise ValueError("project step") from None
            raw = _data_text(item["text"], materials.CHUNK_BYTES).encode("utf-8")
            if first > last or len(raw) != ref.end - ref.start or materials.sha256(raw) != ref.fragment_sha256:
                raise ValueError("project step")
            data += len(raw) + len(item["path"].encode("utf-8"))
        elif kind == "dropped":
            if set(item) != {"kind", "path", "first", "last"}:
                raise ValueError("project step")
            _path(item["path"])
            if _int(item["first"], 1, MAX_LINES) > _int(item["last"], 1, MAX_LINES):
                raise ValueError("project step")
        elif kind == "refused":
            if (set(item) != {"kind", "action", "code"} or item["action"] not in REFUSAL_ACTIONS
                    or item["code"] not in REFUSAL_CODES):
                raise ValueError("project step")
        else:
            raise ValueError("project step")
    if data > DATA_BYTES:
        raise ValueError("project step")
    return value


def data_bytes(observations, history=()):
    total = sum(len(t["question"].encode("utf-8")) + len(t["answer"].encode("utf-8")) for t in history)
    for item in observations:
        if item["kind"] == "tree":
            total += len(item["prefix"].encode("utf-8")) + sum(len(e["name"].encode("utf-8")) for e in item["items"])
        elif item["kind"] == "overview":
            total += (sum(len(e["name"].encode("utf-8")) for e in item["top"]) + sum(len(e["ext"].encode("utf-8")) for e in item["extensions"])
                      + sum(len(p.encode("utf-8")) for p in item["key_files"]))
        elif item["kind"] == "pasted":
            total += len(item["text"].encode("utf-8"))
        elif item["kind"] == "names":
            total += (len(item["prefix"].encode("utf-8")) + len(item["query"].encode("utf-8"))
                      + sum(len(e["path"].encode("utf-8")) for e in item["items"]))
        elif item["kind"] == "search":
            total += len(item["needle"].encode("utf-8")) + sum(
                len(h["path"].encode("utf-8")) + len(h["text"].encode("utf-8")) for h in item["hits"])
        elif item["kind"] == "read":
            total += len(item["text"].encode("utf-8")) + len(item["path"].encode("utf-8"))
    return total


def _span(item):
    if type(item) is not dict or set(item) != {"first", "last"}:
        raise ValueError("decision")
    first, last = _int(item["first"], 1, MAX_LINES), _int(item["last"], 1, MAX_LINES)
    if first > last:
        raise ValueError("decision")
    return first, last


def plain_answer(raw):
    """Prose instead of a JSON action: shown as an answer without sources or actions (see material_step.plain_answer)."""
    decision = base.plain_answer(raw)
    return None if decision is None else dict(decision, progress="", **{"continue": False})


def parse_decision(raw):
    """Exactly one closed action; unknown keys, duplicates, NaN or oversize text are refused."""
    raw = base.unfence(raw)
    try:
        if type(raw) is not str or not 0 < len(raw.encode("utf-8")) <= 16384:
            raise ValueError("decision")
        value = json.loads(raw, object_pairs_hook=base._unique, parse_constant=base._constant)
        if type(value) is not dict:
            raise ValueError("decision")
        action = value.get("action")
        if action == "list" and set(value) == {"action", "prefix", "cursor"}:
            prefix = value["prefix"]
            if type(prefix) is not str:
                raise ValueError("decision")
            if prefix:
                _path(prefix.rstrip("/"))
            return {"action": "list", "prefix": prefix, "cursor": _int(value["cursor"], 0, MAX_CURSOR)}
        if action == "test" and set(value) == {"action", "recipe"}:
            from .task_contract import TEST_SUITES
            if value["recipe"] not in TEST_SUITES:
                raise ValueError("decision")
            return {"action": "test", "recipe": value["recipe"]}
        if action == "overview" and set(value) == {"action"}:
            return {"action": "overview"}
        if action == "edit" and {"action", "files", "create", "check"} <= set(value) <= {"action", "files", "create", "check", "delete"}:
            files, create, delete = value["files"], value["create"], value.get("delete", [])
            if (type(files) is not list or type(create) is not list or type(delete) is not list
                    or len(files) > MAX_EDIT_FILES or len(create) > MAX_EDIT_FILES or len(delete) > MAX_EDIT_FILES
                    or not (files or create or delete) or (delete and (files or create))
                    or type(value["check"]) is not str or value["check"] not in EDIT_CHECKS):
                raise ValueError("decision")
            names = [_path(p) for p in files] + [_path(p) for p in create] + [_path(p) for p in delete]
            if len(set(names)) != len(names):
                raise ValueError("decision")
            return {"action": "edit", "files": names[:len(files)], "create": names[len(files):len(files) + len(create)],
                    "delete": names[len(files) + len(create):], "check": value["check"]}
        if action == "find" and set(value) == {"action", "fragment", "cursor"}:
            return {"action": "find", "fragment": _text(value["fragment"], materials.MAX_NEEDLE_BYTES),
                    "cursor": _int(value["cursor"], 0, MAX_CURSOR)}
        if action == "search" and set(value) == {"action", "needle", "continue"}:
            if type(value["continue"]) is not bool:
                raise ValueError("decision")
            return {"action": "search", "needle": _text(value["needle"], base.MAX_NEEDLE),
                    "continue": value["continue"]}
        if action == "read" and set(value) == {"action", "path", "ranges"}:
            ranges = value["ranges"]
            if type(ranges) is not list or not 1 <= len(ranges) <= MAX_RANGES:
                raise ValueError("decision")
            spans = [_span(item) for item in ranges]
            if len(set(spans)) != len(spans):
                raise ValueError("decision")
            return {"action": "read", "path": _path(value["path"]), "ranges": spans}
        if action == "answer" and {"action", "text", "sources"} <= set(value) <= {"action", "text", "sources", "progress", "continue"}:
            more = value.get("continue", False)
            if type(more) is not bool:
                raise ValueError("decision")
            progress = value.get("progress", "")
            if progress != "":
                _text(progress, PROGRESS_BYTES, multiline=True)
            sources = value["sources"]
            if type(sources) is not list or len(sources) > MAX_SOURCES:
                raise ValueError("decision")
            parsed = []
            for item in sources:
                if type(item) is not dict or set(item) != {"path", "first", "last"}:
                    raise ValueError("decision")
                first, last = _span({"first": item["first"], "last": item["last"]})
                parsed.append((_path(item["path"]), first, last))
            return {"action": "answer", "text": _text(value["text"], base.MAX_ANSWER_BYTES, multiline=True),
                    "sources": parsed, "progress": progress, "continue": more}
        if action == "ask_user" and set(value) == {"action", "text"}:
            return {"action": "ask_user", "text": _text(value["text"], base.MAX_ASK_BYTES)}
        raise ValueError("decision")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError("decision") from None
