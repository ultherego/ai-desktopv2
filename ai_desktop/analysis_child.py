"""Trusted bootstrap inside the model namespace; fixed entrypoint only."""
import ctypes
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import threading

from .analysis_contract import AnalysisDenied, MAX_PACKET_LARGE, canonical, request_seconds, result_text_limit
from .e2_ipc import _unique_pairs
from .execution import validate as validate_execution
from .analysis_seccomp import install_mode_filter
from .analysis_sandbox import NAMESPACES, client_command, client_environment, failure_stage
from .e2_connect_proxy import relay
from .e2_subscription_boundary import privileges_locked, userns_refused


def check_boundary(metadata):
    if (os.geteuid() == 0 or os.getuid() != os.geteuid()
            or not privileges_locked(Path("/proc/self/status").read_text())
            or set(metadata) != set(NAMESPACES)
            or any(os.readlink("/proc/self/ns/" + name) == metadata[name] for name in NAMESPACES)):
        raise AnalysisDenied("sandbox")
    interfaces = [line.split(":", 1)[0].strip() for line in
                  Path("/proc/net/dev").read_text().splitlines()[2:]]
    if interfaces != ["lo"] or Path("/var/lib/ai-desktop").exists() or Path("/run/user").exists():
        raise AnalysisDenied("sandbox")
    proc_mounts = [line.split(" - ", 1)[0].split() for line in Path("/proc/self/mountinfo").read_text().splitlines()
                   if line.split()[4] == "/proc"]
    if not proc_mounts or "ro" not in proc_mounts[-1][5].split(","):
        raise AnalysisDenied("sandbox")
    count = len(list(Path("/proc/self/task").iterdir()))
    libc = ctypes.CDLL(None, use_errno=True)
    code = libc.unshare(0x10000000)
    if not userns_refused(code, ctypes.get_errno(), thread_count=count):
        raise AnalysisDenied("sandbox")


class _BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as outer:
                outer.settimeout(3)
                outer.connect("/run/egress.sock")
                relay(self.request, outer, seconds=self.server.stream_seconds)
        except (OSError, ValueError):
            pass


class _Bridge(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 8

    def __init__(self, stream_seconds=120):
        self.stream_seconds = stream_seconds
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", 18080), _BridgeHandler)

    def verify_request(self, request, client_address):
        return self.slots.acquire(blocking=False)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass


def native_input(action, raw):
    """Separate bound controller options from the JSON sent to the model."""
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_PACKET_LARGE:
            raise ValueError()
        data = json.loads(raw, object_pairs_hook=_unique_pairs)
        if type(data) is not dict or (data["mode"] == "web") != (action == "web"):
            raise ValueError()
        result_text_limit(data["mode"])
        selected = validate_execution(data.get("execution"))
        if selected is not None:
            if selected["provider"] != "claude" or (selected["access"] == "read-only" and data["mode"] == "workspace"):
                raise ValueError()
            del data["execution"]
            raw = canonical(data).encode("utf-8")
        elif "execution" in data:
            raise ValueError()
        return raw, selected
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise AnalysisDenied("input") from None


def main(argv):
    try:
        if len(argv) != 2 or argv[0] not in ("version", "status", "analyze", "web", "login", "offline-status", "offline-logout"):
            raise AnalysisDenied("input")
        with failure_stage("boundary"):
            check_boundary(json.loads(argv[1]))
        with failure_stage("mode-filter"):
            install_mode_filter()
        body, selected = None, None
        if argv[0] in ("analyze", "web"):
            with failure_stage("input"):
                body, selected = native_input(argv[0], sys.stdin.buffer.read(MAX_PACKET_LARGE + 1))
        command = client_command(argv[0], selected)
        if argv[0] in ("version", "offline-status", "offline-logout"):
            with failure_stage("native-exec"):
                os.execve(command[0], command, client_environment(argv[0]))
        with failure_stage("bridge"), _Bridge(request_seconds(selected["effort"] if selected else None) + 10) as bridge:
            thread = threading.Thread(target=bridge.serve_forever, daemon=True)
            thread.start()
            try:
                # When the supervisor kills bwrap, kernel PID namespace teardown
                # kills all descendants, including ones that create a session.
                with failure_stage("native-exec"):
                    if body is not None:
                        # Output remains inherited and bounded by the outer
                        # supervisor; credentials and native errors are never
                        # captured here. Its PID namespace owns every child.
                        return subprocess.run(command, input=body,
                                              env=client_environment(argv[0], selected['effort'] if selected else None),
                                              close_fds=True, check=False,
                                              timeout=request_seconds(selected['effort'] if selected else None)).returncode
                    return subprocess.call(command, env=client_environment(argv[0]), close_fds=True)
            finally:
                bridge.shutdown()
                thread.join(timeout=2)
    except (AnalysisDenied, OSError, ValueError, TypeError, KeyError, KeyboardInterrupt) as exc:
        failure = exc if isinstance(exc, AnalysisDenied) else AnalysisDenied("internal")
        print("CLIENT: REFUSED stage=" + failure.stage + " reason=" + failure.reason, file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
