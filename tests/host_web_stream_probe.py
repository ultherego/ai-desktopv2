"""Explicit offline native stream probe; never a WebSearch acceptance proof.

The existing COMPAT sandbox provides fresh PID/network/HOME namespaces. The
installed official client talks only to a synthetic loopback fixture. Native
stdout/stderr stay in memory. Event labels remain closed. After a successful
synthetic request only, the first pre-init system header additionally exposes
bounded ASCII schema identifiers (subtype and field names), JSON value types,
and a session-equality label. No other field values or nested data are exported.
This diagnostic exception exists only here, never in the production parser.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
VERSION = "2.1.312"
FLAG = "--allow-offline-native-probe"
INNER_FLAG = "--inside-offline-native-probe"
MAX_BYTES = 131072
MAX_EVENTS = 64
MARKER = "AI_DESKTOP_SYNTHETIC_REQUEST_ONLY"
STATES = ("queued", "started", "completed", "cancelled", "discarded")
SYSTEM_KINDS = ("init", "status", "informational", "warning", "thinking_tokens",
                "hook_started", "hook_progress", "hook_response", "plugin_install",
                "commands_changed", "background_tasks_changed", "api_retry",
                "permission_denied", "task_started", "task_notification", "ui_log")
PLAIN_KINDS = ("assistant", "user", "rate_limit_event", "tool_progress", "auth_status",
               "stream_event", "control_request", "control_response", "keep_alive")
EVENT_NAMES = frozenset((*PLAIN_KINDS, "other", "system/other", "result/success",
    "result/error", "result/other", "command_lifecycle/other",
    *("system/" + value for value in SYSTEM_KINDS),
    *("command_lifecycle/" + value for value in STATES)))
FAILURES = frozenset(("none", "setup", "boundary", "version", "native", "fixture", "stream"))


# These are schema identifiers, not general native text. The probe has no real
# OAuth or provider connection; never reuse this formatter for production logs.
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,47}\Z", re.ASCII)
JSON_KINDS = {str: "str", bool: "bool", int: "int", float: "float",
              list: "list", dict: "dict", type(None): "null"}
SESSION_LINKS = frozenset({"same", "different", "missing", "invalid"})
MAX_HEADER_FIELDS = 32


def schema_identifier(value):
    return type(value) is str and IDENTIFIER.fullmatch(value) is not None


def preinit_header(events, names):
    """Describe the first system header, not its values or search validity."""
    if (names.count("system/init") != 1 or names.index("system/init") <= 0
            or events[0].get("type") != "system"):
        return None
    first = events[0]
    if len(first) > MAX_HEADER_FIELDS:
        raise ValueError("header bound")
    subtype = first.get("subtype")
    fields = {name: JSON_KINDS[type(value)] for name, value in sorted(first.items())
              if schema_identifier(name)}
    current = first.get("session_id")
    initial = events[names.index("system/init")].get("session_id")
    if "session_id" not in first:
        relation = "missing"
    elif (type(current) is not str or not 0 < len(current) <= 256
            or type(initial) is not str or not 0 < len(initial) <= 256):
        relation = "invalid"
    else:
        relation = "same" if current == initial else "different"
    return {"subtype": subtype if schema_identifier(subtype) else "redacted",
            "fields": fields, "redacted_fields": len(first) - len(fields), "session": relation}


def validate_preinit(header):
    if (type(header) is not dict or set(header) != {"subtype", "fields", "redacted_fields", "session"}
            or not schema_identifier(header["subtype"])
            or type(header["session"]) is not str or header["session"] not in SESSION_LINKS
            or type(header["redacted_fields"]) is not int
            or not 0 <= header["redacted_fields"] <= MAX_HEADER_FIELDS):
        raise ValueError("header")
    fields = header["fields"]
    if (type(fields) is not dict or not 1 <= len(fields) <= MAX_HEADER_FIELDS
            or len(fields) + header["redacted_fields"] > MAX_HEADER_FIELDS
            or any(not schema_identifier(name) or type(kind) is not str or kind not in JSON_KINDS.values()
                   for name, kind in fields.items()) or fields.get("type") != "str"):
        raise ValueError("header fields")
    if header["subtype"] != "redacted" and fields.get("subtype") != "str":
        raise ValueError("header subtype")
    return header


def print_preinit(header):
    """Only independently validated metadata reaches the terminal."""
    header = validate_preinit(header)
    print("PREINIT_SUBTYPE=" + header["subtype"])
    print("PREINIT_FIELDS=" + ",".join(name + ":" + kind for name, kind in sorted(header["fields"].items())))
    print("PREINIT_SESSION=" + header["session"] + " PREINIT_REDACTED_FIELDS=" + str(header["redacted_fields"]))


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def invalid_constant(_):
    raise ValueError("non-JSON number")


def event_name(event):
    """Return our literals, never an unknown native value or its repr."""
    kind = event.get("type")
    if type(kind) is not str:
        return "other"
    if kind == "system":
        subtype = event.get("subtype")
        return "system/" + subtype if type(subtype) is str and subtype in SYSTEM_KINDS else "system/other"
    if kind == "command_lifecycle":
        state = event.get("state")
        return "command_lifecycle/" + state if type(state) is str and state in STATES else "command_lifecycle/other"
    if kind == "result":
        if event.get("is_error") is True:
            return "result/error"
        return "result/success" if event.get("subtype") == "success" else "result/other"
    return kind if kind in PLAIN_KINDS else "other"


def stream_shape(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("stream")
    lines = raw.splitlines()
    if not 1 <= len(lines) <= MAX_EVENTS or any(not line.strip() for line in lines):
        raise ValueError("events")
    events = [json.loads(line, object_pairs_hook=unique, parse_constant=invalid_constant) for line in lines]
    if any(type(event) is not dict for event in events):
        raise ValueError("events")
    names = [event_name(event) for event in events]
    shape = {"events": names, "first": names[0], "init_index": names.index("system/init")
             if "system/init" in names else -1, "init_count": names.count("system/init")}
    header = preinit_header(events, names)
    if header is not None:
        shape["preinit"] = header
    return shape


def validate_observation(raw):
    """Second redaction boundary, outside the native client's namespace."""
    if type(raw) is not bytes or not 0 < len(raw) <= 8192:
        raise ValueError("observation")
    data = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    keys = {"schema", "boundary", "version", "failure", "native_exit", "requests", "messages",
            "events", "first", "init_index", "init_count", "oauth", "upstream"}
    if (type(data) is not dict or set(data) not in (keys, keys | {"preinit"}) or type(data["schema"]) is not int
            or data["schema"] != 1 or data["boundary"] is not True
            or data["version"] != VERSION or data["oauth"] is not False or data["upstream"] is not False
            or type(data["failure"]) is not str or data["failure"] not in FAILURES):
        raise ValueError("observation")
    code = data["native_exit"]
    if code is not None and (type(code) is not int or not -128 <= code <= 255):
        raise ValueError("exit")
    for key in ("requests", "messages"):
        if type(data[key]) is not int or not 0 <= data[key] <= 32:
            raise ValueError("count")
    names = data["events"]
    if (type(names) is not list or not 1 <= len(names) <= MAX_EVENTS
            or any(type(name) is not str or name not in EVENT_NAMES for name in names)
            or type(data["first"]) is not str or data["first"] != names[0]
            or type(data["init_count"]) is not int or data["init_count"] != names.count("system/init")
            or type(data["init_index"]) is not int
            or data["init_index"] != (names.index("system/init") if "system/init" in names else -1)):
        raise ValueError("events")
    if data["failure"] == "none" and (code != 0 or data["messages"] != 1):
        raise ValueError("success")
    if "preinit" in data:
        header = validate_preinit(data["preinit"])
        expected = "system/" + header["subtype"] if header["subtype"] in SYSTEM_KINDS else "system/other"
        if (data["failure"] != "none" or data["init_count"] != 1 or data["init_index"] <= 0
                or names[0] != expected):
            raise ValueError("header placement")
    return data


def validate_fixture_request(path, headers, raw, *, model, token):
    """One synthetic user request, WebSearch schema only, no real credential."""
    if (path not in ("/v1/messages", "/v1/messages?beta=true")
            or headers.get_all("Authorization") != ["Bearer " + token]
            or any(headers.get_all(name) for name in ("x-api-key", "Cookie", "Transfer-Encoding"))
            or type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES):
        raise ValueError("request-shape")
    try:
        body = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
        if type(body) is not dict or body.get("model") != model:
            raise ValueError()
        tools, messages = body.get("tools"), body.get("messages")
        if (type(tools) is not list or len(tools) != 1 or type(tools[0]) is not dict
                or tools[0].get("name") != "WebSearch"
                or type(messages) is not list or len(messages) != 1
                or type(messages[0]) is not dict or messages[0].get("role") != "user"):
            raise ValueError()
        content = messages[0].get("content")
        if type(content) is str:
            texts = [content]
        elif type(content) is list and 1 <= len(content) <= 16:
            if any(type(block) is not dict or block.get("type") != "text"
                   or type(block.get("text")) is not str for block in content):
                raise ValueError()
            texts = [block["text"] for block in content]
        else:
            raise ValueError()
        if not any(MARKER in text for text in texts) or type(body.get("stream", False)) is not bool:
            raise ValueError()
        return body
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ValueError("request-shape") from None


def native_invocation(production, fixture, port):
    command = production.client_command("web")
    if "--bare" in command or command[0] != str(production.BINARY):
        raise ValueError("command")
    command = ["/opt/claude", *command[1:], "--model", fixture.MODEL]
    env = production.client_environment("web")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE"):
        env.pop(name, None)
    env.update(fixture.client_environment(port))
    # Production's profile must not be mounted or inherited by this probe.
    if env["CLAUDE_CONFIG_DIR"] != "/home/model/profile" or env["HOME"] != "/home/model":
        raise ValueError("profile")
    return command, env


def inside():
    sys.path.insert(0, "/usr/lib/ai-desktop")
    from ai_desktop import e2_compat_fixture as fixture, analysis_sandbox as production
    stage = "boundary"
    try:
        if os.geteuid() <= 0 or Path("/var/lib/ai-desktop").exists():
            raise ValueError("boundary")
        fixture.boundary_check()
        stage = "version"
        code, out, _ = fixture.bounded_run(["/opt/claude", "--version"],
            env={"PATH": "/usr/bin", "HOME": "/home/model", "LANG": "C.UTF-8"}, timeout=10, limit=4096)
        if code != 0 or out.strip() != (VERSION + " (Claude Code)").encode("ascii"):
            raise ValueError("version")
        fixture.validate_request = lambda path, headers, raw: validate_fixture_request(
            path, headers, raw, model=fixture.MODEL, token=fixture.TOKEN)
        stage = "native"
        with fixture.FixtureServer("reply") as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
            thread.start()
            try:
                command, env = native_invocation(production, fixture, server.server_port)
                code, raw, _ = fixture.bounded_run(command, env=env, input_bytes=MARKER.encode("ascii"),
                                                   timeout=40, limit=MAX_BYTES)
            finally:
                server.shutdown()
                thread.join(timeout=2)
            failure = "none" if code == 0 and server.messages == 1 and not server.invalid else "fixture"
            stage = "stream"
            observation = {"schema": 1, "boundary": True, "version": VERSION, "failure": failure,
                "native_exit": code, "requests": min(server.requests, 32), "messages": min(server.messages, 32),
                **stream_shape(raw), "oauth": False, "upstream": False}
        if failure != "none":
            observation.pop("preinit", None)
        encoded = json.dumps(observation, sort_keys=True).encode("ascii")
        validate_observation(encoded)
        print(encoded.decode("ascii"))
        return 0 if failure == "none" else 2
    except (OSError, ValueError, TypeError, KeyError, RecursionError, fixture.FixtureDenied):
        # No native error string, JSON fragment, traceback or credential leaves.
        print(json.dumps({"error": "PROBE REFUSED", "stage": stage if stage in FAILURES else "setup"}))
        return 2


def installed_matches():
    """Compare source code only, never inspect an OAuth profile."""
    installed = Path("/usr/lib/ai-desktop/ai_desktop")
    paths = sorted((ROOT / "ai_desktop").glob("*.py"))
    if not paths:
        raise ValueError("source")
    for source in paths:
        target = installed / source.name
        with source.open("rb") as a:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as b:
                info = os.fstat(b.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                        or hashlib.file_digest(a, "sha256").digest() != hashlib.file_digest(b, "sha256").digest()):
                    raise ValueError("installed")


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if args == [INNER_FLAG]:
        return inside()
    if args != [FLAG] or os.getuid() != os.geteuid() or os.geteuid() <= 0:
        print("WEB STREAM PROBE: REFUSED explicit offline consent and desktop UID required")
        return 2
    sys.path.insert(0, str(ROOT))
    from ai_desktop import e2_client_compat as compat, e2_compat_fixture as fixture, analysis_sandbox as production
    try:
        installed_matches()
        production.runtime_ready()
        script = Path(__file__).resolve()
        if script.is_symlink() or not script.is_file():
            raise ValueError("script")
        command = compat.build_command(production.BINARY, ROOT / "ai_desktop", Path.home(),
                                       os.readlink("/proc/self/ns/net"), "reply")
        # Reuse the reviewed COMPAT boundary; bind only this additional test code.
        position = command.index("--chdir")
        command[position:position] = ["--ro-bind", str(script), "/opt/web_stream_probe.py"]
        command = command[:command.index("-c")] + ["/opt/web_stream_probe.py", INNER_FLAG]
        code, stdout, _ = fixture.bounded_run(command, env={"PATH": "/usr/bin"}, timeout=55, limit=8192)
        data = validate_observation(stdout)
        print("WEB STREAM PROBE: " + ("PASS" if code == 0 and data["failure"] == "none" else "FAIL")
              + " offline=true oauth=false upstream=false native=" + VERSION)
        print("FIRST=" + data["first"] + " INIT_INDEX=" + str(data["init_index"])
              + " INIT_COUNT=" + str(data["init_count"]))
        print("EVENTS=" + " > ".join(data["events"]))
        if "preinit" in data:
            print_preinit(data["preinit"])
        print("NATIVE_EXIT=" + str(data["native_exit"]) + " FIXTURE_MESSAGES=" + str(data["messages"])
              + " FAILURE=" + data["failure"])
        print("This is a protocol observation with a synthetic answer, not WebSearch/OAuth acceptance.")
        return 0 if code == 0 and data["failure"] == "none" else 2
    except (OSError, ValueError, TypeError, KeyError, RecursionError,
            fixture.FixtureDenied, compat.CompatibilityDenied, production.AnalysisDenied):
        print("WEB STREAM PROBE: REFUSED setup, installed code, sandbox or redacted observation")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
