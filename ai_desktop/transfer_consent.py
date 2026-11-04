"""E3 read-only transfer consent contract. No AI, network or filesystem writes.

This module is a *trusted UI integration primitive*, not a human consent UI.
Only the local controller may call authorize after a real confirmation gesture.
"""
from dataclasses import dataclass
import hashlib
import hmac
import secrets
import time

from .report_policy import serialize_report

RECIPIENT = "claude-analysis"
PURPOSE = "readonly-diagnostics"
TTL_SECONDS = 180


class ConsentDenied(RuntimeError):
    """Sanitized refusal, without sensitive values."""


@dataclass(frozen=True)
class ConsentPreview:
    payload: str
    sha256: str
    recipient: str
    purpose: str
    nonce: str


@dataclass(frozen=True)
class TransferAuthorization:
    payload: str
    sha256: str
    recipient: str
    purpose: str


class ConsentSession:
    """One preview and one authorization for exactly the same report bytes."""

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._preview = None
        self._deadline = None
        self._used = False

    def preview(self, report):
        if self._preview is not None:
            raise ConsentDenied("CONSENT: REFUSED")
        try:
            payload = serialize_report(report)
        except (TypeError, ValueError):
            raise ConsentDenied("CONSENT: REFUSED") from None
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self._preview = ConsentPreview(
            payload=payload, sha256=digest, recipient=RECIPIENT,
            purpose=PURPOSE, nonce=secrets.token_hex(24),
        )
        self._deadline = self._clock() + TTL_SECONDS
        return self._preview

    def authorize(self, report, *, confirmed_sha256, nonce,
                  recipient=RECIPIENT, purpose=PURPOSE):
        """Trusted UI must verify explicit confirmation before calling this."""
        preview = self._preview
        if preview is None or self._used:
            raise ConsentDenied("CONSENT: REFUSED")
        # Consume on *any* attempt: failed/expired approvals cannot be retried.
        self._used = True
        try:
            if self._clock() > self._deadline:
                raise ValueError("expired")
            if type(confirmed_sha256) is not str or type(nonce) is not str:
                raise ValueError("wrong types")
            if recipient != RECIPIENT or purpose != PURPOSE:
                raise ValueError("changed recipient")
            if not hmac.compare_digest(confirmed_sha256, preview.sha256):
                raise ValueError("hash mismatch")
            if not hmac.compare_digest(nonce, preview.nonce):
                raise ValueError("nonce mismatch")
            payload = serialize_report(report)
            if not hmac.compare_digest(payload.encode("utf-8"),
                                       preview.payload.encode("utf-8")):
                raise ValueError("report changed")
        except (TypeError, ValueError):
            raise ConsentDenied("CONSENT: REFUSED") from None
        return TransferAuthorization(
            payload=payload, sha256=preview.sha256, recipient=RECIPIENT,
            purpose=PURPOSE,
        )
