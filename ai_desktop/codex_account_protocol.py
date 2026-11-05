"""Account-only stdio RPC for an isolated native Codex 0.166.0 session.

The parent provides a private CODEX_HOME and enforces the namespace, egress and
binary checks. Device challenges are returned only to the explicitly interactive
caller. Account identities, login secrets, credits, banners and server prose
are never included in the public result.
"""
import json
import re

from . import __version__
from . import codex_protocol as rpc
from .account_store import PLANS, codex_windows
from .analysis_contract import AnalysisDenied
from .e2_ipc import _unique_pairs


DEVICE_URL = "https://auth.openai.com/codex/device"
_DEVICE_CODE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\Z", re.ASCII)
_LOGIN_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.ASCII)
_QUOTA_FIELDS = {"rateLimits", "rateLimitsByLimitId", "accountId", "ordinaryUsageAllowed",
                 "rateLimitResetCredits", "rateLimitUpsell"}


def validate_challenge(url, code):
    """Only the pinned device site and a short printable user code may reach the terminal."""
    if (url != DEVICE_URL or type(code) is not str or not 4 <= len(code) <= 32
            or not _DEVICE_CODE.fullmatch(code)):
        raise AnalysisDenied("result")


def fixed_command(binary):
    return rpc.app_server_command(binary, (*rpc.LOCKED_APP_SERVER_CONFIG,
        'model_provider="openai"', 'cli_auth_credentials_store="file"', 'forced_login_method="chatgpt"'))


def _quota(result):
    """Never attribute a different metered pool or a token count to the Codex subscription."""
    if "rateLimits" not in result or set(result) - _QUOTA_FIELDS:
        raise AnalysisDenied("result")
    pools = result.get("rateLimitsByLimitId")
    if pools is None:
        snapshot = result["rateLimits"]
    else:
        if type(pools) is not dict or len(pools) > 128:
            raise AnalysisDenied("result")
        if "codex" not in pools:
            return {}, None
        snapshot = pools["codex"]
    rpc._rate_limits({"rateLimits": snapshot})
    if snapshot.get("limitId") not in (None, "codex"):
        return {}, None
    plan = snapshot.get("planType")
    return codex_windows(snapshot), plan if plan in PLANS else None


class AccountProtocol:
    limits = rpc.Limits(wire_bytes=512 * 1024, line_bytes=128 * 1024, messages=96, stderr_bytes=16384)

    def __init__(self, action, *, challenge=None):
        if action not in ("read", "connect") or (action == "connect" and not callable(challenge)):
            raise AnalysisDenied("input")
        self.action = action
        self.challenge = challenge
        self.state = "new"
        self.login_id = None
        self.plan = None
        self.windows = {}
        self.messages = 0

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
        if self.state == "waiting" and self.login_id:
            return rpc._encode({"id": 99, "method": "account/login/cancel", "params": {"loginId": self.login_id}})
        return b""

    def failure_check(self, reason):
        # Only the closed reason travels back; the turn diagnostics refer to different RPCs.
        return None

    def _account(self):
        self.state = "account"
        return rpc._encode({"id": 2, "method": "account/read", "params": {"refreshToken": False}})

    def _response(self, message):
        ident = message.get("id")
        expected = {"initialize": 0, "device": 1, "account": 2, "limits": 3}.get(self.state)
        if type(ident) is not int or ident != expected:
            raise AnalysisDenied("result")
        if set(message) == {"id", "error"} and type(message["error"]) is dict:
            raise AnalysisDenied("client")
        data = message.get("result")
        if set(message) != {"id", "result"} or type(data) is not dict:
            raise AnalysisDenied("result")
        if self.state == "initialize":
            if not isinstance(data.get("userAgent"), str):
                raise AnalysisDenied("result")
            ready = rpc._encode({"method": "initialized", "params": {}})
            if self.action == "read":
                return [ready, self._account()]
            self.state = "device"
            return [ready, rpc._encode({"id": 1, "method": "account/login/start",
                                        "params": {"type": "chatgptDeviceCode"}})]
        if self.state == "device":
            if (set(data) != {"type", "loginId", "verificationUrl", "userCode"}
                    or data["type"] != "chatgptDeviceCode" or type(data["loginId"]) is not str
                    or not _LOGIN_ID.fullmatch(data["loginId"])):
                raise AnalysisDenied("result")
            validate_challenge(data["verificationUrl"], data["userCode"])
            self.login_id = data["loginId"]
            self.state = "waiting"
            self.challenge(data["verificationUrl"], data["userCode"])
            return []
        if self.state == "account":
            if (not {"account", "requiresOpenaiAuth"} <= set(data)
                    or set(data) - {"account", "requiresOpenaiAuth", "workspaceRouting"}
                    or data["requiresOpenaiAuth"] is not True):
                raise AnalysisDenied("result")
            account = data["account"]
            if account is None:
                self.state = "signed-out"
                raise AnalysisDenied("auth-required")
            if type(account) is not dict or account.get("type") != "chatgpt":
                raise AnalysisDenied("auth-method")
            plan = account.get("planType")
            self.plan = plan if type(plan) is str and plan in PLANS else None
            if self.action == "connect":
                self.state = "done"
                return []
            self.state = "limits"
            return [rpc._encode({"id": 3, "method": "account/rateLimits/read",
                                 "params": {"excludeResetCreditDetails": True}})]
        self.windows, plan = _quota(data)
        self.plan = plan or self.plan
        self.state = "done"
        return []

    def _notification(self, message):
        if "id" in message:
            # This protocol never grants a server request, including tools, tokens, and approvals.
            raise AnalysisDenied("authorization")
        method, params = rpc.notification_fields(message)
        if rpc.startup_notification(method, params):
            return []
        if method == "account/updated":
            if set(params) - {"authMode", "planType"} or params.get("authMode") not in (None, "chatgpt"):
                raise AnalysisDenied("auth-method")
            if params.get("authMode") is None and self.state in ("limits", "done"):
                raise AnalysisDenied("auth-required")
            # This notice can follow device login. Only the explicit account/read proves success.
            return []
        if method == "account/rateLimits/updated":
            rpc._rate_limits(params)
            return []  # Only the response to our explicit read may update the widget.
        if method == "account/login/completed":
            if (self.state != "waiting" or params.get("loginId") != self.login_id
                    or set(params) - {"loginId", "success", "error", "onboardingEntrypoint"}
                    or type(params.get("success")) is not bool):
                raise AnalysisDenied("result")
            if not params["success"]:
                raise AnalysisDenied("auth-required")
            if params.get("error") is not None:
                raise AnalysisDenied("result")
            return [self._account()]
        raise AnalysisDenied("authorization")

    def feed(self, record):
        self.messages += 1
        if (self.messages > self.limits.messages or type(record) is not bytes
                or not 0 < len(record) <= self.limits.line_bytes):
            raise AnalysisDenied("output-limit")
        try:
            data = json.loads(record, object_pairs_hook=_unique_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if type(data) is not dict:
                raise AnalysisDenied("result")
            if "method" in data:
                return self._notification(data)
            return self._response(data)
        except (ValueError, TypeError, UnicodeError, RecursionError, KeyError):
            raise AnalysisDenied("result") from None

    def finish(self):
        if not self.done:
            raise AnalysisDenied("result")
        return {"windows": self.windows, "plan": self.plan}


def run(binary, action, env, *, challenge=None):
    """Run only inside the isolated account namespace; the SIWC token never enters it."""
    from .codex_runtime import client_environment
    if env != client_environment():
        raise AnalysisDenied("input")
    protocol = AccountProtocol(action, challenge=challenge)
    return rpc._exchange(fixed_command(binary), protocol, env=env, timeout=300 if action == "connect" else 18,
                         stop_after_reply=True)
