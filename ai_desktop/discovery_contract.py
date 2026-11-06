"""Closed, bounded shape of the local discovery observations sent to the model during one question.

List/find discovery is metadata only; explicit content operations add bounded,
source-hashed excerpts. Names are untrusted observations, never source
authority; every content read still goes through the normal file-policy boundary. The client and the worker both
validate this exact shape against the ceilings below (the owner's budgets in permissions.toml can only be lower or equal).
"""
import json
import re
import unicodedata

MAX_SKILL_ROUNDS = 3
MAX_REUSED_SKILLS = 4
MAX_STEPS = 8 + 4 + 32 + MAX_SKILL_ROUNDS * 4 + MAX_REUSED_SKILLS
MAX_DIRECTORIES = 4            # directories in one list step
CEILING_ENTRIES = 512          # entries of one list/find step
CEILING_TOTAL_ENTRIES = 1024   # entries of the whole document
CEILING_BYTES = 262144 + 192 * 1024  # diagnostics plus separately budgeted procedures/references
MAX_QUERIES = 4
MIN_QUERY_CHARS = 2
MAX_QUERY_BYTES = 64
MAX_PATH_BYTES = 512
MAX_SELECTORS = 4
KINDS = ("any", "file", "directory")
ENTRY_TYPES = ("file", "directory", "alias")
RUN_STATUSES = ("ok", "failed", "timeout", "declined", "not-allowed", "unavailable", "missing")
RUN_ROLES = ("read", "change", "test", "undo")
CEILING_OUTPUT = 131072
# Recoverable selection problems: the model may correct its plan (once per correction budget). Security refusals
# (denied, changed, owner, permission, private-key, policy) are never listed here: they stop the operation.
REFUSAL_CODES = ("discovery-required", "discovery-budget", "files-missing", "files-count", "files-total", "files-size", "files-empty",
                 "files-depth", "files-entries", "files-type", "files-symlink")
_ID = re.compile(r"f(?:[0-9]|[1-5][0-9]|6[0-3])\Z", re.ASCII)
_SKILL = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)?\Z", re.ASCII)
MAX_SKILL_TEXT = 96 * 1024


def clean_path(value, *, empty=False, wildcard=False):
    """A scope-relative path: no absolute, home, variable, backslash or dot segments; optional terminal /** or /*."""
    if type(value) is not str or (not value and not empty):
        raise ValueError("discovery")
    body = value
    if wildcard:
        for suffix in ("/**", "/*"):
            if body.endswith(suffix):
                body = body[:-len(suffix)]
                break
    if (len(value.encode("utf-8")) > MAX_PATH_BYTES or value.startswith("/")
            or any(char in body for char in "~$\\?[]{}*")
            or any(unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp") for char in value)
            or body and any(part in ("", ".", "..") for part in body.split("/"))):
        raise ValueError("discovery")
    return value


def clean_query(value):
    if (type(value) is not str or len(value.strip()) < MIN_QUERY_CHARS or value != value.strip()
            or len(value.encode("utf-8")) > MAX_QUERY_BYTES or "/" in value
            or any(char in value for char in "*?[]{}\\")
            or any(unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp") for char in value)):
        raise ValueError("discovery")
    return value


def _entry(value):
    if type(value) is not dict or set(value) not in ({"path", "type", "size"}, {"path", "type", "size", "target"}):
        raise ValueError("discovery")
    kind, size = value["type"], value["size"]
    if type(kind) is not str or kind not in ENTRY_TYPES:
        raise ValueError("discovery")
    if kind == "file":
        if type(size) is not int or isinstance(size, bool) or not 0 <= size < 2**63 or "target" in value:
            raise ValueError("discovery")
    elif size is not None or (kind == "alias") != ("target" in value):
        raise ValueError("discovery")
    result = {"path": clean_path(value["path"]), "type": kind, "size": size}
    if kind == "alias":
        result["target"] = clean_path(value["target"])
    return result


def _entries(values, total):
    if type(values) is not list or len(values) > CEILING_ENTRIES:
        raise ValueError("discovery")
    seen, result = set(), []
    for item in values:
        entry = _entry(item)
        if entry["path"] in seen:
            raise ValueError("discovery")
        seen.add(entry["path"])
        result.append(entry)
    total[0] += len(result)
    return result


def _identifier(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError("discovery")
    return value


def _step(value, total):
    if type(value) is not dict or type(value.get("op")) is not str:
        raise ValueError("discovery")
    op = value["op"]
    if op == "list":
        if (set(value) != {"op", "directories", "truncated"} or type(value["truncated"]) is not bool
                or type(value["directories"]) is not list or not 1 <= len(value["directories"]) <= MAX_DIRECTORIES):
            raise ValueError("discovery")
        directories, keys = [], set()
        for directory in value["directories"]:
            if (type(directory) is not dict or set(directory) != {"id", "path", "entries", "truncated", "next"}
                    or type(directory["truncated"]) is not bool):
                raise ValueError("discovery")
            ident, path = _identifier(directory["id"]), clean_path(directory["path"], empty=True)
            if (ident, path) in keys:
                raise ValueError("discovery")
            keys.add((ident, path))
            following = directory["next"]
            if following is not None:
                clean_path(following)
                if not directory["truncated"]:
                    raise ValueError("discovery")
            if directory["truncated"] and not value["truncated"]:
                raise ValueError("discovery")
            directories.append({"id": ident, "path": path, "entries": _entries(directory["entries"], total),
                                "truncated": directory["truncated"], "next": following})
        return {"op": "list", "directories": directories, "truncated": value["truncated"]}
    if op == "find":
        if (set(value) != {"op", "id", "path", "queries", "kind", "matches", "truncated", "incomplete"}
                or type(value["truncated"]) is not bool or type(value["incomplete"]) is not bool
                or type(value["kind"]) is not str or value["kind"] not in KINDS
                or type(value["queries"]) is not list or not 1 <= len(value["queries"]) <= MAX_QUERIES):
            raise ValueError("discovery")
        queries = [clean_query(item) for item in value["queries"]]
        if len({item.casefold() for item in queries}) != len(queries):
            raise ValueError("discovery")
        return {"op": "find", "id": _identifier(value["id"]), "path": clean_path(value["path"], empty=True),
                "queries": queries, "kind": value["kind"], "matches": _entries(value["matches"], total),
                "truncated": value["truncated"], "incomplete": value["incomplete"]}
    if op == "run":
        from .command_ops import clean_argv
        role = value.get("role", "read")
        if (set(value) - {"role"} != {"op", "argv", "status", "exit", "output", "truncated"}
                or type(value["truncated"]) is not bool or type(role) is not str or role not in RUN_ROLES
                or type(value["status"]) is not str or value["status"] not in RUN_STATUSES):
            raise ValueError("discovery")
        code, output = value["exit"], value["output"]
        if code is not None and (type(code) is not int or isinstance(code, bool) or not -255 <= code <= 255):
            raise ValueError("discovery")
        if (type(output) is not str or len(output.encode("utf-8")) > CEILING_OUTPUT
                or any(char not in "\n\t" and unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp")
                       for char in output)):
            raise ValueError("discovery")
        step = {"op": "run", "argv": clean_argv(value["argv"]), "status": value["status"], "exit": code,
                "output": output, "truncated": value["truncated"]}
        if "role" in value:
            step["role"] = role
        return step
    if op == "content":
        from .ask_content import validate_step
        return validate_step(value)
    if op == "skill":
        if (set(value) != {"op", "name", "status", "text"} or type(value["name"]) is not str
                or not _SKILL.fullmatch(value["name"]) or value["status"] not in ("ok", "missing")
                or type(value["text"]) is not str or len(value["text"].encode("utf-8")) > MAX_SKILL_TEXT
                or (value["status"] == "missing" and value["text"])
                or any(char not in "\n\t" and unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value["text"])):
            raise ValueError("discovery")
        return dict(value)
    if op == "refusal":
        if (set(value) != {"op", "code", "selectors", "limit"} or type(value["code"]) is not str
                or value["code"] not in REFUSAL_CODES or type(value["selectors"]) is not list
                or len(value["selectors"]) > MAX_SELECTORS):
            raise ValueError("discovery")
        limit = value["limit"]
        if limit is not None and (type(limit) is not int or isinstance(limit, bool) or not 0 <= limit < 2**31):
            raise ValueError("discovery")
        selectors = []
        for item in value["selectors"]:
            if type(item) is not dict or set(item) != {"id", "path"}:
                raise ValueError("discovery")
            selectors.append({"id": _identifier(item["id"]), "path": clean_path(item["path"], empty=True, wildcard=True)})
        return {"op": "refusal", "code": value["code"], "selectors": selectors, "limit": limit}
    raise ValueError("discovery")


def validate_discovery(value):
    """Copy a closed document without I/O or interpretation of names."""
    try:
        if (type(value) is not dict or set(value) != {"steps", "remaining"} or type(value["steps"]) is not list
                or not 1 <= len(value["steps"]) <= MAX_STEPS
                or type(value["remaining"]) is not dict or set(value["remaining"]) != {"rounds", "corrections", "commands"}):
            raise ValueError()
        remaining = value["remaining"]
        for name, ceiling in (("rounds", 8), ("corrections", 4), ("commands", 32)):
            if (type(remaining[name]) is not int or isinstance(remaining[name], bool)
                    or not 0 <= remaining[name] <= ceiling):
                raise ValueError()
        total = [0]
        steps = [_step(item, total) for item in value["steps"]]
        if total[0] > CEILING_TOTAL_ENTRIES:
            raise ValueError()
        result = {"steps": steps, "remaining": {"rounds": remaining["rounds"], "corrections": remaining["corrections"],
                                   "commands": remaining["commands"]}}
        if len(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")) > CEILING_BYTES:
            raise ValueError()
        return result
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ValueError("discovery") from None


def document_bytes(document):
    return len(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8"))


def scope_ids(document):
    """Every scope id a validated document mentions (directories, finds, refused selectors)."""
    found = set()
    for step in document["steps"]:
        if step["op"] == "list":
            found.update(item["id"] for item in step["directories"])
        elif step["op"] in ("find", "content"):
            found.add(step["id"])
        elif step["op"] in ("run", "skill"):
            continue
        else:
            found.update(item["id"] for item in step["selectors"])
    return found
