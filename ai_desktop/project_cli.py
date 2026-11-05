"""`ai-desktop project DIRECTORY`: one read-only project, explored by the model through the controller.

Same transport, pacing and consent rules as `ai-desktop material`: one consent per question, a
step-by-step visible loop (list, find, search, read), at most 8 requests / 768 KiB, nothing sent
before a yes, no Internet, no writes, no code execution. File NAMES, search hits and fragments are
sent as data, so the consent says so. The answer lists the places Claude really received and what
was skipped or unchecked; sources are confirmed by the controller, never taken from the model.
"""
import argparse
import collections
import os
import sys
import time

import json
import shlex

from .run_controls import recipient, failure_hint, network_hint
from . import ask_cli, ask_edit, assistant_flow, permissions, task_contract
from .analysis_cli import HINTS
from .analysis_contract import AnalysisDenied, MAX_QUESTION_BYTES, validate_question
from .assistant_contract import parse_plan
from .question_consent import QuestionConsent
from .workspace_executor import ExecutionDenied
from .analysis_service import send_once
from .analysis_terminal import render_answer
from .file_context import _read_hint
from .material_cli import (GRANT_HINTS, GRANT_SECONDS, SEPARATOR, Session, SessionWindow, _countdown, _countdown_end,
                           _safe, field, rule)
from .material_grant import GrantDenied, MaterialGrant, TransferSession
from .material_loop import lines_label
from .project_loop import Loop, history_turn
from .project_scope import Project, ProjectError
from .project_step import MAX_HISTORY, MAX_STAGES, MAX_STEPS, TOTAL_BYTES, parse_decision, plain_answer
from .question_input import PromptSession, paste_token
from . import task_workspace, workspace_trash
from .terminal_consent import confirm

Binding = collections.namedtuple("Binding", "material_id sha256")
ROOT_HINTS = {
    "PROJECT_ROOT": "This must be a regular directory (not a link) within files.read in permissions.toml.",
    "PROJECT_POLICY": "The file policy changed or is invalid. Run the command again.",
}
SKIP_LABELS = (("denied", "denied or out of scope"), ("links", "links"), ("special", "special files"),
               ("unsafe", "unsafe attributes or hard links"), ("vcs", ".git directories"),
               ("other", "other skipped"))
REASONS = {"files": "file count limit", "entries": "entry count limit", "depth": "depth limit",
           "unreadable": "unreadable directory"}
HELP = """Ask about the project in a plain sentence; the program picks the files and parts it needs. You can also ask for file changes
(you see a diff and approve the save first) or type "carry on". Large pastes go to Claude as attachments; Ctrl+C twice quits.
Optional (local commands, nothing is sent):
  /files [DIR]      show file names (up to 40)
  /search TEXT      search project file contents for this exact text (up to 20 matches)
  /refresh          scan the directory again (also happens before every question)
  /log              what the app handed to Claude in this session
  /exit             quit (also Ctrl+D)"""


def _source(path, first, last):
    return "%s:%d–%d" % (_safe(path, 80), first, last) if first != last else "%s:%d" % (_safe(path, 80), first)


class EditRefused(ValueError):
    """The proposed change cannot be mapped onto the owner's write policy; the message is for the user."""


def build_edit_plan(project, snapshot, decision):
    """Map a project-relative edit proposal onto configured scopes, or refuse with a human reason.

    The model names only project files that the scan indexed; the CONTROLLER picks the write and read
    scopes, so the model never chooses its own authority."""
    policy = snapshot.policy
    if policy is None:
        raise EditRefused("No permissions.toml: changing files needs a write scope.")
    root = project.root
    writes = [s for s in policy.write_scopes() if s.mode == "tree" and (root == s.base or root.startswith(s.base + "/"))]
    if not writes:
        raise EditRefused("files.write in permissions.toml does not cover this project (it needs an entry like ~/Projects/**).")
    write = max(writes, key=lambda s: len(s.base))
    reads = [s for s in policy.scopes() if s.mode == "tree"]

    def relative(path, base):
        absolute = root + "/" + path
        return absolute[len(base) + 1:]
    selectors, existing, created = [], [], []
    try:
        for path in decision["files"]:
            entry = project._by_path.get(path)
            if entry is None:
                raise EditRefused(recipient("Claude named a file that is not in the project index."))
            if entry.size > task_contract.MAX_FILE:
                raise EditRefused("File %s is over the 64 KiB limit for edit mode." % _safe(path, 80))
            covering = [s for s in reads if (root + "/" + path).startswith(s.base + "/")]
            if not covering:
                raise EditRefused("File %s is not within files.read." % _safe(path, 80))
            scope = max(covering, key=lambda s: len(s.base))
            selectors.append({"id": "f%d" % scope.index, "path": relative(path, scope.base)})
            existing.append(task_contract.path(relative(path, write.base)))
        for path in decision["create"]:
            if path in project._by_path:
                raise EditRefused("File %s already exists and is not overwritten as a new file." % _safe(path, 80))
            parent = path.rpartition("/")[0]
            if parent and not any(e.path.startswith(parent + "/") for e in project.files):
                raise EditRefused("Directory %s does not exist; edit mode does not create directories." % _safe(parent, 80))
            created.append(task_contract.path(relative(path, write.base)))
    except ValueError as exc:
        if isinstance(exc, EditRefused):
            raise
        raise EditRefused("This path cannot be used in edit mode (up to 5 levels below the write scope directory, "
                          "names made of letters, digits, dots, _ and -).") from None
    plan = {"action": "edit", "write": "w%d" % write.index, "files": selectors, "create": created, "check": decision["check"]}
    try:
        return parse_plan(json.dumps(plan), snapshot.access), existing, created
    except ValueError:
        raise EditRefused("This change is outside the scopes in permissions.toml.") from None


def build_delete_plan(project, snapshot, decision):
    """Map project-relative deletions onto the owner's write scope; returns (scope, workspace, names)."""
    policy = snapshot.policy
    if policy is None:
        raise EditRefused("No permissions.toml: deleting files needs a write scope.")
    root = project.root
    writes = [s for s in policy.write_scopes() if s.mode == "tree" and (root == s.base or root.startswith(s.base + "/"))]
    if not writes:
        raise EditRefused("files.write in permissions.toml does not cover this project (it needs an entry like ~/Projects/**).")
    scope = max(writes, key=lambda s: len(s.base))
    names, absolute = [], []
    try:
        for path in decision["delete"]:
            entry = project._by_path.get(path)
            if entry is None:
                raise EditRefused(recipient("Claude named a file that is not in the project index."))
            if entry.size > workspace_trash.MAX_FILE_BYTES:
                raise EditRefused("File %s is too large to delete safely (limit 16 MiB)." % _safe(path, 80))
            names.append(task_contract.path((root + "/" + path)[len(scope.base) + 1:]))
            absolute.append(root + "/" + path)
        policy.require_write(tuple(absolute), index=scope.index)
        workspace = policy.write_workspace(scope.index)
    except EditRefused:
        raise
    except ValueError:
        raise EditRefused("This deletion is outside the scopes in permissions.toml or the path is too deep "
                          "(up to 5 levels below the write scope directory).") from None
    return scope, workspace, names


def build_parser():
    from .run_controls import add_arguments
    parser = argparse.ArgumentParser(prog="ai-desktop project",
                                     description="Work with one project directory: read-only analysis, with changes and deletions only after you approve them")
    parser.add_argument("path", help="Directory within files.read in permissions.toml")
    add_arguments(parser, session=True)
    parser.add_argument("--format", choices=("auto", "plain", "markdown"), default="auto")
    parser.add_argument("--v", "--verbose", "-v", "--details", dest="details", action="store_true",
                        help="Show the exact payload and diagnostics")
    return parser


class ProjectSession(Session):
    accepts_pastes = True
    def __init__(self, project, name, policy, args, reader, *, clock=time.monotonic, sleep=time.sleep,
                 window=None, sender=send_once):
        self.project, self.name, self.policy, self.args, self.reader = project, name, policy, args, reader
        self._clock, self._sender = clock, sender
        from .material_cli import Pacer
        self.window = window or SessionWindow(quiet=not args.details, clock=clock)
        self.pacer = Pacer(clock=clock, sleep=sleep)
        self.tasks, self.history = [], []
        self.edit_transfers = []                # whole files handed over for a proposed change, shown by /log
        self.prompt_session = PromptSession()
        self._skipped_shown = None              # last skipped-entry count already reported after an answer
        self.progress = ""                      # the model's own working summary (what is done, what next), session-only
        self._current = None
        from .run_controls import provider_allowed
        self.can_send = provider_allowed(policy)
        self.binding = Binding(project.project_id, project.manifest_sha256())

    def _gate_kwargs(self, packet):
        return dict(question=packet.question, context=None, project=packet.material)

    def ledger(self):
        if not self.edit_transfers or any(task["session"].ledger() for task in self.tasks):
            super().ledger()
        for number, item in enumerate(self.edit_transfers, 1):
            print("Edit %d: %d file(s), %d B, state: %s: %s" % (
                number, len(item["files"]), item["bytes"], item["state"],
                ", ".join(_safe(path, 60) for path in item["files"][:8]) or "new files only"))

    # --- optional local helpers --------------------------------------------------------
    def files(self, prefix):
        if not self._refresh():
            return
        try:
            items, following, total = self.project.listing(prefix.strip(), 0, 40)
        except ProjectError:
            print("Enter a directory relative to the project, e.g. /files src.")
            return
        for entry in items:
            print("%s  (%d B)" % (_safe(entry.path), entry.size))
        print("(showing %d of %d files)" % (len(items), total) if following else "(%d file(s))" % total)

    def search(self, needle):
        if not self._refresh():
            return
        try:
            result = self.project.search_text(needle)
        except ProjectError as exc:
            print(ROOT_HINTS["PROJECT_POLICY"] if exc.code == "PROJECT_POLICY"
                  else "Enter non-empty text of up to 256 bytes, without control characters.")
            return
        for hit in result.hits:
            print("%s:%d: %s" % (_safe(hit.path), hit.line, _safe(hit.text)))
        if not result.hits:
            print("No matches in the files searched." if result.complete else "No matches in this batch; the search did not cover everything.")
        elif not result.complete:
            print("There are more matches or unsearched files; narrow the phrase.")
        skipped = {k: v for k, v in result.skipped.items() if v}
        if skipped:
            print("Skipped in the search: %s." % ", ".join("%s: %d" % kv for kv in skipped.items()))

    # --- a question in plain words -----------------------------------------------------
    def question(self, text, span=None):
        if not self.can_send:
            print(network_hint())
            return
        try:
            text = validate_question(text.strip())
        except ValueError:
            print("The question must be one line of plain text, up to %d bytes." % MAX_QUESTION_BYTES)
            return
        pastes = self.prompt_session.take_pastes(text)
        self._pastes = pastes
        if not self._refresh():
            return
        self._current = text
        loop = Loop(self.project, history=self.history, progress=self.progress, pasted=pastes)
        grant = MaterialGrant(self.binding.material_id, self.binding.sha256, text[:256], self._clock() + GRANT_SECONDS)
        session = TransferSession(self.binding, grant, clock=self._clock)
        self.tasks.append({"session": session, "label": "%s: %s" % (self.name, _safe(text, 60))})
        self._scope(text, loop)
        try:
            approved = confirm("project-task", reader=self.reader)
        except EOFError:
            approved = False
        except KeyboardInterrupt:
            self._interrupted()
            approved = False
        if not approved:
            print("Cancelled. No data was sent.")
            return
        self._open_section()
        try:
            partial = self._run(text, loop, session)
        finally:
            self._close_section()
        stage = 1
        while partial is not None and stage < MAX_STAGES:
            # Claude says the analysis is not finished. Each further stage has its own small yes and fresh limits.
            print(self._coverage_line(loop, stage))
            try:
                again = confirm("project-continue", reader=self.reader)
            except EOFError:
                again = False
            except KeyboardInterrupt:
                self._interrupted()
                again = False
            if not again:
                print("Stopped after stage %d of %d; partial conclusions are above." % (stage, MAX_STAGES))
                print()
                return
            stage += 1
            if not self._refresh():
                return
            loop = Loop(self.project, history=self.history, progress=self.progress, pasted=pastes,
                        visited=loop.visited, stage=stage)
            grant = MaterialGrant(self.binding.material_id, self.binding.sha256, text[:256], self._clock() + GRANT_SECONDS)
            session = TransferSession(self.binding, grant, clock=self._clock)
            self.tasks.append({"session": session, "label": "%s: %s (stage %d)" % (self.name, _safe(text, 50), stage)})
            self._open_section("Stage %d of %d" % (stage, MAX_STAGES))
            try:
                partial = self._run(text, loop, session)
            finally:
                self._close_section()
        if partial is not None:
            print("Reached the limit of %d stages; partial conclusions are above. Type \"continue\" to start another round." % MAX_STAGES)
            print()

    def _coverage_line(self, loop, stage):
        """Controller-counted coverage of the whole deep task (never the model's claim)."""
        total = sum(e.size for e in self.project.files)
        files = len(loop.visited)
        size = sum(self.project._by_path[p].size for p in loop.visited if p in self.project._by_path)
        return (recipient("Partial analysis, stage %d of %d: Claude read %d of %d files (%s of %s in the index).") % (
            stage, MAX_STAGES, files, len(self.project.files),
            "%.1f KiB" % (size / 1024), "%.1f MiB" % (total / 1048576)))

    def _refresh(self, announce=False):
        """Rescan before each question so the index matches the disk now; the grant binds the fresh manifest."""
        before = self.project.stats.get("indexed")
        try:
            self.project.scan()
        except (ProjectError, ValueError):
            print("Could not refresh the index (the policy or directory changed). Run the command again.")
            return False
        self.binding = Binding(self.project.project_id, self.project.manifest_sha256())
        now = self.project.stats["indexed"]
        if announce or now != before:
            print("Index refreshed: %d file(s)%s." % (now, "" if now == before else " (was %d)" % before))
        return True

    def _scope(self, text, loop):
        stats = self.project.stats
        pasted = [o for o in loop.observations if o["kind"] == "pasted"]
        skipped = ["%s: %d" % (label, stats[key]) for key, label in SKIP_LABELS if stats[key]]
        print()
        if not self.args.details:
            # Compact by default: what leaves the computer in one line, extras only when present.
            total = self._skipped_total()
            print(recipient("To Claude: the question, file names and parts from %s (%d file(s)%s%s)") % (
                self.name, stats["indexed"], ", %d entries skipped" % total if total else "",
                "" if self.project.complete else "; INDEX INCOMPLETE: %s" % REASONS.get(self.project.reason, "limit")))
            extras = []
            if pasted:
                extras.append("pastes: " + ", ".join("#%d (%d lines, %d B)" % (o["id"], o["lines"], len(o["text"].encode("utf-8")))
                                                       for o in pasted))
            if loop.history:
                extras.append("context: %d earlier question(s) and answer(s)" % len(loop.history))
            if extras:
                print(" · ".join(extras))
            return
        print(rule("To send"))
        print(field("Question", _safe(text, 200)))
        print(field("Project", "%s (%s), %d file(s)%s" % (
            self.name, self.project.project_id[:8], stats["indexed"],
            "" if self.project.complete else " — INDEX INCOMPLETE (%s)" % REASONS.get(self.project.reason, "limit"))))
        if skipped:
            print(field("Skipped", *skipped))
        if pasted:
            print(field("Pastes", *["#%d: %d lines, %d B" % (o["id"], o["lines"], len(o["text"].encode("utf-8"))) for o in pasted]))
        if loop.history:
            print(field("Context", "%d earlier question(s) and answer(s) from this session (shortened)" % len(loop.history)))
        print(field("Data", recipient("the question, plus the file names, matches and parts Claude asks for")))
        print(field("Without", "Internet, file writes or running code · time limit %d s" % GRANT_SECONDS))
        print(field("Content", recipient("Claude will ask for a list of file names, a search or lines of files from this project."),
                    "File names, matches and parts are sent as data; the rest of the project is not sent.",
                    "Limits: up to %d requests, up to %d KiB of data in total; you will see each step as it happens." % (MAX_STEPS, TOTAL_BYTES // 1024)))
        print(SEPARATOR)

    def _run(self, text, loop, session):
        for number in range(1, loop.max_steps + 1):
            step_input = loop.request_input(number, session.bytes_left())
            try:
                packet = session.prepare_step(text, step_input, loop.data_bytes(), (), project=True)
            except GrantDenied as exc:
                print(GRANT_HINTS[exc.code])
                return self._stopped(loop)
            if self.args.details:
                if number == 1:
                    self._line("=== EXACT PAYLOAD (request 1) ===\n" + packet.payload)
                self._line("REQUEST %d: SHA256 %s, %d B of data" % (number, packet.sha256, packet.data_bytes))
            delivery = self._deliver(session, packet, on_handoff=loop.mark_handed_off)
            if delivery is None:
                return
            self._timing(delivery)
            try:
                decision = parse_decision(delivery.answer)
            except ValueError:
                decision = plain_answer(delivery.answer)          # prose instead of an action: shown, but without sources
                if decision is None:
                    print(recipient("The answer from Claude is not in the required format; the task was stopped without a retry."))
                    print("Start of the answer: %s" % _safe(str(delivery.answer).replace("\n", " "), 4000 if self.args.details else 160))
                    return self._stopped(loop)
                loop.format_note = True
            action = decision["action"]
            if action == "answer":
                return self._answer(text, loop, decision)
            if action == "edit":
                return self._edit(text, loop, decision)
            if action == "ask_user":
                self._close_section()
                print(recipient("Claude asks: %s") % _safe(decision["text"], 500))
                self.remember(text, recipient("[Claude asked: %s]") % decision["text"])
                return
            if number == loop.max_steps:
                print(recipient("Claude did not answer in the last step."))
                return self._stopped(loop)
            if loop.repeated(decision):
                print(recipient("Claude repeated an earlier request without progress; the task was stopped."))
                return self._stopped(loop)
            try:
                note = loop.execute(decision)
            except ValueError:                  # ProjectError is a ValueError: a changed policy or directory
                print("The file policy changed during the task, so it was stopped.")
                return self._stopped(loop)
            self._line(recipient("  Step %d/%d: Claude asked for %s") % (number, loop.max_steps, _safe(note, 200)))
        return self._stopped(loop)

    def _delivered_lines(self, loop):
        """What Claude really received, one entry per file, as display lines."""
        received, size = loop.delivered_summary()
        if not received:
            return [], "no file lines", size
        lines = ["%s: %s" % (_safe(path, 80), lines_label(spans[:8]) + (" …" if len(spans) > 8 else ""))
                 for path, spans in list(received.items())[:10]]
        if len(received) > 10:
            lines.append("… (+%d more file(s))" % (len(received) - 10))
        return lines, "", size

    def _notes(self, loop, rejected=()):
        """Things a reader must not assume away: shown even without --v, and nothing else."""
        notes = []
        if rejected:
            notes.append(("Rejected", [_source(p, a, b) for p, a, b in rejected]
                          + [recipient("(Claude cited sources it did not receive)")]))
        if not self.project.complete:
            notes.append(("Index", [recipient("incomplete (%s) — there may be files Claude could not see") %
                                     REASONS.get(self.project.reason, "limit")]))
        partial = [needle for needle, complete in loop.searches if not complete]
        if partial:
            notes.append(("Incomplete", ["search (budget or further pages): " + ", ".join('"%s"' % _safe(n, 40) for n in partial)]))
        if loop.format_note:
            notes.append(("Format", [recipient("Claude answered in plain text instead of the agreed action; sources are not confirmed.")]))
        if loop.changed:
            notes.append(("Changed", [", ".join(_safe(p, 80) for p in loop.changed[:8]) + " (during the task or after the scan)"]))
        return notes

    def _received_field(self, loop):
        lines, empty, size = self._delivered_lines(loop)
        if not lines:
            return field(recipient("Claude received"), "%s; the rest of the project was not sent" % empty)
        return field(recipient("Claude received"), *lines, "(%s; the rest of the project was not sent)" % (
            "%d B of parts read" % size if size else "search results only"))

    def _skipped_field(self):
        stats = self.project.stats
        skipped = ["%s: %d" % (label, stats[key]) for key, label in SKIP_LABELS if stats[key]]
        return field("Out of reach", *skipped) if skipped else None

    def _edit(self, text, loop, decision):
        from .run_controls import require_changes
        try:
            require_changes()
        except AnalysisDenied:
            print("This agent is read-only. The proposed change was rejected.")
            return self._remember_edit(text, "the agent profile allows reading only")
        """Claude proposes a change: map it to the owner's write policy, ask once, then hand over to the proven
        ask/task pipeline (copy test, full diff, separate write consent, rollback). Nothing is written here."""
        self._close_section()
        if decision.get("delete"):
            return self._delete(text, decision)
        try:
            request_text = self._expand_pastes(text)
            snapshot = assistant_flow.capture_access()
            plan, existing, created = build_edit_plan(self.project, snapshot, decision)
            versions = {}
            for path, name in zip(decision["files"], existing):
                try:
                    _, raw = permissions.read_bounded(snapshot.policy, self.project.root + "/" + path,
                                                      task_contract.MAX_FILE)
                except ValueError:
                    raise EditRefused("File %s is unavailable or changed since the analysis. Ask again." % _safe(path, 80)) from None
                version = task_workspace.sha(raw)
                if loop.fully_read(path) and version != loop.delivered_versions.get(path):
                    raise EditRefused("File %s changed since the analysis. Ask again to work on the current version." % _safe(path, 80))
                versions[name] = version
            blocker = self._edit_blocker(snapshot, plan)
            if blocker:
                raise EditRefused(blocker)
        except (EditRefused, ProjectError, assistant_flow.AccessDenied, AnalysisDenied) as exc:
            print(rule("File change"))
            print(field("Refused", str(exc) if isinstance(exc, EditRefused) else "The file policy does not allow this change."))
            print(SEPARATOR)
            print()
            return self._remember_edit(text, "proposed change rejected by the app")
        # Content Claude has not seen yet would leave the computer in the edit request: that needs its own yes.
        # A new file, or files Claude already read whole in this task, add nothing new: no second question.
        unsent = [path for path in decision["files"] if not loop.fully_read(path)]
        summary = " · ".join(part for part in (
            "new: " + ", ".join(_safe(p, 80) for p in decision["create"]) if created else "",
            "to change: " + ", ".join(_safe(p, 80) for p in decision["files"]) if existing else "",
            "check: " + decision["check"]) if part)
        if unsent:
            print(rule("File change"))
            print(field("To change", *[_safe(p, 80) for p in decision["files"]]))
            if created:
                print(field("New", *[_safe(p, 80) for p in decision["create"]]))
            print(field("Check", decision["check"]))
            print(field("Next", recipient("the content of files Claude has not seen yet (%s) goes to Claude in a separate task;") %
                        ", ".join(_safe(p, 60) for p in unsent), "saving happens only after you see the diff and give separate consent."))
            print(SEPARATOR)
            try:
                approved = confirm("project-edit", reader=self.reader)
            except EOFError:
                approved = False
            except KeyboardInterrupt:
                self._interrupted()
                approved = False
            if not approved:
                print("Cancelled. File contents were not sent. Nothing was saved.")
                print()
                return self._remember_edit(text, "the user rejected the proposed change")
        else:
            print("File change — %s. Preparing the diff…" % summary)
        code = self._run_edit(request_text, snapshot, plan, versions)
        print()
        return self._remember_edit(text, "change saved after the user's consent" if code == 0
                                   else "change NOT saved (rejected, failed test or error)")

    def _expand_pastes(self, text):
        """The edit request carries the pasted text itself (the marker alone would tell the model nothing)."""
        expanded = text
        for number, body in getattr(self, "_pastes", ()):
            expanded = expanded.replace(paste_token(number, body), body)
        if len(expanded.encode("utf-8")) > task_contract.MAX_INSTRUCTION:
            raise EditRefused("The file change request with its paste is over %d B. Shorten the paste or split the change." % task_contract.MAX_INSTRUCTION)
        return expanded

    def _edit_blocker(self, snapshot, plan):
        """Why a change cannot start right now (an unfinished earlier task, a full journal ...), in words."""
        try:
            workspace = snapshot.policy.write_workspace(int(plan["write"][1:]))
        except ValueError:
            return ("The write scope in permissions.toml does not point to a safe working directory "
                    "(e.g. a whole HOME needs policy version = 2; an entry like ~/Projects/** is recommended).")
        try:
            task_workspace._can_apply(workspace)
        except task_workspace.JournalLimit:
            return "Reached the limit of saved tasks in the write scope directory. Check: ai-desktop task status --workspace DIRECTORY."
        except ValueError as exc:
            if str(exc) == "recovery required":
                return ("An earlier file change task is unfinished or in conflict. "
                        "Check: ai-desktop task status --workspace DIRECTORY (then task rollback).")
            return "The task journal in the write scope directory is unreadable or has unexpected entries (ai-desktop task status)."
        except (ExecutionDenied, OSError):
            return "The write scope directory is unavailable or has unsafe permissions."
        return None

    def _delete(self, text, decision):
        from .run_controls import require_changes
        try:
            require_changes()
        except AnalysisDenied:
            print("This agent is read-only. The proposed deletion was rejected.")
            return self._remember_edit(text, "the agent profile allows reading only")
        """Deleting is moving to a recoverable trash: it needs the owner's own yes (nothing is sent to Claude)."""
        print(rule("File deletion"))
        try:
            snapshot = assistant_flow.capture_access()
            scope, workspace, names = build_delete_plan(self.project, snapshot, decision)
            prepared = workspace_trash.prepare(workspace, names)
        except (EditRefused, assistant_flow.AccessDenied, AnalysisDenied, ExecutionDenied, ValueError, OSError) as exc:
            print(field("Refused", str(exc) if isinstance(exc, EditRefused)
                        else workspace_trash.FULL_MESSAGE if isinstance(exc, workspace_trash.TrashFull)
                        else "The file policy does not allow this deletion."))
            print(SEPARATOR)
            print()
            return self._remember_edit(text, "proposed deletion rejected by the app")
        print(field("To delete", *["%s (%d B)" % (_safe(p, 80), e["size"])
                                     for p, e in zip(decision["delete"], prepared.data()["entries"])]))
        print(field("Trash", recipient("files are moved to a private task directory and can be restored; nothing goes to Claude")))
        print(SEPARATOR)
        try:
            approved = confirm("project-delete", reader=self.reader)
        except EOFError:
            approved = False
        except KeyboardInterrupt:
            self._interrupted()
            approved = False
        if not approved:
            print("Cancelled. Nothing was deleted.")
            print()
            return self._remember_edit(text, "the user rejected the file deletion")
        try:
            snapshot.policy.recheck()
            plan = workspace_trash.execute(prepared, prepared.authorize())
        except workspace_trash.TrashRecoveryRequired as exc:
            print("Deletion interrupted. The result needs checking; the data and journal were kept.")
            self._trash_recovery(workspace, exc.plan)
            return self._remember_edit(text, "deletion interrupted; the task trash needs checking")
        except KeyboardInterrupt:
            print("\nDeletion interrupted. Check the task state before the next operation.")
            self._trash_recovery(workspace, prepared.data()["plan"])
            return self._remember_edit(text, "deletion interrupted; the state needs checking")
        except (ExecutionDenied, ValueError, OSError):
            print("Not deleted: a file changed, a name is unusual or the policy changed. Nothing was deleted.")
            print()
            return self._remember_edit(text, "file deletion failed, nothing was deleted")
        print("Deleted %d file(s) (in the task trash)." % len(names))
        print("Restore: " + shlex.join(["ai-desktop", "task", "restore", "--workspace", workspace, "--plan", plan]))
        print()
        return self._remember_edit(text, "deleted %d file(s) after the user's consent (can be restored)" % len(names))

    @staticmethod
    def _trash_recovery(workspace, plan):
        print("State: " + shlex.join(["ai-desktop", "task", "status", "--workspace", workspace]))
        print("Restore: " + shlex.join(["ai-desktop", "task", "restore", "--workspace", workspace, "--plan", plan]))
        print()

    def _run_edit(self, text, snapshot, plan, versions):
        args = ask_cli.build_parser().parse_args(["--v"] if self.args.details else [])
        args.format = self.args.format
        permit = None
        try:
            self.window.close()                       # the edit pipeline opens its own managed session
            self.pacer.wait_turn(_countdown, _countdown_end)
            permit = QuestionConsent(text, snapshot, plan_ready=True)
            permit.approve()
            prepared = ask_edit.prepare(plan, snapshot, args, self.reader, question_consent=permit,
                                        expected_versions=versions)
            self.pacer.record()
            sent = prepared.source["files"]
            transfer = {"files": [item["path"] for item in sent if item["content"] is not None],
                        "bytes": sum(len(item["content"].encode("utf-8")) for item in sent if item["content"] is not None),
                        "state": "not known whether it arrived"}      # recorded before the hand-off, like the read steps
            self.edit_transfers.append(transfer)
            response = ask_cli.request(text, args, self.reader, task=prepared.source, write_grant=prepared.write_grant,
                                       check=permit.check, question_consent=permit)
            transfer["state"] = "answer received"
            permit.check()
            permit.close()
            return ask_edit.finish(prepared, response, args, self.reader).code
        except ask_edit.CreateCollision:
            print("The file to create already exists. Nothing was saved.")
        except (ask_edit.EditDenied, ExecutionDenied) as exc:
            print("The change was not prepared (the consent, policy or file state changed). Nothing was saved.")
        except AnalysisDenied as exc:
            print(failure_hint(getattr(exc, "reason", None), HINTS.get(getattr(exc, "reason", None), "Could not get an answer. Check ai-desktop status."),
                               **({"diagnostic": exc.diagnostic} if exc.diagnostic is not None else {})))
            print("Data already handed over cannot be taken back; the change will not be retried automatically.")
        except assistant_flow.Declined:
            print("Cancelled.")
        except KeyboardInterrupt:
            print("\nInterrupted.")
            self._interrupted()
        finally:
            if permit is not None:
                permit.close()
        return 2

    def _remember_edit(self, text, outcome):
        self.remember(text, "[%s]" % outcome)

    def _skipped_total(self):
        stats = self.project.stats
        return sum(stats[key] for key, _ in SKIP_LABELS)

    def _skipped_line(self):
        """One short line shown by default: excluded entries affect completeness, but their names are never shown."""
        total = self._skipped_total()
        return recipient("Out of reach for Claude: %d entries skipped during the scan (details: --v).") % total if total else None

    def _answer(self, text, loop, decision):
        self._close_section()
        confirmed, rejected = loop.confirm_sources(decision["sources"])
        print(rule("Answer") + "\n")
        print(render_answer(decision["text"], output_format=self.args.format))
        notes = self._notes(loop, rejected)
        if self.args.details:                       # --v: the full account of sources and scope, every time
            print("\n" + rule("Sources"))
            if confirmed:
                print(field("Confirmed", *[_source(p, a, b) for p, a, b in confirmed]))
            else:
                print(field("Confirmed", recipient("none — Claude named no sources the app could confirm")))
            print()
            print(rule("Scope"))
            print(self._received_field(loop))
            skipped = self._skipped_field()
            if skipped:
                print(skipped)
            for label, values in notes:
                print(field(label, *values))
            print(SEPARATOR)
            print("ASK: PASS")
        elif notes:                                  # default: silence unless there is something to be careful about
            print("\n" + rule("Note"))
            for label, values in notes:
                print(field(label, *values))
            if self._skipped_line():
                print(field("Out of reach", "%d entries skipped during the scan (details: --v)" % self._skipped_total()))
            print(SEPARATOR)
            self._skipped_shown = self._skipped_total()
        elif self._skipped_line() and self._skipped_total() != self._skipped_shown:
            print("\n" + self._skipped_line())          # reported once per session, again only if the count changes
            self._skipped_shown = self._skipped_total()
        print()
        self.remember(text, decision["text"])
        if decision.get("progress"):
            self.progress = decision["progress"]
        return decision if decision.get("continue") else None

    def _stopped(self, loop):
        self._close_section()
        print(rule("Stopped without an answer"))
        print(field("What next", "Narrow the question or ask it again (new consent).",
                    "Type e.g. \"continue\" so the next question knows what was already checked."))
        print(self._received_field(loop))
        if self.args.details:
            skipped = self._skipped_field()
            if skipped:
                print(skipped)
        for label, values in self._notes(loop):
            print(field(label, *values))
        if not self.args.details and self._skipped_line():
            print(field("Out of reach", "%d entries skipped during the scan (details: --v)" % self._skipped_total()))
        print(SEPARATOR)
        print()
        received, size = loop.delivered_summary()
        self._remember_stop(received, "parts of %d file(s) (%d B)" % (len(received), size))

    # --- loop ----------------------------------------------------------------------------
    def run(self):
        stats = self.project.stats
        print()
        print("Project %s (%s): %d file(s) in the index%s. Scanned locally." % (
            self.name, self.project.project_id[:8], stats["indexed"],
            "" if self.project.complete else " (index incomplete: %s)" % REASONS.get(self.project.reason, "limit")))
        print(recipient("Only what you approve goes to Claude."))
        print()
        print("Options:")
        print("/exit      quits")
        print("/help      shows optional commands")
        print()
        print("What do you want to do with it?")
        if not self.can_send:
            print(network_hint(short=True))
        return self._loop()

    def _loop(self):
        try:
            while True:
                try:
                    line = self._read_command()
                except EOFError:
                    return 0
                except KeyboardInterrupt:
                    print()
                    return 0
                if not self._dispatch(line.strip()) or self._quit:
                    return 0
        finally:
            try:
                self.window.close()
            except AnalysisDenied:
                print("Could not confirm that the session stopped. Run ai-desktop stop.")

    def _dispatch(self, line):
        from .run_controls import conversation_command
        if conversation_command(line, saved=getattr(self, "conversation", None) is not None):
            return True
        if not line:
            return True
        if line.casefold() in ("koniec", "exit"):         # the bare word ends the session too, it is not a question
            return False
        if not line.startswith("/"):
            self.question(line)
            return True
        command, _, rest = line.partition(" ")
        rest = rest.strip()
        if command in ("/koniec", "/exit", "/q"):
            return False
        if command in ("/pomoc", "/help", "/?"):
            print(recipient(HELP))
        elif command in ("/files", "/pliki"):
            self.files(rest)
        elif command in ("/search", "/szukaj"):
            self.search(rest)
        elif command in ("/log", "/rejestr"):
            self.ledger()
        elif command in ("/refresh", "/odswiez"):
            self._refresh(announce=True)
        else:
            print("Unknown command. /help shows the available ones.")
        return True


from .run_controls import configured


@configured
def main(argv=None, *, reader=input, clock=time.monotonic, sleep=time.sleep, window=None, sender=send_once):
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if os.geteuid() == 0 or os.getuid() != os.geteuid():
            raise AnalysisDenied("identity")
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise AnalysisDenied("consent")
        try:
            policy = permissions.load_policy(optional=True)
        except ValueError as exc:
            print(_read_hint(exc))
            return 2
        if policy is None:
            print("No permissions.toml: this directory needs a files.read scope. See config/permissions.example.toml.")
            return 2
        try:
            project = Project(policy, os.path.abspath(os.path.expanduser(args.path))).scan()
        except ProjectError as exc:
            print(ROOT_HINTS.get(exc.code, ROOT_HINTS["PROJECT_ROOT"]))
            return 2
        except ValueError as exc:
            print(_read_hint(exc))
            return 2
    except AnalysisDenied:
        print("This command needs a regular user and a terminal.")
        return 2
    session = ProjectSession(project, os.path.basename(project.root), policy, args, reader, clock=clock, sleep=sleep,
                             window=window, sender=sender)
    try:
        session.attach_conversation("project", project.root, project.manifest_sha256())
        return session.run()
    except (OSError, ValueError):
        print("Cannot use the saved conversation. The project or settings may have changed; check ai-desktop sessions list.")
        return 2
    finally:
        session.close_conversation()


if __name__ == "__main__":
    raise SystemExit(main())
