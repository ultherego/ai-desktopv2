"""Trusted Codex bootstrap inside fresh namespaces; no credential files."""
import json
import os
from pathlib import Path
import sys
import threading

from .analysis_child import _Bridge, check_boundary
from .analysis_contract import AnalysisDenied, MAX_PACKET_LARGE, canonical, request_seconds, validate_meta
from .analysis_sandbox import failure_stage
from .analysis_seccomp import install_mode_filter
from .codex_protocol import Protocol, run
from .codex_runtime import BINARY, INNER_HOME, PRIVATE_PACKET_BYTES, checked_token, client_environment
from .e2_ipc import _unique_pairs
from .execution import validate as validate_execution


def private_input(raw):
    """Only the supervisor's private pipe carries the ephemeral access token."""
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= PRIVATE_PACKET_BYTES:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
        if type(value) is not dict or set(value) != {"token", "request"} or type(value["request"]) is not dict:
            raise ValueError()
        token = checked_token(value["token"])
        request = canonical(value["request"]).encode("utf-8")
        if len(request) > MAX_PACKET_LARGE:
            raise ValueError()
        selected = validate_execution(value["request"].get("execution"))
        if selected is None or selected["provider"] != "codex":
            raise ValueError()
        Protocol(request, model=selected["model"], effort=selected["effort"])
        return token, request, selected
    except (ValueError, TypeError, UnicodeError, KeyError, RecursionError):
        raise AnalysisDenied("input") from None


def main(argv):
    try:
        if len(argv) != 2 or argv[0] not in ("version", "analyze"):
            raise AnalysisDenied("input")
        with failure_stage("boundary"):
            check_boundary(json.loads(argv[1]))
            # Neither native config nor OAuth files may be inherited. These
            # directories were created empty by this particular bwrap launch.
            if (Path("/home/client/profile").exists() or any(Path(INNER_HOME).iterdir())
                    or any(Path("/work").iterdir())):
                raise AnalysisDenied("sandbox")
        with failure_stage("mode-filter"):
            install_mode_filter()
        if argv[0] == "version":
            with failure_stage("native-exec"):
                os.execve(str(BINARY), [str(BINARY), "--version"], client_environment())
        with failure_stage("input"):
            token, request, selected = private_input(sys.stdin.buffer.read(PRIVATE_PACKET_BYTES + 1))
        with failure_stage("bridge"), _Bridge(request_seconds(selected["effort"]) + 10) as bridge:
            thread = threading.Thread(target=bridge.serve_forever, daemon=True)
            thread.start()
            try:
                with failure_stage("native-exec"):
                    text = run(str(BINARY), request, env=client_environment(token),
                               model=selected["model"], effort=selected["effort"],
                               timeout=request_seconds(selected["effort"]))
                # Only a closed final result crosses back to the supervisor.
                # Native stderr, RPC messages and credentials remain private.
                meta = getattr(text, "meta", None)
                if meta is not None:
                    meta = validate_meta(meta)
                if token in text or meta is not None and token in canonical(meta):
                    raise AnalysisDenied("result")
                print(canonical({"text": str(text), "meta": meta}), flush=True)
                return 0
            finally:
                bridge.shutdown()
                thread.join(timeout=2)
    except (AnalysisDenied, OSError, ValueError, TypeError, KeyError, KeyboardInterrupt) as error:
        failure = error if isinstance(error, AnalysisDenied) else AnalysisDenied("internal")
        check = " check=" + failure.diagnostic if failure.diagnostic is not None else ""
        print("CLIENT: REFUSED stage=" + failure.stage + " reason=" + failure.reason + check,
              file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
