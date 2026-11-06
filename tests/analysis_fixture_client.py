"""Synthetic native-client payload installed ONLY in a disposable CI VM.

No provider, OAuth, tokens or real report. Reached through a compiled ELF shim
so production installation/version checks and namespace launcher stay intact.
"""
import json
import hashlib
import errno
import os
from pathlib import Path
import socket
import sys
import time

sys.path.insert(0, "/usr/lib/ai-desktop")
from ai_desktop.analysis_contract import CLIENT_VERSION, MAX_PACKET, MAX_CONTEXT_BYTES, canonical, application_input
from ai_desktop.analysis_sandbox import BINARY, INNER_PROFILE, client_command, client_environment
from ai_desktop.client_compat import FIRST_INVALID_OPTION, INVALID_OPTION
from ai_desktop.e2_subscription_boundary import privileges_locked
from ai_desktop.report_policy import validate_report


FILE_FIXTURE = None
EDIT_FIXTURE = None
HOME_FIXTURE = None
WEB_FIXTURE = None
EXECUTION_FIXTURE = None


def validate_invocation(args, environment):
    """Match exact legacy or controller-frozen fixture commands and environment."""
    for action in ("version", "status", "analyze", "login", "web", "offline-logout",
                   "compat-options", "compat-web", "compat-reject", "compat-rpc"):
        selections = (None, EXECUTION_FIXTURE) if action in ("analyze", "web") else (None,)
        for selected in selections:
            if args == client_command(action, selected):
                effort = selected["effort"] if selected is not None else None
                if environment == client_environment(action, effort):
                    return action
    raise AssertionError("fixture-command")


def validate_web_payload(data):
    assert data == {"mode": "web", "question": WEB_FIXTURE["question"]}


def web_events(mode="success"):
    """Synthetic documented WebSearch transcript; no provider or URL fetch."""
    url = "https://www.python.org/downloads/"
    result = {"answer": "SYNTHETIC WEB: metadata=PASS isolation=PASS [1]", "sources": [url]}
    tool = "Bash" if mode == "web-foreign-tool" else "WebSearch"
    if mode == "web-forged-source":
        result["sources"] = ["https://example.org/not-observed"]
    records = [
        {"type": "system", "subtype": "init", "tools": ["WebSearch"], "mcp_servers": [], "session_id": "web-ci"},
        {"type": "assistant", "session_id": "web-ci", "parent_tool_use_id": None, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tool-web", "name": tool, "input": {"query": "Python stable release"}}]}},
        {"type": "user", "session_id": "web-ci", "parent_tool_use_id": None, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tool-web", "content": "SYNTHETIC search results"}]},
         "tool_use_result": {"query": "Python stable release", "durationSeconds": 0.1, "searchCount": 1,
             "results": [] if mode == "web-no-results" else [
                 {"tool_use_id": "backend-web", "content": [{"title": "Python downloads", "url": url}]}]}},
        {"type": "result", "session_id": "web-ci", "subtype": "success", "is_error": False,
         "num_turns": 2, "permission_denials": [], "result": json.dumps(result)},
    ]
    return records


def validate_home_payload(data):
    """A broad grant transfers only the exact requested synthetic context."""
    assert data["mode"] == "workspace" and data["task"]["instruction"] == HOME_FIXTURE["question"]
    assert len(data["task"]["files"]) == 2
    assert {item["path"]: (item["content"], item["writable"]) for item in data["task"]["files"]} == {
        "test/context.txt": (HOME_FIXTURE["context"], True), "test/hello.py": (None, True)}


def validate_file_payload(data):
    """Check every transported synthetic byte, not just a matching excerpt."""
    from ai_desktop.file_context_contract import validate_context
    assert data["question"] == FILE_FIXTURE["question"]
    assert len(data["context"]) == 1
    context = validate_context(data["context"][0])
    assert len(context["files"]) == len(FILE_FIXTURE["files"]) == 5
    actual, total = {}, 0
    for file in context["files"]:
        path, content = Path(file["path"]), file["content"]
        assert path.parent.name == "aidfixture" and path.name not in actual
        expected = FILE_FIXTURE["files"][path.name]
        raw = content.encode("utf-8")
        assert len(raw) == expected["bytes"]
        assert hashlib.sha256(raw).hexdigest() == expected["sha256"]
        actual[path.name] = content
        total += len(raw)
        # The approved packet contains text; the source paths have no mount.
        assert not path.exists()
    assert set(actual) == set(FILE_FIXTURE["files"]) and total == 12175
    assert actual["notes.txt"].index(FILE_FIXTURE["marker"]) > 4096
    assert actual["notes.txt"].endswith("answer_marker = " + FILE_FIXTURE["marker"] + "\n")
    assert len(canonical(data["context"]).encode("utf-8")) <= MAX_CONTEXT_BYTES == 34 * 1024 * 1024
    assert len(application_input(None, question=data["question"], context=data["context"]).encode("utf-8")) <= MAX_PACKET == 36 * 1024 * 1024


def main():
    args = [str(BINARY)] + sys.argv[1:]
    action = validate_invocation(args, dict(os.environ))
    # auth status has the same argv in both cases. Only the compatibility
    # namespace has no mounted profile at all; never inspect real credentials.
    if action == "status" and not Path(INNER_PROFILE).exists():
        action = "compat-rpc"
    assert os.geteuid() > 0 and privileges_locked(Path("/proc/self/status").read_text())
    assert not Path("/var/lib/ai-desktop").exists() and not Path("/run/user").exists()
    assert not Path("/etc/claude-code").exists()
    assert not Path("/home/runner").exists() and not Path("/root").exists()
    assert os.getcwd() == "/work"
    assert any(line.split()[4] == "/proc" and "ro" in line.split()[5].split(",")
               for line in Path("/proc/self/mountinfo").read_text().splitlines())
    # Actual post-exec kernel refusal, in every version/login/status/analysis.
    probe = Path("/tmp/fixture-mode-check")
    probe.write_text("SYNTHETIC")
    for mode in (0o4700, 0o2700):
        try:
            probe.chmod(mode)
        except OSError as exc:
            assert exc.errno == errno.EPERM
        else:
            raise AssertionError("suid-sgid-allowed")
    probe.chmod(0o600)
    probe.unlink()
    if action == "version":
        assert not Path(INNER_PROFILE).exists()
        print(CLIENT_VERSION + " (Claude Code)")
        return 0
    if action.startswith("compat-"):
        assert not Path(INNER_PROFILE).exists()
        assert not Path("/run/egress.sock").exists()
        if action in ("compat-options", "compat-web", "compat-reject"):
            sentinel = FIRST_INVALID_OPTION if action == "compat-reject" else INVALID_OPTION
            print("error: unknown option '" + sentinel + "'", file=sys.stderr)
            return 1
        assert action == "compat-rpc"
        print(json.dumps({"loggedIn": False, "configDirectory": INNER_PROFILE, "authMethod": "none"}))
        return 1
    profile = Path(INNER_PROFILE)
    mode = (profile / "fixture-mode").read_text()
    if mode == "account-lifecycle":
        assert action in ("status", "offline-logout")
        assert not Path("/run/egress.sock").exists()
        fixture = profile / ".credentials.json"
        if action == "offline-logout":
            fixture.unlink()
            return 0
        # The offline check must never mount a writable credential profile.
        try:
            (profile / "check-wrote-profile").touch()
        except OSError as exc:
            assert exc.errno == errno.EROFS
        else:
            raise AssertionError("writable-status-profile")
        present = fixture.is_file()
        print(json.dumps({"loggedIn": present, "authMethod": "claude.ai", "configDirectory": INNER_PROFILE}))
        return 0 if present else 1
    # Actual private profile writes/atomic replacement, always synthetic bytes.
    (profile / "fixture-refresh.tmp").write_text("SYNTHETIC")
    (profile / "fixture-refresh.tmp").replace(profile / "fixture-refresh")
    if action == "login":
        assert mode in ("login", "wrong-auth")
        assert os.isatty(0) and os.isatty(1) and os.isatty(2)
        print("SYNTHETIC LOGIN INPUT:", flush=True)
        assert sys.stdin.readline(64) == "SYNTHETIC\n"
        (profile / "fixture-login").write_text("SYNTHETIC")
        return 0
    if action == "status":
        if mode == "expired":
            print(json.dumps({"loggedIn": False, "email": "SYNTHETIC-PRIVATE"}))
            return 1
        if mode == "login":
            assert (profile / "fixture-login").read_text() == "SYNTHETIC"
        print(json.dumps({"loggedIn": True, "authMethod": "api_key" if mode == "wrong-auth" else "claude.ai",
                          "configDirectory": INNER_PROFILE, "email": "SYNTHETIC-PRIVATE"}))
        return 0
    assert mode in ("success", "network-failure", "hang", "api-auth", "api-rate", "api-unavailable",
                    "web-no-results", "web-forged-source", "web-foreign-tool")
    raw = sys.stdin.buffer.read(MAX_PACKET + 1)
    assert 0 < len(raw) <= MAX_PACKET
    data = json.loads(raw)
    assert (action == "web") == (data["mode"] == "web")
    if data["mode"] == "analysis":
        validate_report(data["report"])
        assert data["report"]["kernel"] == "0.0.0-synthetic"
    elif data["mode"] == "assistant":
        from ai_desktop.assistant_contract import validate_access
        assert set(data) in ({"mode", "question", "access"}, {"mode", "question", "access", "discovery"})
        if "discovery" in data:
            from ai_desktop.discovery_contract import validate_discovery
            assert validate_discovery(data["discovery"]) == data["discovery"]
        access = validate_access(data["access"])
        if WEB_FIXTURE and data["question"] == WEB_FIXTURE["question"]:
            assert access == {"files": [], "systemd": [], "weather": False, "web": True}
        elif HOME_FIXTURE and data["question"] == HOME_FIXTURE["question"]:
            assert access["weather"] is False and access["systemd"] == []
            assert access["files"] == [{"id": "f0", "pattern": "~/"}]
            assert access["write"] == [{"id": "w0", "pattern": "~/"}]
            assert HOME_FIXTURE["marker"] not in json.dumps(data)
        elif EDIT_FIXTURE and data["question"] == EDIT_FIXTURE["question"]:
            assert access["weather"] is False and access["systemd"] == []
            assert len(access["files"]) == len(access["write"]) == 1
            assert access["files"][0]["id"] == "f0" and access["write"][0]["id"] == "w0"
            assert access["files"][0]["pattern"].endswith("/aideditfixture/**")
            assert access["write"][0]["pattern"].endswith("/aideditfixture/*")
        elif data["question"] == "SYNTHETIC: unconfigured question":
            assert access == {"files": [], "systemd": [], "weather": False}
        else:
            assert access["weather"] is False and access["systemd"] == ["system"]
            assert len(access["files"]) == 1 and access["files"][0]["id"] == "f0"
            assert access["files"][0]["pattern"].endswith("/aidfixture/*")
        assert FILE_FIXTURE["marker"] not in json.dumps(data)
        assert "SYNTHETIC FILE DENIED CONTENT" not in json.dumps(data)
        assert data["question"] in ("SYNTHETIC: general question", "SYNTHETIC: unconfigured question",
                                    "SYNTHETIC: systemd question", FILE_FIXTURE["question"], EDIT_FIXTURE["question"],
                                    HOME_FIXTURE["question"], WEB_FIXTURE["question"])
    elif data["mode"] == "web":
        validate_web_payload(data)
    elif data["mode"] == "question":
        # What the task collected earlier travels with its last request, and the model must really receive it.
        assert set(data) - {"discovery"} == {"mode", "question", "context"}
        if "discovery" in data:
            from ai_desktop.discovery_contract import validate_discovery
            assert validate_discovery(data["discovery"]) == data["discovery"] and data["discovery"]["steps"]
        if data["question"] == "SYNTHETIC: general question":
            assert data["context"] == []
        elif data["question"] == "SYNTHETIC: systemd question":
            from ai_desktop.systemd_context import validate_context
            assert len(data["context"]) == 1
            context = validate_context(data["context"][0])
            assert context["scope"] == "system" and context["view"] == "enabled"
            assert context["listing"] == "unit-files" and context["status"] == "ok"
            assert context["total"] >= len(context["items"]) > 0
        else:
            validate_file_payload(data)
    else:
        from ai_desktop.task_contract import request
        request(data["task"])
        assert data["task"]["instruction"] in ("SYNTHETIC: fix addition", "SYNTHETIC: large JSON edit", EDIT_FIXTURE["question"], HOME_FIXTURE["question"])
        if data["task"]["instruction"] == HOME_FIXTURE["question"]:
            validate_home_payload(data)
        if data["task"]["instruction"] == EDIT_FIXTURE["question"]:
            assert len(raw) > 32768
            expected = EDIT_FIXTURE["files"]
            assert len(data["task"]["files"]) == len(expected) + 1 == 10
            for item in data["task"]["files"]:
                assert item["content"] == expected.get(item["path"])
                assert item["writable"] is (item["path"] in ("main.py", "result.txt"))
            assert {f["path"] for f in data["task"]["files"]} == set(expected) | {"result.txt"}
        if data["task"]["instruction"] == "SYNTHETIC: large JSON edit":
            assert len(raw) > 32768 and len(data["task"]["files"]) == 14
            selected = next(f for f in data["task"]["files"] if f["path"] == "settings.json")
            settings = json.loads(selected["content"])
            assert settings["enabled"] is False and settings["tail"] == "SYNTHETIC_LARGE_TAIL"
            assert all(value == "padding-value" for key, value in settings.items() if key.startswith("p"))
    if mode == "network-failure":
        print("SYNTHETIC-PRIVATE-NETWORK-ERROR", file=sys.stderr)
        return 1
    if mode in ("api-auth", "api-rate", "api-unavailable"):
        # Documented native result metadata only. Strings must never leak to
        # the desktop or be interpreted as commands/recovery instructions.
        status = {"api-auth": 401, "api-rate": 429, "api-unavailable": 503}[mode]
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "api_error_status": status, "result": "SYNTHETIC-PRIVATE",
                          "errors": ["SYNTHETIC-PRIVATE-ERROR"]}))
        return 1
    if mode == "hang":
        (profile / "fixture-hanging").write_text("SYNTHETIC")
        time.sleep(90)
        return 1
    # A direct public address is unreachable inside the actual private netns.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as direct:
        direct.settimeout(.2)
        try:
            direct.connect(("192.0.2.1", 443))
        except OSError:
            pass
        else:
            raise AssertionError("uplink")
    # Exercise real TCP -> Unix -> external proxy; denied before DNS/dial.
    with socket.create_connection(("127.0.0.1", 18080), timeout=2) as bridge:
        bridge.sendall(b"CONNECT forbidden.invalid:443 HTTP/1.1\r\nHost: forbidden.invalid:443\r\n\r\n")
        assert bridge.recv(1) == b""
    if data["mode"] == "web":
        for event in web_events(mode):
            print(json.dumps(event))
        return 0
    result = "SYNTHETIC ANALYSIS: isolation=PASS proxy-denial=PASS profile=PASS\n\u001b[31m"
    if data["mode"] == "assistant":
        if data["question"] == WEB_FIXTURE["question"]:
            result = json.dumps({"action": "web"})
        elif data["question"] == HOME_FIXTURE["question"]:
            result = json.dumps({"action": "edit", "write": "w0",
                                 "files": [{"id": "f0", "path": "test/context.txt"}],
                                 "create": ["test/hello.py"], "check": "python-syntax"})
        elif data["question"] == EDIT_FIXTURE["question"]:
            result = json.dumps({"action": "edit", "write": "w0", "files": ["f0"],
                                 "create": ["result.txt"], "check": "python-unittest"})
        elif data["question"] == "SYNTHETIC: unconfigured question":
            result = json.dumps({"action": "answer", "text": "SYNTHETIC ASK: no-config=PASS sources=none isolation=PASS\n&#x1b;[31m"})
        elif data["question"] == "SYNTHETIC: general question":
            result = json.dumps({"action": "answer", "text": "SYNTHETIC ASK: general=PASS context=empty isolation=PASS\n&#x1b;[31m"})
        elif data["question"] == "SYNTHETIC: systemd question":
            result = json.dumps({"action": "read", "files": [],
                                 "systemd": [{"scope": "system", "view": "enabled"}], "weather": None})
        elif "discovery" not in data:
            # Discovery first, as a real planner must: a directory scope is listed before it is read.
            result = json.dumps({"action": "list", "files": ["f0"]})
        else:
            result = json.dumps({"action": "read", "files": ["f0"], "systemd": [], "weather": None})
    if data["mode"] == "question":
        if data["question"] == "SYNTHETIC: general question":
            result = "SYNTHETIC ASK: general=PASS context=empty isolation=PASS\n\u001b[31m"
        elif data["question"] == "SYNTHETIC: systemd question":
            result = "SYNTHETIC ASK: systemd=PASS context=validated isolation=PASS"
        else:
            result = ("SYNTHETIC ASK: files=5 bytes=12175 exact-content=PASS tail-marker=PASS "
                      "wire-budget=PASS no-host-mount=PASS marker=" + FILE_FIXTURE["marker"])
    if data["mode"] == "workspace":
        result = json.dumps({"summary": "SYNTHETIC fix addition", "files": [
            {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}]})
    if data["mode"] == "workspace" and data["task"]["instruction"] == "SYNTHETIC: large JSON edit":
        original = next(f["content"] for f in data["task"]["files"] if f["path"] == "settings.json")
        changed = original.replace('"enabled": false', '"enabled": true')
        assert changed != original and len(changed.encode()) > 32768
        result = json.dumps({"summary": "SYNTHETIC large JSON edit", "files": [
            {"path": "settings.json", "content": changed},
            {"path": "created.json", "content": '{"created": true}\n'}]})
    if data["mode"] == "workspace" and data["task"]["instruction"] == EDIT_FIXTURE["question"]:
        result = json.dumps({"summary": "SYNTHETIC ask edit", "files": [
            {"path": "main.py", "content": EDIT_FIXTURE["updated"]},
            {"path": "result.txt", "content": EDIT_FIXTURE["marker"] + "\n"}]})
    if data["mode"] == "workspace" and data["task"]["instruction"] == HOME_FIXTURE["question"]:
        result = json.dumps({"summary": "SYNTHETIC HOME create", "files": [
            {"path": "test/hello.py", "content": HOME_FIXTURE["created"]}]})
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "num_turns": 1, "permission_denials": [],
                      "result": result,
                      "email": "SYNTHETIC-PRIVATE"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
