"""Trusted E2 request gate. Offline policy only; no provider transport or OAuth.

The sandboxed model can return untrusted observations, never a network URL,
credentials or an executable action. Transport remains deliberately disabled
until a separately verified authenticated network broker exists.
"""
import hashlib
import hmac
import re

from . import e2_boundary
from .report_policy import serialize_report

_OPERATION = "diagnostic.readonly"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
# Symbolic identifier, not a URL or an arbitrary host chosen by model output.
_TARGET = "claude-analysis"


class BrokerDenied(RuntimeError):
    """Generic fail-closed refusal. Never include payloads or tokens."""


def _digest(payload):
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_intent(intent, expected_digest):
    """Validate untrusted JSON before granting any meaningful operation."""
    if type(expected_digest) is not str or not _DIGEST.fullmatch(expected_digest):
        raise BrokerDenied("BROKER: REFUSED")
    if type(intent) is not dict or set(intent) != {
        "version", "operation", "payload_sha256"
    }:
        raise BrokerDenied("BROKER: REFUSED")
    if type(intent["version"]) is not int or intent["version"] != 1:
        raise BrokerDenied("BROKER: REFUSED")
    if type(intent["operation"]) is not str or intent["operation"] != _OPERATION:
        raise BrokerDenied("BROKER: REFUSED")
    digest = intent["payload_sha256"]
    if type(digest) is not str or not _DIGEST.fullmatch(digest):
        raise BrokerDenied("BROKER: REFUSED")
    if not hmac.compare_digest(digest, expected_digest):
        raise BrokerDenied("BROKER: REFUSED")
    # The untrusted process never chooses a target, URL, method or credential.
    return {"operation": _OPERATION, "target": _TARGET,
            "payload_sha256": expected_digest}


def prepare(report, approved_sha256, process=e2_boundary.execute):
    """Require explicit digest consent and real boundary results, fail closed.

    This authorizes only a symbolic, OFFLINE intent. It never makes requests
    or loads credentials; no approval flag can enable external network access.
    """
    if type(approved_sha256) is not str or not _DIGEST.fullmatch(approved_sha256):
        raise BrokerDenied("BROKER: REFUSED")
    try:
        payload = serialize_report(report)
    except (ValueError, TypeError):
        raise BrokerDenied("BROKER: REFUSED") from None
    digest = _digest(payload)
    if not hmac.compare_digest(approved_sha256, digest):
        raise BrokerDenied("BROKER: REFUSED")

    try:
        probe = process("--probe")
        if type(probe) is not dict or probe != {"probe": "PASS"}:
            raise BrokerDenied("BROKER: REFUSED")
        inspected = process("--inspect", report)
        if type(inspected) is not dict or inspected != {
            "sha256": digest, "field_count": len(report)
        }:
            raise BrokerDenied("BROKER: REFUSED")
    except (OSError, TypeError, RuntimeError, ValueError):
        raise BrokerDenied("BROKER: REFUSED") from None

    return validate_intent({
        "version": 1, "operation": _OPERATION, "payload_sha256": digest
    }, approved_sha256)


def main(argv=None):
    """No public dispatch; separate authenticated transport does not exist."""
    import sys
    args = sys.argv[1:] if argv is None else argv
    if args == ["status"]:
        print("E2 BROKER: OFFLINE ONLY (no OAuth, no network)")
        return 0
    print("E2 BROKER: REFUSED")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
