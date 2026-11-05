"""One question-scoped approval with a shared deadline and finite transfers.

The UI approves the captured permissions once. Individual immutable wire
payloads still require their own AnalysisConsent, without additional prompts.
"""
from contextlib import contextmanager
import hashlib
import json
import time

from .analysis_contract import (MAX_MESSAGE_BYTES, AnalysisConsent, AnalysisDenied, CONSENT_CEILING, consent_seconds, canonical,
                                validate_context, validate_question)
from .assistant_contract import validate_access, file_selections, selected_file_ids
from .execution import current as current_execution, current_history
from .discovery_contract import MAX_SKILL_ROUNDS, MAX_REUSED_SKILLS
from .permissions import Policy


class ConversationConsent:
    """The owner's single yes for one conversation, in this process only and never saved.

    It covers later questions only while the whole permissions.toml and the recipient stay the same: any edit, also one
    that only removes a deny rule or changes a limit, or another service asks again. It replaces the prompt, not the checks: every question still gets
    its own QuestionConsent with a deadline and bound payloads, and writes and changes keep their separate approvals.
    """

    def __init__(self):
        self._binding = None
        self._skill_hashes = {}

    def remember_skills(self, steps):
        """Keep only fingerprints of procedures accepted in this live conversation, most recent first on reuse."""
        for step in steps:
            if step["op"] == "skill" and step["status"] == "ok":
                name = step["name"]
                self._skill_hashes.pop(name, None)
                self._skill_hashes[name] = hashlib.sha256(step["text"].encode()).digest()
        while len(self._skill_hashes) > MAX_REUSED_SKILLS:
            del self._skill_hashes[next(iter(self._skill_hashes))]

    def reusable_skills(self, loader):
        for name, digest in reversed(tuple(self._skill_hashes.items())):
            body = loader(name)
            if body is not None and hashlib.sha256(body.encode()).digest() == digest:
                yield name, body
            else:
                del self._skill_hashes[name]

    @staticmethod
    def _state(snapshot):
        # `access` lists what the model may ask for; deny rules, limits and process sharing are only in the file itself.
        source = getattr(snapshot, "policy", None)
        policy = source.fingerprint() if type(source) is Policy else None
        return canonical({"access": snapshot.access, "sources": snapshot.sources, "execution": current_execution(),
                          "policy": policy})

    def covers(self, snapshot):
        return self._binding is not None and self._binding == self._state(snapshot)

    def grant(self, snapshot):
        self._binding = self._state(snapshot)
        self._skill_hashes.clear()


class QuestionConsent:
    def __init__(self, question, snapshot, *, clock=None, plan_ready=False):
        validate_question(question, MAX_MESSAGE_BYTES)
        if not question.strip():
            raise AnalysisDenied("consent")
        self._question = question
        self._snapshot = snapshot
        self._policy = snapshot.policy
        self._access = canonical(validate_access(snapshot.access))
        self._execution = canonical(current_execution())
        self._binding = self._current_binding()
        self._clock = time.monotonic if clock is None else clock
        self._born = self._clock()
        selected = current_execution()
        self._effort = selected.get("effort") if selected is not None else None
        self._expires = self._born + consent_seconds(self._effort)
        self._approved = False
        self._closed = False
        # `plan_ready` is only for a caller that built and validated the edit plan itself (the project controller, after its
        # own consented read loop): the workspace payload may then be the FIRST one. Default behaviour is unchanged.
        self._phase = "context" if plan_ready is True else "first"
        self._pending = None
        # Typed local read snapshots only tighten this question's authority.
        # They keep a directory alias bound through the later session startup
        # and final transfer, without changing the captured policy object.
        self._file_reads = ()
        self._discovery_snapshot = None
        self._discovery_bytes = None
        self._discovery_sent = 0
        self._skill_sent = 0
        self._skill_batch = False
        self._planner_sent = False
        self._seeded_skills = False
        self._skill_loader = None
        self._reused_bodies = {}

    @property
    def question(self):
        return self._question

    def _current_binding(self):
        return canonical({"question": self._question, "access": self._snapshot.access,
                          "sources": self._snapshot.sources, "execution": current_execution(),
                          "conversation": current_history()})

    def _validate(self):
        try:
            if not self._closed and self._clock() > self._expires:
                self.close()
                denied = AnalysisDenied("consent")
                denied.expired = True
                raise denied
            if (self._closed or self._clock() > self._expires
                    or canonical(current_execution()) != self._execution
                    or self._snapshot.policy is not self._policy
                    or self._current_binding() != self._binding):
                raise ValueError()
            self._snapshot.require_claude()
            if self._file_reads:
                from .permissions import recheck_selection
                for policy, selection in self._file_reads:
                    if policy is not self._policy:
                        raise ValueError()
                    recheck_selection(policy, selection)
            if self._discovery_snapshot is not None:
                from .directory_context import recheck_discovery
                from .discovery_contract import validate_discovery
                if self._discovery_snapshot.policy is not self._policy:
                    raise ValueError()
                if canonical(validate_discovery(self._discovery_snapshot.document)) != self._discovery_bytes:
                    raise ValueError()
                recheck_discovery(self._discovery_snapshot)
                if any(self._skill_loader(name) != text for name, text in self._reused_bodies.items()):
                    raise ValueError()
            if (self._clock() > self._expires or self._snapshot.policy is not self._policy
                    or self._current_binding() != self._binding):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.close()
            raise AnalysisDenied("consent") from None

    def approve(self):
        """Called once by the trusted UI after an explicit affirmative answer."""
        if self._approved:
            self.close()
            raise AnalysisDenied("consent")
        if not self._closed:
            # The window starts at the user's yes, not when the question object was made (the user's own thinking
            # time is not counted).
            self._born = self._clock()
            self._expires = self._born + consent_seconds(self._effort)
        self._validate()
        self._approved = True

    def renew(self):
        """Slide a LIVE window when the next payload of this very question is claimed: one step's worth of time from
        now, never beyond the question's ceiling. A closed, unapproved or already expired question is never revived."""
        if not self._approved or self._closed:
            return
        now = self._clock()
        if now > self._expires:
            return
        self._expires = max(self._expires, min(now + consent_seconds(self._effort), self._born + CONSENT_CEILING))

    @contextmanager
    def waiting(self):
        """Time spent waiting for the USER (an answer to a prompt, a sudo password) is not part of the window or the
        ceiling: both are pushed forward by exactly that long. Only the machine's own time is limited."""
        started = self._clock()
        try:
            yield
        finally:
            if self._approved and not self._closed:
                gone = max(self._clock() - started, 0)
                self._born += gone
                self._expires += gone

    def check(self):
        if not self._approved:
            raise AnalysisDenied("consent")
        self._validate()

    def require_files(self, policy, scope_ids):
        self.check()
        try:
            if policy is not self._policy or policy is None:
                raise ValueError()
            selected = file_selections(scope_ids, json.loads(self._access))
            if not selected_file_ids(selected):
                raise ValueError()
        except (ValueError, TypeError, KeyError, UnicodeError):
            self.close()
            raise AnalysisDenied("consent") from None

    def bind_file_selection(self, policy, selection):
        """Retain only a typed, bounded alias snapshot of this exact policy."""
        self.check()
        try:
            if policy is not self._policy or policy is None:
                raise ValueError()
            if type(selection) in (tuple, list):
                return
            from .permissions import _ExpandedSelection, recheck_selection
            if (type(selection) is not _ExpandedSelection or selection.policy is not policy
                    or len(self._file_reads) >= 8):
                raise ValueError()
            recheck_selection(policy, selection)
            self._file_reads += ((policy, selection),)
            self.check()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.close()
            raise AnalysisDenied("consent") from None

    def bind_discovery(self, policy, snapshot, *, reuse_loader=None):
        """Bind the cumulative local metadata document before one more planner round.

        Each round extends the previous document (earlier steps are an exact prefix) and the number of rounds can never
        exceed the owner's own discovery_rounds + plan_corrections, so the finite multi-step flow stays under the single
        approval of this question.
        """
        self.check()
        try:
            from .directory_context import DiscoverySnapshot, recheck_discovery
            from .discovery_contract import validate_discovery
            if (policy is not self._policy or type(snapshot) is not DiscoverySnapshot
                    or snapshot.policy is not policy or self._pending is not None):
                raise ValueError()
            document = validate_discovery(snapshot.document)
            seed_skills = reuse_loader is not None
            if seed_skills:
                if (self._phase != "first" or self._planner_sent or snapshot.guards
                        or any(step["op"] != "skill" or step["status"] != "ok" for step in document["steps"])):
                    raise ValueError()
            elif self._phase != "context" or not self._planner_sent:
                raise ValueError()
            if policy is not None:
                limits = policy.limits()
                rounds = limits.discovery_rounds + limits.plan_corrections + limits.command_rounds
            else:
                rounds = 0
            before = [] if self._discovery_snapshot is None else self._discovery_snapshot.document["steps"]
            if document["steps"][:len(before)] != before or len(document["steps"]) <= len(before):
                raise ValueError()
            added = document["steps"][len(before):]
            self._skill_batch = all(step["op"] == "skill" for step in added)
            if self._skill_batch:
                if len(added) > MAX_REUSED_SKILLS or (not seed_skills and self._skill_sent >= MAX_SKILL_ROUNDS):
                    raise ValueError()
            elif self._discovery_sent >= rounds:
                raise ValueError()
            recheck_discovery(snapshot)
            self._discovery_bytes = canonical(document)
            self._discovery_snapshot = snapshot
            self._seeded_skills = seed_skills
            if seed_skills:
                self._skill_loader = reuse_loader
                self._reused_bodies = {step["name"]: step["text"] for step in document["steps"]}
            self._phase = "discovery"
            self.check()
            return document
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.close()
            raise AnalysisDenied("consent") from None

    def require_systemd(self, scope):
        self.check()
        if type(scope) is not str or scope not in json.loads(self._access)["systemd"]:
            self.close()
            raise AnalysisDenied("consent")

    def require_weather(self):
        self.check()
        if json.loads(self._access)["weather"] is not True:
            self.close()
            raise AnalysisDenied("consent")

    def require_web(self):
        self.check()
        if (self._policy is None or json.loads(self._access).get("web") is not True
                or self._snapshot.sources.get("web") is not True):
            self.close()
            raise AnalysisDenied("consent")

    def require_write_scope(self, policy, scope_id):
        self.check()
        allowed = {item["id"] for item in json.loads(self._access).get("write", [])}
        if policy is not self._policy or policy is None or type(scope_id) is not str or scope_id not in allowed:
            self.close()
            raise AnalysisDenied("consent")

    def _payload(self, question, context, access, task=None, write_grant=None, *, web=False, discovery=None):
        if question != self._question or type(web) is not bool:
            raise AnalysisDenied("consent")
        if discovery is not None:
            # What this task already collected may accompany its next payload, but only the exact bound bytes.
            from .discovery_contract import validate_discovery
            if (web or (access is None and context is None and task is None) or self._discovery_bytes is None
                    or canonical(validate_discovery(discovery)) != self._discovery_bytes):
                raise AnalysisDenied("consent")
        if web:
            if any(value is not None for value in (context, access, task, write_grant)):
                raise AnalysisDenied("consent")
            self.require_web()
            return "web"
        if task is not None:
            from .ask_edit import WriteGrant
            if context is not None or access is not None or type(write_grant) is not WriteGrant:
                raise AnalysisDenied("consent")
            write_grant.require_transfer(self, task)
            return "workspace"
        if write_grant is not None:
            raise AnalysisDenied("consent")
        if access is not None:
            if context is not None or canonical(access) != self._access:
                raise AnalysisDenied("consent")
            if discovery is not None:
                from .discovery_contract import validate_discovery
                if (self._discovery_bytes is None
                        or canonical(validate_discovery(discovery)) != self._discovery_bytes):
                    raise AnalysisDenied("consent")
                return "discovery"
            return "planner"
        validate_context(context)
        allowed = json.loads(self._access)
        for item in context:
            if item["kind"] == "systemd" and item["scope"] not in allowed["systemd"]:
                raise AnalysisDenied("consent")
            if item["kind"] == "weather" and allowed["weather"] is not True:
                raise AnalysisDenied("consent")
            if item["kind"] == "files":
                if self._policy is None or not allowed["files"]:
                    raise AnalysisDenied("consent")
                paths = tuple(file["path"] for file in item["files"])
                if self._policy.select(paths, limit=self._policy.limits().read_files) != paths:
                    raise AnalysisDenied("consent")
        return "answer"

    def claim(self, gate, question, *, context=None, access=None, task=None, write_grant=None, web=False, discovery=None):
        """Reserve one payload before session startup; no replay or retries."""
        try:
            self.renew()
            self.check()
            if type(gate) is not AnalysisConsent or self._pending is not None:
                raise AnalysisDenied("consent")
            mode = self._payload(question, context, access, task, write_grant, web=web, discovery=discovery)
            if not ((self._phase == "first" and mode in ("planner", "answer"))
                    or (self._phase == "context" and mode in ("answer", "workspace", "web"))
                    or (self._phase == "discovery" and mode == "discovery")):
                raise AnalysisDenied("consent")
            # Web stays a separate request with nothing local. A workspace payload may follow discovery: it carries only
            # the question and the selected files, and the discovery snapshot is still rechecked by _validate.
            if self._discovery_snapshot is not None and not self._seeded_skills and mode == "web":
                raise AnalysisDenied("consent")
            gate.check(None, question="" if task is not None else question, task=task, context=context, access=access,
                       sha256=gate.sha256, nonce=gate.nonce, web=web,
                       **({"discovery": discovery} if discovery is not None else {}))
            self._pending = (gate, mode)
        except BaseException:
            self.close()
            raise

    def authorize(self, gate, question, *, context=None, access=None, task=None, write_grant=None, web=False, discovery=None):
        """Consume the reserved payload after sudo and immediately before send."""
        try:
            self.check()
            if self._pending is None or self._pending[0] is not gate:
                raise AnalysisDenied("consent")
            mode = self._payload(question, context, access, task, write_grant, web=web, discovery=discovery)
            if mode != self._pending[1]:
                raise AnalysisDenied("consent")
            self._pending = None
            if mode == "planner":
                self._planner_sent = True
            elif mode == "discovery":
                if self._seeded_skills:
                    self._planner_sent = True
                elif self._skill_batch:
                    self._skill_sent += 1
                else:
                    self._discovery_sent += 1
            self._phase = "context" if mode in ("planner", "discovery") else "complete"
            return gate.authorize(None, question="" if task is not None else question, task=task, context=context, access=access,
                                  sha256=gate.sha256, nonce=gate.nonce, web=web,
                                  **({"discovery": discovery} if discovery is not None else {}))
        except BaseException:
            self.close()
            raise

    def close(self):
        self._closed = True
        self._pending = None
