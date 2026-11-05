"""Pure finite planner data; a model proposal never grants local authority."""
import json
import re
import unicodedata

from .task_contract import CHECKS


MAX_ACCESS_BYTES = 8192
MAX_PLAN_BYTES = 16384
MAX_COMMANDS_IN_PLAN = 16
MAX_DELETE_FILES = 8           # workspace_trash.MAX_DELETE
MAX_FILE_SCOPES = 64
MAX_SELECTED_FILES = 8
MAX_LISTED_SCOPES = 4
MAX_CREATE_FILES = 32
MAX_PATTERN_BYTES = 512
SYSTEMD_SCOPES = frozenset({"system", "user"})
SYSTEMD_VIEWS = frozenset({"enabled", "active", "inactive", "failed", "all", "installed"})
_FILE_ID = re.compile(r"f(?:[0-9]|[1-5][0-9]|6[0-3])\Z", re.ASCII)
_WRITE_ID = re.compile(r"w(?:[0-9]|[1-5][0-9]|6[0-3])\Z", re.ASCII)
EDIT_CHECKS = frozenset(CHECKS)
PLAN_CHECKS = frozenset({"access", "type", "size", "json", "shape", "action", "files", "systemd",
                         "weather", "write", "check", "create", "text", "empty", "command", "invalid"})


class PlanError(ValueError):
    """A rejected model plan, with a finite check and no native error prose."""
    def __init__(self, check="invalid"):
        self.check = check if type(check) is str and check in PLAN_CHECKS else "invalid"
        super().__init__("assistant-plan")


def _text(value, limit, *, multiline=False):
    if (type(value) is not str or not value.strip()
            or any(not (multiline and char in "\n\t")
                   and unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value)
            or len(value.encode("utf-8")) > limit):
        raise ValueError("assistant-text")
    return value


def validate_access(value):
    """Copy only configured labels and finite source capabilities, without I/O."""
    if (type(value) is not dict or not {"files", "systemd", "weather"} <= set(value)
            or set(value) - {"files", "systemd", "weather", "write", "web", "commands", "change_commands"}
            or type(value["files"]) is not list or len(value["files"]) > MAX_FILE_SCOPES
            or type(value["systemd"]) is not list or len(value["systemd"]) > 2
            or type(value["weather"]) is not bool
            or "web" in value and type(value["web"]) is not bool):
        raise ValueError("assistant-access")
    files, seen = [], set()
    for item in value["files"]:
        if (type(item) is not dict or set(item) != {"id", "pattern"}
                or type(item["id"]) is not str or not _FILE_ID.fullmatch(item["id"])
                or item["id"] in seen):
            raise ValueError("assistant-access")
        seen.add(item["id"])
        files.append({"id": item["id"], "pattern": _text(item["pattern"], MAX_PATTERN_BYTES)})
    scopes = []
    for scope in value["systemd"]:
        if type(scope) is not str or scope not in SYSTEMD_SCOPES or scope in scopes:
            raise ValueError("assistant-access")
        scopes.append(scope)
    result = {"files": files, "systemd": scopes, "weather": value["weather"]}
    if "web" in value:
        result["web"] = value["web"]
    if "commands" in value:
        if (type(value["commands"]) is not list or not 1 <= len(value["commands"]) <= 128
                or any(type(item) is not str for item in value["commands"])
                or len(set(value["commands"])) != len(value["commands"])):
            raise ValueError("assistant-access")
        result["commands"] = [_text(item, 120) for item in value["commands"]]
    if "change_commands" in value:
        if ("commands" not in result or type(value["change_commands"]) is not list
                or not 1 <= len(value["change_commands"]) <= 128
                or any(type(item) is not str for item in value["change_commands"])
                or len(set(value["change_commands"])) != len(value["change_commands"])):
            raise ValueError("assistant-access")
        result["change_commands"] = [_text(item, 120) for item in value["change_commands"]]
    if "write" in value:
        if type(value["write"]) is not list or not 1 <= len(value["write"]) <= MAX_FILE_SCOPES:
            raise ValueError("assistant-access")
        writes, seen = [], set()
        for item in value["write"]:
            if (type(item) is not dict or set(item) != {"id", "pattern"}
                    or type(item["id"]) is not str or not _WRITE_ID.fullmatch(item["id"])
                    or item["id"] in seen):
                raise ValueError("assistant-access")
            seen.add(item["id"])
            writes.append({"id": item["id"], "pattern": _text(item["pattern"], MAX_PATTERN_BYTES)})
        result["write"] = writes
    if len(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")) > MAX_ACCESS_BYTES:
        raise ValueError("assistant-access")
    return result


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("assistant-plan")
        value[key] = item
    return value


def _constant(_):
    raise ValueError("assistant-plan")


def relative_selection(value):
    """Literal relative path, optionally ending in /* or /**; no expansion."""
    value = _text(value, MAX_PATTERN_BYTES)
    base = value[:-3] if value.endswith("/**") else value[:-2] if value.endswith("/*") else value
    if (not base or base.startswith("/") or any(char in base for char in "$~\\?[]{}*")
            or any(part in ("", ".", "..") for part in base.split("/"))):
        raise ValueError("assistant-plan")
    return value


def file_selections(value, access=None):
    """Copy bounded selectors without resolving paths or granting any access."""
    if type(value) not in (list, tuple) or len(value) > MAX_SELECTED_FILES:
        raise ValueError("assistant-plan")
    allowed = None if access is None else {item["id"] for item in validate_access(access)["files"]}
    selected, seen = [], set()
    for item in value:
        if type(item) is str:
            file_id, relative = item, None
            result = item
        elif type(item) is dict and set(item) == {"id", "path"}:
            file_id, relative = item["id"], relative_selection(item["path"])
            result = {"id": file_id, "path": relative}
        else:
            raise ValueError("assistant-plan")
        if (type(file_id) is not str or not _FILE_ID.fullmatch(file_id)
                or allowed is not None and file_id not in allowed
                or (file_id, relative) in seen):
            raise ValueError("assistant-plan")
        seen.add((file_id, relative))
        selected.append(result)
    return selected


def selected_file_ids(value):
    """Collapse scope identities only after validating every distinct selector."""
    return tuple(dict.fromkeys(item if type(item) is str else item["id"]
                               for item in file_selections(value)))


_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def parse_plan(raw, access):
    """Accept one bounded answer or one proposed read within advertised access."""
    check = "access"
    try:
        access = validate_access(access)
        check = "type"
        if type(raw) is not str:
            raise ValueError("assistant-plan")
        check = "size"
        if not 0 < len(raw.encode("utf-8")) <= MAX_PLAN_BYTES:
            raise ValueError("assistant-plan")
        check = "json"
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant)
        check = "shape"
        if type(value) is not dict:
            raise ValueError("assistant-plan")
        check = "action"
        if value.get("action") == "web":
            check = "shape"
            if set(value) != {"action"}:
                raise ValueError("assistant-plan")
            check = "access"
            if access.get("web") is not True:
                raise ValueError("assistant-plan")
            return {"action": "web"}
        if value.get("action") == "skill":
            check = "shape"
            names = value.get("names")
            if (set(value) != {"action", "names"} or type(names) is not list or not 1 <= len(names) <= 4
                    or any(type(name) is not str or not _SKILL_NAME.fullmatch(name) for name in names)
                    or len(set(names)) != len(names)):
                raise ValueError("assistant-plan")
            return {"action": "skill", "names": names}
        if value.get("action") == "answer":
            check = "shape"
            if set(value) != {"action", "text"}:
                raise ValueError("assistant-plan")
            check = "text"
            return {"action": "answer", "text": _text(value["text"], MAX_PLAN_BYTES, multiline=True)}
        if value.get("action") == "run":
            from .command_ops import clean_argv
            check = "shape"
            if set(value) not in ({"action", "argv"}, {"action", "argv", "test", "undo"}, {"action", "commands"}):
                raise ValueError("assistant-plan")
            check = "access"
            if not access.get("commands"):
                raise ValueError("assistant-plan")
            check = "command"
            if "commands" in value:
                if type(value["commands"]) is not list or not 1 <= len(value["commands"]) <= MAX_COMMANDS_IN_PLAN:
                    raise ValueError("assistant-plan")
                return {"action": "run", "commands": [clean_argv(words) for words in value["commands"]]}
            plan = {"action": "run", "argv": clean_argv(value["argv"])}
            if "test" in value:
                if not access.get("change_commands"):
                    raise ValueError("assistant-plan")       # test and undo exist only for a change command
                plan["test"], plan["undo"] = clean_argv(value["test"]), clean_argv(value["undo"])
            return plan
        if value.get("action") in ("list", "find"):
            from .discovery_contract import KINDS, MAX_QUERIES, clean_path, clean_query
            finding = value["action"] == "find"
            check = "shape"
            expected = {"action", "files", "queries", "kind"} if finding else {"action", "files"}
            if not finding and "after" in value:
                expected = expected | {"after"}
            if set(value) != expected:
                raise ValueError("assistant-plan")
            check = "files"
            if (type(value["files"]) is not list
                    or not 1 <= len(value["files"]) <= (1 if finding else MAX_LISTED_SCOPES)):
                raise ValueError("assistant-plan")
            selected = file_selections(value["files"], access)
            patterns = {item["id"]: item["pattern"] for item in access["files"]}
            for selection in selected:
                file_id = selection if type(selection) is str else selection["id"]
                # Discovery names a directory, not a wildcard expansion or an exact-file scope.
                # Its size, paging and search cost limits are controller-owned (permissions.toml).
                if (not patterns[file_id].endswith(("/", "/*", "/**"))
                        or type(selection) is dict and "*" in selection["path"]):
                    raise ValueError("assistant-plan")
            if finding:
                if (type(value["queries"]) is not list or not 1 <= len(value["queries"]) <= MAX_QUERIES
                        or type(value["kind"]) is not str or value["kind"] not in KINDS):
                    raise ValueError("assistant-plan")
                queries = list({item.casefold(): item for item in reversed([clean_query(i) for i in value["queries"]])}.values())[::-1]
                return {"action": "find", "files": selected, "queries": queries, "kind": value["kind"]}
            result = {"action": "list", "files": selected}
            if "after" in value:
                if len(selected) != 1:
                    raise ValueError("assistant-plan")
                result["after"] = clean_path(value["after"])
            return result
        if value.get("action") == "delete":
            from .task_contract import path
            check = "shape"
            if set(value) != {"action", "write", "paths"}:
                raise ValueError("assistant-plan")
            check = "write"
            if (type(value["write"]) is not str
                    or value["write"] not in {item["id"] for item in access.get("write", [])}):
                raise ValueError("assistant-plan")
            check = "create"
            if type(value["paths"]) is not list or not 1 <= len(value["paths"]) <= MAX_DELETE_FILES:
                raise ValueError("assistant-plan")
            paths = [path(item) for item in value["paths"]]
            if len(set(paths)) != len(paths):
                raise ValueError("assistant-plan")
            return {"action": "delete", "write": value["write"], "paths": paths}
        if value.get("action") == "edit":
            from .task_contract import path
            check = "shape"
            if set(value) != {"action", "write", "files", "create", "check"}:
                raise ValueError("assistant-plan")
            check = "write"
            if (type(value["write"]) is not str
                    or value["write"] not in {item["id"] for item in access.get("write", [])}):
                raise ValueError("assistant-plan")
            check = "check"
            if type(value["check"]) is not str or value["check"] not in EDIT_CHECKS:
                raise ValueError("assistant-plan")
            check = "create"
            if type(value["create"]) is not list or len(value["create"]) > MAX_CREATE_FILES:
                raise ValueError("assistant-plan")
            check = "files"
            files = file_selections(value["files"], access)
            check = "create"
            created = [path(item) for item in value["create"]]
            if len(set(created)) != len(created) or not (files or created):
                raise ValueError("assistant-plan")
            return {"action": "edit", "write": value["write"], "files": files,
                    "create": created, "check": value["check"]}
        if value.get("action") != "read" or set(value) != {"action", "files", "systemd", "weather"}:
            check = "shape" if value.get("action") == "read" else "action"
            raise ValueError("assistant-plan")
        check = "shape"
        if (type(value["files"]) is not list or len(value["files"]) > MAX_SELECTED_FILES
                or type(value["systemd"]) is not list or len(value["systemd"]) > 2):
            raise ValueError("assistant-plan")
        check = "files"
        selected_files = file_selections(value["files"], access)
        check = "systemd"
        selected_systemd, scopes = [], set()
        for item in value["systemd"]:
            if (type(item) is not dict or set(item) != {"scope", "view"}
                    or type(item["scope"]) is not str or item["scope"] not in access["systemd"]
                    or item["scope"] in scopes or type(item["view"]) is not str
                    or item["view"] not in SYSTEMD_VIEWS):
                raise ValueError("assistant-plan")
            scopes.add(item["scope"])
            selected_systemd.append({"scope": item["scope"], "view": item["view"]})
        city = value["weather"]
        check = "weather"
        if city is not None:
            if not access["weather"] or len(_text(city, 120).strip()) < 2:
                raise ValueError("assistant-plan")
        if not selected_files and not selected_systemd and city is None:
            check = "empty"
            raise ValueError("assistant-plan")
        return {"action": "read", "files": selected_files, "systemd": selected_systemd, "weather": city}
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise PlanError(check) from None
