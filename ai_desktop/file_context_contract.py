"""Pure bounded data contract for selected read-only files, without host access."""
import re
import unicodedata


MAX_FILES = 64                 # defaults (the owner may change them in permissions.toml, see permissions.Limits)
MAX_FILE_BYTES = 65536
MAX_TOTAL_BYTES = 131072
CEILING_FILES = 128            # hard ceilings of the wire schema; the client enforces the owner's (lower or equal) budget
CEILING_FILE_BYTES = 131072
CEILING_TOTAL_BYTES = 262144
MAX_PATH_BYTES = 512
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|PuTTY-User-Key-File-",
                          re.IGNORECASE)


def contains_private_key(value):
    """Recognize known private-key envelopes; this is not a general secret scanner."""
    return bool(_PRIVATE_KEY.search(value))


def validate_path(value):
    if (type(value) is not str or not value.startswith("/") or value == "/"
            or any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value)):
        raise ValueError("files-path")
    if (len(value.encode("utf-8")) > MAX_PATH_BYTES
            or any(part in ("", ".", "..") for part in value.split("/")[1:])):
        raise ValueError("files-path")
    return value


def validate_text(value):
    if (type(value) is not str
            or any(char not in "\n\t" and unicodedata.category(char) in ("Cc", "Cf", "Cs")
                   for char in value)):
        raise ValueError("files-text")
    if len(value.encode("utf-8")) > CEILING_FILE_BYTES:
        raise ValueError("files-text")
    return value


def validate_context(value):
    """Return a detached copy; paths are labels, never authority to read or write."""
    if (type(value) is not dict or set(value) != {"kind", "files"} or value["kind"] != "files"
            or type(value["files"]) is not list or not 1 <= len(value["files"]) <= CEILING_FILES):
        raise ValueError("files-context")
    result, seen, total = [], set(), 0
    for item in value["files"]:
        if type(item) is not dict or set(item) != {"path", "content"}:
            raise ValueError("files-item")
        path, content = validate_path(item["path"]), validate_text(item["content"])
        if path in seen:
            raise ValueError("files-duplicate")
        seen.add(path)
        total += len(content.encode("utf-8"))
        if total > CEILING_TOTAL_BYTES:
            raise ValueError("files-total")
        result.append({"path": path, "content": content})
    return {"kind": "files", "files": result}
