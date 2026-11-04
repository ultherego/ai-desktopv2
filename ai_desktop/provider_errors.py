"""Redacted native-client failure metadata; never inspect model/error prose.

The official SDK ResultMessage documents api_error_status for CLI >= 2.1.110
when type=result, subtype=success and is_error=True:
https://github.com/anthropics/claude-agent-sdk-python/blob/1ea8cb74ddcdadbfaa9b20dfa8268e18047851e7/src/claude_agent_sdk/types.py
Unknown fields or versions never produce a success or an automatic retry here.
"""
import json

from .e2_ipc import _unique_pairs

MAX_FAILURE_BYTES = 131072
HTTP_REASONS = {401: "auth-required", 429: "rate-limit",
                500: "provider-unavailable", 502: "provider-unavailable",
                503: "provider-unavailable", 504: "provider-unavailable",
                529: "provider-unavailable"}


def _invalid_constant(value):
    raise ValueError("non-JSON number")


def classify_failure(code, output):
    """Return a fixed reason or None; never return any part of native output."""
    if (type(code) is not int or code not in (0, 1) or type(output) is not bytes
            or not 0 < len(output) <= MAX_FAILURE_BYTES):
        return None
    try:
        result = json.loads(output, object_pairs_hook=_unique_pairs,
                            parse_constant=_invalid_constant)
        if (type(result) is not dict or result.get("type") != "result"
                or result.get("subtype") != "success" or result.get("is_error") is not True
                or type(result.get("api_error_status")) is not int):
            return None
        return HTTP_REASONS.get(result["api_error_status"])
    except (ValueError, UnicodeError, TypeError, RecursionError):
        return None
