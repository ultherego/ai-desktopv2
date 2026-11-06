"""Strict schemas for the model-driven read loop over ONE local material.

Request side (`validate_input`): the controller's metadata and observations, all
checked again by the worker. Response side (`parse_decision`): exactly one action
from a closed set; anything else ends the task, there is no "repair" loop.
Observation and decision texts are untrusted data and never commands; a model
decision is a proposal that the controller may refuse.
"""
import json
import re
import unicodedata

from . import materials

MAX_STEPS = 8
MAX_OBSERVATIONS = 24
MAX_HISTORY = 16
NOTE_BYTES = 3072
MAX_PARTS = 512
HISTORY_TEXT = 8192
MAX_HITS = 20
MAX_HIT_TEXT = 200
MAX_NEEDLE = 128
MAX_RANGES = 4
MAX_SOURCES = 8
MAX_ANSWER_BYTES = 15 * 1024
MAX_ASK_BYTES = 4096
DATA_BYTES = 256 * 1024
PART_DATA_BYTES = 512 * 1024          # data of ONE request, only in the "map" phase of a whole-file analysis (opt-in profile)
REDUCE_DATA_BYTES = 2 * 1024 * 1024  # bounded notes only, never additional local reads
TOTAL_BYTES = 2 * 1024 * 1024
_ID = re.compile(r"[0-9a-f]{16}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
REFUSAL_CODES = frozenset({"NEEDLE_INVALID", "RANGE_INVALID", "TOO_LARGE", "LIMIT"})


def _int(value, low, high):
    if type(value) is not int or isinstance(value, bool) or not low <= value <= high:
        raise ValueError("material step")
    return value


def _text(value, limit, *, multiline=False):
    if (type(value) is not str or not value.strip() or len(value.encode("utf-8")) > limit
            or any(c not in ("\n\t" if multiline else "") and unicodedata.category(c) in ("Cc", "Cf", "Cs")
                   for c in value)):
        raise ValueError("material step")
    return value


def _data_text(value, limit):
    """Material text may legally contain CR/LF/TAB; other controls were refused at import."""
    if type(value) is not str or len(value.encode("utf-8")) > limit:
        raise ValueError("material step")
    return value


def validate_input(value):
    if type(value) is not dict or set(value) != {"material", "task", "observations", "history"}:
        raise ValueError("material step")
    meta, task, observations = value["material"], value["task"], value["observations"]
    history = value["history"]
    if type(history) is not list or len(history) > MAX_HISTORY:
        raise ValueError("material step")
    if (type(meta) is not dict or set(meta) != {"id", "version", "size", "lines"}
            or type(meta["id"]) is not str or not _ID.fullmatch(meta["id"])
            or type(meta["version"]) is not str or not _HEX64.fullmatch(meta["version"])):
        raise ValueError("material step")
    size = _int(meta["size"], 1, materials.MAX_PROJECT_FILE_BYTES)     # the owner's file_size is enforced by the client
    lines = _int(meta["lines"], 1, size)
    base = {"step", "max_steps", "bytes_left"}
    if type(task) is not dict or set(task) not in (base, base | {"phase", "part"}):
        raise ValueError("material step")
    if "phase" in task:
        part = task["part"]
        if (task["phase"] not in ("map", "reduce") or type(part) is not dict or set(part) != {"index", "total"}):
            raise ValueError("material step")
        if _int(part["index"], 0, MAX_PARTS) > _int(part["total"], 1, MAX_PARTS):
            raise ValueError("material step")
    maximum = _int(task["max_steps"], 1, MAX_STEPS)
    _int(task["step"], 1, maximum)
    _int(task["bytes_left"], 0, TOTAL_BYTES)
    observation_limit = MAX_PARTS if task.get("phase") == "reduce" else MAX_OBSERVATIONS
    if type(observations) is not list or len(observations) > observation_limit:
        raise ValueError("material step")
    data = 0
    for turn in history:
        if type(turn) is not dict or set(turn) != {"question", "answer"}:
            raise ValueError("material step")
        data += len(_text(turn["question"], 2048, multiline=True).encode("utf-8"))
        data += len(_text(turn["answer"], HISTORY_TEXT + 64, multiline=True).encode("utf-8"))
    for item in observations:
        if type(item) is not dict or type(item.get("kind")) is not str:
            raise ValueError("material step")
        kind = item["kind"]
        if task.get("phase") == "reduce" and kind != "notes":
            raise ValueError("material step")
        if kind == "search":
            if set(item) != {"kind", "needle", "complete", "hits"} or type(item["complete"]) is not bool:
                raise ValueError("material step")
            _text(item["needle"], materials.MAX_NEEDLE_BYTES, multiline=True)
            hits = item["hits"]
            if type(hits) is not list or len(hits) > MAX_HITS:
                raise ValueError("material step")
            data += len(item["needle"].encode("utf-8"))
            for hit in hits:
                if type(hit) is not dict or set(hit) != {"line", "text"}:
                    raise ValueError("material step")
                _int(hit["line"], 1, lines)
                data += len(_data_text(hit["text"], MAX_HIT_TEXT * 4).encode("utf-8"))
        elif kind == "read":
            if set(item) != {"kind", "first", "last", "ref", "text"}:
                raise ValueError("material step")
            first, last = _int(item["first"], 1, lines), _int(item["last"], 1, lines)
            try:
                ref = materials.parse_ref(item["ref"])
            except materials.MaterialError:
                raise ValueError("material step") from None
            raw = _data_text(item["text"], materials.CHUNK_BYTES).encode("utf-8")
            if (first > last or ref.material_id != meta["id"] or ref.version != meta["version"]
                    or ref.end > size or len(raw) != ref.end - ref.start
                    or materials.sha256(raw) != ref.fragment_sha256):
                raise ValueError("material step")
            data += len(raw)
        elif kind == "notes":
            if set(item) != {"kind", "first", "last", "text"}:
                raise ValueError("material step")
            if _int(item["first"], 1, lines) > _int(item["last"], 1, lines):
                raise ValueError("material step")
            data += len(_text(item["text"], NOTE_BYTES, multiline=True).encode("utf-8"))
        elif kind == "dropped":
            if set(item) != {"kind", "first", "last"}:
                raise ValueError("material step")
            if _int(item["first"], 1, lines) > _int(item["last"], 1, lines):
                raise ValueError("material step")
        elif kind == "refused":
            if (set(item) != {"kind", "action", "code"} or item["action"] not in ("search", "read")
                    or item["code"] not in REFUSAL_CODES):
                raise ValueError("material step")
        else:
            raise ValueError("material step")
    if data > data_limit(task):
        raise ValueError("material step")
    return value


def data_limit(task):
    """Separate bounded fragment, whole-file map and notes-only synthesis budgets."""
    phase = task.get("phase") if type(task) is dict else None
    return {"map": PART_DATA_BYTES, "reduce": REDUCE_DATA_BYTES}.get(phase, DATA_BYTES)


def data_bytes(observations, history=()):
    """Dynamic data carried by a request (what counts against the packet budget)."""
    total = sum(len(t["question"].encode("utf-8")) + len(t["answer"].encode("utf-8")) for t in history)
    for item in observations:
        if item["kind"] == "search":
            total += len(item["needle"].encode("utf-8")) + sum(len(h["text"].encode("utf-8")) for h in item["hits"])
        elif item["kind"] in ("read", "notes"):
            total += len(item["text"].encode("utf-8"))
    return total


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("decision")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("decision")


def _span(item, lines):
    if type(item) is not dict or set(item) != {"first", "last"}:
        raise ValueError("decision")
    first, last = _int(item["first"], 1, lines), _int(item["last"], 1, lines)
    if first > last:
        raise ValueError("decision")
    return first, last


def plain_answer(raw):
    """A reply that is clearly prose (not an attempted JSON action) is shown as an unsourced answer instead of being lost.

    It is displayed text only: it carries no authority, no sources and no actions. An `answer` object that only fails the
    strict schema (missing or odd optional fields) is salvaged the same way: its text is shown, its sources are dropped."""
    salvaged = _salvage_answer(raw)
    if salvaged is not None:
        return salvaged
    try:
        if type(raw) is not str or not raw.strip() or raw.lstrip()[:1] in ("{", "[") or '"action"' in raw:
            return None
        return {"action": "answer", "text": _text(raw.strip(), MAX_ANSWER_BYTES, multiline=True), "sources": []}
    except (ValueError, TypeError):
        return None


EMBEDDED_PROSE_BYTES = 600


def _salvage_answer(raw):
    try:
        value = json.loads(unfence(raw), object_pairs_hook=_unique, parse_constant=_constant)
        if type(value) is dict and value.get("action") == "answer" and type(value.get("text")) is str:
            return {"action": "answer", "text": _text(value["text"], MAX_ANSWER_BYTES, multiline=True), "sources": []}
    except (ValueError, TypeError, RecursionError, UnicodeError):
        pass
    return None


def unfence(raw):
    """Unwrap, never repair: one Markdown code fence around the JSON, or ONE JSON object after/before a short sentence.

    The object itself is then parsed by the same strict schema; surrounding prose is ignored. A second object, a long
    commentary or anything unparsable leaves the reply unchanged (and therefore refused or shown as plain prose)."""
    if type(raw) is not str:
        return raw
    text = raw.strip()
    if text.startswith("```") and text.endswith("```") and text.count("```") == 2:
        body = text[3:-3]
        first, _, rest = body.partition("\n")
        if first.strip().lower() in ("", "json") and rest.strip():
            return rest.strip()
        return raw
    start = text.find("{")
    if start < 0 or start > EMBEDDED_PROSE_BYTES:
        return raw
    try:
        _, end = json.JSONDecoder(object_pairs_hook=_unique, parse_constant=_constant).raw_decode(text[start:])
    except (ValueError, RecursionError):
        return raw
    tail = text[start + end:].strip()
    if "{" in text[:start] or "{" in tail or len(tail.encode("utf-8")) > EMBEDDED_PROSE_BYTES:
        return raw
    return text[start:start + end]


def parse_decision(raw, lines):
    """Exactly one closed action; unknown keys, duplicates, NaN or oversize text are refused."""
    raw = unfence(raw)
    try:
        if type(raw) is not str or not 0 < len(raw.encode("utf-8")) <= 16384:
            raise ValueError("decision")
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant)
        if type(value) is not dict:
            raise ValueError("decision")
        action = value.get("action")
        if action == "search" and set(value) == {"action", "needle"}:
            return {"action": "search", "needle": _text(value["needle"], MAX_NEEDLE)}
        if action == "read" and set(value) == {"action", "ranges"}:
            ranges = value["ranges"]
            if type(ranges) is not list or not 1 <= len(ranges) <= MAX_RANGES:
                raise ValueError("decision")
            spans = [_span(item, lines) for item in ranges]
            if len(set(spans)) != len(spans):
                raise ValueError("decision")
            return {"action": "read", "ranges": spans}
        if action == "answer" and set(value) == {"action", "text", "sources"}:
            sources = value["sources"]
            if type(sources) is not list or len(sources) > MAX_SOURCES:
                raise ValueError("decision")
            return {"action": "answer", "text": _text(value["text"], MAX_ANSWER_BYTES, multiline=True),
                    "sources": [_span(item, lines) for item in sources]}
        if action == "notes" and set(value) == {"action", "text"}:
            return {"action": "notes", "text": _text(value["text"], NOTE_BYTES, multiline=True)}
        if action == "whole" and set(value) == {"action"}:
            return {"action": "whole"}
        if action == "ask_user" and set(value) == {"action", "text"}:
            return {"action": "ask_user", "text": _text(value["text"], MAX_ASK_BYTES)}
        raise ValueError("decision")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError("decision") from None
