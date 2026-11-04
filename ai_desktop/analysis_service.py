"""Separate systemd activation endpoint for the explicitly approved A+ path."""
import os
import json
from pathlib import Path
import re
import select
import socket
import stat

from .analysis_contract import (SERVICE_MARGIN, request_seconds, AnalysisDenied, Approval, MAX_PACKET, REASONS,
                                digest, parse_request, read_result, result_packet)
from .analysis_sandbox import run_client, supervisor_ready
from .analysis_framing import send_message, receive_message
from . import e2_account, e2_ipc

DIRECTORY = "/run/ai-desktop-analysis"
READY = b"ANALYSIS2 READY"
REFUSED = b"ANALYSIS2 REFUSED "
MAX_CONTROL = 128
UID = re.compile(r"[1-9][0-9]{0,9}\Z", re.ASCII)


def socket_path(uid):
    if type(uid) is not int or not 0 < uid < 2**32 - 1:
        raise AnalysisDenied("identity")
    return DIRECTORY + "/" + str(uid) + ".sock"


def endpoint(uid):
    path = socket_path(uid)
    for name in ("/", "/run", DIRECTORY):
        info = os.lstat(name)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise AnalysisDenied("endpoint")
    info = os.lstat(path)
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise AnalysisDenied("endpoint")
    return info.st_dev, info.st_ino


def manager_peer(channel, uid):
    if (channel.family != socket.AF_UNIX
            or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
            or channel.getpeername() != socket_path(uid)
            or e2_ipc._kernel_peer(channel) != (1, 0, 0)):
        raise AnalysisDenied("endpoint")


def local_readiness():
    """Metadata-only check. No activation, profile access or native execution."""
    from .analysis_sandbox import runtime_ready
    try:
        uid = os.geteuid()
        if uid == 0 or uid != os.getuid() or not e2_account.account_ready():
            raise AnalysisDenied("identity")
        endpoint(uid)
    except (OSError, ValueError, KeyError):
        raise AnalysisDenied("endpoint") from None
    try:
        runtime_ready()
    except (OSError, ValueError):
        raise AnalysisDenied("runtime") from None


def packet(channel):
    """One small readiness/refusal record; application messages are framed."""
    body, ancillary, flags, _ = channel.recvmsg(MAX_CONTROL + 1, 0)
    if not 0 < len(body) <= MAX_CONTROL or ancillary or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        raise AnalysisDenied("input")
    return body


def send_packet(channel, body):
    if type(body) is not bytes or not 0 < len(body) <= MAX_CONTROL or channel.send(body) != len(body):
        raise AnalysisDenied("result")


def send_once(approval):
    send_once.last_meta = None
    try:
        uid = os.geteuid()
        if (type(approval) is not Approval or os.getuid() != uid or uid == 0
                or digest(approval.payload) != approval.sha256 or not e2_account.account_ready()):
            raise AnalysisDenied("consent")
        body = approval.payload.encode("utf-8")
        model_input, _ = parse_request(body)
        parsed = json.loads(model_input)
        mode = parsed["mode"]
        selected = parsed.get("execution")
        provider = selected["provider"] if selected is not None else "claude"
        before = endpoint(uid)
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as conn:
            conn.settimeout(5)
            conn.connect(socket_path(uid))
            manager_peer(conn, uid)
            if endpoint(uid) != before:
                raise AnalysisDenied("endpoint")
            ready = packet(conn)
            if ready != READY:
                if ready.startswith(REFUSED):
                    reason = ready[len(REFUSED):].decode("ascii")
                    if reason in REASONS:
                        raise AnalysisDenied(reason)
                raise AnalysisDenied("endpoint")
            send_message(conn, body, kind="request")
            result = read_result(receive_message(conn, kind="response", timeout=request_seconds(
                                         selected["effort"] if selected else None) + SERVICE_MARGIN), approval.sha256,
                                 mode=mode, provider=provider)
            send_once.last_meta = getattr(result, "meta", None)      # closed model metadata, kept apart from the text
            return str(result)                                       # callers get the plain text they always got
    except socket.timeout:
        raise AnalysisDenied("timeout") from None
    except (OSError, ValueError, TypeError, KeyError):
        raise AnalysisDenied("endpoint") from None


def activation_listener(uid):
    env = {key: os.environ.pop(key, None) for key in (
        "LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES", "LISTEN_PIDFDID")}
    if (env["LISTEN_PID"] != str(os.getpid()) or env["LISTEN_FDS"] != "1"
            or env["LISTEN_FDNAMES"] != "analysis"):
        raise AnalysisDenied("endpoint")
    listener = socket.socket(fileno=3)
    try:
        os.set_inheritable(3, False)
        if (listener.family != socket.AF_UNIX
                or listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
                or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1
                or listener.getsockname() != socket_path(uid)):
            raise AnalysisDenied("endpoint")
        return listener
    except BaseException:
        listener.close()
        raise


def receive_once(uid):
    socket_path(uid)
    with activation_listener(uid) as listener:
        if not select.select([listener], [], [], 5)[0]:
            raise AnalysisDenied("timeout")
        with listener.accept()[0] as conn:
            conn.settimeout(5)
            e2_ipc._peer_uid(conn, uid)
            try:
                supervisor_ready()
                if uid == os.geteuid():
                    raise AnalysisDenied("identity")
            except (AnalysisDenied, OSError, ValueError, KeyError) as exc:
                reason = exc.reason if isinstance(exc, AnalysisDenied) else "runtime"
                send_packet(conn, REFUSED + reason.encode("ascii"))
                return
            send_packet(conn, READY)
            model_input, sha256 = parse_request(receive_message(conn, kind="request"))
            parsed = json.loads(model_input)
            mode = parsed["mode"]
            selected = parsed.get("execution")
            provider = selected["provider"] if selected is not None else "claude"

            def cancelled():
                # After the single request, closure OR a second packet cancels.
                return bool(select.select([conn], [], [], 0)[0])

            try:
                if cancelled():
                    raise AnalysisDenied("cancelled")
                action = "web" if mode == "web" else "analyze"
                if selected is not None and selected["provider"] == "codex":
                    from .codex_runtime import run_client as run_codex
                    result = run_codex(action, model_input.encode("utf-8"), cancelled=cancelled, mode=mode)
                else:
                    result = run_client(action, model_input.encode("utf-8"), cancelled=cancelled, mode=mode)
                response = result_packet(sha256, text=result, mode=mode, meta=getattr(result, "meta", None))
            except (AnalysisDenied, OSError, ValueError, KeyError) as exc:
                reason = exc.reason if isinstance(exc, AnalysisDenied) else "internal"
                diagnostic = exc.diagnostic if isinstance(exc, AnalysisDenied) else None
                response = result_packet(sha256, reason=reason, mode=mode, diagnostic=diagnostic, provider=provider)
            send_message(conn, response, kind="response")


def worker_main(argv):
    try:
        if argv in (["login"], ["login-verbose"]):
            # Entered only by the explicit root-admin command, on a private TTY.
            if not os.isatty(0) or not os.isatty(1):
                raise AnalysisDenied("consent", stage="terminal")
            run_client("login")
            if argv == ["login-verbose"]:
                print("LOGIN: PASS auth=claude.ai")
            return 0
        if len(argv) != 1 or not UID.fullmatch(argv[0]):
            raise AnalysisDenied("input")
        receive_once(int(argv[0]))
        return 0
    except (AnalysisDenied, e2_ipc.HandoffDenied, OSError, ValueError, TypeError,
            KeyError, KeyboardInterrupt) as exc:
        if argv in (["login"], ["login-verbose"]):
            failure = exc if isinstance(exc, AnalysisDenied) else AnalysisDenied("internal")
            print("LOGIN: REFUSED stage=" + failure.stage + " reason=" + failure.reason, flush=True)
        return 2
