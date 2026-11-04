"""E2/E3: approved read-only report handoff over an authenticated Unix socket.

The trusted desktop controller and the dedicated worker are separate UIDs.
A future privileged supervisor must establish and hand off SOCK_SEQPACKET
descriptors; this module cannot create listeners, accounts or Claude sessions.
No token, path, command or model output is accepted in the protocol.
"""
import hashlib
import hmac
import json
import os
import socket
import struct

from .report_policy import serialize_report
from .transfer_consent import PURPOSE, RECIPIENT, TransferAuthorization

MAX_PACKET = 8192
DEADLINE = 3


class HandoffDenied(RuntimeError):
    """A redacted boundary refusal; never carries payload or peer details."""


def _unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate")
        out[key] = value
    return out


def _kernel_peer(channel):
    raw = channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                             struct.calcsize("3i"))
    return struct.unpack("3i", raw)


def _peer_uid(channel, expected_uid):
    """Kernel SO_PEERCRED check on an already established AF_UNIX socket."""
    try:
        if (type(expected_uid) is not int or expected_uid <= 0 or
                os.geteuid() == 0 or expected_uid == os.geteuid() or
                channel.family != socket.AF_UNIX or
                channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET):
            raise ValueError("bad channel")
        pid, uid, gid = _kernel_peer(channel)
        if pid <= 0 or uid != expected_uid or gid <= 0:
            raise ValueError("wrong peer")
    except (OSError, ValueError, TypeError, AttributeError, struct.error):
        raise HandoffDenied("HANDOFF: REFUSED") from None


def _validated(payload, sha256, recipient, purpose):
    try:
        if (type(payload) is not str or type(sha256) is not str or
                type(recipient) is not str or type(purpose) is not str or
                recipient != RECIPIENT or purpose != PURPOSE):
            raise ValueError("fields")
        raw = payload.encode("utf-8", "strict")
        if len(raw) > 4096 or not hmac.compare_digest(
                hashlib.sha256(raw).hexdigest(), sha256):
            raise ValueError("hash")
        document = json.loads(payload, object_pairs_hook=_unique_pairs)
        if serialize_report(document) != payload:
            raise ValueError("noncanonical")
        return payload
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise HandoffDenied("HANDOFF: REFUSED") from None


def encode_approved(authorization):
    """Validate and encode consent; this does not authenticate any transport."""
    if type(authorization) is not TransferAuthorization:
        raise HandoffDenied("HANDOFF: REFUSED")
    payload = _validated(authorization.payload, authorization.sha256,
                         authorization.recipient, authorization.purpose)
    body = json.dumps({
        "schema": 1, "sha256": authorization.sha256,
        "recipient": RECIPIENT, "purpose": PURPOSE, "payload": payload,
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_PACKET:
        raise HandoffDenied("HANDOFF: REFUSED")
    return body


def send_approved(channel, authorization, *, worker_uid):
    """Direct worker transport; the trusted UI must obtain real E3 consent.

    A typed authorization is not itself proof of a human gesture. This path
    still refuses root peers; systemd activation has its own trust checks.
    """
    _peer_uid(channel, worker_uid)
    body = encode_approved(authorization)
    try:
        original = channel.gettimeout()
        try:
            channel.settimeout(DEADLINE)
            if channel.send(body) != len(body):
                raise OSError("partial packet")
        finally:
            channel.settimeout(original)
    except OSError:
        raise HandoffDenied("HANDOFF: REFUSED") from None


def receive_approved(channel, *, desktop_uid):
    """Worker side; returns only a validated diagnostics payload, never OAuth."""
    _peer_uid(channel, desktop_uid)
    try:
        original = channel.gettimeout()
        try:
            channel.settimeout(DEADLINE)
            raw, ancillary, flags, address = channel.recvmsg(MAX_PACKET + 1, 0)
        finally:
            channel.settimeout(original)
        if not raw or len(raw) > MAX_PACKET or ancillary or (
                flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)):
            raise ValueError("bad packet")
        parsed = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_unique_pairs)
        if (type(parsed) is not dict or set(parsed) != {
                "schema", "sha256", "recipient", "purpose", "payload"} or
                type(parsed["schema"]) is not int or parsed["schema"] != 1):
            raise ValueError("schema")
        payload = _validated(parsed["payload"], parsed["sha256"],
                             parsed["recipient"], parsed["purpose"])
        if json.dumps(parsed, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8") != raw:
            raise ValueError("noncanonical packet")
        return payload
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError, HandoffDenied):
        raise HandoffDenied("HANDOFF: REFUSED") from None
