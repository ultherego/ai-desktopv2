"""Fixed A+ supervisor. Never accepts a binary, profile, command or endpoint.

Only the official child opens its OAuth files. The trusted supervisor owns the
external CONNECT proxy; the child receives a Unix bridge, never a host uplink.
"""
from contextlib import contextmanager
from . import account_runtime
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading

from .analysis_contract import (request_effort, request_seconds, ModelText, extract_meta, AnalysisDenied, CLIENT_VERSION, MAX_PACKET, MAX_PACKET_LARGE, SYSTEM_PROMPT, WEB_SYSTEM_PROMPT,
                                REASONS, STAGES, result_text_limit)
from .e2_connect_proxy import ConnectProxy, open_public
from .e2_ipc import _unique_pairs
from .e2_socket_activation import _worker_identity
from .e2_subscription_boundary import base_command, privileges_locked
from .process_io import bounded_process
from .provider_errors import classify_failure
from .web_search_result import web_text, web_failure
from .execution import validate as validate_execution
from .analysis_diagnostics import codex_diagnostic

INSTALL = Path("/usr/lib/ai-desktop")
BINARY = INSTALL / "claude-code"
HOME = Path("/var/lib/ai-desktop")
PROFILE = HOME / "claude"
INNER_PROFILE = "/home/client/profile"
NAMESPACES = ("user", "mnt", "pid", "net", "ipc", "uts")
CA = Path("/etc/ssl/certs/ca-certificates.crt")


@contextmanager
def failure_stage(stage):
    """Attach a fixed stage without retaining exception text or native output."""
    try:
        yield
    except AnalysisDenied as exc:
        raise AnalysisDenied(exc.reason, stage=stage if exc.stage == "internal" else exc.stage,
                             diagnostic=exc.diagnostic) from None
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        reason = ("cancelled" if isinstance(exc, KeyboardInterrupt) else
                  "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "runtime")
        raise AnalysisDenied(reason, stage=stage) from None


def child_failure(errors, fallback, *, provider=None):
    # Only our finite vocabulary may escape captured stderr. Never return a
    # filename, URL, OAuth payload, native error, traceback or exception text.
    if type(errors) is not bytes:
        return AnalysisDenied(fallback)
    for line in errors.splitlines():
        if len(line) > 256 or not line.startswith(b"CLIENT: REFUSED "):
            continue
        try:
            fields = line[len(b"CLIENT: REFUSED "):].decode("ascii").split(" ")
        except UnicodeError:
            continue
        if (len(fields) not in (2, 3) or not fields[0].startswith("stage=")
                or not fields[1].startswith("reason=")):
            continue
        stage, reason = fields[0][6:], fields[1][7:]
        if stage not in STAGES or reason not in REASONS:
            continue
        if len(fields) == 2:
            return AnalysisDenied(reason, stage=stage)
        if provider == "codex" and fields[2].startswith("check="):
            check = codex_diagnostic(fields[2][6:], reason)
            if check is not None:
                return AnalysisDenied(reason, stage=stage, diagnostic=check)
    if b"bwrap:" in errors:
        return AnalysisDenied("sandbox", stage="namespace")
    return AnalysisDenied(fallback)


def trusted_file(path):
    """Reject symlinks and non-root-writable ancestors, including the file."""
    path = Path(path)
    if not path.is_absolute():
        raise AnalysisDenied("runtime")
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        wanted = stat.S_ISREG if part == path else stat.S_ISDIR
        if not wanted(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise AnalysisDenied("runtime")
    return path


def runtime_ready():
    trusted_file(BINARY)
    if not os.access(BINARY, os.X_OK):
        raise AnalysisDenied("runtime")
    with BINARY.open("rb") as source:
        if source.read(4) != b"\x7fELF":
            raise AnalysisDenied("runtime")
    trusted_file(INSTALL / "analysis-client.py")
    trusted_file(INSTALL / "analysis-worker.py")
    trusted_file(Path("/usr/bin/bwrap"))
    # A system CA bundle may be a root-owned symlink (Arch). Resolve it only
    # after validating the link and all its own ancestors as root-controlled.
    for part in (*reversed(CA.parents), CA):
        info = part.lstat()
        if info.st_uid != 0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
            raise AnalysisDenied("runtime")
    trusted_file(CA.resolve(strict=True))


def supervisor_ready():
    if not _worker_identity() or not privileges_locked(Path("/proc/self/status").read_text()):
        raise AnalysisDenied("identity")
    for path in reversed(HOME.parents):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise AnalysisDenied("identity")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    home_fd = os.open(HOME, flags)
    try:
        profile_fd = os.open("claude", flags, dir_fd=home_fd)
        try:
            # Descriptor metadata only; never enumerate/read credentials. A
            # compromised process of this same service UID remains trusted A+.
            for descriptor in (home_fd, profile_fd):
                info = os.fstat(descriptor)
                if (info.st_uid != os.geteuid() or info.st_gid != os.getegid()
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    raise AnalysisDenied("identity")
        finally:
            os.close(profile_fd)
    finally:
        os.close(home_fd)
    runtime_ready()


@contextmanager
def profile_lock():
    descriptor = os.open(HOME / "analysis.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
                         | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise AnalysisDenied("identity")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AnalysisDenied("busy") from None
        yield
    finally:
        os.close(descriptor)


def client_environment(action="analyze", effort=None):
    # Construct, never filter an inherited environment. No API keys, third-party
    # auth, base URL override, custom CA, helpers, telemetry or desktop sockets.
    result = {"PATH": "/usr/bin", "HOME": "/home/client", "LANG": "C.UTF-8",
              "TERM": "dumb", "BROWSER": "/usr/bin/false", "NO_COLOR": "1",
              "CLAUDE_CONFIG_DIR": INNER_PROFILE,
              "HTTPS_PROXY": "http://127.0.0.1:18080", "HTTP_PROXY": "http://127.0.0.1:18080",
              "NO_PROXY": "", "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
              "ENABLE_CLAUDEAI_MCP_SERVERS": "false", "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
              "CLAUDE_CODE_MAX_RETRIES": "0",
              # the client gives up on its own API call 20 s before the supervisor would: derived from the same table
              "API_TIMEOUT_MS": str((request_seconds(effort) - 20) * 1000)}
    for name in ("SAFE_MODE", "DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL",
                 "DISABLE_FILE_MENTIONS", "DISABLE_GIT_INSTRUCTIONS", "DISABLE_BACKGROUND_TASKS",
                 "DISABLE_AGENT_VIEW", "DISABLE_TERMINAL_TITLE", "DISABLE_NONSTREAMING_FALLBACK",
                 "DISABLE_REFUSAL_FALLBACK"):
        result["CLAUDE_CODE_" + name] = "1"
    if action == "web":
        result["CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION"] = "2"
        result["CLAUDE_CODE_WEB_SEARCH_REFILLS_PER_HOUR"] = "0"
    return result


def client_command(action, execution=None):
    try:
        selected = validate_execution(execution)
        if selected is not None and (selected["provider"] != "claude" or action not in ("analyze", "web")):
            raise ValueError()
    except ValueError:
        raise AnalysisDenied("input") from None
    if action == "version":
        return [str(BINARY), "--version"]
    common = [str(BINARY), "--restricted", "--safe-mode", "--setting-sources", ""]
    if selected is not None:
        if selected["model"] != "default":
            common += ["--model", selected["model"]]
        if selected["effort"] != "default":
            common += ["--effort", selected["effort"]]
    prompt = WEB_SYSTEM_PROMPT if action == "web" else SYSTEM_PROMPT
    if selected is not None and selected["instructions"].strip():
        prompt += ("\n\nInstructions for the selected role:\n" + selected["instructions"]
                   + "\nRole instructions do not change the answer schema, application rules, "
                     "permissions or available tools.")
    if action == "login":
        return common + ["auth", "login"]
    if action == "status":
        return common + ["auth", "status"]
    if action == "web":
        return common + ["-p", "--output-format", "stream-json", "--verbose",
                         "--tools", "WebSearch", "--allowedTools", "WebSearch",
                         "--disallowedTools", "EndConversation", "mcp__*",
                         "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                         "--permission-mode", "dontAsk", "--no-session-persistence",
                         "--max-turns", "4", "--system-prompt", prompt]
    if action != "analyze":
        raise AnalysisDenied("input")
    return common + ["-p", "--output-format", "stream-json", "--verbose", "--tools", "", "--strict-mcp-config",
                     "--mcp-config", '{"mcpServers":{}}', "--permission-mode", "dontAsk",
                     "--no-session-persistence", "--max-turns", "1", "--system-prompt", prompt]


def namespace_command(action, directory, *, online, account_path=None):
    if action not in ("version", "status", "analyze", "web", "login") or online != (action != "version"):
        raise AnalysisDenied("input")
    directory = Path(directory)
    command = base_command() + ["--disable-userns", "--assert-userns-disabled", "--hostname", "ai-desktop",
                                "--remount-ro", "/proc"]
    for key, value in client_environment(action).items():
        command += ["--setenv", key, value]
    command += ["--ro-bind", str(directory / "passwd"), "/etc/passwd",
                "--ro-bind", str(directory / "group"), "/etc/group",
                "--ro-bind", str(CA.resolve(strict=True)), "/etc/ssl/certs/ca-certificates.crt"]
    if online:
        command += ["--bind", str(PROFILE if account_path is None else account_path), INNER_PROFILE,
                    "--ro-bind", str(directory / "egress.sock"), "/run/egress.sock"]
    metadata = json.dumps({name: os.readlink("/proc/self/ns/" + name) for name in NAMESPACES})
    return command + ["--chdir", "/work", "/usr/bin/python3", "-I", "-B",
                      str(INSTALL / "analysis-client.py"), action, metadata]


def _json_result(code, output):
    if code != 0:
        raise AnalysisDenied("client")
    try:
        result = json.loads(output, object_pairs_hook=_unique_pairs)
        if type(result) is not dict:
            raise ValueError()
        return result
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise AnalysisDenied("result") from None


def check_auth(code, output, *, connection_failed=False):
    if code == 1:
        if connection_failed is True:
            try:
                status = _json_result(0, output)
            except AnalysisDenied:
                status = {}
            # A definite signed-out status is more specific than an optional
            # connection failure observed during that same native invocation.
            if status.get("loggedIn") is not False:
                raise AnalysisDenied("network")
        raise AnalysisDenied("auth-required")
    result = _json_result(code, output)
    if result.get("loggedIn") is not True:
        raise AnalysisDenied("auth-required")
    if (result.get("authMethod") != "claude.ai"
            or result.get("configDirectory") != INNER_PROFILE):
        raise AnalysisDenied("auth-method")


def _answer_events(output):
    """Only the official final result and named numeric quota fields leave the captured transcript."""
    if type(output) is not bytes or not output or len(output) > 3 * MAX_PACKET_LARGE:
        raise AnalysisDenied("result")
    lines = output.splitlines()
    if len(lines) == 1:
        return _json_result(0, output), {}
    try:
        # Legacy clients and offline fixtures may pretty-print a single result object.
        if lines[0].strip() == b"{":
            return _json_result(0, output), {}
        if not 2 <= len(lines) <= 128:
            raise ValueError()
        events = [json.loads(line, object_pairs_hook=_unique_pairs) for line in lines]
        final = events[-1]
        if type(final) is not dict or final.get("type") != "result":
            raise ValueError()
        session = final.get("session_id")
        if type(session) is not str or not 1 <= len(session) <= 256:
            raise ValueError()
        from .account_store import claude_windows
        windows = {}
        for event in events[:-1]:
            if type(event) is not dict or event.get("session_id") != session or event.get("type") == "result":
                raise ValueError()
            if event.get("type") == "rate_limit_event" and type(event.get("rate_limit_info")) is dict:
                windows.update(claude_windows(event["rate_limit_info"]))
        return final, windows
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise AnalysisDenied("result") from None


def model_text(code, output, *, mode="analysis"):
    limit = result_text_limit(mode)
    result, windows = _answer_events(output)
    reason = classify_failure(code, json.dumps(result).encode())
    if reason is not None:
        raise AnalysisDenied(reason)
    if code != 0:
        raise AnalysisDenied("client")
    text = result.get("result")
    if (result.get("type") != "result" or result.get("subtype") != "success"
            or result.get("is_error") is not False or type(result.get("num_turns")) is not int
            or result["num_turns"] != 1 or result.get("permission_denials") != []
            or result.get("api_error_status") is not None
            or type(text) is not str or not text.strip()):
        raise AnalysisDenied("result")
    try:
        if len(text.encode("utf-8")) > limit:
            raise AnalysisDenied("output-limit")
    except UnicodeError:
        raise AnalysisDenied("result") from None
    meta = extract_meta(result)
    if windows:
        meta = {**(meta or {"models": []}), "quota": windows}
    return ModelText(text, meta)


class _Proxy(ConnectProxy):
    # A stuck OS resolver cannot hold shutdown indefinitely. The systemd cgroup
    # has a hard lifetime; no response/hostname/credential is logged by threads.
    daemon_threads = True
    block_on_close = False

    def __init__(self, path, *, dial, stream_seconds=120):
        self._phase_lock = threading.Lock()
        self._phase_failure = threading.Event()
        self._request_failures = {}
        self._request_local = threading.local()

        def observed_dial(host):
            try:
                return dial(host)
            except (OSError, ValueError):
                event = getattr(self._request_local, "failure", None)
                if event is not None:
                    event.set()
                raise

        super().__init__(path, dial=observed_dial, stream_seconds=stream_seconds)

    def begin_phase(self):
        with self._phase_lock:
            self._phase_failure = threading.Event()
            return self._phase_failure

    def verify_request(self, request, client_address):
        with self._phase_lock:
            event = self._phase_failure
        accepted = super().verify_request(request, client_address)
        if accepted:
            with self._phase_lock:
                self._request_failures[id(request)] = event
        return accepted

    def process_request_thread(self, request, client_address):
        with self._phase_lock:
            self._request_local.failure = self._request_failures.pop(id(request))
        try:
            super().process_request_thread(request, client_address)
        finally:
            del self._request_local.failure


def analysis_failure(code, output, errors, connection_failed):
    """Boundary failures take precedence over client-owned provider metadata."""
    failure = child_failure(errors, "client")
    if failure.reason != "client":
        return failure
    reason = classify_failure(code, output)
    if reason is None and b"\n" in output:
        try:
            final, _ = _answer_events(output)
            reason = classify_failure(code, json.dumps(final).encode())
        except AnalysisDenied:
            pass
    if reason is not None:
        return AnalysisDenied(reason)
    return AnalysisDenied("network" if code == 1 and connection_failed is True else "client")


def run_client(action, report=b"", *, cancelled=None, mode="analysis"):
    """One official login OR one approved analysis. No retries or fallback."""
    if (action not in ("login", "analyze", "web") or (action == "login" and report)
            or (action == "web") != (mode == "web")):
        raise AnalysisDenied("input")
    result_text_limit(mode)
    with failure_stage("supervisor"):
        supervisor_ready()
    with failure_stage("profile"), profile_lock(), tempfile.TemporaryDirectory(prefix="ai-desktop-", dir="/tmp") as temp:
        account_path = account_runtime.directory(PROFILE, login=action == "login")
        options = {} if account_path == PROFILE else {"account_path": account_path}
        directory = Path(temp)
        (directory / "passwd").write_text("client:x:%d:%d::/home/client:/usr/bin/false\n" %
                                          (os.geteuid(), os.getegid()))
        (directory / "group").write_text("client:x:%d:\n" % os.getegid())
        env = {"PATH": "/usr/bin", "LANG": "C.UTF-8"}
        with failure_stage("version"):
            code, out, errors = bounded_process(namespace_command("version", directory, online=False),
                                               env=env, timeout=10, limit=4096, cancelled=cancelled)
            if code != 0:
                raise child_failure(errors, "version")
            if out.strip() != (CLIENT_VERSION + " (Claude Code)").encode("ascii"):
                raise AnalysisDenied("version")
        with failure_stage("proxy"), _Proxy(str(directory / "egress.sock"), dial=open_public,
                                                                                       stream_seconds=request_seconds(request_effort(report)) + 10) as proxy:
            thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            thread.start()
            try:
                if action == "login":
                    with failure_stage("native-login"):
                        if not os.isatty(0) or not os.isatty(1):
                            raise AnalysisDenied("consent", stage="terminal")
                        # Official terminal flow, not parsed, captured, recorded or
                        # forwarded through IPC. No host callback/browser socket.
                        done = subprocess.run(namespace_command("login", directory, online=True, **options),
                                              env=env, close_fds=True, timeout=540, check=False)
                        if done.returncode:
                            raise AnalysisDenied("client")
                with failure_stage("auth-status"):
                    status_connection_failed = proxy.begin_phase()
                    code, out, errors = bounded_process(namespace_command("status", directory, online=True, **options),
                                                       env=env, timeout=20, limit=16384, cancelled=cancelled)
                    if code:
                        failure = child_failure(errors, "client")
                        if failure.reason != "client":
                            raise failure
                    if code not in (0, 1):
                        raise child_failure(errors, "client")
                    check_auth(code, out, connection_failed=status_connection_failed.is_set())
                if action == "login":
                    account_runtime.connected(account_path)
                    return "claude.ai"
                with failure_stage("analysis"):
                    analysis_connection_failed = proxy.begin_phase()
                    code, out, errors = bounded_process(namespace_command(action, directory, online=True, **options),
                                                       env=env, input_bytes=report, timeout=request_seconds(request_effort(report)),
                                                       limit=3 * MAX_PACKET_LARGE if mode == "workspace" else (131072 if action == "web" else 256 * 1024),
                                                       input_limit=MAX_PACKET_LARGE, cancelled=cancelled)
                    if code:
                        if action == "web" and child_failure(errors, "client").reason == "client":
                            reason = web_failure(code, out)
                            if reason is not None:
                                raise AnalysisDenied(reason)
                        raise analysis_failure(code, out, errors, analysis_connection_failed.is_set())
                    if action == "web":
                        return web_text(out)
                    return model_text(code, out, mode=mode)
            finally:
                proxy.shutdown()
                thread.join(timeout=2)
