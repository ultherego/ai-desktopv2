"""Bounded offline Codex RPC probe; no thread, turn, login or token request.

Run only inside the verified empty client namespace. A successful probe proves
the locked app-server configuration and these read-only message shapes, not
every account endpoint or future inference behavior. Those retain their strict
protocol checks at use.
"""
import json
import time
import unicodedata

from . import __version__
from . import codex_protocol as rpc
from .analysis_contract import AnalysisDenied
from .codex_account_protocol import fixed_command
from .e2_ipc import _unique_pairs
from .execution import CODEX_MODEL, MODELS


def _models(result):
    if (type(result) is not dict or set(result) - {"data", "nextCursor"}
            or type(result.get("data")) is not list or not 1 <= len(result["data"]) <= 64):
        raise AnalysisDenied("result")
    cursor = result.get("nextCursor")
    if cursor is not None and (type(cursor) is not str or not 1 <= len(cursor) <= 256
                               or any(not 32 <= ord(char) <= 126 for char in cursor)):
        raise AnalysisDenied("result")
    entries, seen = [], set()
    for item in result["data"]:
        if type(item) is not dict:
            raise AnalysisDenied("result")
        ident = item.get("id")
        if (type(ident) is not str or not CODEX_MODEL.fullmatch(ident) or ident in MODELS
                or ident in seen or type(item.get("hidden", False)) is not bool):
            raise AnalysisDenied("result")
        seen.add(ident)
        label = item.get("displayName")
        if (type(label) is not str or not label.strip() or len(label) > 64
                or any(unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp") for char in label)):
            label = ident
        entries.append({"id": ident, "displayName": label, "hidden": item.get("hidden", False)})
    return {"data": entries, "nextCursor": None}


class ProbeProtocol:
    limits = rpc.Limits(wire_bytes=512 * 1024, line_bytes=256 * 1024,
                        messages=96, stderr_bytes=16384)

    def __init__(self, *, native_account=True):
        if type(native_account) is not bool:
            raise AnalysisDenied("input")
        self.native_account = native_account
        self.state = "new"
        self.messages = 0
        self.models = None

    @property
    def done(self):
        return self.state == "done"

    def start(self):
        if self.state != "new":
            raise AnalysisDenied("result")
        self.state = "initialize"
        return rpc._encode({"id": 0, "method": "initialize", "params": {
            "clientInfo": {"name": "AI Desktop", "title": "AI Desktop", "version": __version__}}})

    def interrupt(self):
        return b""

    def failure_check(self, reason):
        return None

    def feed(self, record):
        self.messages += 1
        if (type(record) is not bytes or not 0 < len(record) <= self.limits.line_bytes
                or self.messages > self.limits.messages):
            raise AnalysisDenied("output-limit")
        try:
            value = json.loads(record, object_pairs_hook=_unique_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if type(value) is not dict:
                raise AnalysisDenied("result")
            if "method" in value:
                if "id" in value:
                    raise AnalysisDenied("authorization")
                method, params = rpc.notification_fields(value)
                if not rpc.startup_notification(method, params):
                    raise AnalysisDenied("authorization")
                return []
            if (set(value) != {"id", "result"} or type(value["id"]) is not int
                    or type(value["result"]) is not dict
                    or value["id"] != {"initialize": 0, "account": 1, "models": 2}.get(self.state)):
                raise AnalysisDenied("result")
            result = value["result"]
            if self.state == "initialize":
                agent = result.get("userAgent")
                if (type(agent) is not str or not 1 <= len(agent) <= 512
                        or any(not 32 <= ord(char) <= 126 for char in agent)):
                    raise AnalysisDenied("result")
                self.state = "account"
                if not self.native_account:
                    # Startup has accepted the actual SIWC strict config. Do
                    # not ask this custom provider to enumerate models without
                    # a token; the independent native probe checks model/list.
                    self.state = "done"
                    return []
                return [rpc._encode({"method": "initialized", "params": {}}),
                        rpc._encode({"id": 1, "method": "account/read", "params": {"refreshToken": False}})]
            if self.state == "account":
                if (not {"account", "requiresOpenaiAuth"} <= set(result)
                        or set(result) - {"account", "requiresOpenaiAuth", "workspaceRouting"}
                        or result["account"] is not None or result["requiresOpenaiAuth"] is not True):
                    raise AnalysisDenied("auth-method")
                self.state = "models"
                return [self._models_request()]
            self.models = _models(result)
            self.state = "done"
            return []
        except (ValueError, UnicodeError, KeyError, TypeError, RecursionError):
            raise AnalysisDenied("result") from None

    def _models_request(self):
        return rpc._encode({"id": 2, "method": "model/list", "params": {
            "limit": 64, "includeHidden": False}})

    def finish(self):
        if not self.done:
            raise AnalysisDenied("result")
        return self.models


def run(binary, env):
    from .codex_runtime import client_environment
    if env != client_environment():
        raise AnalysisDenied("input")
    deadline = time.monotonic() + 15
    # The SIWC inference and native account configurations have different
    # provider/auth settings. Validate both before supplying either token kind.
    # No account, model-list or inference RPC is requested from the SIWC config.
    for native in (False, True):
        command = fixed_command(binary) if native else rpc.fixed_command(binary, effort="high")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AnalysisDenied("timeout")
        result = rpc._exchange(command, ProbeProtocol(native_account=native), env=env,
                               timeout=remaining, stop_after_reply=True)
    return result
