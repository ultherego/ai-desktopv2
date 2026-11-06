"""The finite, owner-budgeted discovery flow of one `ask` question.

A ledger belongs to exactly one question. It owns the budgets (rounds, corrections, scanned entries, wire bytes),
executes local list/find steps through `directory_context`, remembers which names were actually observed, and refuses a
content read whose selectors were neither observed nor named by the user (`discovery-required`). It never decides what the
model may read: the captured policy still does that. Nothing here retries silently or sends a partial selection.
"""
from dataclasses import replace
import json
import re

from . import directory_context, permissions
from .skill_runtime import MAX_KNOWLEDGE_BYTES, observe_command
from .discovery_contract import (CEILING_TOTAL_ENTRIES, REFUSAL_CODES, MAX_SELECTORS, clean_path, document_bytes,
                                 diagnostic_bytes, skill_bytes, validate_discovery)

REFUSAL_RESERVE = 2400
OVERHEAD = 1024            # envelope of a document with its counters; leaves room for the remaining steps' framing


class BudgetExhausted(Exception):
    """Owner budget used up. `name` is the permissions.toml key; the text is built from it by the caller."""

    def __init__(self, name):
        self.name = name
        super().__init__("discovery budget")


class Recoverable(Exception):
    """A selection problem the model may correct once per correction budget (never a security refusal)."""

    def __init__(self, code, selectors, limit=None):
        self.code, self.selectors, self.limit = code, selectors, limit
        super().__init__(code)


def _split(selection):
    """(scope id, relative path without terminal wildcard, whether the path ends in a wildcard)."""
    if type(selection) is str:
        return selection, "", True
    path = selection["path"]
    for suffix in ("/**", "/*"):
        if path.endswith(suffix):
            return selection["id"], path[:-len(suffix)], True
    return selection["id"], path, False


class DiscoveryLedger:
    def __init__(self, policy, question, hints=""):
        self.policy = policy
        self.limits = policy.limits() if policy is not None else permissions.Limits(discovery_rounds=0, plan_corrections=0, command_rounds=0)
        self.question = question
        # The agent's own instructions (role text and Skills) are the owner's data, never the model's: a path they name
        # counts like one typed in the question. It never widens what the policy allows and never beats deny.
        self.hints = hints if type(hints) is str else ""
        try:
            self.scopes = {"f" + str(item.index): item for item in policy.scopes()}
        except (TypeError, AttributeError):
            self.scopes = {}
        self.home = getattr(policy, "home", "") if type(getattr(policy, "home", "")) is str else ""
        self.steps, self.guards = [], ()
        self.contents, self.content_bytes = (), 0
        self.scan = directory_context.Scan(self.limits.search_scan)
        self.rounds_left = self.limits.discovery_rounds
        self.corrections_left = self.limits.plan_corrections
        self.commands_left = self.limits.command_rounds
        self.listed, self.files, self.directories = set(), set(), set()

    # -- state ---------------------------------------------------------------------------------------------------
    def document(self):
        return {"steps": [dict(step) for step in self.steps],
                "remaining": {"rounds": self.rounds_left, "corrections": self.corrections_left,
                                  "commands": self.commands_left}}

    def snapshot(self):
        document = validate_discovery(self.document())
        if self._diagnostic_bytes(document) > self.limits.discovery_bytes:
            raise ValueError("discovery_bytes")
        return directory_context.DiscoverySnapshot(self.policy, document, self.guards, self.contents)

    @staticmethod
    def _diagnostic_bytes(document):
        return diagnostic_bytes(document)

    def _budget(self):
        # Room for the refusal steps the correction budget may still add (4 selectors of ~560 bytes + framing each).
        reserve = min(self.corrections_left * REFUSAL_RESERVE, self.limits.discovery_bytes // 4)
        return self.limits.discovery_bytes - (self._diagnostic_bytes(self.document()) if self.steps else 0) - OVERHEAD - reserve

    def _entries_left(self):
        used = sum(len(d["entries"]) for s in self.steps if s["op"] == "list" for d in s["directories"])
        used += sum(len(s["matches"]) for s in self.steps if s["op"] == "find")
        return CEILING_TOTAL_ENTRIES - used

    def _record(self, step):
        if step["op"] == "list":
            for directory in step["directories"]:
                self.listed.add((directory["id"], directory["path"]))
                self.directories.add((directory["id"], directory["path"]))
                for entry in directory["entries"]:
                    self._see(directory["id"], entry)
        elif step["op"] == "find":
            self.directories.add((step["id"], step["path"]))
            for entry in step["matches"]:
                self._see(step["id"], entry)

    def _see(self, ident, entry):
        (self.files if entry["type"] == "file" else self.directories).add((ident, entry["path"]))

    # -- steps -----------------------------------------------------------------------------------------------------
    def take_round(self):
        if self.rounds_left <= 0:
            raise BudgetExhausted("discovery_rounds")
        if self.scan.left <= 0:
            raise BudgetExhausted("search_scan")
        if self._budget() <= 0:
            raise BudgetExhausted("discovery_bytes")
        if self._entries_left() <= 0:
            raise BudgetExhausted("discovery_bytes")
        self.rounds_left -= 1

    def take_command(self):
        """One command round of the owner's budget (0 means the assistant may not run anything)."""
        if self.commands_left <= 0:
            raise BudgetExhausted("command_rounds")
        self.commands_left -= 1

    def output_room(self):
        """Bytes of command output that still fit the discovery budget; BudgetExhausted when almost nothing does."""
        # JSON can double a byte (newline, quote), so the raw output gets at most half of what is left.
        room = min(self.limits.command_output, (self._budget() - 600) // 2)
        if room < 256:
            raise BudgetExhausted("discovery_bytes")
        return room

    def can_afford(self, steps):
        """True when `steps` more commands each still get a usable slice of output and a command round."""
        return self.commands_left >= steps and (self._budget() - 600) // 2 >= 256 * steps

    def add_skill(self, name, text):
        """A Skill the model asked for, as the owner's data in the next round. A path it names counts like a typed one."""
        status = "missing" if text is None else "ok"
        document = self.document()
        step = {"op": "skill", "name": name, "status": status, "text": text or ""}
        document["steps"].append(step)
        if skill_bytes(document) > MAX_KNOWLEDGE_BYTES:
            raise BudgetExhausted("skills_bytes")
        self.steps.append(step)
        if text and "/" not in name:
            self.hints += "\n" + text

    def add_run(self, step):
        # Test and undo must finish after an approved live change. Keep their status even if command
        # output cannot fit on the wire. snapshot() refuses a transfer if the statuses alone overflow.
        self.steps.append(step)
        observe_command(step)
        if step["output"] and self._diagnostic_bytes(self.document()) > self.limits.discovery_bytes:
            step["output"] = ""
            step["truncated"] = True
        return step

    def run(self, plan, permit):
        """Execute one validated list/find plan. Raises ValueError with a finite code on any local refusal."""
        self.take_round()
        left = self._entries_left()
        limits = replace(self.limits, listing_entries=min(self.limits.listing_entries, left),
                         search_results=min(self.limits.search_results, left))
        if plan["action"] == "list":
            step, guards = directory_context.list_step(self.policy, plan["files"], plan.get("after"), limits,
                                                       self.scan, self._budget(), permit)
        else:
            # One broad search may use at most a quarter of the question's scan budget, so a wide scope cannot
            # starve the narrower follow-up the model is told to make when a result is incomplete.
            cap = min(self.scan.left, max(1000, self.limits.search_scan // 4))
            local = directory_context.Scan(cap)
            step, guards = directory_context.find_step(self.policy, plan["files"][0], plan["queries"], plan["kind"],
                                                       limits, local, self._budget(), permit)
            self.scan.left -= cap - local.left
        self.steps.append(step)
        self.guards += guards
        self._record(step)
        return step

    def content(self, plan, permit):
        """One grounded file, local large-file scan, bounded excerpts on the wire."""
        from . import ask_content
        self.ground([plan["file"]])
        self.take_round()
        distinct = {(guard.ident, guard.relative) for guard in self.contents}
        selection = plan["file"]
        key = (selection, "") if type(selection) is str else (selection["id"], selection["path"])
        if key not in distinct and len(distinct) >= self.limits.read_files:
            raise ValueError("files-count")
        room = min(ask_content.MAX_FRAGMENT_BYTES, self.limits.read_file_size,
                   self.limits.read_total_size - self.content_bytes, max(0, (self._budget() - 2048) // 2))
        if room < 1:
            raise BudgetExhausted("discovery_bytes")
        step, guard, amount = ask_content.collect(self.policy, plan, permit, room=room)
        if document_bytes({"steps": [step], "remaining": self.document()["remaining"]}) > self._budget():
            raise BudgetExhausted("discovery_bytes")
        self.steps.append(step)
        self.contents += (guard,)
        self.content_bytes += amount
        return step

    # -- grounding ---------------------------------------------------------------------------------------------------
    def _typed(self, ident, base):
        """Did the user spell this path? Only the full path below the scope root, or that path with the scope's own
        prefix, counts; the match is case-sensitive and bounded, so 'logs.conf' is not found inside 'nested/logs.conf'."""
        forms = {base}
        scope = self.scopes.get(ident)
        if scope is not None:
            prefix = scope.base
            forms.add(prefix + "/" + base)
            home = self.home
            if home and (prefix == home or prefix.startswith(home + "/")):
                forms.add("~" + prefix[len(home):] + "/" + base)
        return any(re.search(r"(?<![\w./~-])" + re.escape(form) + r"(?![\w-]|\.\w)", text)
                   for form in forms for text in (self.question, self.hints))

    def ground(self, selections):
        """Every read must be backed by a list/find observation or by a path the user spelled out."""
        missing = []
        for selection in selections:
            if type(selection) is str:
                scope = self.scopes.get(selection)
                # A directory scope is the owner's own literal rule, but what it contains is unknown until it was
                # listed; an exact-file scope is fully described by its own pattern.
                if scope is not None and scope.mode in ("tree", "children") and (selection, "") not in self.directories:
                    missing.append(selection)
                continue
            ident, base, _ = _split(selection)
            if (ident, base) in self.files or (ident, base) in self.directories:
                continue
            if len(base) >= 3 and self._typed(ident, base):
                continue
            missing.append(selection)
        if missing:
            raise Recoverable("discovery-required", self.selectors(missing))

    @staticmethod
    def selectors(selections):
        result = []
        for selection in selections[:MAX_SELECTORS]:
            ident, path = (selection, "") if type(selection) is str else (selection["id"], selection["path"])
            try:
                result.append({"id": ident, "path": clean_path(path, empty=True, wildcard=True)})
            except ValueError:
                continue
        return result

    def soft_stop(self, name):
        """Scan/byte budgets close further discovery but do not abort the question: the model is told, once,
        that discovery is over (rounds -> 0) and may still read observed paths or answer from what it has."""
        if name not in ("search_scan", "discovery_bytes") or any(
                step["op"] == "refusal" and step["code"] == "discovery-budget" for step in self.steps):
            return False
        self.steps.append({"op": "refusal", "code": "discovery-budget", "selectors": [],
                           "limit": getattr(self.limits, name)})
        self.rounds_left = 0
        return True

    # -- corrections ---------------------------------------------------------------------------------------------------
    def correction(self, problem):
        """Record a recoverable refusal; returns False when the owner's correction budget is used up."""
        if self.corrections_left <= 0:
            return False
        step = {"op": "refusal", "code": problem.code, "selectors": problem.selectors, "limit": problem.limit}
        self.steps.append(step)
        if self._diagnostic_bytes(self.document()) > self.limits.discovery_bytes:
            step["selectors"] = []                     # the owner's byte budget wins over naming the selectors
            if self._diagnostic_bytes(self.document()) > self.limits.discovery_bytes:
                self.steps.pop()
                return False
        self.corrections_left -= 1
        return True

    def limit_for(self, code):
        return {"files-count": self.limits.read_files, "files-size": self.limits.read_file_size,
                "files-total": self.limits.read_total_size, "files-entries": permissions.MAX_DIRECTORY_ENTRIES}.get(code)


RECOVERABLE = frozenset(REFUSAL_CODES) - {"discovery-required", "discovery-budget"}
