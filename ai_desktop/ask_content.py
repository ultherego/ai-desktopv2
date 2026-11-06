"""Bounded excerpts from one authorized file during an ordinary ask turn.

The full source stays local. Its descriptor-checked identity is pinned through
every handoff; the model receives only literal-search snippets or exact lines.
"""
from dataclasses import dataclass
import hashlib
import os
import re

from . import conversation_state, file_context, materials, permissions
from .file_context_contract import contains_private_key

MAX_FRAGMENT_BYTES = 32 * 1024
MAX_FRAGMENTS = 20
MAX_NEEDLE_BYTES = 256
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def query(value):
    if (type(value) is not str or not value or len(value.encode("utf-8")) > MAX_NEEDLE_BYTES
            or "\n" in value or "\r" in value):
        raise ValueError("files-text")
    materials._validate_text(value)
    return value


@dataclass(frozen=True)
class ContentGuard:
    policy: object
    ident: str
    relative: str
    path: str
    version: str
    identity: tuple
    selection: object

    def recheck(self):
        if type(self.policy) is not permissions.Policy:
            raise ValueError("files-selection")
        self.policy.recheck()
        paths = permissions.recheck_selection(self.policy, self.selection)
        if paths != (self.path,) or self.policy.select(paths) != paths:
            raise ValueError("files-selection")
        parent, name = self.path.rsplit("/", 1)
        with permissions.open_directory(parent or "/", self.policy.uid) as (directory, check):
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if permissions._fingerprint(current) != self.identity:
                raise ValueError("files-changed")
            check()
        self.policy.recheck()


def _fragment(raw, start, end):
    if not 0 <= start < end <= len(raw) or end - start > MAX_FRAGMENT_BYTES:
        raise ValueError("files-total")
    text = raw[start:end].decode("utf-8")
    return {"first": raw.count(b"\n", 0, start) + 1,
            "last": raw.count(b"\n", 0, end - 1) + 1,
            "start": start, "end": end, "sha256": _hash(raw[start:end]), "text": text}


def _range(raw, first, last):
    # Avoid a Python integer per line: a large file made entirely of newlines
    # must still use bounded memory, independently of its line count.
    start = 0
    for _ in range(1, first):
        position = raw.find(b"\n", start)
        if position == -1:
            raise ValueError("files-empty")
        start = position + 1
    end = start
    for _ in range(first, last + 1):
        position = raw.find(b"\n", end)
        end = len(raw) if position == -1 else position + 1
        if end - start > MAX_FRAGMENT_BYTES:
            raise ValueError("files-total")
        if end == len(raw):
            break
    return _fragment(raw, start, end)


def collect(policy, plan, permit, *, room):
    """Return one closed content observation plus its immutable local guard."""
    if type(policy) is not permissions.Policy:
        raise ValueError("policy-type")
    selection = plan["file"]
    permit.require_files(policy, [selection])
    paths = file_context.resolve_paths(policy, [selection])
    permit.bind_file_selection(policy, paths)
    selected = permissions.recheck_selection(policy, paths)
    if len(selected) != 1:
        raise ValueError("files-count")
    (path,) = policy.select(selected)
    ident = selection if type(selection) is str else selection["id"]
    relative = "" if type(selection) is str else selection["path"]
    permit.check()
    raw, identity = permissions._read(path, policy.uid, policy.file_size(), system=policy._version == 2)
    if not raw:
        raise ValueError("files-empty")
    try:
        text = raw.decode("utf-8")
        materials._validate_text(text)
        if policy._version == 1 and contains_private_key(text):
            raise ValueError("files-private-key")
    except (UnicodeError, materials.MaterialError):
        raise ValueError("files-text") from None
    version = _hash(raw)
    guard = ContentGuard(policy, ident, relative, path, version, identity, paths)
    guard.recheck()
    permit.check()
    step = {"op": "content", "operation": "read" if plan["action"] == "read-range" else "search",
            "id": ident, "path": relative, "version": version, "size": len(raw),
            "lines": raw.count(b"\n") + 1, "fragments": [], "complete": False, "search": None}
    if plan["action"] == "read-range":
        step["fragments"] = [_range(raw, plan["first"], plan["last"])]
    else:
        needle = query(plan["needle"]).encode("utf-8")
        cursor = plan["cursor"]
        if cursor > len(raw):
            raise ValueError("files-empty")
        position, complete = cursor, False
        while len(step["fragments"]) < MAX_FRAGMENTS:
            found = raw.find(needle, position)
            if found == -1:
                position, complete = len(raw), True
                break
            stop = found + len(needle)
            start = materials._widen(raw, max(0, found - materials.CONTEXT_BYTES), -1)
            end = materials._widen(raw, min(len(raw), stop + materials.CONTEXT_BYTES), 1)
            step["fragments"].append(_fragment(raw, start, end))
            position = stop
        step["search"] = {"needle": plan["needle"], "cursor": cursor,
                          "next": None if complete else position, "complete": complete}
    amount = sum(len(item["text"].encode("utf-8")) for item in step["fragments"])
    if amount > room:
        raise ValueError("files-total")
    # Search completion means only that the literal was scanned; it is not
    # whole-file analysis. Complete content requires one exact full-file read.
    step["complete"] = (step["operation"] == "read" and len(step["fragments"]) == 1
                        and step["fragments"][0]["start"] == 0 and step["fragments"][0]["end"] == len(raw))
    guard.recheck()
    permit.check()
    scope = next(scope for scope in policy.scopes() if "f" + str(scope.index) == ident)
    logical = scope.base if not relative else scope.base + "/" + relative
    conversation_state.note_sources([logical, path])
    return step, guard, amount


def validate_step(value):
    """Worker-side pure validation. It cannot turn an excerpt into authority."""
    from .discovery_contract import _identifier, clean_path
    expected = {"op", "operation", "id", "path", "version", "size", "lines", "fragments", "complete", "search"}
    if (type(value) is not dict or set(value) != expected or value["op"] != "content"
            or value["operation"] not in ("search", "read") or type(value["complete"]) is not bool
            or type(value["version"]) is not str or not _HASH.fullmatch(value["version"])
            or type(value["size"]) is not int or not 1 <= value["size"] <= permissions.MAX_FILE_SIZE
            or type(value["lines"]) is not int or not 1 <= value["lines"] <= value["size"] + 1
            or type(value["fragments"]) is not list or len(value["fragments"]) > MAX_FRAGMENTS):
        raise ValueError("discovery")
    _identifier(value["id"])
    clean_path(value["path"], empty=True)
    for fragment in value["fragments"]:
        if (type(fragment) is not dict or set(fragment) != {"first", "last", "start", "end", "sha256", "text"}
                or any(type(fragment[name]) is not int for name in ("first", "last", "start", "end"))
                or not 1 <= fragment["first"] <= fragment["last"] <= value["lines"]
                or not 0 <= fragment["start"] < fragment["end"] <= value["size"]
                or type(fragment["text"]) is not str):
            raise ValueError("discovery")
        encoded = fragment["text"].encode("utf-8")
        if (len(encoded) != fragment["end"] - fragment["start"] or len(encoded) > MAX_FRAGMENT_BYTES
                or fragment["sha256"] != _hash(encoded)):
            raise ValueError("discovery")
        materials._validate_text(fragment["text"])
    if value["operation"] == "read":
        if len(value["fragments"]) != 1 or value["search"] is not None:
            raise ValueError("discovery")
        full = value["fragments"][0]["start"] == 0 and value["fragments"][0]["end"] == value["size"]
        if value["complete"] != full:
            raise ValueError("discovery")
    else:
        search = value["search"]
        if (value["complete"] or type(search) is not dict
                or set(search) != {"needle", "cursor", "next", "complete"}
                or type(search["complete"]) is not bool or type(search["cursor"]) is not int
                or not 0 <= search["cursor"] <= value["size"]):
            raise ValueError("discovery")
        query(search["needle"])
        if search["complete"]:
            if search["next"] is not None:
                raise ValueError("discovery")
        elif type(search["next"]) is not int or not search["cursor"] < search["next"] <= value["size"]:
            raise ValueError("discovery")
    return value
