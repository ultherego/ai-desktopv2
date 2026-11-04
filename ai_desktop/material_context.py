"""Strict schema of the `material` context item sent inside a `question` request.

Both the client and the installed worker validate the same item, so a
fragment must match its own reference: same length, same SHA-256, valid text.
References prove provenance, never that an interpretation is correct.
"""
from . import materials

MAX_FRAGMENTS = 8
MAX_DATA_BYTES = 256 * 1024
MAX_PURPOSE_BYTES = 512


def validate_context(item):
    keys = {"kind", "material", "purpose", "fragments"}
    if type(item) is not dict or set(item) != keys or item["kind"] != "material":
        raise ValueError("material context")
    meta = item["material"]
    if (type(meta) is not dict or set(meta) != {"id", "version", "size"}
            or type(meta["id"]) is not str or type(meta["version"]) is not str
            or type(meta["size"]) is not int or isinstance(meta["size"], bool)
            or not 1 <= meta["size"] <= materials.MAX_PROJECT_FILE_BYTES):
        raise ValueError("material context")
    purpose = item["purpose"]
    if type(purpose) is not str or not purpose.strip() or len(purpose.encode("utf-8")) > MAX_PURPOSE_BYTES:
        raise ValueError("material context")
    fragments = item["fragments"]
    if type(fragments) is not list or not 1 <= len(fragments) <= MAX_FRAGMENTS:
        raise ValueError("material context")
    total, seen = 0, set()
    for fragment in fragments:
        if type(fragment) is not dict or set(fragment) != {"ref", "text"} or type(fragment["text"]) is not str:
            raise ValueError("material context")
        try:
            ref = materials.parse_ref(fragment["ref"])
        except materials.MaterialError:
            raise ValueError("material context") from None
        raw = fragment["text"].encode("utf-8")
        if (ref.material_id != meta["id"] or ref.version != meta["version"] or ref.end > meta["size"]
                or len(raw) != ref.end - ref.start or len(raw) > materials.CHUNK_BYTES
                or materials.sha256(raw) != ref.fragment_sha256 or (ref.start, ref.end) in seen):
            raise ValueError("material context")
        seen.add((ref.start, ref.end))
        total += len(raw)
    if total > MAX_DATA_BYTES:
        raise ValueError("material context")
    return item
