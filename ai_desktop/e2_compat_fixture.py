"""Secret-free, loopback-only Claude transport experiment; NOT a provider broker.

Only the sandbox entrypoint starts a listener. Importing this file has no effects.
No upstream connection, OAuth loader or caller-selected payload/endpoint exists.
"""
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from .provider_errors import HTTP_REASONS, MAX_FAILURE_BYTES, classify_failure

PROMPT = "AI_DESKTOP_SYNTHETIC_REQUEST_ONLY"
REPLY = "AI_DESKTOP_SYNTHETIC_REPLY_ONLY"
TOKEN = "AI_DESKTOP_SYNTHETIC_NOT_A_CREDENTIAL"
MODEL = "claude-sonnet-4-6"
MAX_BODY = 131072
MAX_PROCESS_OUTPUT = 262144
MAX_REQUESTS = 8
RECOVERY_SCENARIOS = frozenset({"recovery-deny", "recovery-rate", "recovery-unavailable",
                                "recovery-disconnect", "recovery-timeout"})
SCENARIOS = ("version", "reply", "deny", "recovery-deny", "rate-limit", "recovery-rate",
             "unavailable", "recovery-unavailable", "disconnect", "recovery-disconnect",
             "timeout", "recovery-timeout")
HTTP_FAULTS = {"deny": (401, "authentication_error", "auth-required"),
               "rate-limit": (429, "rate_limit_error", "rate-limit"),
               "unavailable": (503, "api_error", "provider-unavailable")}
STALL_DEADLINE = 8
STALL_SOCKET_DEADLINE = 12
API_STATUS_SHAPES = frozenset({"none", "other", *(str(status) for status in HTTP_REASONS)})
RESULT_FLAGS = ("result_type", "success_subtype", "is_error")


REASONS = frozenset({"internal", "timeout", "output-limit", "process-io",
    "http-parser", "http-method", "handler-error", "request-limit", "body-length",
    "body-read", "endpoint", "auth-header", "unexpected-header", "request-json",
    "request-shape", "request-model", "request-tools", "request-messages",
    "request-content", "request-stream", "no-message", "client-exit", "client-json",
    "client-reply", "client-denial", "client-options", "client-auth", "client-permission",
    "turn-limit", "boundary", "version", "fault-not-observed"})


class FixtureDenied(RuntimeError):
    def __init__(self, reason="internal"):
        super().__init__("COMPAT: FIXTURE REFUSED")
        self.reason = reason if type(reason) is str and reason in REASONS else "internal"
        self.context = {}


def mark_invalid(server, reason):
    server.invalid = True
    if getattr(server, "invalid_reason", "none") == "none":
        server.invalid_reason = reason if reason in REASONS else "internal"


def failure_metadata(error, phase):
    """Only fixed vocabulary, bounded counters and booleans; no native text."""
    context = error.context if type(getattr(error, "context", None)) is dict else {}
    result = {"error": "COMPAT REFUSED", "phase": phase if phase in ("setup", *SCENARIOS) else "setup",
              "reason": getattr(error, "reason", "internal"), "client_exit": None,
              "requests": 0, "messages": 0, "input_messages": 0,
              "text_blocks": 0, "marker": False, "exact": False,
              "result_type": False, "success_subtype": False, "is_error": False,
              "api_status": "none"}
    if type(result["reason"]) is not str or result["reason"] not in REASONS:
        result["reason"] = "internal"
    code = context.get("client_exit")
    if type(code) is int and -128 <= code <= 255:
        result["client_exit"] = code
    for key in ("requests", "messages", "input_messages", "text_blocks"):
        value = context.get(key, 0)
        if type(value) is int:
            result[key] = max(0, min(value, 32))
    for key in ("marker", "exact", *RESULT_FLAGS):
        result[key] = context.get(key) is True
    status = context.get("api_status")
    if type(status) is str and status in API_STATUS_SHAPES:
        result["api_status"] = status
    return result


def result_shape(raw):
    """Finite diagnostic shape only; it cannot turn a failed proof into PASS."""
    shape = {"result_type": False, "success_subtype": False, "is_error": False,
             "api_status": "none"}
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_FAILURE_BYTES:
        return shape
    try:
        result = json.loads(raw, object_pairs_hook=_unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(result) is not dict:
            return shape
        shape.update(result_type=result.get("type") == "result",
                     success_subtype=result.get("subtype") == "success",
                     is_error=result.get("is_error") is True)
        status = result.get("api_error_status")
        if status is not None:
            shape["api_status"] = str(status) if type(status) is int and status in HTTP_REASONS else "other"
    except (ValueError, TypeError, UnicodeError, RecursionError):
        pass
    return shape


def request_shape(raw):
    """Deliberately reveal shape only, never keys/values copied from input."""
    result = {"input_messages": 0, "text_blocks": 0, "marker": False, "exact": False}
    try:
        data = json.loads(raw, object_pairs_hook=_unique)
        messages = data.get("messages") if type(data) is dict else None
        if type(messages) is not list:
            return result
        result["input_messages"] = min(len(messages), 32)
        texts = []
        for message in messages:
            if type(message) is not dict or message.get("role") != "user":
                continue
            content = message.get("content")
            if type(content) is str:
                texts.append(content)
            elif type(content) is list:
                texts.extend(b["text"] for b in content if type(b) is dict and
                             b.get("type") == "text" and type(b.get("text")) is str)
        result["text_blocks"] = min(len(texts), 32)
        result["marker"] = any(PROMPT in t for t in texts)
        result["exact"] = texts == [PROMPT]
    except (ValueError, TypeError, UnicodeError, RecursionError):
        pass
    return result


def client_failure_reason(code, stdout, stderr, default):
    # Redacted failure hint only; no matched prose can grant a proof or PASS.
    combined = (stdout + b"\n" + stderr).lower()
    if b"unknown option" in combined or b"unrecognized option" in combined or b"unknown argument" in combined:
        return "client-options"
    if b"not logged in" in combined or b"please log in" in combined or b"invalid api key" in combined:
        return "client-auth"
    if b"workspace trust" in combined or b"permission denied" in combined:
        return "client-permission"
    if b"error_max_turns" in combined:
        return "turn-limit"
    return default


def bounded_run(command, *, env=None, timeout=40, input_bytes=b"", limit=MAX_PROCESS_OUTPUT):
    """Internal bounded pipes + process-group cleanup (not escaped sessions).

    Each real-client phase must run in its own disposable outer PID namespace.
    Namespace teardown, rather than killpg alone, removes escaped descendants.
    """
    process = subprocess.Popen(command, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, close_fds=True)
    try:
        process.stdin.write(input_bytes)
        process.stdin.close()
        deadline = time.monotonic() + timeout
        output = [bytearray(), bytearray()]
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 0)
            selector.register(process.stderr, selectors.EVENT_READ, 1)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FixtureDenied("timeout")
                for key, _ in selector.select(min(remaining, 0.2)):
                    block = os.read(key.fileobj.fileno(), 8192)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    output[key.data].extend(block)
                    if sum(map(len, output)) > limit:
                        raise FixtureDenied("output-limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FixtureDenied("timeout")
            process.wait(timeout=remaining)
        return process.returncode, bytes(output[0]), bytes(output[1])
    except (OSError, subprocess.TimeoutExpired):
        raise FixtureDenied("process-io") from None
    finally:
        # Also terminate descendants which outlive a normally exited parent.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def validate_request(path, headers, raw):
    """Validate synthetic traffic; errors carry a fixed reason, never values."""
    if path not in ("/v1/messages", "/v1/messages?beta=true"):
        raise FixtureDenied("endpoint")
    if headers.get_all("Authorization") != ["Bearer " + TOKEN]:
        raise FixtureDenied("auth-header")
    if any(headers.get_all(h) for h in ("x-api-key", "Cookie", "Transfer-Encoding")):
        raise FixtureDenied("unexpected-header")
    if not 0 < len(raw) <= MAX_BODY:
        raise FixtureDenied("body-length")
    try:
        body = json.loads(raw, object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise FixtureDenied("request-json") from None
    if type(body) is not dict:
        raise FixtureDenied("request-shape")
    if body.get("model") != MODEL:
        raise FixtureDenied("request-model")
    if body.get("tools", []) != []:
        raise FixtureDenied("request-tools")
    messages = body.get("messages")
    if (type(messages) is not list or len(messages) != 1 or
            type(messages[0]) is not dict or messages[0].get("role") != "user"):
        raise FixtureDenied("request-messages")
    content = messages[0].get("content")
    if type(content) is str:
        texts = [content]
    elif type(content) is list and content and all(
            type(b) is dict and b.get("type") == "text" and
            type(b.get("text")) is str for b in content):
        texts = [b["text"] for b in content]
    else:
        raise FixtureDenied("request-content")
    if texts != [PROMPT]:
        raise FixtureDenied("request-content")
    if "stream" in body and type(body["stream"]) is not bool:
        raise FixtureDenied("request-stream")
    return body


def reply_body(stream):
    message = {"id": "msg_synthetic", "type": "message", "role": "assistant",
               "model": MODEL, "content": [{"type": "text", "text": REPLY}],
               "stop_reason": "end_turn", "stop_sequence": None,
               "usage": {"input_tokens": 1, "output_tokens": 1}}
    if not stream:
        return "application/json", json.dumps(message).encode()
    start = {**message, "content": [], "stop_reason": None,
             "usage": {"input_tokens": 1, "output_tokens": 0}}
    events = [
        {"type": "message_start", "message": start},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": REPLY}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    wire = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events)
    return "text/event-stream", wire.encode()


class FixtureServer(HTTPServer):
    # One handler at a time; connection timeout bounds slow clients.

    def handle_error(self, request, client_address):
        mark_invalid(self, "handler-error")  # No traceback or request details in logs.

    def __init__(self, scenario):
        if scenario not in SCENARIOS[1:]:
            raise FixtureDenied("COMPAT: SCENARIO DENIED")
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.scenario = scenario
        self.requests = 0
        self.messages = 0
        self.invalid = False
        self.invalid_reason = "none"
        self.shape = {}
        self.fault_applied = False
        self.stall_closed = False
        self.lock = threading.Lock()


class FixtureHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def log_message(self, *_):
        pass  # Never log headers, payloads, paths or native client output.

    def _count(self):
        with self.server.lock:
            self.server.requests += 1
            if self.server.requests > MAX_REQUESTS:
                mark_invalid(self.server, "request-limit")
                return False
            return True

    def _response(self, code, content_type="application/json", body=b""):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def send_error(self, code, message=None, explain=None):
        # Parser failures and unsupported methods are observable refusals too.
        mark_invalid(self.server, "http-parser" if code != 501 else "http-method")
        self._count()
        self._response(403)

    def do_HEAD(self):
        if self._count() and self.path == "/api/hello":
            self._response(204)
        else:
            mark_invalid(self.server, "endpoint")
            self._response(403)

    def do_POST(self):
        try:
            if not self._count():
                raise FixtureDenied("request-limit")
            lengths = self.headers.get_all("Content-Length")
            if (not lengths or len(lengths) != 1 or not lengths[0].isascii() or
                    not lengths[0].isdigit() or not 0 < int(lengths[0]) <= MAX_BODY):
                raise FixtureDenied("body-length")
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                raise FixtureDenied("body-read")
            self.server.shape = request_shape(raw)
            if self.path in ("/v1/messages/count_tokens", "/v1/messages/count_tokens?beta=true"):
                # Optional endpoint: the native client must fall back locally.
                if self.headers.get_all("Authorization") != ["Bearer " + TOKEN]:
                    raise FixtureDenied("auth-header")
                if any(self.headers.get_all(h) for h in ("x-api-key", "Cookie", "Transfer-Encoding")):
                    raise FixtureDenied("unexpected-header")
                self._response(404)
                return
            body = validate_request(self.path, self.headers, raw)
            with self.server.lock:
                self.server.messages += 1
            if self.server.scenario in HTTP_FAULTS:
                status, kind, _ = HTTP_FAULTS[self.server.scenario]
                error = {"type": "error", "error": {"type": kind, "message": "SYNTHETIC_DENIAL"}}
                self._response(status, body=json.dumps(error).encode())
                self.server.fault_applied = True
            elif self.server.scenario == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                self.server.fault_applied = True
            elif self.server.scenario == "timeout":
                # No response. The trusted outer process deadline must kill the
                # native client. This is supervisor timeout/cleanup, not proof
                # of the native HTTP library's private timeout configuration.
                self.connection.settimeout(STALL_SOCKET_DEADLINE)
                self.server.fault_applied = True
                self.server.stall_closed = self.connection.recv(1) == b""
                self.close_connection = True
                if not self.server.stall_closed:
                    raise FixtureDenied("fault-not-observed")
            else:
                kind, response = reply_body(body.get("stream", False))
                self._response(200, kind, response)
        except (FixtureDenied, OSError, ValueError) as exc:
            mark_invalid(self.server, getattr(exc, "reason", "handler-error"))
            try:
                self._response(403)
            except OSError:
                pass

    def do_GET(self):
        mark_invalid(self.server, "http-method")
        self._count()
        self._response(403)


def client_command():
    # A custom system prompt does not remove conversation-level reminders.
    # Bare mode suppresses that implicit context; the fixture still verifies
    # exact input and refuses clients/versions which add anything to it.
    return ["/opt/claude", "--bare", "--print", "--restricted", "--safe-mode",
            "--tools", "", "--disallowedTools", "mcp__*", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
            "--permission-mode", "dontAsk", "--no-session-persistence",
            "--max-turns", "1", "--model", MODEL, "--output-format", "json",
            "--system-prompt", "Synthetic local transport test. No tools."]


def client_environment(port):
    if type(port) is not int or not 1 <= port <= 65535:
        raise FixtureDenied("COMPAT: PORT DENIED")
    return {"PATH": "/usr/bin", "HOME": "/home/model",
            "CLAUDE_CONFIG_DIR": "/home/model/profile",
            "TMPDIR": "/tmp", "LANG": "C.UTF-8",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:" + str(port),
            "ANTHROPIC_AUTH_TOKEN": TOKEN,
            # The fixture proves a single response/denial, not retry recovery.
            "CLAUDE_CODE_MAX_RETRIES": "0",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1"}


def run_scenario(scenario):
    if scenario not in SCENARIOS[1:]:
        raise FixtureDenied("internal")
    with FixtureServer(scenario) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        failure = None
        code, stdout, stderr = None, b"", b""
        try:
            code, stdout, stderr = bounded_run(client_command(),
                env=client_environment(server.server_port), input_bytes=PROMPT.encode(),
                timeout=STALL_DEADLINE if scenario == "timeout" else 40)
        except FixtureDenied as exc:
            failure = exc
        except OSError:
            failure = FixtureDenied("process-io")
        finally:
            server.shutdown()
            thread.join(timeout=2)
        context = {"client_exit": code, "requests": getattr(server, "requests", 0),
                   "messages": server.messages, **getattr(server, "shape", {}),
                   **result_shape(stdout)}
        # A watchdog timeout is success only for the deliberate, witnessed
        # stalled-response case. A cold-start hang or unrelated timeout fails.
        timeout_stopped = (scenario == "timeout" and isinstance(failure, FixtureDenied)
                           and failure.reason == "timeout" and server.messages == 1
                           and not server.invalid and server.fault_applied is True
                           and server.stall_closed is True)
        if timeout_stopped:
            return True
        if failure is not None:
            failure.context = context
            raise failure
        if server.invalid or server.messages != 1:
            reason = (getattr(server, "invalid_reason", "internal") if server.invalid else
                      "request-limit" if server.messages > 1 else
                      client_failure_reason(code, stdout, stderr, "no-message"))
            error = FixtureDenied(reason)
            error.context = context
            raise error
        if scenario == "reply" or scenario in RECOVERY_SCENARIOS:
            try:
                result = json.loads(stdout, object_pairs_hook=_unique)
                valid = (type(code) is int and code == 0 and type(result) is dict and result.get("type") == "result"
                         and result.get("subtype") == "success" and result.get("result") == REPLY
                         and result.get("is_error") is False and result.get("api_error_status") is None
                         and type(result.get("num_turns")) is int and result["num_turns"] == 1
                         and result.get("permission_denials") == [])
            except (ValueError, UnicodeError, RecursionError):
                valid = False
        elif scenario in HTTP_FAULTS:
            # The application classifier must recognize native metadata. Error
            # prose, generic exit 1 and crashes can never substitute for it.
            valid = (type(code) is int and code in (0, 1) and server.fault_applied is True
                     and classify_failure(code, stdout) == HTTP_FAULTS[scenario][2])
        elif scenario == "disconnect":
            try:
                result = json.loads(stdout, object_pairs_hook=_unique)
                valid = (type(code) is int and code in (0, 1) and server.fault_applied is True and type(result) is dict
                         and result.get("type") == "result" and result.get("subtype") == "success"
                         and result.get("is_error") is True and result.get("api_error_status") is None
                         and classify_failure(code, stdout) is None)
            except (ValueError, TypeError, AttributeError, RecursionError):
                valid = False
        else:
            valid = False  # Timeout without the witnessed watchdog never passes.
        if not valid:
            reason = client_failure_reason(code, stdout, stderr,
                        "client-reply" if scenario == "reply" or scenario in RECOVERY_SCENARIOS else "client-denial")
            error = FixtureDenied(reason)
            error.context = context
            raise error
        return True


def boundary_check():
    host_home = os.environ["HOST_HOME"]
    if (Path.home() != Path("/home/model") or Path(host_home).exists() or
            Path("/etc/passwd").exists() or Path("/home/model/profile").exists() or
            os.readlink("/proc/self/ns/net") == os.environ["HOST_NETNS"] or
            not os.statvfs("/usr").f_flag & os.ST_RDONLY or
            {name for _, name in socket.if_nameindex()} != {"lo"}):
        raise FixtureDenied("boundary")
    if any(name in os.environ for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "SSH_AUTH_SOCK",
            "DBUS_SESSION_BUS_ADDRESS", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")):
        raise FixtureDenied("boundary")
    with socket.socket() as external:
        external.settimeout(0.2)
        try:
            external.connect(("198.18.0.1", 9))
        except OSError:
            pass
        else:
            raise FixtureDenied("boundary")


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in SCENARIOS:
        print('{"error":"COMPAT REFUSED"}')
        return 2
    scenario = args[0]
    try:
        boundary_check()
        version = None
        if scenario == "version":
            code, output, _ = bounded_run(["/opt/claude", "--version"],
                                           env=client_environment(1), timeout=10)
            import re
            match = re.fullmatch(rb"([0-9]+\.[0-9]+\.[0-9]+) \(Claude Code\)\s*", output)
            if code != 0 or match is None:
                raise FixtureDenied("version")
            version = match[1].decode()
        else:
            run_scenario(scenario)
        print(json.dumps({"schema": 1, "scenario": scenario, "version": version,
              "boundary": "PASS", "result": "PASS",
              "real_auth": False, "upstream": False}, sort_keys=True))
        return 0
    except (FixtureDenied, OSError, KeyError, ValueError) as exc:
        print(json.dumps(failure_metadata(exc, scenario), sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
