"""Read native Codex limit windows through a dedicated per-user systemd socket.

The request is one validated local alias. The kernel supplies the desktop UID.
Only the interactive administrative path can connect or disconnect a grant.
"""
import json
import os
import re
import select
import socket
import time

from . import activation_socket, e2_account, e2_ipc
from .account_store import PLANS, named_account, quota
from .analysis_contract import AnalysisDenied
from .analysis_sandbox import service_identity


DIRECTORY = "/run/ai-desktop-codex-limits"
READY = b"CODEX LIMITS 1 READY"
REFUSED = b"REFUSED "
MAX_PACKET = 1024
READ_TIMEOUT = 45
REASONS = frozenset({"not-connected", "auth-required", "busy", "network", "timeout", "unavailable"})


class QuotaError(ValueError):
    """A stable local status; never includes native messages or server input."""

    def __init__(self, reason="unavailable"):
        self.reason = reason if type(reason) is str and reason in REASONS else "unavailable"
        super().__init__(self.reason)


def validated_uid(value):
    if type(value) is str and re.fullmatch(r"[1-9][0-9]{0,9}", value, re.ASCII):
        value = int(value)
    if type(value) is not int or not 1000 <= value < 2**32 - 1:
        raise QuotaError()
    return value


def socket_path(uid):
    return f"{DIRECTORY}/{validated_uid(uid)}.sock"


def endpoint(uid):
    uid = validated_uid(uid)
    return activation_socket.endpoint(socket_path(uid), uid)


def _request(alias):
    if type(alias) is not str or (alias != "default" and not named_account(alias)):
        raise QuotaError()
    return b"READ " + alias.encode("ascii")


def _result(value):
    if type(value) is not dict or set(value) != {"windows", "plan"}:
        raise QuotaError()
    plan, windows = value["plan"], value["windows"]
    if plan is not None and (type(plan) is not str or plan not in PLANS):
        raise QuotaError()
    if type(windows) is not dict:
        raise QuotaError()
    if windows:
        quota(windows)
    return value


def _recv(channel):
    data, ancillary, flags, _ = channel.recvmsg(MAX_PACKET + 1, 0)
    if not 0 < len(data) <= MAX_PACKET or ancillary or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        raise QuotaError()
    return data


def read_limits(alias):
    """Called only for an explicit refresh; ordinary status/usage never activates the service."""
    request = _request(alias)
    try:
        uid = validated_uid(os.geteuid())
        if uid != os.getuid() or not e2_account.account_ready():
            raise QuotaError()
        path, inode = socket_path(uid), endpoint(uid)
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as channel:
            channel.settimeout(5)
            channel.connect(path)
            activation_socket.manager_peer(channel, path)
            if endpoint(uid) != inode or _recv(channel) != READY:
                raise QuotaError()
            channel.settimeout(READ_TIMEOUT)
            if channel.send(request) != len(request):
                raise QuotaError()
            reply = _recv(channel)
        if reply.startswith(REFUSED):
            raise QuotaError(reply[len(REFUSED):].decode("ascii"))
        return _result(json.loads(reply.decode("ascii"), object_pairs_hook=e2_ipc._unique_pairs))
    except QuotaError:
        raise
    except TimeoutError:
        raise QuotaError("timeout") from None
    except (OSError, ValueError, TypeError, KeyError, RecursionError, AnalysisDenied):
        raise QuotaError() from None


def _read_native(uid, alias):
    from .codex_quota_runtime import run

    return run("read", uid, alias)


def _serve_connection(channel, uid):
    e2_ipc._peer_uid(channel, uid)
    channel.settimeout(5)
    channel.sendall(READY)
    try:
        data = _recv(channel)
        alias = data.removeprefix(b"READ ").decode("ascii")
        # A closed UI or a second queued packet cancels this transaction before native I/O.
        if data != _request(alias) or select.select([channel], [], [], 0)[0]:
            raise QuotaError()
        result = _result(_read_native(uid, alias))
        reply = json.dumps(result, separators=(",", ":"), allow_nan=False).encode("ascii")
    except (AnalysisDenied, QuotaError) as error:
        reply = REFUSED + QuotaError(error.reason).reason.encode("ascii")
    except TimeoutError:
        reply = REFUSED + b"timeout"
    except (ValueError, KeyError, TypeError, OSError):
        reply = REFUSED + b"unavailable"
    channel.sendall(reply)


def serve(uid):
    """Handle one read per connection and remain available briefly for queued clicks."""
    uid = validated_uid(uid)
    try:
        service_identity()
        if uid == os.geteuid():
            raise QuotaError()
        with activation_socket.listener(socket_path(uid), "codex-limits") as listener:
            # Stop accepting early enough for the final native read to finish before RuntimeMaxSec.
            until = time.monotonic() + 50
            while time.monotonic() < until and select.select([listener], [], [], 4)[0]:
                with listener.accept()[0] as channel:
                    try:
                        _serve_connection(channel, uid)
                    except (OSError, e2_ipc.HandoffDenied):
                        # A disconnected or unauthorized peer must not disrupt another reader.
                        continue
    except (AnalysisDenied, OSError, ValueError, KeyError):
        raise QuotaError() from None
