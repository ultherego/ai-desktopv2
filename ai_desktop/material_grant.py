"""LC0: minimal grant for ONE pinned material and a ledger of what the app handed over.

Pure, in-memory, no I/O, no transport. A grant is created by the trusted UI after
a real consent gesture (never from model output) and binds one material version,
one recipient, one purpose, a deadline and budgets. It does not allow Internet,
writing, other sources or running code. Every packet is re-validated against the
grant immediately before hand-off. The ledger records the app's hand-off to the
official client (not TLS traffic, not provider receipt), keeps no text and never
claims "nothing was sent" after a hand-off whose outcome is unknown.
"""
from dataclasses import dataclass, field
import copy
import secrets
import time

from .analysis_contract import application_input, digest, canonical
from .execution import current as current_execution
from . import materials

RECIPIENT = "claude-analysis"
CODEX_RECIPIENT = "codex-analysis"
MAX_PURPOSE_BYTES = 512
QUESTION_BYTES = 2048
MAX_REQUESTS = 8
MAX_TOTAL_BYTES = 2 * 1024 * 1024
PACKET_BYTES = 256 * 1024              # dynamic data of one request, before serialization
PART_PACKET_BYTES = 512 * 1024         # the same for a "map" request of a whole-file analysis (opt-in profile)
MAX_RANGES = 8                         # operations per packet
MAX_DEADLINE_SECONDS = 600

CODES = frozenset({"GRANT_INVALID", "GRANT_EXPIRED", "BUDGET_EXHAUSTED", "SOURCE_CHANGED",
                   "SCOPE_DENIED", "PACKET_LIMIT", "STATE_INVALID"})
STATES = ("prepared", "handed_off", "answered", "unknown", "cancelled")


class GrantDenied(RuntimeError):
    """Closed refusal code; never carries material text."""

    def __init__(self, code):
        if code not in CODES:
            raise AssertionError("unknown code")
        super().__init__(code)
        self.code = code


def current_recipient():
    """The trusted selection chooses one closed recipient before consent."""
    selected = current_execution()
    return CODEX_RECIPIENT if selected is not None and selected["provider"] == "codex" else RECIPIENT


@dataclass(frozen=True)
class MaterialGrant:
    material_id: str
    material_sha256: str
    purpose: str
    deadline: float                       # absolute, on the session clock
    max_requests: int = MAX_REQUESTS
    max_bytes: int = MAX_TOTAL_BYTES
    recipient: str = field(default_factory=current_recipient)
    internet: bool = False
    write: bool = False


def _check_grant(material, grant, now):
    if (type(grant) is not MaterialGrant or grant.recipient != current_recipient() or grant.internet is not False
            or grant.write is not False or type(grant.purpose) is not str or not grant.purpose.strip()
            or len(grant.purpose.encode("utf-8")) > MAX_PURPOSE_BYTES
            or type(grant.max_requests) is not int or not 1 <= grant.max_requests <= MAX_REQUESTS
            or type(grant.max_bytes) is not int or isinstance(grant.max_bytes, bool) or not 1 <= grant.max_bytes <= MAX_TOTAL_BYTES
            or not isinstance(grant.deadline, (int, float)) or isinstance(grant.deadline, bool)
            or grant.deadline - now > MAX_DEADLINE_SECONDS):
        raise GrantDenied("GRANT_INVALID")
    _check_binding(material, grant)


def _check_binding(material, grant):
    if grant.material_id != material.material_id or grant.material_sha256 != material.sha256:
        raise GrantDenied("SOURCE_CHANGED")


@dataclass(frozen=True)
class Packet:
    """Exactly what the app will pass to the official client: the transport payload."""
    packet_id: str
    payload: str
    sha256: str
    question: str
    context: tuple
    refs: tuple
    data_bytes: int
    material: object = None            # loop step input (mode=material); None for fragment packets

    def verify(self):
        """The hash is over the exact payload; any later edit is detected."""
        return digest(self.payload) == self.sha256


class TransferSession:
    def __init__(self, material, grant, *, clock=time.monotonic):
        self._clock = clock
        _check_grant(material, grant, clock())
        self._material, self._grant = material, grant
        self._execution = canonical(current_execution())
        self._entries = {}
        self._order = []
        self.delivered = set()            # refs the controller really handed over

    # --- budget -----------------------------------------------------------------
    def _check_execution(self):
        if canonical(current_execution()) != self._execution:
            raise GrantDenied("GRANT_INVALID")

    def _live(self):
        return [e for e in self._entries.values() if e["state"] != "cancelled"]

    def _used_requests(self):
        return len(self._live())

    def _used_bytes(self):
        return sum(e["bytes"] for e in self._live())

    # --- packet -----------------------------------------------------------------
    def prepare(self, question, ranges):
        """Build the exact packet for ranges of THIS material, or refuse before any effect."""
        self._check_execution()
        if self._clock() > self._grant.deadline:
            raise GrantDenied("GRANT_EXPIRED")
        _check_binding(self._material, self._grant)
        if type(question) is not str or not question.strip() or len(question.encode("utf-8")) > QUESTION_BYTES:
            raise GrantDenied("PACKET_LIMIT")
        try:
            ranges = [tuple(r) for r in ranges]
            valid = 1 <= len(ranges) <= MAX_RANGES and len(set(ranges)) == len(ranges)
        except TypeError:
            valid = False
        if not valid:
            raise GrantDenied("SCOPE_DENIED")
        if self._used_requests() >= self._grant.max_requests:
            raise GrantDenied("BUDGET_EXHAUSTED")
        fragments = []
        try:
            for start, end in ranges:
                fragments.append(materials.read_range(self._material, self._material.sha256, start, end))
        except materials.MaterialError as exc:
            raise GrantDenied("SOURCE_CHANGED" if exc.code == "SOURCE_CHANGED" else "SCOPE_DENIED") from None
        except (TypeError, ValueError):
            raise GrantDenied("SCOPE_DENIED") from None
        data_bytes = sum(f.ref.end - f.ref.start for f in fragments)
        if data_bytes > PACKET_BYTES:
            raise GrantDenied("PACKET_LIMIT")
        if self._used_bytes() + data_bytes > self._grant.max_bytes:
            raise GrantDenied("BUDGET_EXHAUSTED")
        item = {"kind": "material", "purpose": self._grant.purpose,
                "material": {"id": self._material.material_id, "version": self._material.sha256,
                             "size": self._material.size},
                "fragments": [{"ref": f.ref.to_json(), "text": f.text} for f in fragments]}
        try:
            payload = application_input(None, question, context=[item])
        except ValueError:                # includes the input/context budget refusals
            raise GrantDenied("PACKET_LIMIT") from None
        packet = Packet(secrets.token_hex(8), payload, digest(payload), question, (item,),
                        tuple(f.ref for f in fragments), data_bytes)
        self._entries[packet.packet_id] = {
            "packet": packet.packet_id, "sha256": packet.sha256, "state": "prepared",
            "bytes": data_bytes, "ranges": [[f.ref.start, f.ref.end] for f in fragments],
            "fragment_sha256": [f.ref.fragment_sha256 for f in fragments],
            "prepared_at": self._clock(), "handed_off_at": None}
        self._order.append(packet.packet_id)
        return packet

    def prepare_step(self, question, material_input, data_bytes, ranges=(), *, project=False):
        """One request of a model-driven loop. `data_bytes` is the dynamic data this request carries
        (observations); `ranges` are the byte ranges of fragments it contains, for the ledger."""
        self._check_execution()
        if self._clock() > self._grant.deadline:
            raise GrantDenied("GRANT_EXPIRED")
        _check_binding(self._material, self._grant)
        if type(question) is not str or not question.strip() or len(question.encode("utf-8")) > QUESTION_BYTES:
            raise GrantDenied("PACKET_LIMIT")
        if self._used_requests() >= self._grant.max_requests:
            raise GrantDenied("BUDGET_EXHAUSTED")
        phase = material_input.get("task", {}).get("phase") if type(material_input) is dict and type(material_input.get("task")) is dict else None
        ceiling = PART_PACKET_BYTES if phase == "map" and not project else PACKET_BYTES
        if type(data_bytes) is not int or isinstance(data_bytes, bool) or not 0 <= data_bytes <= ceiling:
            raise GrantDenied("PACKET_LIMIT")
        if self._used_bytes() + data_bytes > self._grant.max_bytes:
            raise GrantDenied("BUDGET_EXHAUSTED")
        try:
            if project:
                payload = application_input(None, question, project=material_input)
            else:
                payload = application_input(None, question, material=material_input)
        except ValueError:
            raise GrantDenied("PACKET_LIMIT") from None
        try:
            fragments = [materials.read_range(self._material, self._material.sha256, a, b) for a, b in ranges]
        except (materials.MaterialError, TypeError, ValueError):
            raise GrantDenied("SCOPE_DENIED") from None
        packet = Packet(secrets.token_hex(8), payload, digest(payload), question, (),
                        tuple(f.ref for f in fragments), data_bytes, material_input)
        self._entries[packet.packet_id] = {
            "packet": packet.packet_id, "sha256": packet.sha256, "state": "prepared",
            "bytes": data_bytes, "ranges": [[f.ref.start, f.ref.end] for f in fragments],
            "fragment_sha256": [f.ref.fragment_sha256 for f in fragments],
            "prepared_at": self._clock(), "handed_off_at": None}
        self._order.append(packet.packet_id)
        return packet

    def _entry(self, packet, *states):
        entry = self._entries.get(getattr(packet, "packet_id", None))
        if entry is None or entry["state"] not in states or not packet.verify():
            raise GrantDenied("STATE_INVALID")
        if entry["sha256"] != packet.sha256:
            raise GrantDenied("STATE_INVALID")
        return entry

    def hand_off(self, packet):
        """Last check before the app passes the payload to the official client."""
        entry = self._entry(packet, "prepared")
        try:
            self._check_execution()
        except GrantDenied:
            entry["state"] = "cancelled"
            raise
        if self._clock() > self._grant.deadline:
            entry["state"] = "cancelled"
            raise GrantDenied("GRANT_EXPIRED")
        entry["state"], entry["handed_off_at"] = "handed_off", self._clock()
        self.delivered.update(packet.refs)
        return packet

    def cancel(self, packet):
        """Before hand-off only; releases the reservation. A handed-off packet cannot be cancelled."""
        self._entry(packet, "prepared")["state"] = "cancelled"

    def record_answer(self, packet):
        self._entry(packet, "handed_off")["state"] = "answered"

    def record_unknown(self, packet):
        """Outcome after hand-off is unknown (timeout, Ctrl+C, crash): counted as sent, no retry."""
        self._entry(packet, "handed_off")["state"] = "unknown"

    def requests_left(self):
        return max(0, self._grant.max_requests - self._used_requests())

    def bytes_left(self):
        return max(0, self._grant.max_bytes - self._used_bytes())

    # --- evidence ---------------------------------------------------------------
    def requests_handed_off(self):
        return sum(1 for e in self._entries.values() if e["state"] in ("handed_off", "answered", "unknown"))

    def ledger(self):
        """Copy of the registry: hashes, sizes, ranges and states only, never text."""
        return [copy.deepcopy(self._entries[i]) | {"seq": n + 1} for n, i in enumerate(self._order)]
