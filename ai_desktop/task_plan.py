"""A one-use, expiring approval for exact selected-file replacements and tests."""
from dataclasses import dataclass
import difflib
import json
import secrets
import time

from .analysis_contract import canonical, digest
from . import task_contract as contract


@dataclass(frozen=True)
class Authorization:
    sha256: str
    nonce: str


class TaskPlan:
    def __init__(self, before, source, proposal, check, *, clock=time.monotonic):
        contract.proposal(canonical(proposal), source)
        if set(before["files"]) != {f["path"] for f in source["files"]}:
            raise ValueError("selection")
        for item in source["files"]:
            old = before["files"][item["path"]]["file"]
            if item["content"] != (contract.plain_text(old["content"]) if old else None):
                raise ValueError("source changed")
        if check not in ("text", "toml", "python-unittest", "python-syntax", "json", "bash-syntax", "fish-syntax"):
            raise ValueError("check")
        self._clock = clock
        self._nonce = secrets.token_hex(24)
        self._payload = canonical({"schema": 1, "before": before, "proposal": proposal,
                                   "check": check, "nonce": self._nonce})
        self.sha256 = digest(self._payload)
        self._expires = clock() + 180
        self._issued = None
        self._used = False

    def data(self):
        # No mutable reference to the approved plan escapes.
        return json.loads(self._payload)

    def diff(self):
        data = self.data()
        result = []
        for item in data["proposal"]["files"]:
            old = data["before"]["files"][item["path"]]["file"]
            result.append("".join(difflib.unified_diff(
                (contract.plain_text(old["content"]) if old else "").splitlines(keepends=True),
                item["content"].splitlines(keepends=True),
                fromfile="a/" + item["path"] if old else "/dev/null",
                tofile="b/" + item["path"])))
            # Explicit exact text covers missing final-newline ambiguity.
            result.append("\nNew content JSON: " + canonical(item["content"]) + "\n")
        return "".join(result)

    def authorize(self, answer):
        if self._used or self._issued is not None or self._clock() > self._expires or answer != "SAVE " + self.sha256:
            self._used = True
            raise ValueError("consent")
        self._issued = Authorization(self.sha256, self._nonce)
        return self._issued

    def consume(self, authorization):
        valid = (not self._used and self._issued is not None and authorization is self._issued
                 and self._clock() <= self._expires)
        self._used = True
        if not valid:
            raise ValueError("consent")
        return self.data()
