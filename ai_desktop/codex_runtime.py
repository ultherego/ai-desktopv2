"""Fixed Codex supervisor: private auth store, empty model home, one request.

Only this trusted service process opens the Sign in with ChatGPT store. The
native model process receives an ephemeral access token over a private pipe;
it never mounts the store, a host home, or a persistent Codex profile. The
compiled OpenAI egress policy is disjoint from the Claude CONNECT policy.
"""
import json
import os
from pathlib import Path
import tempfile
import threading

from . import account_runtime
from .analysis_contract import (request_effort, request_seconds, AnalysisDenied, MAX_PACKET_LARGE, MAX_RESPONSE_BYTES, ModelText,
                                canonical, result_text_limit, terminal_text, validate_meta)
from .analysis_sandbox import (CA, HOME, INSTALL, NAMESPACES, child_failure,
                               cloud_transport_ready, failure_stage, profile_lock, service_identity, trusted_file)
from .codex_protocol import CLIENT_VERSION, Protocol, TOKEN_ENV
from .e2_connect_proxy import ANALYSIS_STREAM_LIMIT, ConnectProxy, OPENAI_HOSTS, open_public
from .e2_ipc import _unique_pairs
from .e2_subscription_boundary import base_command
from .execution import validate as validate_execution
from .process_io import MAX_PRIVATE_INPUT_BYTES, bounded_process
from .siwc_auth import AuthClient, AuthError, AuthStore

BINARY = INSTALL / "codex"
PROFILE = HOME / "codex"
INNER_HOME = "/home/client/.codex"
MAX_TOKEN_BYTES = 16 * 1024
PRIVATE_PACKET_BYTES = MAX_PRIVATE_INPUT_BYTES


def runtime_ready():
    """Local installed-file checks only; never execute or inspect credentials."""
    trusted_file(BINARY)
    if not os.access(BINARY, os.X_OK):
        raise AnalysisDenied("runtime")
    with BINARY.open("rb") as source:
        if source.read(4) != b"\x7fELF":
            raise AnalysisDenied("runtime")
    trusted_file(INSTALL / "codex-client.py")
    cloud_transport_ready()


def supervisor_ready():
    service_identity()
    runtime_ready()


def checked_token(value):
    # Secret is a deliberately redacted str subclass. Never include it in an
    # error, argv, endpoint, filename, diagnostic, model prompt or result.
    if (not isinstance(value, str) or not 1 <= len(value) <= MAX_TOKEN_BYTES
            or any(not 33 <= ord(c) <= 126 for c in value)):
        raise AnalysisDenied("auth-required")
    return str(value)


def client_environment(token=None):
    environment = {"PATH": "/usr/bin", "HOME": "/home/client", "CODEX_HOME": INNER_HOME,
                   "LANG": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1", "BROWSER": "/usr/bin/false",
                   "HTTPS_PROXY": "http://127.0.0.1:18080", "HTTP_PROXY": "http://127.0.0.1:18080",
                   "NO_PROXY": "", "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
                   "CODEX_CA_CERTIFICATE": "/etc/ssl/certs/ca-certificates.crt"}
    if token is not None:
        environment[TOKEN_ENV] = checked_token(token)
    return environment


def namespace_command(action, directory):
    if action not in ("version", "analyze", "quota-connect", "quota-read"):
        raise AnalysisDenied("input")
    directory = Path(directory)
    command = base_command() + ["--disable-userns", "--assert-userns-disabled", "--hostname", "ai-desktop",
                                "--remount-ro", "/proc", "--dir", INNER_HOME]
    for key, value in client_environment().items():
        command += ["--setenv", key, value]
    command += ["--ro-bind", str(directory / "passwd"), "/etc/passwd",
                "--ro-bind", str(directory / "group"), "/etc/group",
                "--ro-bind", str(CA.resolve(strict=True)), "/etc/ssl/certs/ca-certificates.crt"]
    if action in ("quota-connect", "quota-read"):
        # A disposable home contains, at most, the selected native credential.
        # Native config, hooks, shell state, logs and databases never cross runs.
        command += ["--bind", str(directory / "native-home"), INNER_HOME]
    if action != "version":
        command += ["--ro-bind", str(directory / "egress.sock"), "/run/egress.sock"]
    metadata = json.dumps({name: os.readlink("/proc/self/ns/" + name) for name in NAMESPACES})
    return command + ["--chdir", "/work", "/usr/bin/python3", "-I", "-B",
                      str(INSTALL / "codex-client.py"), action, metadata]


class OpenAIProxy(ConnectProxy):
    daemon_threads = True
    block_on_close = False

    def __init__(self, path, stream_seconds=120):
        def fixed_dial(host):
            return open_public(host, hosts=OPENAI_HOSTS)
        super().__init__(path, dial=fixed_dial, stream_seconds=stream_seconds, hosts=OPENAI_HOSTS,
                         stream_limit=ANALYSIS_STREAM_LIMIT)


def private_packet(token, request):
    token = checked_token(token)
    if type(request) is not bytes or not 0 < len(request) <= MAX_PACKET_LARGE:
        raise AnalysisDenied("input")
    try:
        data = json.loads(request, object_pairs_hook=_unique_pairs)
        if type(data) is not dict:
            raise ValueError()
        packet = canonical({"token": token, "request": data}).encode("utf-8")
        if len(packet) > PRIVATE_PACKET_BYTES:
            raise ValueError()
        return packet
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise AnalysisDenied("input") from None


def result_text(output, *, mode, token):
    try:
        value = json.loads(output, object_pairs_hook=_unique_pairs)
        if type(value) is not dict or set(value) != {"text", "meta"}:
            raise ValueError()
        text = value["text"]
        if type(text) is not str or not text.strip() or token in text:
            raise ValueError()
        if len(text.encode("utf-8")) > result_text_limit(mode):
            raise AnalysisDenied("output-limit")
        if terminal_text(text) != text:
            raise ValueError()
        meta = validate_meta(value["meta"]) if value["meta"] is not None else None
        if meta is not None and token in canonical(meta):
            raise ValueError()
        return ModelText(text, meta)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise AnalysisDenied("result") from None


def _auth_failure(error):
    reason = {"busy": "busy", "network": "network", "dependency": "runtime", "identity": "identity",
              "storage": "auth-required", "expired": "auth-required", "denied": "auth-required",
              "scope": "auth-required"}.get(error.reason, "auth-required")
    return AnalysisDenied(reason, stage="auth-status")


def run_client(action, report=b"", *, cancelled=None, mode="analysis"):
    """Exactly one validated request, no native OAuth/profile or provider fallback."""
    if action != "analyze" or mode == "web":
        raise AnalysisDenied("input")
    try:
        data = json.loads(report, object_pairs_hook=_unique_pairs)
        selected = validate_execution(data.get("execution")) if type(data) is dict else None
        if selected is None or selected["provider"] != "codex":
            raise ValueError()
        protocol = Protocol(report, model=selected["model"], effort=selected["effort"])
        if protocol.mode != mode:
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, KeyError, RecursionError):
        raise AnalysisDenied("input") from None
    if cancelled is not None and cancelled():
        raise AnalysisDenied("cancelled")
    with failure_stage("supervisor"):
        supervisor_ready()
    with failure_stage("profile"), profile_lock(), tempfile.TemporaryDirectory(prefix="ai-desktop-codex-", dir="/tmp") as temp:
        directory = Path(temp)
        (directory / "passwd").write_text("client:x:%d:%d::/home/client:/usr/bin/false\n" %
                                          (os.geteuid(), os.getegid()))
        (directory / "group").write_text("client:x:%d:\n" % os.getegid())
        env = {"PATH": "/usr/bin", "LANG": "C.UTF-8"}
        # This fixed offline version check happens before any credential read
        # or auth refresh. It has no proxy socket or profile mounted.
        with failure_stage("version"):
            code, output, errors = bounded_process(namespace_command("version", directory), env=env,
                                                  timeout=10, limit=4096, cancelled=cancelled)
            if code != 0:
                raise child_failure(errors, "version", provider="codex")
            if output.strip() != ("codex-cli " + CLIENT_VERSION).encode("ascii"):
                raise AnalysisDenied("version")
        if cancelled is not None and cancelled():
            raise AnalysisDenied("cancelled")
        try:
            token = checked_token(AuthClient(AuthStore(account_runtime.directory(PROFILE))).access_token())
        except AuthError as error:
            raise _auth_failure(error) from None
        packet = private_packet(token, report)
        with failure_stage("proxy"), OpenAIProxy(
                str(directory / "egress.sock"), stream_seconds=request_seconds(request_effort(report)) + 10) as proxy:
            thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            thread.start()
            try:
                with failure_stage("analysis"):
                    code, output, errors = bounded_process(namespace_command("analyze", directory), env=env,
                        input_bytes=packet, input_limit=PRIVATE_PACKET_BYTES, private_input=True,
                        timeout=request_seconds(request_effort(report)) + 5, limit=MAX_RESPONSE_BYTES if mode == "workspace" else 131072, cancelled=cancelled)
                    if code != 0:
                        raise child_failure(errors, "client", provider="codex")
                    return result_text(output, mode=mode, token=token)
            finally:
                proxy.shutdown()
                thread.join(timeout=2)
