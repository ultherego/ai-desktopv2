"""Bounded task memory of one `ask` conversation: facts the user and the assistant agreed on.

The memory is DATA for the model, never authority: it carries no grants, paths, commands or file contents by design of
its schema, and the controller (not the model) assigns ids, turns and statuses. Corrections do not rewrite history: an
entry that was corrected is marked `superseded` (pointing at its replacement) and a rejected proposal is marked
`rejected`, so a later request can never read both versions as true or a rejected idea as an applied change.
"""
import json
import re
import unicodedata

KINDS = ("goal", "success", "baseline", "trial", "observation", "value", "correction", "decision", "proposal", "open")
STATUSES = ("active", "superseded", "rejected", "done")
PINNED = ("goal", "success", "baseline")        # trimmed last, and only after everything else
MAX_ENTRIES = 40
ENTRY_BYTES = 400
MEMORY_BYTES = 10 * 1024
MAX_OPS = 12
_ID = re.compile(r"m(?:[1-9][0-9]{0,3})\.(?:[1-9][0-9]?)\Z")
MARKER = "@@MEMORY "


def _text(value, limit=ENTRY_BYTES):
    if (type(value) is not str or not value.strip() or len(value.encode("utf-8")) > limit
            or any(char not in "\n\t" and unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp") for char in value)):
        raise ValueError("memory")
    return value


def cut(value, limit=ENTRY_BYTES):
    """Visible shortening: the entry says so, nothing is silently dropped from the middle of a value."""
    value = " ".join(value.split())
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value
    return raw[:limit - len(" …[skrócono]".encode("utf-8"))].decode("utf-8", "ignore") + " …[skrócono]"


def empty():
    return {"entries": [], "dropped": 0}


def size(memory):
    return len(json.dumps(memory, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def validate(value):
    """Copy a closed document without interpreting any text."""
    try:
        if (type(value) is not dict or set(value) != {"entries", "dropped"} or type(value["entries"]) is not list
                or len(value["entries"]) > MAX_ENTRIES or type(value["dropped"]) is not int
                or isinstance(value["dropped"], bool) or not 0 <= value["dropped"] < 2**31):
            raise ValueError()
        entries, seen = [], set()
        for item in value["entries"]:
            keys = {"id", "turn", "kind", "status", "text"}
            if type(item) is not dict or not keys <= set(item) <= keys | {"by"}:
                raise ValueError()
            if (type(item["id"]) is not str or not _ID.fullmatch(item["id"]) or item["id"] in seen
                    or type(item["turn"]) is not int or isinstance(item["turn"], bool) or not 1 <= item["turn"] < 10**4
                    or type(item["kind"]) is not str or item["kind"] not in KINDS
                    or type(item["status"]) is not str or item["status"] not in STATUSES):
                raise ValueError()
            seen.add(item["id"])
            entry = {"id": item["id"], "turn": item["turn"], "kind": item["kind"], "status": item["status"],
                     "text": _text(item["text"])}
            if "by" in item:
                if type(item["by"]) is not str or not _ID.fullmatch(item["by"]):
                    raise ValueError()
                entry["by"] = item["by"]
            entries.append(entry)
        result = {"entries": entries, "dropped": value["dropped"]}
        if size(result) > MEMORY_BYTES:
            raise ValueError()
        return result
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ValueError("memory") from None


def split_marker(text):
    """(visible text, ops JSON text or None): the last line `@@MEMORY {json}` is removed from what the user sees."""
    if type(text) is not str:
        return text, None
    stripped = text.rstrip()
    head, _, last = stripped.rpartition("\n")
    if last.startswith(MARKER):
        return head.rstrip(), last[len(MARKER):]
    if stripped.startswith(MARKER) and "\n" not in stripped:
        return "", stripped[len(MARKER):]
    return text, None


def parse_ops(raw):
    """Model-proposed updates -> a validated list, or None when absent/invalid (the caller records that, never guesses)."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        if type(data) is not dict or not set(data) <= {"add", "mark"}:
            raise ValueError()
        adds, marks = data.get("add", []), data.get("mark", [])
        if type(adds) is not list or type(marks) is not list or len(adds) + len(marks) > MAX_OPS:
            raise ValueError()
        result = {"add": [], "mark": []}
        for item in adds:
            if type(item) is not dict or not {"kind", "text"} <= set(item) <= {"kind", "text", "status", "supersedes"}:
                raise ValueError()
            kind = item["kind"]
            status = item.get("status", "active")
            supersedes = item.get("supersedes", [])
            if (type(kind) is not str or kind not in KINDS or type(status) is not str or status not in STATUSES
                    or type(supersedes) is not list or len(supersedes) > 4
                    or any(type(ident) is not str or not _ID.fullmatch(ident) for ident in supersedes)):
                raise ValueError()
            result["add"].append({"kind": kind, "text": cut(_text(item["text"], 4 * ENTRY_BYTES)), "status": status,
                                  "supersedes": list(supersedes)})
        for item in marks:
            if (type(item) is not dict or set(item) != {"id", "status"} or type(item["id"]) is not str
                    or not _ID.fullmatch(item["id"]) or item["status"] not in STATUSES):
                raise ValueError()
            result["mark"].append({"id": item["id"], "status": item["status"]})
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def _drop_order(entry):
    pinned = entry["kind"] in PINNED and entry["status"] == "active"
    finished = entry["status"] in ("superseded", "rejected", "done")
    return (pinned, not finished, entry["turn"])


def trim(entries, dropped):
    """Keep the memory inside its budget; what is lost is counted, so the model is told it is incomplete."""
    entries = list(entries)
    while entries and (len(entries) > MAX_ENTRIES
                       or size({"entries": entries, "dropped": dropped}) > MEMORY_BYTES):
        victim = min(entries, key=_drop_order)
        entries.remove(victim)
        dropped += 1
    return entries, dropped


def apply(memory, turn, ops, user_note=None, goal=False, facts=()):
    """Return (new memory, notes). `ops` may be None: then only the deterministic user note is recorded.

    Ids are assigned here, `supersedes` marks the old entry `superseded` and points at the new one, and an unknown id
    is ignored and reported instead of being invented."""
    entries = [dict(item) for item in memory["entries"]]
    known = {item["id"] for item in entries}
    notes, counter = [], 0

    def new_id():
        nonlocal counter
        counter += 1
        return "m%d.%d" % (turn, counter)

    if user_note:
        ident = new_id()
        entries.append({"id": ident, "turn": turn, "kind": "goal" if goal else "observation", "status": "active",
                        "text": cut(user_note)})
        known.add(ident)
    if ops is None:
        notes.append("no-update")
    else:
        for add in ops["add"]:
            ident = new_id()
            entries.append({"id": ident, "turn": turn, "kind": add["kind"], "status": add["status"], "text": add["text"]})
            known.add(ident)
            for old in add["supersedes"]:
                target = next((item for item in entries if item["id"] == old), None)
                if target is None or old == ident:
                    notes.append("unknown-id")
                    continue
                target["status"], target["by"] = "superseded", ident
        for mark in ops["mark"]:
            target = next((item for item in entries if item["id"] == mark["id"]), None)
            if target is None:
                notes.append("unknown-id")
            else:
                target["status"] = mark["status"]
    for fact in facts:
        ident = new_id()
        entries.append({"id": ident, "turn": turn, "kind": "observation", "status": "done", "text": cut(fact)})
        known.add(ident)
    entries, dropped = trim(entries, memory["dropped"])
    if dropped != memory["dropped"]:
        notes.append("trimmed")
    return {"entries": entries, "dropped": dropped}, notes
