"""Bounded, fixed systemd inventory for an explicitly selected ask context.

Only names and states leave this collector. Unit contents, command lines,
descriptions, journal messages and arbitrary user-supplied commands are excluded.
The result states whether it describes installed unit files or loaded units.
"""
from datetime import datetime, timezone
import json
import os
import re

from .analysis_contract import AnalysisDenied
from .process_io import bounded_process


SCOPES = ("system", "user")
VIEWS = ("enabled", "active", "inactive", "failed", "all", "installed")
MAX_ITEMS = 128
# Two scopes plus a weather observation must fit the shared consent envelope.
MAX_CONTEXT_BYTES = 10240
MAX_OUTPUT_BYTES = 262144
TIMEOUT_SECONDS = 5
STATUSES = frozenset({"ok", "unavailable", "timeout", "output-limit", "invalid"})
FILE_STATES = frozenset({"enabled", "enabled-runtime", "linked", "linked-runtime",
                         "alias", "masked", "masked-runtime", "static", "disabled",
                         "indirect", "generated", "transient", "bad"})
UNIT_STATES = frozenset({"active", "reloading", "inactive", "failed", "activating",
                         "deactivating", "maintenance", "refreshing"})
LOAD_STATES = frozenset({"loaded", "not-found", "bad-setting", "error", "masked", "merged", "stub"})
UNIT_NAME = re.compile(r"(?:[A-Za-z0-9:_.@-]|\\x[0-9a-fA-F]{2})+\.(?:service|socket|target|device|mount|automount|swap|timer|path|slice|scope)\Z", re.ASCII)
SUBSTATE = re.compile(r"[a-z][a-z0-9-]{0,39}\Z", re.ASCII)
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z", re.ASCII)


def _encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8"))


def listing_for(view):
    if type(view) is not str or view not in VIEWS:
        raise ValueError("systemd view")
    return "unit-files" if view in ("enabled", "installed") else "units"


def command(scope, view):
    if type(scope) is not str or scope not in SCOPES:
        raise ValueError("systemd scope")
    listing = listing_for(view)
    argv = ["/usr/bin/systemctl", "--" + scope, "--no-pager", "--no-legend",
            "--plain", "--full", "--no-ask-password"]
    if listing == "unit-files":
        argv += ["list-unit-files"]
        if view == "enabled":
            argv += ["--state=enabled,enabled-runtime"]
    else:
        argv += ["list-units", "--all"]
        if view in ("active", "inactive", "failed"):
            argv += ["--state=" + view]
    return argv


def _environment(scope, uid):
    # Construct rather than filter. In particular, do not inherit pager,
    # SYSTEMD_UNIT_PATH, remote host selectors or arbitrary bus addresses.
    env = {"PATH": "/usr/bin", "LANG": "C", "LC_ALL": "C", "SYSTEMD_COLORS": "0",
           "SYSTEMD_URLIFY": "0", "SYSTEMD_PAGER": "", "SYSTEMD_PAGERSECURE": "1"}
    if scope == "user":
        runtime = "/run/user/" + str(uid)
        env["XDG_RUNTIME_DIR"] = runtime
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + runtime + "/bus"
    return env


def _valid_name(name):
    return type(name) is str and len(name) <= 255 and UNIT_NAME.fullmatch(name) is not None


def _parse(raw, view):
    if type(raw) is not bytes or len(raw) > MAX_OUTPUT_BYTES:
        raise ValueError("systemd output")
    rows, seen = [], set()
    listing = listing_for(view)
    for line in raw.decode("utf-8", errors="strict").splitlines():
        if not line.strip():
            continue
        # --full and --plain prevent truncated names and colored state glyphs.
        # Descriptions after the four fixed unit columns are never retained.
        fields = line.split(None, 4 if listing == "units" else 2)
        if listing == "unit-files":
            if len(fields) not in (2, 3):
                raise ValueError("unit file fields")
            name, state = fields[:2]
            substate = None
            if state not in FILE_STATES or (view == "enabled" and state not in ("enabled", "enabled-runtime")):
                raise ValueError("unit file state")
        else:
            if len(fields) < 4 or fields[1] not in LOAD_STATES:
                raise ValueError("unit fields")
            name, _, state, substate = fields[:4]
            if (state not in UNIT_STATES or SUBSTATE.fullmatch(substate) is None
                    or (view in ("active", "inactive", "failed") and state != view)):
                raise ValueError("unit state")
        if not _valid_name(name) or name in seen:
            raise ValueError("unit name")
        seen.add(name)
        rows.append({"name": name, "state": state, "substate": substate})
    return sorted(rows, key=lambda item: item["name"])


def validate_context(value):
    """Validate the exact wire shape without collecting or contacting a bus."""
    keys = {"kind", "source", "scope", "view", "listing", "observed_at", "status",
            "truncated", "total", "items"}
    if (type(value) is not dict or set(value) != keys or value["kind"] != "systemd"
            or value["source"] != "systemctl" or type(value["scope"]) is not str
            or value["scope"] not in SCOPES or value["listing"] != listing_for(value["view"])
            or type(value["status"]) is not str or value["status"] not in STATUSES
            or type(value["observed_at"]) is not str or not TIMESTAMP.fullmatch(value["observed_at"])
            or type(value["truncated"]) is not bool or type(value["items"]) is not list
            or len(value["items"]) > MAX_ITEMS):
        raise ValueError("systemd context")
    datetime.strptime(value["observed_at"], "%Y-%m-%dT%H:%M:%SZ")
    items, total = value["items"], value["total"]
    if value["status"] == "ok":
        if (type(total) is not int or not len(items) <= total <= MAX_OUTPUT_BYTES
                or value["truncated"] != (len(items) < total)):
            raise ValueError("systemd count")
    elif total is not None or items or value["truncated"]:
        raise ValueError("systemd failure")
    seen = set()
    for item in items:
        if (type(item) is not dict or set(item) != {"name", "state", "substate"}
                or not _valid_name(item["name"]) or item["name"] in seen
                or type(item["state"]) is not str):
            raise ValueError("systemd item")
        if value["listing"] == "unit-files":
            if (item["state"] not in FILE_STATES or item["substate"] is not None
                    or (value["view"] == "enabled" and item["state"] not in ("enabled", "enabled-runtime"))):
                raise ValueError("systemd file item")
        elif (item["state"] not in UNIT_STATES or type(item["substate"]) is not str
              or SUBSTATE.fullmatch(item["substate"]) is None
              or (value["view"] in ("active", "inactive", "failed") and item["state"] != value["view"])):
            raise ValueError("systemd unit item")
        seen.add(item["name"])
    if items != sorted(items, key=lambda item: item["name"]) or _encoded_size(value) > MAX_CONTEXT_BYTES:
        raise ValueError("systemd size or order")
    return value


def collect(scope="system", view="installed"):
    """Collect one explicit scope/view, or return a typed incomplete result.

    Failures never become an empty successful list. The host's trusted systemctl
    is the sole child, with bounded pipes/time and no shell, pager or privilege
    escalation. Names may themselves be identifying data and need preview.
    """
    argv = command(scope, view)
    uid = os.getuid()
    if uid == 0 or uid != os.geteuid():
        raise ValueError("systemd identity")
    result = {"kind": "systemd", "source": "systemctl", "scope": scope, "view": view,
              "listing": listing_for(view), "observed_at": "", "status": "unavailable",
              "truncated": False, "total": None, "items": []}
    try:
        code, output, _ = bounded_process(argv, env=_environment(scope, uid),
                                         timeout=TIMEOUT_SECONDS, limit=MAX_OUTPUT_BYTES)
        if code == 0:
            rows = _parse(output, view)
            result.update(status="ok", total=len(rows), items=rows[:MAX_ITEMS])
    except AnalysisDenied as exc:
        result["status"] = exc.reason if exc.reason in ("timeout", "output-limit") else "unavailable"
    except OSError:
        pass
    except (ValueError, UnicodeError, TypeError):
        result["status"] = "invalid"
    result["observed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if result["status"] == "ok":
        result["truncated"] = len(result["items"]) < result["total"]
        while _encoded_size(result) > MAX_CONTEXT_BYTES and result["items"]:
            result["items"].pop()
            result["truncated"] = True
    return validate_context(result)
