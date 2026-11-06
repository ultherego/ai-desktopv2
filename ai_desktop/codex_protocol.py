"""Bounded, one-turn Codex app-server protocol for ChatGPT-plan OAuth.

This module neither logs in nor reads credentials. Its caller must validate the
official executable, supply an isolated empty CODEX_HOME and /work, and enforce
the kernel/egress boundary before calling ``exchange``. Configuration switches
and rejecting a tool event do not by themselves prove absence of tool effects.

Protocol/configuration reference, checked 2026-11-02:
https://learn.chatgpt.com/docs/app-server
https://learn.chatgpt.com/docs/config-file/config-reference
https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server
"""
from dataclasses import dataclass
import json
import math
import os
from pathlib import PurePosixPath
import re
import selectors
import signal
import subprocess
import time
import unicodedata

from . import __version__
from .analysis_contract import (AnalysisDenied, MAX_PACKET_LARGE, MAX_RESPONSE_BYTES, REQUEST_SECONDS_LONG, SYSTEM_PROMPT, ModelText, MODEL_ID,
                                application_input, canonical, parse_request,
                                result_text_limit, validate_meta)
from .e2_ipc import _unique_pairs
from .execution import using_history, using_memory, validate as validate_execution, validate_history
from .analysis_diagnostics import codex_diagnostic


API_URL = "https://api.openai.com/v1"
PROVIDER = "openai_chatgpt_plan"
TOKEN_ENV = "ACCESS_TOKEN"
CLIENT_VERSION = "0.166.0"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)
_MODEL = re.compile(r"[a-z][a-z0-9._-]{0,95}\Z", re.ASCII)
EFFORTS = ("default", "low", "medium", "high", "xhigh")
_READ_ONLY = {"type": "readOnly", "networkAccess": False}
NESTED_USERNS_WARNING = "Codex's Linux sandbox uses bubblewrap and needs access to create user namespaces."
CODE_MODE_WARNING = ("Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; "
                     "enable `features.code_mode_host` and install `codex-code-mode-host`.")


@dataclass(frozen=True)
class Limits:
    # A maximum-size application JSON string is escaped once more for RPC and
    # echoed by userMessage lifecycle events. These internal limits include that
    # framing overhead; they do not change the application or final-answer caps.
    wire_bytes: int = 4 * MAX_PACKET_LARGE + 4 * MAX_RESPONSE_BYTES
    line_bytes: int = 2 * MAX_PACKET_LARGE + 65536
    messages: int = 4096
    stderr_bytes: int = 16384
    items: int = 64

    def __post_init__(self):
        for name, maximum in (("wire_bytes", 4 * MAX_PACKET_LARGE + 4 * MAX_RESPONSE_BYTES),
                              ("line_bytes", 2 * MAX_PACKET_LARGE + 65536), ("messages", 4096),
                              ("stderr_bytes", 16384), ("items", 64)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("protocol limits")


def _selection(model, effort):
    if (type(model) is not str or not _MODEL.fullmatch(model)
            or type(effort) is not str or effort not in EFFORTS):
        raise AnalysisDenied("input")


LOCKED_APP_SERVER_CONFIG = (
    'approval_policy="never"', 'sandbox_mode="read-only"',
    'agents.enabled=false', 'features.multi_agent=false',
    'features.shell_tool=false', 'features.apps=false',
    'features.hooks=false', 'features.memories=false',
    'features.goals=false', 'features.remote_plugin=false',
    'features.plugins=false', 'features.js_repl=false',
    'features.browser_use=false', 'features.computer_use=false',
    'features.image_generation=false',
    'features.tool_search=false', 'features.tool_suggest=false',
    'features.apply_patch_freeform=false',
    'features.skip_host_skill_discovery=true',
    'features.skill_mcp_dependency_install=false',
    'features.view_image=false', 'web_search="disabled"',
    # Pinned 0.166.0 warns about every under-development feature we use on purpose (skip_host_skill_discovery);
    # suppressing that notice changes no capability. Code mode stays off; the client still sends one exact notice.
    'suppress_unstable_features_warning=true', 'features.code_mode=false', 'features.code_mode_host=false',
    'mcp_servers={}', 'plugins={}', 'skills.config=[]',
    'skills.bundled.enabled=false', 'skills.include_instructions=false',
    'apps._default.enabled=false', 'project_doc_max_bytes=0',
    'project_doc_fallback_filenames=[]', 'notify=[]',
    'history.persistence="none"', 'analytics.enabled=false',
    'feedback.enabled=false', 'check_for_update_on_startup=false',
)


def app_server_command(binary, config):
    if (not isinstance(binary, (str, os.PathLike))
            or not str(binary).startswith("/") or "\x00" in str(binary)
            or str(PurePosixPath(binary)) != str(binary)
            or ".." in PurePosixPath(binary).parts):
        raise AnalysisDenied("input")
    command = [str(binary), "--strict-config", "app-server", "--listen", "stdio://"]
    for value in config:
        command += ["-c", value]
    return command


def fixed_command(binary, *, model="default", effort="default"):
    """Fixed official SIWC provider; the caller checks binary trust/version.

    No secret is accepted as an argument. Never use an inherited home/config:
    TOML map overrides do not erase entries in other configuration layers.
    ``default`` deliberately omits a model id rather than inventing entitlement.
    """
    _selection(model, effort)
    config = [
        'model_provider="openai_chatgpt_plan"',
        'model_providers.openai_chatgpt_plan.name="ChatGPT plan"',
        'model_providers.openai_chatgpt_plan.base_url="https://api.openai.com/v1"',
        'model_providers.openai_chatgpt_plan.env_key="ACCESS_TOKEN"',
        'model_providers.openai_chatgpt_plan.wire_api="responses"',
        'model_providers.openai_chatgpt_plan.requires_openai_auth=false',
        'model_providers.openai_chatgpt_plan.supports_websockets=false',
        'model_providers.openai_chatgpt_plan.request_max_retries=0',
        'model_providers.openai_chatgpt_plan.stream_max_retries=0',
        *LOCKED_APP_SERVER_CONFIG,
        'cli_auth_credentials_store="ephemeral"',
    ]
    if model != "default":
        config.append("model=" + json.dumps(model))
    if effort != "default":
        config.append("model_reasoning_effort=" + json.dumps(effort))
    return app_server_command(binary, config)


def _encode(value):
    return (canonical(value) + "\n").encode("utf-8")


def _identifier(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise AnalysisDenied("result")
    return value


def notification_fields(value):
    """Validate the pinned server notification envelope, without trusting time.

    0.166.0 adds emittedAtMs outside method/params. Older synthetic envelopes
    may omit it. It never controls deadlines, ordering, identifiers or grants.
    """
    if (type(value) is not dict or set(value) not in ({"method", "params"},
            {"method", "params", "emittedAtMs"}) or type(value["method"]) is not str
            or type(value["params"]) is not dict):
        raise AnalysisDenied("result")
    if "emittedAtMs" in value:
        timestamp = value["emittedAtMs"]
        if type(timestamp) is not int or not 0 <= timestamp < 2**63:
            raise AnalysisDenied("result")
    return value["method"], value["params"]


def _bounded_private_metadata(value):
    try:
        return (type(value) is str and len(value.encode("utf-8")) <= 4096
                and not any(unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp")
                            for char in value))
    except UnicodeError:
        return False


def startup_notification(method, params):
    """Classify only pinned harmless startup notices; grant no facility.

    The userns warning is expected inside our already-established outer boundary
    that deliberately disallows nested namespaces. It does not prove capability
    safety. All other warning prose remains a refusal. Known nullable location
    fields are optional on the pinned wire, and never interpreted as paths.
    """
    if method == "configWarning":
        required = {"summary", "details"}
        if (type(params) is not dict or not required <= set(params)
                or set(params) - required - {"path", "range"} or type(params["summary"]) is not str):
            raise AnalysisDenied("result")
        if (params["summary"] != NESTED_USERNS_WARNING or params["details"] is not None
                or params.get("path") is not None or params.get("range") is not None):
            raise AnalysisDenied("authorization")
        return "nested-userns-warning"
    if method != "remoteControl/status/changed":
        return False
    if (type(params) is not dict or set(params) != {"status", "serverName", "installationId", "environmentId"}
            or type(params["status"]) is not str
            or params["status"] not in ("disabled", "connecting", "connected", "errored")
            or not _bounded_private_metadata(params["serverName"])
            or not _bounded_private_metadata(params["installationId"])
            or params["environmentId"] is not None and type(params["environmentId"]) is not str):
        raise AnalysisDenied("result")
    if params["status"] != "disabled" or params["environmentId"] is not None:
        raise AnalysisDenied("authorization")
    return "remote-status"


def _signed(value, bits):
    return type(value) is int and -(2**(bits - 1)) <= value < 2**(bits - 1)


def _closed_metadata(value, required, optional):
    """Validate one fixed metadata object; never coerce or retain its values."""
    if (type(value) is not dict or not set(required) <= set(value)
            or set(value) - set(required) - set(optional)):
        return False
    for name, item in value.items():
        if name in optional and item is None:
            continue
        check = required.get(name, optional.get(name))
        if not check(item):
            return False
    return True


def _rate_window(value):
    return _closed_metadata(value, {"usedPercent": lambda item: _signed(item, 32)},
        {"windowDurationMins": lambda item: _signed(item, 64), "resetsAt": lambda item: _signed(item, 64)})


def _rate_credits(value):
    return _closed_metadata(value,
        {"hasCredits": lambda item: type(item) is bool, "unlimited": lambda item: type(item) is bool},
        {"balance": _bounded_private_metadata})


def _rate_spend(value):
    return _closed_metadata(value, {"limit": _bounded_private_metadata, "used": _bounded_private_metadata,
        "remainingPercent": lambda item: _signed(item, 32), "resetsAt": lambda item: _signed(item, 64)}, {})


def _rate_limits(params):
    """Check pinned 0.166.0 metadata before selecting numeric windows and a recognized plan.

    v2/account.rs defines optional snapshot fields and signed integer windows;
    protocol/account.rs defines emitted plan names. TokenCountEvent may emit
    this without thread/turn IDs, even without accompanying token-usage data.
    Metadata never changes authority, model output, deadlines, pacing or retries.
    """
    plans = {"free", "go", "plus", "pro", "prolite", "promax", "team",
             "self_serve_business_prolite", "self_serve_business_usage_based", "business", "ent26",
             "enterprise_cbp_automation", "enterprise_cbp_usage_based", "enterprise", "edu", "edu_plus",
             "edu_pro", "sci", "sci_plus", "unknown"}
    reached = {"rate_limit_reached", "workspace_owner_credits_depleted", "workspace_member_credits_depleted",
               "workspace_owner_usage_limit_reached", "workspace_member_usage_limit_reached"}
    fields = {"limitId": _bounded_private_metadata, "limitName": _bounded_private_metadata,
              "normalModelSlug": _bounded_private_metadata, "primary": _rate_window,
              "secondary": _rate_window, "credits": _rate_credits, "individualLimit": _rate_spend,
              "spendControlReached": lambda item: type(item) is bool,
              "planType": lambda item: type(item) is str and item in plans,
              "rateLimitReachedType": lambda item: type(item) is str and item in reached}
    if (type(params) is not dict or set(params) != {"rateLimits"}
            or not _closed_metadata(params["rateLimits"], {}, fields)):
        raise AnalysisDenied("result")


def _provider_failure(info):
    """Classify only pinned 0.166.0 CodexErrorInfo, never native error prose.

    v2/shared.rs uses serde camelCase. Struct variants are externally tagged
    objects with one optional u16 httpStatusCode. Unknown values stay generic.
    """
    units = {
        "usageLimitExceeded": ("rate-limit", "codex-usage-limit"),
        "rateLimitExceeded": ("rate-limit", "codex-rate-limit"),
        "unauthorized": ("auth-required", "codex-unauthorized"),
        "modelNotFound": ("client", "codex-model-unavailable"),
        "serverOverloaded": ("provider-unavailable", "codex-server-overloaded"),
    }
    if type(info) is str:
        return units.get(info, ("client", "codex-server-error"))
    structures = {
        "httpConnectionFailed": "codex-http-connection",
        "responseStreamConnectionFailed": "codex-stream-connection",
        "responseStreamDisconnected": "codex-stream-disconnected",
        "responseTooManyFailedAttempts": "codex-stream-attempts",
    }
    if type(info) is dict and len(info) == 1:
        kind, value = next(iter(info.items()))
        if (kind in structures and type(value) is dict and set(value) == {"httpStatusCode"}
                and (value["httpStatusCode"] is None or type(value["httpStatusCode"]) is int
                     and 0 <= value["httpStatusCode"] <= 65535)):
            status = value["httpStatusCode"]
            if status == 401:
                return "auth-required", "codex-http-unauthorized"
            if status == 403:
                return "authorization", "codex-http-forbidden"
            if status == 429:
                return "rate-limit", "codex-http-rate-limit"
            if status in (500, 502, 503, 504):
                return "provider-unavailable", "codex-http-unavailable"
            return "network", structures[kind]
    return "client", "codex-server-error"


def _validated_input(raw):
    """Accept a full wire envelope or the controller's already-unwrapped JSON.

    Reconstructing the envelope runs every existing closed mode validator; a
    worker cannot smuggle model options, endpoints or an arbitrary system prompt.
    Execution controls are removed from model-visible application data.
    """
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_PACKET_LARGE:
            raise ValueError()
        data = json.loads(raw, object_pairs_hook=_unique_pairs)
        if type(data) is not dict:
            raise ValueError()
        if "schema" in data:
            model_input, _ = parse_request(raw)
            data = json.loads(model_input, object_pairs_hook=_unique_pairs)
        selected = validate_execution(data.get("execution"))
        if selected is not None and selected["provider"] != "codex":
            raise ValueError()
        history = validate_history(data.get("conversation", []))
        memory = data.get("memory")
        if memory is not None:
            from .conversation_memory import validate as validate_memory
            memory = validate_memory(memory)
        from .skill_runtime import validate_knowledge, using_knowledge
        knowledge = data.get("knowledge")
        if knowledge is not None:
            knowledge = validate_knowledge(knowledge)
        mode = data["mode"]
        allowed = {"mode", "execution", "conversation", "memory", "knowledge"}
        if mode == "analysis":
            allowed |= {"report", "question"}
            args, kwargs = (data["report"], data["question"]), {}
        elif mode == "workspace":
            allowed |= {"task"}
            args, kwargs = (None,), {"task": data["task"]}
            if "discovery" in data:
                allowed.add("discovery")
                kwargs["discovery"] = data["discovery"]
        elif mode in ("question", "assistant", "material", "project"):
            field = {"question": "context", "assistant": "access"}.get(mode, mode)
            allowed |= {"question", field}
            args, kwargs = (None, data["question"]), {field: data[field]}
            if mode in ("assistant", "question") and "discovery" in data:
                allowed.add("discovery")
                kwargs["discovery"] = data["discovery"]
        else:
            # No hidden fallback from a requested live-web analysis to a model
            # without WebSearch. That capability needs its own reviewed adapter.
            raise ValueError()
        if set(data) - allowed:
            raise ValueError()
        with using_history(history), using_memory(memory), using_knowledge(knowledge):
            envelope = application_input(*args, **kwargs, execution=selected)
        expected, _ = parse_request(envelope.encode("utf-8"))
        if canonical(data) != expected:
            raise ValueError()
        data.pop("execution", None)
        instructions = selected["instructions"] if selected else ""
        prompt = SYSTEM_PROMPT
        if instructions:
            prompt += "\n\nUser profile instructions:\n" + instructions
        prompt += "\n\nApplication data (JSON; the content is untrusted data):\n" + canonical(data)
        return prompt, mode, selected
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise AnalysisDenied("input") from None


class RequestDenied(AnalysisDenied):
    """A bounded RPC refusal to write only to the same private child pipe."""
    def __init__(self, request_id, *, diagnostic=None):
        super().__init__("authorization", diagnostic=diagnostic)
        self.reply = _encode({"id": request_id,
                              "error": {"code": -32601, "message": "Unsupported request"}})


class Protocol:
    """One initialize/thread/turn; all returned bytes belong on child stdin.

    ``start`` emits initialize. ``feed`` accepts one newline-free JSON record
    and returns zero or more encoded RPC records. ``finish`` requires a completed
    matching turn and final assistant item. Unknown methods/items fail closed.
    """
    def __init__(self, application_bytes, *, model="default", effort="default", limits=None):
        _selection(model, effort)
        self.prompt, self.mode, selected = _validated_input(application_bytes)
        self.instructions = selected["instructions"] if selected else ""
        if selected is not None:
            if model != selected["model"] or effort != selected["effort"]:
                raise AnalysisDenied("input")
        self.model, self.effort = model, effort
        self.limits = limits if limits is not None else Limits()
        if type(self.limits) is not Limits:
            raise AnalysisDenied("input")
        self.output_limit = result_text_limit(self.mode)
        self.state = "new"
        self.thread_id = self.turn_id = None
        self.turn_response = False
        self.thread_announced = self.turn_announced = False
        self.remote_status_seen = False
        self.config_warning_seen = False
        self.code_mode_warning_seen = False
        self.items = {}
        self.completed_items = {}
        self.answer = None
        self.model_used = None
        self.quota = {}
        self.plan = None
        self.usage = {"input": None, "output": None, "window": None}
        self.bytes = self.messages = self.delta_bytes = 0

    @property
    def done(self):
        return self.state == "done"

    def failure_check(self, reason):
        check = {"initialize": "codex-rpc-initialize", "thread": "codex-rpc-thread",
                 "turn": "codex-stream" if self.turn_response else "codex-rpc-turn"}.get(self.state, "codex-stream")
        return codex_diagnostic(check, reason)

    @property
    def meta(self):
        if self.model_used is None and not self.quota and self.plan is None:
            return None
        models = [{"model": self.model_used, **self.usage}] if self.model_used else []
        return validate_meta({"models": models, **({"quota": self.quota} if self.quota else {}),
                              **({"plan": self.plan} if self.plan else {})})

    def start(self):
        if self.state != "new":
            raise AnalysisDenied("result")
        self.state = "initialize"
        return _encode({"id": 0, "method": "initialize", "params": {
            "clientInfo": {"name": "AI Desktop", "title": "AI Desktop", "version": __version__}}})

    def interrupt(self):
        if self.thread_id is None or self.turn_id is None or self.done:
            return b""
        return _encode({"id": 99, "method": "turn/interrupt", "params": {
            "threadId": self.thread_id, "turnId": self.turn_id}})

    def _thread(self, value):
        value = _identifier(value)
        if self.thread_id is None:
            self.thread_id = value
        elif self.thread_id != value:
            raise AnalysisDenied("result")

    def _turn(self, value):
        value = _identifier(value)
        if self.turn_id is None:
            self.turn_id = value
        elif self.turn_id != value:
            raise AnalysisDenied("result")

    def _scope(self, params, *, turn=True):
        if self.thread_id is None or params.get("threadId") != self.thread_id:
            raise AnalysisDenied("result")
        if turn and (self.turn_id is None or params.get("turnId") != self.turn_id):
            raise AnalysisDenied("result")

    def _response(self, data):
        if set(data) != {"id", "result"} or type(data["id"]) is not int or type(data["result"]) is not dict:
            raise AnalysisDenied("result")
        ident, value = data["id"], data["result"]
        if self.state == "initialize" and ident == 0:
            self.state = "thread"
            params = {"cwd": "/work", "ephemeral": True, "approvalPolicy": "never",
                      "sandbox": "read-only", "serviceName": "ai-desktop",
                      "baseInstructions": SYSTEM_PROMPT,
                      "developerInstructions": self.instructions}
            if self.model != "default":
                params["model"] = self.model
            return [_encode({"method": "initialized", "params": {}}),
                    _encode({"id": 1, "method": "thread/start", "params": params})]
        if self.state == "thread" and ident == 1:
            thread = value.get("thread")
            if type(thread) is not dict or thread.get("ephemeral") is not True:
                raise AnalysisDenied("result")
            self._thread(thread.get("id"))
            if (value.get("instructionSources", []) != [] or value.get("approvalPolicy") != "never"
                    or value.get("cwd") != "/work" or value.get("sandbox") != _READ_ONLY
                    or value.get("modelProvider") != PROVIDER):
                raise AnalysisDenied("authorization")
            actual_model = value.get("model")
            if type(actual_model) is not str or not MODEL_ID.fullmatch(actual_model):
                raise AnalysisDenied("result")
            self.model_used = actual_model
            self.state = "turn"
            params = {"threadId": self.thread_id, "cwd": "/work", "approvalPolicy": "never",
                      "sandboxPolicy": json.loads(canonical(_READ_ONLY)),
                      "input": [{"type": "text", "text": self.prompt}]}
            if self.model != "default":
                params["model"] = self.model
            if self.effort != "default":
                params["effort"] = self.effort
            return [_encode({"id": 2, "method": "turn/start", "params": params})]
        if self.state == "turn" and ident == 2 and not self.turn_response:
            turn = value.get("turn")
            if (type(turn) is not dict or turn.get("status") != "inProgress"
                    or turn.get("error") is not None or turn.get("items", []) != []):
                raise AnalysisDenied("result")
            self._turn(turn.get("id"))
            self.turn_response = True
            return []
        raise AnalysisDenied("result")

    def _item(self, params, completed):
        self._scope(params)
        timestamp = "completedAtMs" if completed else "startedAtMs"
        if (set(params) != {"threadId", "turnId", "item", timestamp}
                or type(params["item"]) is not dict or type(params[timestamp]) is not int
                or not 0 <= params[timestamp] < 2**63):
            raise AnalysisDenied("result")
        item = params["item"]
        ident, kind = _identifier(item.get("id")), item.get("type")
        fields = {"agentMessage": {"id", "type", "text", "phase", "delivery", "memoryCitation", "questions"},
                  "userMessage": {"id", "type", "content", "clientId"},
                  "reasoning": {"id", "type", "summary", "content"}}
        if type(kind) is not str or kind not in fields:
            raise AnalysisDenied("authorization")
        if set(item) - fields[kind] - {"startedAtMs", "completedAtMs"}:
            raise AnalysisDenied("result")
        for field in ("startedAtMs", "completedAtMs"):
            value = item.get(field)
            if value is not None and (type(value) is not int or not 0 <= value < 2**63):
                raise AnalysisDenied("result")
        if completed:
            if ident not in self.items or self.items[ident] != (kind, False):
                raise AnalysisDenied("result")
        else:
            if ident in self.items or len(self.items) >= self.limits.items:
                raise AnalysisDenied("result")
        if kind == "agentMessage":
            if any(item.get(field) is not None for field in ("delivery", "memoryCitation", "questions")):
                raise AnalysisDenied("authorization")
            text, phase = item.get("text"), item.get("phase")
            if type(text) is not str or phase not in (None, "commentary", "partial_answer", "final_answer"):
                raise AnalysisDenied("result")
            if len(text.encode("utf-8")) > self.output_limit:
                raise AnalysisDenied("output-limit")
            if completed and phase in (None, "final_answer"):
                if self.answer is not None or not text.strip():
                    raise AnalysisDenied("result")
                self.answer = text
        elif kind == "reasoning":
            for field in ("summary", "content"):
                if type(item.get(field, [])) is not list or any(type(v) is not str for v in item.get(field, [])):
                    raise AnalysisDenied("result")
        else:
            content = item.get("content")
            if type(content) is not list:
                raise AnalysisDenied("result")
            for part in content:
                if (type(part) is not dict or part.get("type") != "text"
                        or type(part.get("text")) is not str
                        or set(part) - {"type", "text", "textElements", "text_elements"}):
                    raise AnalysisDenied("result")
        self.items[ident] = (kind, completed)
        if completed:
            self.completed_items[ident] = {key: value for key, value in item.items()
                                           if key not in ("startedAtMs", "completedAtMs")}

    def _notification(self, method, params):
        if method in ("remoteControl/status/changed", "configWarning"):
            if self.state not in ("thread", "turn"):
                raise AnalysisDenied("result")
            kind = startup_notification(method, params)
            if kind == "remote-status":
                if self.remote_status_seen:
                    raise AnalysisDenied("result")
                self.remote_status_seen = True
            elif kind == "nested-userns-warning":
                if self.config_warning_seen:
                    raise AnalysisDenied("result")
                self.config_warning_seen = True
            else:
                raise AnalysisDenied("authorization")
            return []
        if method == "thread/started" and self.state in ("thread", "turn"):
            if self.thread_announced or set(params) != {"thread"} or type(params["thread"]) is not dict:
                raise AnalysisDenied("result")
            self._thread(params["thread"].get("id"))
            self.thread_announced = True
            return []
        if method == "warning" and self.state == "turn":
            # Observed on the real pinned client right after thread/started, with code mode disabled by our config and
            # no code-mode host shipped. The notice says code mode fails closed, so it grants nothing. Only this exact
            # text, for our own thread, once. Any other warning prose stays a refusal.
            if (set(params) != {"message", "threadId"} or params["message"] != CODE_MODE_WARNING
                    or self.code_mode_warning_seen):
                raise AnalysisDenied("authorization")
            self._scope(params, turn=False)
            self.code_mode_warning_seen = True
            return []
        if method == "thread/status/changed" and self.state in ("turn", "done"):
            self._scope(params, turn=False)
            status = params.get("status")
            if (set(params) != {"threadId", "status"} or type(status) is not dict
                    or set(status) - {"type", "activeFlags"}
                    or status.get("type") not in ("idle", "active")):
                raise AnalysisDenied("result")
            return []
        if method == "account/rateLimits/updated":
            if self.state not in ("turn", "done"):
                raise AnalysisDenied("result")
            _rate_limits(params)
            from .account_store import PLANS, codex_windows
            limits = params["rateLimits"]
            self.quota.update(codex_windows(limits))
            if limits.get("limitId") in (None, "codex") and limits.get("planType") in PLANS:
                self.plan = limits["planType"]
            return []
        if self.state != "turn":
            raise AnalysisDenied("result")
        if method in ("turn/started", "turn/completed"):
            if set(params) != {"threadId", "turn"} or type(params["turn"]) is not dict:
                raise AnalysisDenied("result")
            self._scope(params, turn=False)
            turn = params["turn"]
            self._turn(turn.get("id"))
            if method == "turn/started":
                if (self.turn_announced or turn.get("status") != "inProgress"
                        or turn.get("error") is not None or turn.get("items", []) != []):
                    raise AnalysisDenied("result")
                self.turn_announced = True
            else:
                status = turn.get("status")
                if status == "interrupted":
                    raise AnalysisDenied("cancelled")
                if status == "failed":
                    raise AnalysisDenied("client", diagnostic="codex-turn-failed")
                if (status != "completed" or turn.get("error") is not None or not self.turn_response
                        or self.answer is None or any(not done for _, done in self.items.values())):
                    raise AnalysisDenied("result")
                items = turn.get("items", [])
                if type(items) is not list:
                    raise AnalysisDenied("result")
                seen = set()
                for item in items:
                    if (type(item) is not dict or item.get("id") not in self.items
                            or item["id"] in seen
                            or {key: value for key, value in item.items()
                                if key not in ("startedAtMs", "completedAtMs")} != self.completed_items.get(item["id"])):
                        raise AnalysisDenied("result")
                    seen.add(item["id"])
                self.state = "done"
            return []
        if method in ("item/started", "item/completed"):
            self._item(params, method == "item/completed")
            return []
        if method == "item/agentMessage/delta":
            self._scope(params)
            if (set(params) != {"threadId", "turnId", "itemId", "delta"}
                    or self.items.get(params["itemId"]) != ("agentMessage", False)
                    or type(params["delta"]) is not str):
                raise AnalysisDenied("result")
            self.delta_bytes += len(params["delta"].encode("utf-8"))
            if self.delta_bytes > self.output_limit:
                raise AnalysisDenied("output-limit")
            return []
        if method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta",
                      "item/reasoning/summaryPartAdded"):
            self._scope(params)
            if self.items.get(params.get("itemId")) != ("reasoning", False):
                raise AnalysisDenied("result")
            allowed = {"threadId", "turnId", "itemId", "summaryIndex", "contentIndex", "delta"}
            if set(params) - allowed or ("delta" in params and type(params["delta"]) is not str):
                raise AnalysisDenied("result")
            return []
        if method == "thread/tokenUsage/updated":
            self._scope(params)
            if set(params) != {"threadId", "turnId", "tokenUsage"} or type(params["tokenUsage"]) is not dict:
                raise AnalysisDenied("result")
            usage = params["tokenUsage"]
            if (not {"last", "total"} <= set(usage)
                    or set(usage) - {"last", "total", "modelContextWindow"}):
                raise AnalysisDenied("result")
            keys = {"cachedInputTokens", "inputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens"}
            for key in ("last", "total"):
                counts = usage[key]
                if (type(counts) is not dict or not keys <= set(counts)
                        or set(counts) - keys - {"cacheWriteInputTokens"}
                        or any(type(v) is not int or not 0 <= v <= 10**9 for v in counts.values())):
                    raise AnalysisDenied("result")
            window = usage.get("modelContextWindow")
            if window is not None and (type(window) is not int or not 0 <= window <= 10**9):
                raise AnalysisDenied("result")
            self.usage = {"input": usage["total"]["inputTokens"], "output": usage["total"]["outputTokens"],
                          "window": window}
            return []
        if method == "error":
            error = params.get("error")
            info = error.get("codexErrorInfo") if type(error) is dict else None
            reason, check = _provider_failure(info)
            raise AnalysisDenied(reason, diagnostic=check)
        raise AnalysisDenied("authorization")

    def feed(self, record):
        try:
            if type(record) is not bytes or not 0 < len(record) <= self.limits.line_bytes:
                raise AnalysisDenied("output-limit")
            self.bytes += len(record) + 1
            self.messages += 1
            if self.bytes > self.limits.wire_bytes or self.messages > self.limits.messages:
                raise AnalysisDenied("output-limit")
            data = json.loads(record, object_pairs_hook=_unique_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if type(data) is not dict:
                raise AnalysisDenied("result")
            if "method" in data:
                if type(data["method"]) is not str or type(data.get("params")) is not dict:
                    raise AnalysisDenied("result")
                if "id" in data:
                    if (set(data) != {"method", "params", "id"}
                            or not (type(data["id"]) is int and 0 <= data["id"] < 2**53
                                    or type(data["id"]) is str and _ID.fullmatch(data["id"]))):
                        raise AnalysisDenied("result")
                    raise RequestDenied(data["id"], diagnostic=self.failure_check("authorization"))
                method, params = notification_fields(data)
                return self._notification(method, params)
            if "error" in data:
                raise AnalysisDenied("client", diagnostic=self.failure_check("client"))
            return self._response(data)
        except RequestDenied:
            raise
        except AnalysisDenied as exc:
            raise AnalysisDenied(exc.reason, stage=exc.stage,
                                 diagnostic=exc.diagnostic or self.failure_check(exc.reason)) from None
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise AnalysisDenied("result", diagnostic=self.failure_check("result")) from None

    def finish(self):
        if not self.done or self.answer is None:
            raise AnalysisDenied("result")
        return ModelText(self.answer, self.meta)


def _best_effort_write(stream, raw):
    """Never wait for a refusal/interrupt acknowledgement on a failing child."""
    if raw and not stream.closed:
        try:
            os.write(stream.fileno(), raw[:4096])
        except (OSError, ValueError):
            pass


def exchange(argv, protocol, *, env, timeout=110, cancelled=None):
    """Drive a trusted sandboxed child using bounded nonblocking private pipes.

    The public integration calls this with ``fixed_command`` inside its verified
    PID/network/filesystem namespace. ``argv`` is never obtained from the model.
    Neither stderr nor JSON-RPC error prose escapes this function. This function
    does not authenticate or retry, and always kills/reaps its process group.
    """
    if type(protocol) is not Protocol:
        raise AnalysisDenied("input")
    return _exchange(argv, protocol, env=env, timeout=timeout, cancelled=cancelled)


def _exchange(argv, protocol, *, env, timeout, cancelled=None, stop_after_reply=False):
    """Bounded pipe transport shared by the fixed turn and account RPC state machines."""
    if (type(timeout) not in (int, float)
            or isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= REQUEST_SECONDS_LONG
            or type(argv) not in (list, tuple) or not argv
            or any(type(arg) is not str or "\x00" in arg for arg in argv)
            or type(env) is not dict):
        raise AnalysisDenied("input")
    if cancelled is not None and cancelled():
        raise AnalysisDenied("cancelled")
    initial = protocol.start()
    process = None
    try:
        try:
            process = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
                                       bufsize=0)
        except OSError:
            raise AnalysisDenied("client", diagnostic="codex-launch") from None
        pending, buffered = bytearray(initial), bytearray()
        stderr_bytes = wire_bytes = 0
        deadline = time.monotonic() + timeout
        stop_at, stopped_by_us = None, False
        with selectors.DefaultSelector() as selector:
            for stream, kind in ((process.stdout, "out"), (process.stderr, "err")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, kind)
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "in")
            while selector.get_map() or process.poll() is None:
                if cancelled is not None and cancelled():
                    raise AnalysisDenied("cancelled")
                now = time.monotonic()
                if stop_at is not None and now >= stop_at:
                    # An authenticated account app-server can keep draining background tasks after
                    # replying and receiving EOF. Its authoritative account operation already finished.
                    # Terminate only this private process group and still drain/validate the pipes.
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                            stopped_by_us = True
                        except ProcessLookupError:
                            pass
                    stop_at = None
                remaining = deadline - now
                if remaining <= 0:
                    raise AnalysisDenied("timeout", diagnostic=protocol.failure_check("timeout"))
                for key, _ in selector.select(min(remaining, .1)):
                    if key.data == "in":
                        try:
                            count = os.write(key.fileobj.fileno(), pending[:8192])
                        except BlockingIOError:
                            continue
                        if count <= 0:
                            raise AnalysisDenied("client", diagnostic="codex-input-write")
                        del pending[:count]
                        if not pending:
                            selector.unregister(key.fileobj)
                    else:
                        try:
                            block = os.read(key.fileobj.fileno(), 8192)
                        except BlockingIOError:
                            continue
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        wire_bytes += len(block)
                        if wire_bytes > protocol.limits.wire_bytes:
                            raise AnalysisDenied("output-limit")
                        if key.data == "err":
                            stderr_bytes += len(block)
                            if stderr_bytes > protocol.limits.stderr_bytes:
                                raise AnalysisDenied("output-limit")
                            continue
                        buffered.extend(block)
                        while b"\n" in buffered:
                            record, _, tail = buffered.partition(b"\n")
                            buffered[:] = tail
                            try:
                                outgoing = protocol.feed(bytes(record))
                            except RequestDenied as failure:
                                _best_effort_write(process.stdin, failure.reply)
                                raise
                            for message in outgoing:
                                pending.extend(message)
                            if pending and not process.stdin.closed:
                                try:
                                    selector.get_key(process.stdin)
                                except KeyError:
                                    selector.register(process.stdin, selectors.EVENT_WRITE, "in")
                            if protocol.done and not process.stdin.closed:
                                if pending:
                                    raise AnalysisDenied("result", diagnostic=protocol.failure_check("result"))
                                try:
                                    selector.unregister(process.stdin)
                                except KeyError:
                                    pass
                                process.stdin.close()
                                if stop_after_reply:
                                    stop_at = time.monotonic() + 1
                                else:
                                    deadline = min(deadline, time.monotonic() + 3)
                        if len(buffered) > protocol.limits.line_bytes:
                            raise AnalysisDenied("output-limit")
            if buffered:
                raise AnalysisDenied("result", diagnostic=protocol.failure_check("result"))
            if process.returncode != 0 and not (stopped_by_us and process.returncode == -signal.SIGKILL):
                raise AnalysisDenied("client", diagnostic=("codex-native-exit-2" if process.returncode == 2
                                                          else "codex-native-exit"))
            try:
                return protocol.finish()
            except AnalysisDenied as exc:
                raise AnalysisDenied(exc.reason, stage=exc.stage,
                                     diagnostic=exc.diagnostic or protocol.failure_check(exc.reason)) from None
    except KeyboardInterrupt:
        raise AnalysisDenied("cancelled") from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise AnalysisDenied("client", diagnostic="codex-native-io") from None
    finally:
        if process is not None:
            _best_effort_write(process.stdin, protocol.interrupt())
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


def run(binary, application_bytes, *, env, model="default", effort="default", timeout=110,
        cancelled=None, limits=None):
    """Convenience wrapper for a trusted official binary inside its sandbox."""
    protocol = Protocol(application_bytes, model=model, effort=effort, limits=limits)
    return exchange(fixed_command(binary, model=model, effort=effort), protocol,
                    env=env, timeout=timeout, cancelled=cancelled)
