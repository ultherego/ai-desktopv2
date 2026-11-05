"""The local state of one `ask` conversation: finished turns, task memory, and which sources they depend on.

Nothing here is authority. History and memory are data that go into a request only after the ordinary policy, access
snapshot and a fresh consent for THAT turn. A turn that read files remembers which files (its *taint*); every later turn
that could see its answer or its model-written memory inherits that taint. When a tainted source is no longer allowed by
the current policy, the affected answers and model-written memory entries are removed before the next request, so a
revoked file's content cannot come back through a quoted answer, a note or a resumed session.
"""
from contextlib import contextmanager
from contextvars import ContextVar

from . import conversation_memory as memory
from .execution import HISTORY_ANSWER_BYTES, HISTORY_QUESTION_BYTES, HISTORY_TOTAL_BYTES, MAX_HISTORY

MAX_TURNS = 64                 # finished turns kept for revalidation; only the last MAX_HISTORY are sent verbatim
MAX_SOURCE_PATHS = 256         # one saved conversation's table of sources (conversation_sessions.MAX_PATHS)
MAX_SOURCES = 8                # files remembered per turn (more are summarised as "too many": the turn is then tainted by all)
REDACTED = "[answer withheld: its sources cannot be verified under the current permissions]"
FACT_REDACTED = "Action taken: details removed because access to the source it used was revoked."
_log = ContextVar("ai_desktop_turn_sources", default=None)


class Recorder(list):
    """Files read during one turn (the list itself) plus the actions the controller really performed in it."""

    def __init__(self):
        super().__init__()
        self.actions = []


@contextmanager
def recording():
    """Collect the files read and the actions performed during one turn (set by the loop, filled by the flow)."""
    sources = Recorder()
    token = _log.set(sources)
    try:
        yield sources
    finally:
        _log.reset(token)


def note_action(text):
    """The controller records what it actually did (a command run or refused, a change applied or not). These facts are
    written by the program, not by the model, so they stay true when the model's own notes are wrong."""
    recorder = _log.get()
    if recorder is not None and type(text) is str and text.strip() and len(recorder.actions) < 8:
        recorder.actions.append(text)


def note_sources(paths):
    sources = _log.get()
    if sources is not None:
        for path in paths:
            if path not in sources:
                sources.append(path)


def _bounded(text, limit):
    return text if len(text.encode("utf-8")) <= limit else memory.cut(text, limit)


class Turn:
    __slots__ = ("n", "question", "answer", "taint", "redacted")

    def __init__(self, n, question, answer, taint=(), redacted=False):
        self.n, self.question, self.answer = n, question, answer
        self.taint, self.redacted = tuple(sorted(set(taint))), redacted

    def pair(self):
        answer = REDACTED if self.redacted else self.answer
        return {"question": _bounded(self.question, HISTORY_QUESTION_BYTES),
                "answer": _bounded(answer, HISTORY_ANSWER_BYTES - 64)}


class Conversation:
    def __init__(self):
        self.turns = []
        self.memory = memory.empty()
        self.origins = {}          # entry id -> "user" | "model"; only model-written entries can be revoked
        self.taints = {}           # entry id -> tuple(paths) the entry may depend on
        self.number = 0

    # -- what a request carries ---------------------------------------------------------------------------------
    def history(self):
        """The newest turns that fit one request. The newest always stays: one turn fits the limit on its own."""
        kept, total = [], 0
        for turn in reversed(self.turns[-MAX_HISTORY:]):
            pair = turn.pair()
            total += len(pair["question"].encode("utf-8")) + len(pair["answer"].encode("utf-8"))
            if kept and total > HISTORY_TOTAL_BYTES:
                break
            kept.append(pair)
        kept.reverse()
        return kept

    def task_memory(self):
        return memory.validate(self.memory)

    def visible_taint(self):
        """Sources that anything the next request will contain may depend on."""
        found = set()
        for turn in self.turns[-MAX_HISTORY:]:
            if not turn.redacted:             # a redacted answer no longer carries what it read
                found.update(turn.taint)
        for entry in self.memory["entries"]:
            found.update(self.taints.get(entry["id"], ()))
        return tuple(sorted(found))

    # -- revocation ------------------------------------------------------------------------------------------------
    def revalidate(self, allows):
        """Drop what depends on a source `allows(path)` no longer permits. Returns the number of removed items."""
        removed = 0
        for turn in self.turns:
            if turn.taint and not turn.redacted and not all(allows(path) for path in turn.taint):
                turn.redacted, removed = True, removed + 1
        keep = []
        for entry in self.memory["entries"]:
            paths = self.taints.get(entry["id"], ())
            if self.origins.get(entry["id"]) == "fact" and paths and not all(allows(path) for path in paths):
                # The program did this, so that stays known; its words may quote the revoked file, so they go.
                entry["text"] = FACT_REDACTED
                self.origins[entry["id"]] = "user"
                self.taints.pop(entry["id"], None)
                removed += 1
            elif self.origins.get(entry["id"]) == "model" and paths and not all(allows(path) for path in paths):
                removed += 1
                self.origins.pop(entry["id"], None)
                self.taints.pop(entry["id"], None)
                continue
            keep.append(entry)
        if len(keep) != len(self.memory["entries"]):
            alive = {entry["id"] for entry in keep}
            for entry in keep:
                if entry.get("by") and entry["by"] not in alive:
                    entry["status"] = "active"      # its replacement was revoked: the corrected statement stands again
                    entry.pop("by", None)
            self.memory = {"entries": keep, "dropped": self.memory["dropped"]}
        return removed

    # -- a finished turn -------------------------------------------------------------------------------------------
    def finish(self, question, answer, ops, sources, actions=()):
        """Record a completed, approved turn. `ops` is the validated memory update or None (then only the user note)."""
        self.number += 1
        n = self.number
        taint = set(sources) | set(self.visible_taint())
        first = not self.turns and not any(e["kind"] == "goal" for e in self.memory["entries"])
        # The user's own words are kept deterministically for the goal (first message) and whenever the model sent no
        # valid update; otherwise the model's update carries the turn.
        if first and ops is not None:
            # The user's own words are the goal; a second, model-worded goal entry for the same turn would duplicate it.
            ops = {"add": [item for item in ops["add"] if item["kind"] != "goal"], "mark": ops["mark"]}
        facts_text = {memory.cut(item) for item in actions}
        note = question if first or ops is None else None
        updated, notes = memory.apply(self.memory, n, ops, user_note=note, goal=first, facts=tuple(actions))
        existing = {entry["id"] for entry in self.memory["entries"]}
        for entry in updated["entries"]:
            if entry["id"] not in existing:
                user = note is not None and entry["id"] == "m%d.1" % n
                fact = entry["status"] == "done" and entry["kind"] == "observation" and entry["text"] in facts_text
                self.origins[entry["id"]] = "user" if user or (fact and not taint) else "fact" if fact else "model"
                if not user and taint:
                    self.taints[entry["id"]] = tuple(sorted(taint))
        alive = {entry["id"] for entry in updated["entries"]}
        for table in (self.origins, self.taints):
            for ident in [ident for ident in table if ident not in alive]:
                del table[ident]
        self.memory = updated
        self.turns.append(Turn(n, question, answer, taint))
        del self.turns[:-MAX_TURNS]
        return notes


def title_of(text):
    """A short single-line title from the user's own first message (no control characters, at most 80 characters)."""
    line = " ".join("".join(char for char in text if char.isprintable() or char.isspace()).split())
    return line if len(line) <= 80 else line[:79].rstrip() + "…"


def export(conversation):
    """The record fragment persisted with `--session`: verbatim last turns, memory, and the revocation metadata."""
    turns = conversation.turns[-MAX_HISTORY:]
    paths = []

    def index(path):
        if path not in paths:
            paths.append(path)
        return paths.index(path)

    # The source table is bounded. A turn whose sources no longer fit cannot be revoked precisely later, so it is stored
    # redacted (conservative) rather than with a shortened dependency list. A later turn inherits what earlier ones read.
    known, kept_turns = set(), set()
    for turn in reversed(turns):
        if not turn.redacted and len(known | set(turn.taint)) <= MAX_SOURCE_PATHS:
            known.update(turn.taint)
            kept_turns.add(turn)
    history, meta_turns = [], []
    for turn in turns:
        redacted = turn not in kept_turns
        history.append({"question": _bounded(turn.question, HISTORY_QUESTION_BYTES),
                        "answer": REDACTED if redacted else _bounded(turn.answer, HISTORY_ANSWER_BYTES - 64)})
        meta_turns.append({"n": turn.n, "taint": [] if redacted else [index(p) for p in turn.taint],
                           "redacted": redacted})
    # A note that depends on sources which no longer fit is left out, for the same reason.
    kept = []
    for entry in conversation.memory["entries"]:
        wanted = conversation.taints.get(entry["id"], ())
        if len(set(paths) | set(wanted)) <= MAX_SOURCE_PATHS:
            for path in wanted:
                index(path)
            kept.append(entry)
    user = [e["id"] for e in kept if conversation.origins.get(e["id"]) == "user"]
    taints = {e["id"]: [index(p) for p in conversation.taints[e["id"]]] for e in kept if conversation.taints.get(e["id"])}
    saved = memory.validate({**conversation.memory, "entries": kept})
    exported = {"history": history, "memory": saved, "paths": paths, "turns": meta_turns,
                "meta": {"user": user, "taints": taints}}
    if conversation.turns:
        # The title is the user's own first message, shortened; it names the thread in the list and nothing else.
        exported["title"] = title_of(conversation.turns[0].question)
    return exported


def load(state):
    """Rebuild a conversation from a persisted ask state (or from an older record that has only `history`)."""
    conversation = Conversation()
    history = state.get("history", [])
    if "memory" in state:
        paths = state["paths"]
        conversation.memory = memory.validate(state["memory"])
        def origin(entry):
            if entry["id"] in state["meta"]["user"]:
                return "user"
            fact = entry["status"] == "done" and entry["kind"] == "observation" and entry["id"] in state["meta"]["taints"]
            return "fact" if fact else "model"
        conversation.origins = {e["id"]: origin(e) for e in conversation.memory["entries"]}
        conversation.taints = {ident: tuple(paths[i] for i in items) for ident, items in state["meta"]["taints"].items()}
        for pair, meta in zip(history, state["turns"]):
            conversation.turns.append(Turn(meta["n"], pair["question"], pair["answer"],
                                           tuple(paths[i] for i in meta["taint"]), meta["redacted"]))
        conversation.number = max((t.n for t in conversation.turns), default=0)
    else:
        for pair in history:
            # Earlier versions saved the user's words but no evidence of what the answer read.
            conversation.number += 1
            conversation.turns.append(Turn(conversation.number, pair["question"], REDACTED, redacted=True))
    return conversation
