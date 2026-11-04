"""E4 deterministic, typed, no-I/O workspace plan + human-readable diff.

Strictly PREVIEW-ONLY. Does NOT implement writing, executor, rollback or approval UI.
"""
from dataclasses import dataclass
import difflib
import hashlib
import hmac
import json
from pathlib import PurePosixPath
import re
import secrets
import time

MAX_CONTENT_BYTES = 4096
MAX_STEPS = 4
TTL_SECONDS = 180
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_BLOCKED = frozenset({".", "..", ".git", ".ssh", ".config", ".local",
                       "oauth", "secrets", "backup", "backups"})


class PlanDenied(RuntimeError):
    """Sanitized refusal."""


@dataclass(frozen=True)
class PlanPreview:
    workspace: str
    sha256: str
    state_sha256: str
    operations: tuple
    diff: str
    nonce: str


@dataclass(frozen=True)
class PlanAuthorization:
    workspace: str
    plan_sha256: str
    state_sha256: str
    operations: tuple


def _valid_workspace(value):
    if type(value) is not str or not value.startswith("/") or len(value) > 256:
        raise PlanDenied("PLAN: REFUSED")
    if value in ("/", "/home", "/root", "/etc", "/usr", "/tmp", "/var"):
        raise PlanDenied("PLAN: REFUSED")
    components = value.split("/")[1:]
    if (not components or any(not _SEGMENT.fullmatch(x) or x in _BLOCKED
                              for x in components)):
        raise PlanDenied("PLAN: REFUSED")
    # This is purely lexical: E5 must independently verify resolved filesystem
    # identity, ownership and symlinks with descriptor-relative operations.
    return value


def _validate_operations(operations):
    if type(operations) not in (tuple, list) or not 1 <= len(operations) <= MAX_STEPS:
        raise PlanDenied("PLAN: REFUSED")
    normalized = []
    seen = set()
    for op in operations:
        if type(op) is not dict or set(op) != {"action", "path", "content"}:
            raise PlanDenied("PLAN: REFUSED")
        if op["action"] != "create_text" or type(op["action"]) is not str:
            raise PlanDenied("PLAN: REFUSED")
        path, content = op["path"], op["content"]
        if type(path) is not str or len(path) > 160 or not path:
            raise PlanDenied("PLAN: REFUSED")
        segments = path.split("/")
        if (len(segments) > 5 or any(not _SEGMENT.fullmatch(p) or p in _BLOCKED
                                      for p in segments) or path in seen):
            raise PlanDenied("PLAN: REFUSED")
        if type(content) is not str or not content or "\x00" in content:
            raise PlanDenied("PLAN: REFUSED")
        try:
            raw = content.encode("utf-8")
        except UnicodeError:
            raise PlanDenied("PLAN: REFUSED") from None
        if len(raw) > MAX_CONTENT_BYTES or any(
            (ord(c) < 32 and c not in ("\n", "\t")) or ord(c) == 127
            for c in content
        ):
            raise PlanDenied("PLAN: REFUSED")
        seen.add(path)
        normalized.append((path, content))
    return tuple(normalized)


class PlanSession:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._preview = None
        self._deadline = None
        self._used = False
        self._issued = None
        self._consumed = False

    def preview(self, workspace, operations, *, state_sha256):
        if self._preview is not None:
            raise PlanDenied("PLAN: REFUSED")
        workspace = _valid_workspace(workspace)
        if (type(state_sha256) is not str or
            not re.fullmatch(r"[0-9a-f]{64}", state_sha256)):
            raise PlanDenied("PLAN: REFUSED")
        operations = _validate_operations(operations)
        canonical = json.dumps(
            {"version": 1, "workspace": workspace, "state_sha256": state_sha256,
             "operations": [
                {"action": "create_text", "path": p, "content": c}
                for p, c in operations
             ]}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        diff = "".join(
            "".join(difflib.unified_diff(
                [], c.splitlines(keepends=True),
                fromfile="/dev/null", tofile="b/" + p,
            )) for p, c in operations
        )
        self._preview = PlanPreview(
            workspace=workspace, sha256=digest, state_sha256=state_sha256,
            operations=operations, diff=diff, nonce=secrets.token_hex(24))
        self._deadline = self._clock() + TTL_SECONDS
        return self._preview

    def authorize(self, *, confirmed_sha256, nonce, state_sha256):
        preview = self._preview
        if preview is None or self._used:
            raise PlanDenied("PLAN: REFUSED")
        self._used = True
        try:
            if self._clock() > self._deadline:
                raise ValueError("expired")
            if any(type(v) is not str for v in (
                    confirmed_sha256, nonce, state_sha256)):
                raise ValueError("wrong types")
            if (not hmac.compare_digest(preview.sha256, confirmed_sha256) or
                not hmac.compare_digest(preview.nonce, nonce) or
                not hmac.compare_digest(preview.state_sha256, state_sha256)):
                raise ValueError("changed state")
        except (ValueError, TypeError):
            raise PlanDenied("PLAN: REFUSED") from None
        self._issued = PlanAuthorization(
            workspace=preview.workspace, plan_sha256=preview.sha256,
            state_sha256=preview.state_sha256, operations=preview.operations)
        return self._issued

    def consume_for_executor(self, authorization):
        """One-time trusted-process handoff; forged dataclass values are refused."""
        if self._issued is None or self._consumed or authorization is not self._issued:
            raise PlanDenied("PLAN: REFUSED")
        self._consumed = True
        # Approval does not extend the preview TTL. A delayed executor handoff
        # must fail closed, consuming the approval even if the clock moves back.
        if self._clock() > self._deadline:
            raise PlanDenied("PLAN: REFUSED")
        return self._issued

