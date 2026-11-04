"""Bounded WebSearch transcript proof; never fetch URLs or trust model links.

The native stream carries SDKUserMessage.tool_use_result. WebSearchOutput is
documented at https://code.claude.com/docs/en/agent-sdk/typescript . Only its
structured result metadata establishes URL provenance, never tool-result prose.
The pinned CLI's exact stream shape still requires native host acceptance;
unknown shapes fail closed rather than falling back to an unverified answer.
"""
import ipaddress
import json
import math
import re
from urllib.parse import urlsplit

from .analysis_contract import AnalysisDenied, MAX_TEXT, terminal_text
from .e2_ipc import _unique_pairs
from .provider_errors import classify_failure

MAX_STREAM_BYTES = 131072
MAX_EVENTS = 64
MAX_CALLS = 2
MAX_TURNS = 4
MAX_SOURCES = 8


def _invalid_constant(value):
    raise ValueError("non-JSON number")


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant)


def _string(value, limit, *, lines=False):
    if (type(value) is not str or not value.strip() or len(value.encode("utf-8")) > limit
            or terminal_text(value) != value or (not lines and any(c in value for c in "\r\n\t"))):
        raise ValueError("text")
    return value


def _host(value):
    """Syntax only: a public-looking source is not a DNS or content attestation."""
    if type(value) is not str or not value or value != value.strip() or len(value) > 253:
        raise ValueError("host")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        labels = value.rstrip(".").split(".")
        if (len(labels) < 2 or value.endswith(".")
                or labels[-1].isdigit()
                or labels[-1].lower() in {"local", "localhost", "internal", "invalid", "test", "onion"}
                or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                       for label in labels)):
            raise ValueError("host") from None
    else:
        if not address.is_global:
            raise ValueError("host")


def _url(value):
    _string(value, 2048)
    if any(c.isspace() or c in "\\`<>\"" for c in value):
        raise ValueError("URL")
    parsed = urlsplit(value)
    if (parsed.scheme not in ("https", "http") or not parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 80 if parsed.scheme == "http" else 443)):
        raise ValueError("URL")
    _host(parsed.hostname)
    return value


def _events(raw):
    if type(raw) is not bytes or not raw or len(raw) > MAX_STREAM_BYTES:
        raise ValueError("stream")
    lines = raw.splitlines()
    if not 1 <= len(lines) <= MAX_EVENTS or any(not line.strip() for line in lines):
        raise ValueError("events")
    events = [_json(line) for line in lines]
    if any(type(event) is not dict for event in events):
        raise ValueError("event")
    return events


def _ui_invalidation(event, *, session=None):
    """Validate the five-field UI-only notice observed with CLI 2.1.312.

    Its event/uuid values are bounded opaque text, never instructions, tool
    results, URLs or authority. The original stream budget still counts it.
    No optional plugin, subagent or background payload is accepted here.
    """
    if (set(event) != {"type", "subtype", "event", "session_id", "uuid"}
            or event.get("type") != "system" or event.get("subtype") != "ui_invalidate"):
        raise ValueError("UI notice")
    for name in ("event", "session_id", "uuid"):
        _string(event[name], 256)
    if session is not None and event["session_id"] != session:
        raise ValueError("UI session")


def _init_index(events):
    """Only validated UI notices may precede init; never scan past other data."""
    for index, event in enumerate(events):
        if event.get("type") == "system" and event.get("subtype") == "ui_invalidate":
            _ui_invalidation(event)
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            return index
        raise ValueError("init")
    raise ValueError("missing init")


def web_failure(code, raw):
    """Classify only the final structured provider error, without its prose."""
    try:
        events = _events(raw)
        if any(event.get("type") == "result" for event in events[:-1]):
            return None
        reason = classify_failure(code, json.dumps(events[-1]).encode("utf-8"))
        if reason is not None:
            return reason
        if type(code) is int and code in (0, 1):
            start = _init_index(events)
            session = _string(events[start].get("session_id"), 256)
            for notice in events[:start]:
                _ui_invalidation(notice, session=session)
            for event in events[start + 1:]:
                if (event.get("type") == "rate_limit_event" and event.get("session_id") == session
                        and _rate_status(event) == "rejected"):
                    return "rate-limit"
        return None
    except (ValueError, UnicodeError, TypeError, OverflowError, RecursionError):
        return None


def _rate_status(event):
    """Documented quota telemetry grants no tools, results, URLs or turns."""
    info = event.get("rate_limit_info")
    if type(info) is not dict or info.get("status") not in ("allowed", "allowed_warning", "rejected"):
        raise ValueError("rate metadata")
    for field in ("resetsAt", "utilization"):
        if field in info and (type(info[field]) not in (int, float)
                              or not math.isfinite(info[field]) or info[field] < 0):
            raise ValueError("rate metadata")
    for field in ("canUserPurchaseCredits", "hasChargeableSavedPaymentMethod"):
        if field in info and type(info[field]) is not bool:
            raise ValueError("rate metadata")
    if "errorCode" in info and (info["errorCode"] != "credits_required" or info["status"] != "rejected"):
        raise ValueError("rate metadata")
    return info["status"]



def _telemetry(event, pending):
    """Accept only bounded, inert SDK progress; it never proves a search.

    SDKThinkingTokensMessage and SDKToolProgressMessage are documented at
    https://code.claude.com/docs/en/agent-sdk/typescript . The caller has already
    checked session_id. No URL, tool result, permission or turn is derived from
    these messages. Background/subagent progress is deliberately unsupported.
    """
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "thinking_tokens":
        allowed = {"type", "subtype", "session_id", "uuid", "user_message_uuid",
                   "estimated_tokens", "estimated_tokens_delta"}
        numbers = ("estimated_tokens", "estimated_tokens_delta")
        if set(event) - allowed:
            raise ValueError("thinking telemetry")
        if "user_message_uuid" in event:
            _string(event["user_message_uuid"], 256)
    elif kind == "tool_progress":
        allowed = {"type", "session_id", "uuid", "tool_use_id", "tool_name",
                   "parent_tool_use_id", "elapsed_time_seconds", "heartbeat"}
        numbers = ("elapsed_time_seconds",)
        call_id = event.get("tool_use_id")
        if (set(event) - allowed or event.get("tool_name") != "WebSearch"
                or type(call_id) is not str or call_id not in pending
                or event.get("parent_tool_use_id", "missing") is not None
                or ("heartbeat" in event and type(event["heartbeat"]) is not bool)):
            raise ValueError("tool telemetry")
    else:
        return False
    if "uuid" in event:
        _string(event["uuid"], 256)
    for name in numbers:
        value = event.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("telemetry number")
    return True

def _query(value):
    if (type(value) is not dict or not {"query"} <= set(value)
            or set(value) - {"query", "allowed_domains", "blocked_domains"}
            or {"allowed_domains", "blocked_domains"} <= set(value)):
        raise ValueError("query")
    query = _string(value["query"], 2048)
    for name in ("allowed_domains", "blocked_domains"):
        if name in value:
            domains = value[name]
            if type(domains) is not list or not 1 <= len(domains) <= 16:
                raise ValueError("domains")
            for domain in domains:
                _host(domain)
    return query


def _sources(value, query):
    if type(value) is not dict:
        raise AnalysisDenied("result", diagnostic="web-search-shape")
    if value.get("query") != query:
        raise AnalysisDenied("result", diagnostic="web-search-query")
    if type(value.get("results")) is not list or len(value["results"]) > 32:
        raise AnalysisDenied("result", diagnostic="web-search-results")
    if (type(value.get("durationSeconds")) not in (int, float)
            or not math.isfinite(value["durationSeconds"]) or not 0 <= value["durationSeconds"] <= 120):
        raise AnalysisDenied("result", diagnostic="web-search-duration")
    if ("searchCount" in value and (type(value["searchCount"]) is not int
                                    or not 0 <= value["searchCount"] <= 8)):
        raise AnalysisDenied("result", diagnostic="web-search-count")
    sources, backend_ids = {}, set()
    for result in value["results"]:
        if type(result) is str:
            # Backend prose may contain arbitrary text or links. It is not
            # structured evidence and never supplies the displayed source list.
            continue
        if type(result) is not dict or type(result.get("content")) is not list:
            raise ValueError("search result")
        backend_id = _string(result.get("tool_use_id"), 256)
        if backend_id in backend_ids or len(backend_ids) >= 8 or len(result["content"]) > 32:
            raise ValueError("search results")
        backend_ids.add(backend_id)
        for item in result["content"]:
            if type(item) is not dict:
                raise ValueError("source")
            title, url = _string(item.get("title"), 1024), _url(item.get("url"))
            sources.setdefault(url, title)
    if len(sources) > 256:
        raise ValueError("sources")
    return sources


def _message(event, role):
    if (event.get("parent_tool_use_id", "missing") is not None or event.get("agent_id") is not None
            or event.get("isReplay", False) is not False):
        raise ValueError("subagent")
    message = event.get("message")
    if (type(message) is not dict or message.get("role") != role
            or type(message.get("content")) is not list or not 1 <= len(message["content"]) <= 16):
        raise ValueError("message")
    return message["content"]


def _answer(raw, observed):
    check = 'web-answer-json'
    try:
        value = _json(_string(raw, MAX_TEXT, lines=True))
        check = "web-answer-shape"
        if type(value) is not dict or set(value) != {"answer", "sources"}:
            raise ValueError("answer")
        check = "web-answer-text"
        answer = _string(value["answer"], 8192, lines=True).strip()
        check = "web-answer-sources"
        sources = value["sources"]
        if type(sources) is not list or not 1 <= len(sources) <= MAX_SOURCES:
            raise ValueError("cited sources")
        check = "web-answer-provenance"
        if any(type(url) is not str or url not in observed for url in sources) or len(set(sources)) != len(sources):
            raise ValueError("invented source")
        check = "web-answer-references"
        references = {int(number) for number in re.findall(r"\[([0-9]+)\]", answer)}
        if (references != set(range(1, len(sources) + 1))
                or re.search(r"(?i)[a-z][a-z0-9+.-]*://|www\.|\]\(|\[[0-9]+\]:", answer)):
            raise ValueError("references")
        check = "web-answer-render"
        lines = [answer, "", "Źródła:"]
        for index, url in enumerate(sources, 1):
            title = re.sub(r"([\\`*_{}\[\]<>()#+.!|~-])", r"\\\1", observed[url])
            # Inline code preserves literal URL punctuation/entities in Markdown
            # renderers; _url excludes backticks and terminal controls.
            lines.append(f"[{index}] {title} — `{url}`")
        result = "\n".join(lines)
        if len(result.encode("utf-8")) > MAX_TEXT:
            raise AnalysisDenied("output-limit")
        return result
    except (ValueError, UnicodeError, TypeError, KeyError, OverflowError, RecursionError):
        raise AnalysisDenied("result", diagnostic=check) from None


def web_text(raw):
    """Return a cited answer only after a complete, bounded search transcript."""
    check = "web-stream"
    try:
        events = _events(raw)
        failure = web_failure(0, raw)
        if failure is not None:
            raise AnalysisDenied(failure)
        check = "web-init-event"
        start = _init_index(events)
        initial = events[start]
        check = "web-init-tools"
        if initial.get("tools") != ["WebSearch"]:
            raise ValueError("init")
        check = "web-init-mcp"
        if initial.get("mcp_servers") != []:
            raise ValueError("init")
        check = "web-session"
        session = _string(initial.get("session_id"), 256)
        for notice in events[:start]:
            _ui_invalidation(notice, session=session)
        pending, calls, completed, observed = {}, set(), 0, {}
        final = None
        for index, event in enumerate(events[start + 1:], start + 1):
            kind = event.get("type")
            # SDKUserMessage alone permits omission of session_id. Subagents
            # and resumed/injected sessions are not part of this one-shot flow.
            check = "web-session"
            if event.get("session_id", session if kind == "user" else None) != session:
                raise ValueError("session")
            if kind == "assistant":
                check = "web-assistant-message"
                if event.get("error") is not None or event.get("aborted", False) is not False:
                    raise ValueError("assistant error")
                for block in _message(event, "assistant"):
                    check = "web-assistant-tool"
                    if type(block) is not dict:
                        raise ValueError("assistant block")
                    block_type = block.get("type")
                    if block_type in ("text", "thinking", "redacted_thinking"):
                        continue
                    if block_type != "tool_use" or block.get("name") != "WebSearch":
                        raise ValueError("unexpected tool")
                    check = "web-tool-call"
                    call_id = _string(block.get("id"), 256)
                    if call_id in calls or len(calls) >= MAX_CALLS:
                        raise ValueError("calls")
                    calls.add(call_id)
                    check = "web-query"
                    pending[call_id] = _query(block.get("input"))
            elif kind == "user":
                check = "web-tool-result-message"
                blocks = _message(event, "user")
                if len(blocks) != 1 or type(blocks[0]) is not dict:
                    raise ValueError("tool result")
                check = "web-tool-result-pair"
                block = blocks[0]
                call_id = block.get("tool_use_id")
                if (block.get("type") != "tool_result" or type(call_id) is not str or call_id not in pending
                        or ("is_error" in block and block["is_error"] is not False)):
                    raise ValueError("tool result")
                check = "web-search-source"
                observed.update(_sources(event.get("tool_use_result"), pending.pop(call_id)))
                completed += 1
            elif kind == "result":
                check = "web-final-order"
                if index != len(events) - 1:
                    raise ValueError("incomplete result")
                check = "web-final-status"
                if event.get("subtype") != "success" or event.get("is_error") is not False:
                    raise ValueError("incomplete result")
                check = "web-final-turns"
                if type(event.get("num_turns")) is not int or not 1 <= event["num_turns"] <= MAX_TURNS:
                    raise ValueError("incomplete result")
                check = "web-final-permissions"
                if (event.get("permission_denials") != [] or event.get("api_error_status") is not None
                        or event.get("deferred_tool_use") is not None):
                    raise ValueError("incomplete result")
                check = "web-final-pending"
                if pending:
                    raise ValueError("incomplete result")
                final = event.get("result")
            elif kind == "rate_limit_event":
                check = "web-rate-metadata"
                if _rate_status(event) == "rejected":
                    raise AnalysisDenied("rate-limit")
            elif kind == "system" and event.get("subtype") == "ui_invalidate":
                check = "web-telemetry"
                _ui_invalidation(event, session=session)
            else:
                check = "web-telemetry"
                if not _telemetry(event, pending):
                    check = "web-event-type"
                    raise ValueError("unexpected event")
        check = "web-missing-result"
        if final is None or pending:
            raise ValueError("missing result")
        if not completed or not observed:
            raise AnalysisDenied("web-no-results")
        return _answer(final, observed)
    except (ValueError, UnicodeError, TypeError, KeyError, OverflowError, RecursionError):
        raise AnalysisDenied("result", diagnostic=check) from None
