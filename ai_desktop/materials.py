"""LC0: bounded local text materials. Pure functions, no I/O, no model, no network.

A material is one immutable block of UTF-8 bytes (default 32 MiB, configurable up to 64 MiB) that stays local.
Everything the model may later see is derived from it by byte ranges that are
verified against the original bytes: deterministic chunks, exact range reads,
literal search with a cursor, and a coverage tracker that only the controller
fills. Source references name a version (SHA-256 of the whole material), a
byte range and the SHA-256 of that fragment; a reference proves provenance, not
that an interpretation is true. Text is never truncated or transformed:
anything that does not fit or is not plain text is refused with a closed code.
"""
from dataclasses import dataclass
from array import array
import bisect
import hashlib
import re
import secrets
import unicodedata

MAX_MATERIAL_BYTES = 32 * 1_048_576     # local import; the provider gets bounded fragments
MAX_PROJECT_FILE_BYTES = 64 * 1_048_576 # largest single project file (LC4)
CHUNK_BYTES = 32 * 1024                 # one fragment/observation
MAX_NEEDLE_BYTES = 256
MAX_HITS_PER_PAGE = 20
CONTEXT_BYTES = 256                     # window around a hit, widened to boundaries
_ALLOWED_CONTROLS = frozenset("\n\t\r")
_ASCII_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9a-f]{16}\Z")

CODES = frozenset({"IMPORT_EMPTY", "IMPORT_LIMIT", "IMPORT_ENCODING", "IMPORT_CONTROL",
                   "SOURCE_CHANGED", "RANGE_INVALID", "REF_INVALID", "NEEDLE_INVALID"})


class MaterialError(ValueError):
    """Closed refusal code; never carries material text or a path."""

    def __init__(self, code):
        if code not in CODES:
            raise AssertionError("unknown code")
        super().__init__(code)
        self.code = code


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _is_continuation(byte):
    return byte & 0xC0 == 0x80


def _boundary(raw, offset):
    """True when offset is a UTF-8 character boundary (0 and len are boundaries)."""
    return offset == 0 or offset == len(raw) or not _is_continuation(raw[offset])


def _validate_text(text):
    if _ASCII_CONTROL.search(text):
        raise MaterialError("IMPORT_CONTROL")
    if not text.isascii():
        for char in text:
            if char > "\x7f" and unicodedata.category(char) in ("Cc", "Cf", "Cs", "Co", "Cn"):
                raise MaterialError("IMPORT_CONTROL")


@dataclass(frozen=True)
class Material:
    material_id: str
    origin: str
    raw: bytes
    sha256: str
    line_starts: memoryview  # read-only packed uint32 offsets; at most four bytes per source byte

    @property
    def size(self):
        return len(self.raw)

    def line_of(self, offset):
        """1-based line number (lines end at b"\\n") and 0-based byte column."""
        if not 0 <= offset <= self.size:
            raise MaterialError("RANGE_INVALID")
        index = bisect.bisect_right(self.line_starts, offset) - 1
        return index + 1, offset - self.line_starts[index]


def import_bytes(raw, origin, limit=MAX_MATERIAL_BYTES):
    """Accept one complete material or refuse; the bytes are kept exactly as given.

    `limit` may be raised by the owner, never above 64 MiB."""
    if type(raw) is not bytes or origin not in ("file", "paste"):
        raise MaterialError("IMPORT_ENCODING")
    if type(limit) is not int or not 1 <= limit <= MAX_PROJECT_FILE_BYTES:
        raise MaterialError("IMPORT_LIMIT")
    if not raw:
        raise MaterialError("IMPORT_EMPTY")
    if len(raw) > limit:
        raise MaterialError("IMPORT_LIMIT")
    try:
        text = raw.decode("utf-8")  # strict: no replacement, no BOM stripping
    except UnicodeDecodeError:
        raise MaterialError("IMPORT_ENCODING") from None
    _validate_text(text)
    starts, position = array("I", [0]), raw.find(b"\n")
    while position != -1:
        starts.append(position + 1)
        position = raw.find(b"\n", position + 1)
    return Material(secrets.token_hex(8), origin, raw, sha256(raw), memoryview(starts.tobytes()).cast("I"))


@dataclass(frozen=True)
class Chunk:
    index: int
    start: int
    end: int
    start_line: int
    end_line: int
    sha256: str


def chunk_plan(material, chunk_bytes=CHUNK_BYTES):
    """Contiguous, non-overlapping main ranges covering exactly [0, size).

    Cut after a newline when one lies in the second half of the window, else on
    a character boundary. A single huge line is therefore split safely, and the
    plan depends only on the bytes (same input, same plan).
    """
    raw, size = material.raw, material.size
    if type(chunk_bytes) is not int or chunk_bytes < 4:
        raise MaterialError("RANGE_INVALID")
    chunks, start = [], 0
    while start < size:
        end = min(start + chunk_bytes, size)
        if end < size:
            newline = raw.rfind(b"\n", start + chunk_bytes // 2, end)
            if newline != -1:
                end = newline + 1
            else:
                while end > start and _is_continuation(raw[end]):
                    end -= 1
        if end <= start:
            raise MaterialError("RANGE_INVALID")
        first, _ = material.line_of(start)
        last, _ = material.line_of(end - 1)
        chunks.append(Chunk(len(chunks), start, end, first, last, sha256(raw[start:end])))
        start = end
    return tuple(chunks)


def split_range(material, start, end, chunk_bytes=CHUNK_BYTES):
    """Deterministic sub-ranges of [start, end) of at most chunk_bytes, same cutting rules as chunk_plan."""
    raw = material.raw
    if (type(start) is not int or type(end) is not int or isinstance(start, bool) or isinstance(end, bool)
            or not 0 <= start < end <= material.size or not _boundary(raw, start) or not _boundary(raw, end)):
        raise MaterialError("RANGE_INVALID")
    ranges, position = [], start
    while position < end:
        stop = min(position + chunk_bytes, end)
        if stop < end:
            newline = raw.rfind(b"\n", position + chunk_bytes // 2, stop)
            if newline != -1:
                stop = newline + 1
            else:
                while stop > position and _is_continuation(raw[stop]):
                    stop -= 1
        if stop <= position:
            raise MaterialError("RANGE_INVALID")
        ranges.append((position, stop))
        position = stop
    return tuple(ranges)


def line_range(material, first, last):
    """Byte range [start, end) covering 1-based lines first..last (inclusive)."""
    count = len(material.line_starts)
    if (type(first) is not int or type(last) is not int or isinstance(first, bool) or isinstance(last, bool)
            or not 1 <= first <= last <= count):
        raise MaterialError("RANGE_INVALID")
    end = material.line_starts[last] if last < count else material.size
    start = material.line_starts[first - 1]
    if start >= end:                    # e.g. the empty "line" after a final newline
        raise MaterialError("RANGE_INVALID")
    return start, end


@dataclass(frozen=True)
class SourceRef:
    material_id: str
    version: str
    start: int
    end: int
    fragment_sha256: str

    def label(self):
        return "%s:%d-%d" % (self.material_id[:8], self.start, self.end)

    def to_json(self):
        return {"material": self.material_id, "version": self.version, "start": self.start,
                "end": self.end, "fragment": self.fragment_sha256}


def parse_ref(value):
    """Strict schema for a reference coming back from a model."""
    keys = {"material", "version", "start", "end", "fragment"}
    if (type(value) is not dict or set(value) != keys or type(value["material"]) is not str
            or not _ID.fullmatch(value["material"]) or type(value["version"]) is not str
            or not _HEX64.fullmatch(value["version"]) or type(value["fragment"]) is not str
            or not _HEX64.fullmatch(value["fragment"])
            or any(type(value[k]) is not int or isinstance(value[k], bool) or value[k] < 0
                   for k in ("start", "end")) or value["start"] >= value["end"]):
        raise MaterialError("REF_INVALID")
    return SourceRef(value["material"], value["version"], value["start"], value["end"], value["fragment"])


@dataclass(frozen=True)
class Fragment:
    ref: SourceRef
    text: str


def read_range(material, version, start, end, max_bytes=CHUNK_BYTES):
    """Exact fragment of the pinned version; refuses stale, split-character or oversize ranges."""
    if version != material.sha256:
        raise MaterialError("SOURCE_CHANGED")
    if (type(start) is not int or type(end) is not int or isinstance(start, bool) or isinstance(end, bool)
            or not 0 <= start < end <= material.size or end - start > max_bytes
            or not _boundary(material.raw, start) or not _boundary(material.raw, end)):
        raise MaterialError("RANGE_INVALID")
    piece = material.raw[start:end]
    ref = SourceRef(material.material_id, material.sha256, start, end, sha256(piece))
    return Fragment(ref, piece.decode("utf-8"))


def check_ref(material, ref, delivered):
    """A reference is valid only if the controller itself delivered exactly it."""
    if (ref.material_id != material.material_id or ref.version != material.sha256
            or ref not in delivered):
        raise MaterialError("REF_INVALID")
    fragment = read_range(material, ref.version, ref.start, ref.end, max_bytes=material.size)
    if fragment.ref != ref:
        raise MaterialError("REF_INVALID")
    return fragment


@dataclass(frozen=True)
class Hit:
    start: int
    end: int
    line: int
    column: int
    context: tuple  # (start, end) window to read with read_range


@dataclass(frozen=True)
class SearchPage:
    hits: tuple
    next_cursor: int
    complete: bool  # True only when the scan reached the end of the material


def _widen(raw, offset, direction):
    while 0 < offset < len(raw) and _is_continuation(raw[offset]):
        offset += direction
    return offset


def search_text(material, needle, cursor=0, max_hits=MAX_HITS_PER_PAGE):
    """Literal, case-sensitive search over the WHOLE material (chunk borders do not hide hits)."""
    if (type(needle) is not str or not needle or any(c not in _ALLOWED_CONTROLS and unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in needle)):
        raise MaterialError("NEEDLE_INVALID")
    try:
        pattern = needle.encode("utf-8")
    except UnicodeEncodeError:
        raise MaterialError("NEEDLE_INVALID") from None
    if (len(pattern) > MAX_NEEDLE_BYTES or type(cursor) is not int or not 0 <= cursor <= material.size
            or type(max_hits) is not int or not 1 <= max_hits <= MAX_HITS_PER_PAGE):
        raise MaterialError("NEEDLE_INVALID")
    raw, hits, position = material.raw, [], cursor
    while len(hits) < max_hits:
        found = raw.find(pattern, position)
        if found == -1:
            return SearchPage(tuple(hits), material.size, True)
        end = found + len(pattern)
        line, column = material.line_of(found)
        lo = _widen(raw, max(0, found - CONTEXT_BYTES), -1)
        hi = _widen(raw, min(material.size, end + CONTEXT_BYTES), 1)
        hits.append(Hit(found, end, line, column, (lo, hi)))
        position = end
    return SearchPage(tuple(hits), position, False)


class Coverage:
    """Which planned chunks the controller really analysed; the model cannot mark any."""

    def __init__(self, plan):
        self._plan = tuple(plan)
        self._done = set()

    def mark(self, index):
        if type(index) is not int or not 0 <= index < len(self._plan):
            raise MaterialError("RANGE_INVALID")
        self._done.add(index)

    @property
    def complete(self):
        return len(self._done) == len(self._plan)

    def missing(self):
        return tuple((c.start, c.end) for c in self._plan if c.index not in self._done)

    def covered_bytes(self):
        return sum(c.end - c.start for c in self._plan if c.index in self._done)
