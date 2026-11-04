"""`ai-desktop project KATALOG`: one read-only project, explored by the model through the controller.

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
    "PROJECT_ROOT": "Katalog musi być zwykłym katalogiem (nie dowiązaniem) w zakresie files.read z permissions.toml.",
    "PROJECT_POLICY": "Polityka plików zmieniła się albo jest nieprawidłowa; uruchom polecenie ponownie.",
}
SKIP_LABELS = (("denied", "zabronione lub poza zakresem"), ("links", "dowiązania"), ("special", "pliki specjalne"),
               ("unsafe", "niebezpieczne atrybuty lub twarde linki"), ("vcs", "katalogi .git"),
               ("other", "inne pominięte"))
REASONS = {"files": "limit liczby plików", "entries": "limit liczby wpisów", "depth": "limit głębokości",
           "unreadable": "nieczytelny katalog"}
HELP = """Zadaj pytanie o projekt zwykłym zdaniem; program sam dobierze pliki i fragmenty. Możesz też poprosić o zmianę plików
(najpierw zobaczysz diff i zatwierdzisz zapis) albo napisać „rób dalej”. Duże wklejki trafiają do Claude jako załączniki; Ctrl+C ×2 kończy.
Opcjonalnie (polecenia lokalne, nic nie wysyłają):
  /pliki [KATALOG]  pokaż nazwy plików (do 40)
  /szukaj TEKST     szukaj dosłownie w treści plików projektu (do 20 trafień)
  /odswiez          przeskanuj katalog ponownie (dzieje się też przed każdym pytaniem)
  /rejestr          co aplikacja przekazała do Claude w tej sesji
  /koniec           zakończ (też Ctrl+D)"""


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
        raise EditRefused("Brak permissions.toml: zmiana plików wymaga zakresu zapisu.")
    root = project.root
    writes = [s for s in policy.write_scopes() if s.mode == "tree" and (root == s.base or root.startswith(s.base + "/"))]
    if not writes:
        raise EditRefused("files.write w permissions.toml nie obejmuje tego projektu (potrzebny wpis typu ~/Projekty/**).")
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
                raise EditRefused(recipient("Claude wskazał plik spoza indeksu projektu."))
            if entry.size > task_contract.MAX_FILE:
                raise EditRefused("Plik %s przekracza limit 64 KiB trybu edycji." % _safe(path, 80))
            covering = [s for s in reads if (root + "/" + path).startswith(s.base + "/")]
            if not covering:
                raise EditRefused("Plik %s nie leży w zakresie files.read." % _safe(path, 80))
            scope = max(covering, key=lambda s: len(s.base))
            selectors.append({"id": "f%d" % scope.index, "path": relative(path, scope.base)})
            existing.append(task_contract.path(relative(path, write.base)))
        for path in decision["create"]:
            if path in project._by_path:
                raise EditRefused("Plik %s już istnieje; nie nadpisuję go jako nowego." % _safe(path, 80))
            parent = path.rpartition("/")[0]
            if parent and not any(e.path.startswith(parent + "/") for e in project.files):
                raise EditRefused("Katalog %s nie istnieje; tryb edycji nie tworzy katalogów." % _safe(parent, 80))
            created.append(task_contract.path(relative(path, write.base)))
    except ValueError as exc:
        if isinstance(exc, EditRefused):
            raise
        raise EditRefused("Ścieżka nie nadaje się do trybu edycji (do 5 poziomów od katalogu zakresu zapisu, "
                          "nazwy z liter, cyfr, kropki, _ i -).") from None
    plan = {"action": "edit", "write": "w%d" % write.index, "files": selectors, "create": created, "check": decision["check"]}
    try:
        return parse_plan(json.dumps(plan), snapshot.access), existing, created
    except ValueError:
        raise EditRefused("Zmiana nie mieści się w zakresach z permissions.toml.") from None


def build_delete_plan(project, snapshot, decision):
    """Map project-relative deletions onto the owner's write scope; returns (scope, workspace, names)."""
    policy = snapshot.policy
    if policy is None:
        raise EditRefused("Brak permissions.toml: usuwanie plików wymaga zakresu zapisu.")
    root = project.root
    writes = [s for s in policy.write_scopes() if s.mode == "tree" and (root == s.base or root.startswith(s.base + "/"))]
    if not writes:
        raise EditRefused("files.write w permissions.toml nie obejmuje tego projektu (potrzebny wpis typu ~/Projekty/**).")
    scope = max(writes, key=lambda s: len(s.base))
    names, absolute = [], []
    try:
        for path in decision["delete"]:
            entry = project._by_path.get(path)
            if entry is None:
                raise EditRefused(recipient("Claude wskazał plik spoza indeksu projektu."))
            if entry.size > workspace_trash.MAX_FILE_BYTES:
                raise EditRefused("Plik %s jest za duży do bezpiecznego usunięcia (limit 16 MiB)." % _safe(path, 80))
            names.append(task_contract.path((root + "/" + path)[len(scope.base) + 1:]))
            absolute.append(root + "/" + path)
        policy.require_write(tuple(absolute), index=scope.index)
        workspace = policy.write_workspace(scope.index)
    except EditRefused:
        raise
    except ValueError:
        raise EditRefused("Usunięcie nie mieści się w zakresach z permissions.toml albo ścieżka jest zbyt głęboka "
                          "(do 5 poziomów od katalogu zakresu zapisu).") from None
    return scope, workspace, names


def build_parser():
    from .run_controls import add_arguments
    parser = argparse.ArgumentParser(prog="ai-desktop project",
                                     description="Pracuj z jednym katalogiem projektu: analiza do odczytu, zmiany i usuwanie dopiero po Twoim zatwierdzeniu")
    parser.add_argument("path", help="Katalog w zakresie files.read z permissions.toml")
    add_arguments(parser, session=True)
    parser.add_argument("--format", choices=("auto", "plain", "markdown"), default="auto")
    parser.add_argument("--v", "--verbose", "-v", "--details", dest="details", action="store_true",
                        help="Pokaż dokładny ładunek i diagnostykę")
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
        self.edit_transfers = []                # whole files handed over for a proposed change, shown by /rejestr
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
            print("Edycja %d: %d plików, %d B, stan: %s: %s" % (
                number, len(item["files"]), item["bytes"], item["state"],
                ", ".join(_safe(path, 60) for path in item["files"][:8]) or "tylko nowe pliki"))

    # --- optional local helpers --------------------------------------------------------
    def files(self, prefix):
        if not self._refresh():
            return
        try:
            items, following, total = self.project.listing(prefix.strip(), 0, 40)
        except ProjectError:
            print("Podaj katalog względny w projekcie, np. /pliki src.")
            return
        for entry in items:
            print("%s  (%d B)" % (_safe(entry.path), entry.size))
        print("(pokazano %d z %d plików)" % (len(items), total) if following else "(%d plików)" % total)

    def search(self, needle):
        if not self._refresh():
            return
        try:
            result = self.project.search_text(needle)
        except ProjectError as exc:
            print(ROOT_HINTS["PROJECT_POLICY"] if exc.code == "PROJECT_POLICY"
                  else "Podaj niepusty tekst do 256 bajtów bez znaków sterujących.")
            return
        for hit in result.hits:
            print("%s:%d: %s" % (_safe(hit.path), hit.line, _safe(hit.text)))
        if not result.hits:
            print("Brak trafień w przeszukanych plikach." if result.complete else "Brak trafień w tej partii; przeszukanie nie objęło całości.")
        elif not result.complete:
            print("Są kolejne trafienia lub nieprzeszukane pliki; zawęź frazę.")
        skipped = {k: v for k, v in result.skipped.items() if v}
        if skipped:
            print("Pominięte w wyszukiwaniu: %s." % ", ".join("%s: %d" % kv for kv in skipped.items()))

    # --- a question in plain words -----------------------------------------------------
    def question(self, text, span=None):
        if not self.can_send:
            print(network_hint())
            return
        try:
            text = validate_question(text.strip())
        except ValueError:
            print("Pytanie ma być jedną linią zwykłego tekstu do %d bajtów." % MAX_QUESTION_BYTES)
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
            print("Anulowano. Dane nie zostały wysłane.")
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
                print("Zatrzymano po etapie %d z %d; wnioski częściowe powyżej." % (stage, MAX_STAGES))
                print()
                return
            stage += 1
            if not self._refresh():
                return
            loop = Loop(self.project, history=self.history, progress=self.progress, pasted=pastes,
                        visited=loop.visited, stage=stage)
            grant = MaterialGrant(self.binding.material_id, self.binding.sha256, text[:256], self._clock() + GRANT_SECONDS)
            session = TransferSession(self.binding, grant, clock=self._clock)
            self.tasks.append({"session": session, "label": "%s: %s (etap %d)" % (self.name, _safe(text, 50), stage)})
            self._open_section("Etap %d z %d" % (stage, MAX_STAGES))
            try:
                partial = self._run(text, loop, session)
            finally:
                self._close_section()
        if partial is not None:
            print("Osiągnięto limit %d etapów; wnioski częściowe powyżej. Napisz „kontynuuj”, aby zacząć kolejną serię." % MAX_STAGES)
            print()

    def _coverage_line(self, loop, stage):
        """Controller-counted coverage of the whole deep task (never the model's claim)."""
        total = sum(e.size for e in self.project.files)
        files = len(loop.visited)
        size = sum(self.project._by_path[p].size for p in loop.visited if p in self.project._by_path)
        return (recipient("Analiza częściowa, etap %d z %d: Claude przeczytał %d z %d plików (%s z %s w indeksie).") % (
            stage, MAX_STAGES, files, len(self.project.files),
            "%.1f KiB" % (size / 1024), "%.1f MiB" % (total / 1048576)))

    def _refresh(self, announce=False):
        """Rescan before each question so the index matches the disk now; the grant binds the fresh manifest."""
        before = self.project.stats.get("indexed")
        try:
            self.project.scan()
        except (ProjectError, ValueError):
            print("Nie udało się odświeżyć indeksu (zmiana polityki lub katalogu); uruchom polecenie ponownie.")
            return False
        self.binding = Binding(self.project.project_id, self.project.manifest_sha256())
        now = self.project.stats["indexed"]
        if announce or now != before:
            print("Indeks odświeżony: %d plików%s." % (now, "" if now == before else " (było %d)" % before))
        return True

    def _scope(self, text, loop):
        stats = self.project.stats
        pasted = [o for o in loop.observations if o["kind"] == "pasted"]
        skipped = ["%s: %d" % (label, stats[key]) for key, label in SKIP_LABELS if stats[key]]
        print()
        if not self.args.details:
            # Compact by default: what leaves the computer in one line, extras only when present.
            total = self._skipped_total()
            print(recipient("Do Claude: pytanie, nazwy plików i fragmenty z %s (%d plików%s%s)") % (
                self.name, stats["indexed"], ", pominięto %d wpisów" % total if total else "",
                "" if self.project.complete else "; INDEKS NIEKOMPLETNY: %s" % REASONS.get(self.project.reason, "limit")))
            extras = []
            if pasted:
                extras.append("wklejki: " + ", ".join("#%d (%d wierszy, %d B)" % (o["id"], o["lines"], len(o["text"].encode("utf-8")))
                                                       for o in pasted))
            if loop.history:
                extras.append("kontekst: %d poprzednie pytania i odpowiedzi" % len(loop.history))
            if extras:
                print(" · ".join(extras))
            return
        print(rule("Do wysłania"))
        print(field("Pytanie", _safe(text, 200)))
        print(field("Projekt", "%s (%s), %d plików%s" % (
            self.name, self.project.project_id[:8], stats["indexed"],
            "" if self.project.complete else " — INDEKS NIEKOMPLETNY (%s)" % REASONS.get(self.project.reason, "limit"))))
        if skipped:
            print(field("Pominięto", *skipped))
        if pasted:
            print(field("Wklejki", *["#%d: %d wierszy, %d B" % (o["id"], o["lines"], len(o["text"].encode("utf-8"))) for o in pasted]))
        if loop.history:
            print(field("Kontekst", "%d wcześniejsze(-ych) pytań i odpowiedzi z tej sesji (skrócone)" % len(loop.history)))
        print(field("Dane", recipient("pytanie oraz nazwy plików, trafienia i fragmenty, o które poprosi Claude")))
        print(field("Bez", "Internetu, zapisu plików i uruchamiania kodu · limit czasu %d s" % GRANT_SECONDS))
        print(field("Treść", recipient("Claude sam poprosi o listę nazw plików, wyszukiwanie lub odczyt wierszy plików z tego projektu."),
                    "Nazwy plików, trafienia i fragmenty są wysyłane jako dane; reszta projektu nie jest wysyłana.",
                    "Limity: do %d żądań, łącznie do %d KiB danych; każdy krok zobaczysz na bieżąco." % (MAX_STEPS, TOTAL_BYTES // 1024)))
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
                    self._line("=== DOKŁADNY ŁADUNEK (żądanie 1) ===\n" + packet.payload)
                self._line("ŻĄDANIE %d: SHA256 %s, %d B danych" % (number, packet.sha256, packet.data_bytes))
            delivery = self._deliver(session, packet, on_handoff=loop.mark_handed_off)
            if delivery is None:
                return
            self._timing(delivery)
            try:
                decision = parse_decision(delivery.answer)
            except ValueError:
                decision = plain_answer(delivery.answer)          # prose instead of an action: shown, but without sources
                if decision is None:
                    print(recipient("Odpowiedź Claude nie ma wymaganego formatu; zadanie zatrzymano bez ponowienia."))
                    print("Początek odpowiedzi: %s" % _safe(str(delivery.answer).replace("\n", " "), 4000 if self.args.details else 160))
                    return self._stopped(loop)
                loop.format_note = True
            action = decision["action"]
            if action == "answer":
                return self._answer(text, loop, decision)
            if action == "edit":
                return self._edit(text, loop, decision)
            if action == "ask_user":
                self._close_section()
                print(recipient("Claude pyta: %s") % _safe(decision["text"], 500))
                self.remember(text, recipient("[Claude zapytał: %s]") % decision["text"])
                return
            if number == loop.max_steps:
                print(recipient("Claude nie odpowiedział w ostatnim kroku."))
                return self._stopped(loop)
            if loop.repeated(decision):
                print(recipient("Claude powtórzył wcześniejszą prośbę bez postępu; zadanie zatrzymano."))
                return self._stopped(loop)
            try:
                note = loop.execute(decision)
            except ValueError:                  # ProjectError is a ValueError: a changed policy or directory
                print("Polityka plików zmieniła się w trakcie zadania; zatrzymano.")
                return self._stopped(loop)
            self._line(recipient("  Krok %d/%d: Claude poprosił o %s") % (number, loop.max_steps, _safe(note, 200)))
        return self._stopped(loop)

    def _delivered_lines(self, loop):
        """What Claude really received, one entry per file, as display lines."""
        received, size = loop.delivered_summary()
        if not received:
            return [], "żadnych wierszy plików", size
        lines = ["%s: %s" % (_safe(path, 80), lines_label(spans[:8]) + (" …" if len(spans) > 8 else ""))
                 for path, spans in list(received.items())[:10]]
        if len(received) > 10:
            lines.append("… (+%d plików)" % (len(received) - 10))
        return lines, "", size

    def _notes(self, loop, rejected=()):
        """Things a reader must not assume away: shown even without --v, and nothing else."""
        notes = []
        if rejected:
            notes.append(("Odrzucone", [_source(p, a, b) for p, a, b in rejected]
                          + [recipient("(Claude podał źródła, których nie otrzymał)")]))
        if not self.project.complete:
            notes.append(("Indeks", [recipient("niekompletny (%s) — mogą istnieć pliki, których Claude nie mógł zobaczyć") %
                                     REASONS.get(self.project.reason, "limit")]))
        partial = [needle for needle, complete in loop.searches if not complete]
        if partial:
            notes.append(("Niepełne", ["wyszukiwanie (budżet lub kolejne strony): " + ", ".join("„%s”" % _safe(n, 40) for n in partial)]))
        if loop.format_note:
            notes.append(("Format", [recipient("Claude odpowiedział zwykłym tekstem zamiast ustalonej akcji — źródła nie są potwierdzone.")]))
        if loop.changed:
            notes.append(("Zmienione", [", ".join(_safe(p, 80) for p in loop.changed[:8]) + " (w trakcie zadania lub po skanie)"]))
        return notes

    def _received_field(self, loop):
        lines, empty, size = self._delivered_lines(loop)
        if not lines:
            return field(recipient("Claude dostał"), "%s; reszta projektu nie została wysłana" % empty)
        return field(recipient("Claude dostał"), *lines, "(%s; reszta projektu nie została wysłana)" % (
            "%d B odczytanych fragmentów" % size if size else "tylko wyniki wyszukiwania"))

    def _skipped_field(self):
        stats = self.project.stats
        skipped = ["%s: %d" % (label, stats[key]) for key, label in SKIP_LABELS if stats[key]]
        return field("Poza zasięgiem", *skipped) if skipped else None

    def _edit(self, text, loop, decision):
        from .run_controls import require_changes
        try:
            require_changes()
        except AnalysisDenied:
            print("Ten agent pracuje tylko do odczytu. Propozycja zmiany została odrzucona.")
            return self._remember_edit(text, "profil agenta zezwala wyłącznie na odczyt")
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
                    raise EditRefused("Plik %s jest niedostępny lub zmienił się od analizy. Zadaj pytanie ponownie." % _safe(path, 80)) from None
                version = task_workspace.sha(raw)
                if loop.fully_read(path) and version != loop.delivered_versions.get(path):
                    raise EditRefused("Plik %s zmienił się od analizy. Zadaj pytanie ponownie, aby pracować na aktualnej wersji." % _safe(path, 80))
                versions[name] = version
            blocker = self._edit_blocker(snapshot, plan)
            if blocker:
                raise EditRefused(blocker)
        except (EditRefused, ProjectError, assistant_flow.AccessDenied, AnalysisDenied) as exc:
            print(rule("Zmiana plików"))
            print(field("Odmowa", str(exc) if isinstance(exc, EditRefused) else "Polityka plików uniemożliwia zmianę."))
            print(SEPARATOR)
            print()
            return self._remember_edit(text, "propozycja zmiany odrzucona przez aplikację")
        # Content Claude has not seen yet would leave the computer in the edit request: that needs its own yes.
        # A new file, or files Claude already read whole in this task, add nothing new: no second question.
        unsent = [path for path in decision["files"] if not loop.fully_read(path)]
        summary = " · ".join(part for part in (
            "nowe: " + ", ".join(_safe(p, 80) for p in decision["create"]) if created else "",
            "do zmiany: " + ", ".join(_safe(p, 80) for p in decision["files"]) if existing else "",
            "sprawdzenie: " + decision["check"]) if part)
        if unsent:
            print(rule("Zmiana plików"))
            print(field("Do zmiany", *[_safe(p, 80) for p in decision["files"]]))
            if created:
                print(field("Nowe", *[_safe(p, 80) for p in decision["create"]]))
            print(field("Sprawdzenie", decision["check"]))
            print(field("Dalej", recipient("treść plików, których Claude jeszcze nie widział (%s), pójdzie do niego w osobnym zadaniu;") %
                        ", ".join(_safe(p, 60) for p in unsent), "zapis dopiero po obejrzeniu diffu i osobnej zgodzie."))
            print(SEPARATOR)
            try:
                approved = confirm("project-edit", reader=self.reader)
            except EOFError:
                approved = False
            except KeyboardInterrupt:
                self._interrupted()
                approved = False
            if not approved:
                print("Anulowano. Treść plików nie została wysłana, nic nie zapisano.")
                print()
                return self._remember_edit(text, "użytkownik odrzucił propozycję zmiany")
        else:
            print("Zmiana plików — %s. Przygotowuję diff…" % summary)
        code = self._run_edit(request_text, snapshot, plan, versions)
        print()
        return self._remember_edit(text, "zmiana zapisana po zgodzie użytkownika" if code == 0
                                   else "zmiana NIE została zapisana (odrzucona, niezaliczony test albo błąd)")

    def _expand_pastes(self, text):
        """The edit request carries the pasted text itself (the marker alone would tell the model nothing)."""
        expanded = text
        for number, body in getattr(self, "_pastes", ()):
            expanded = expanded.replace(paste_token(number, body), body)
        if len(expanded.encode("utf-8")) > MAX_QUESTION_BYTES:
            raise EditRefused("Polecenie zmiany pliku razem z wklejką przekracza %d B; skróć wklejkę albo podziel zmianę." % MAX_QUESTION_BYTES)
        return expanded

    def _edit_blocker(self, snapshot, plan):
        """Why a change cannot start right now (an unfinished earlier task, a full journal ...), in words."""
        try:
            workspace = snapshot.policy.write_workspace(int(plan["write"][1:]))
        except ValueError:
            return ("Zakres zapisu z permissions.toml nie wskazuje bezpiecznego katalogu roboczego "
                    "(np. cały HOME wymaga polityki version = 2; zalecany wpis typu ~/Projekty/**).")
        try:
            task_workspace._can_apply(workspace)
        except task_workspace.JournalLimit:
            return "Osiągnięto limit zapisanych zadań w katalogu zakresu zapisu. Sprawdź: ai-desktop task status --workspace KATALOG."
        except ValueError as exc:
            if str(exc) == "recovery required":
                return ("Wcześniejsze zadanie zmiany plików jest niedokończone lub w konflikcie. "
                        "Sprawdź: ai-desktop task status --workspace KATALOG (potem task rollback).")
            return "Dziennik zadań w katalogu zakresu zapisu jest nieczytelny lub zawiera nieoczekiwane wpisy (ai-desktop task status)."
        except (ExecutionDenied, OSError):
            return "Katalog zakresu zapisu jest niedostępny albo ma niebezpieczne uprawnienia."
        return None

    def _delete(self, text, decision):
        from .run_controls import require_changes
        try:
            require_changes()
        except AnalysisDenied:
            print("Ten agent pracuje tylko do odczytu. Propozycja usunięcia została odrzucona.")
            return self._remember_edit(text, "profil agenta zezwala wyłącznie na odczyt")
        """Deleting is moving to a recoverable trash: it needs the owner's own yes (nothing is sent to Claude)."""
        print(rule("Usunięcie plików"))
        try:
            snapshot = assistant_flow.capture_access()
            scope, workspace, names = build_delete_plan(self.project, snapshot, decision)
            prepared = workspace_trash.prepare(workspace, names)
        except (EditRefused, assistant_flow.AccessDenied, AnalysisDenied, ExecutionDenied, ValueError, OSError) as exc:
            print(field("Odmowa", str(exc) if isinstance(exc, EditRefused) else "Polityka plików uniemożliwia usunięcie."))
            print(SEPARATOR)
            print()
            return self._remember_edit(text, "propozycja usunięcia odrzucona przez aplikację")
        print(field("Do usunięcia", *["%s (%d B)" % (_safe(p, 80), e["size"])
                                     for p, e in zip(decision["delete"], prepared.data()["entries"])]))
        print(field("Kosz", recipient("pliki zostaną przeniesione do prywatnego katalogu zadania i można je przywrócić; nic nie idzie do Claude")))
        print(SEPARATOR)
        try:
            approved = confirm("project-delete", reader=self.reader)
        except EOFError:
            approved = False
        except KeyboardInterrupt:
            self._interrupted()
            approved = False
        if not approved:
            print("Anulowano. Nic nie usunięto.")
            print()
            return self._remember_edit(text, "użytkownik odrzucił usunięcie plików")
        try:
            snapshot.policy.recheck()
            plan = workspace_trash.execute(prepared, prepared.authorize())
        except workspace_trash.TrashRecoveryRequired as exc:
            print("Usuwanie przerwane. Wynik wymaga sprawdzenia; dane i dziennik zachowano.")
            self._trash_recovery(workspace, exc.plan)
            return self._remember_edit(text, "usuwanie przerwane; wymagane sprawdzenie kosza zadania")
        except KeyboardInterrupt:
            print("\nPrzerwano usuwanie. Sprawdź stan zadania przed kolejną operacją.")
            self._trash_recovery(workspace, prepared.data()["plan"])
            return self._remember_edit(text, "usuwanie przerwane; stan wymaga sprawdzenia")
        except (ExecutionDenied, ValueError, OSError):
            print("Nie usunięto: plik zmienił się, nazwa jest niezwykła albo polityka się zmieniła. Nic nie zostało usunięte.")
            print()
            return self._remember_edit(text, "usunięcie plików nie powiodło się, nic nie usunięto")
        print("Usunięto %d plików (w koszu zadania)." % len(names))
        print("Przywrócenie: " + shlex.join(["ai-desktop", "task", "restore", "--workspace", workspace, "--plan", plan]))
        print()
        return self._remember_edit(text, "usunięto %d plików po zgodzie użytkownika (można przywrócić)" % len(names))

    @staticmethod
    def _trash_recovery(workspace, plan):
        print("Stan: " + shlex.join(["ai-desktop", "task", "status", "--workspace", workspace]))
        print("Przywrócenie: " + shlex.join(["ai-desktop", "task", "restore", "--workspace", workspace, "--plan", plan]))
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
                        "state": "nie wiadomo, czy dotarło"}      # recorded before the hand-off, like the read steps
            self.edit_transfers.append(transfer)
            response = ask_cli.request(text, args, self.reader, task=prepared.source, write_grant=prepared.write_grant,
                                       check=permit.check, question_consent=permit)
            transfer["state"] = "odpowiedź otrzymana"
            permit.check()
            permit.close()
            return ask_edit.finish(prepared, response, args, self.reader).code
        except ask_edit.CreateCollision:
            print("Plik do utworzenia już istnieje; niczego nie zapisano.")
        except (ask_edit.EditDenied, ExecutionDenied) as exc:
            print("Nie przygotowano zmiany (zgoda, polityka lub stan plików się zmieniły). Niczego nie zapisano.")
        except AnalysisDenied as exc:
            print(failure_hint(getattr(exc, "reason", None), HINTS.get(getattr(exc, "reason", None), "Nie udało się uzyskać odpowiedzi. Sprawdź ai-desktop status."),
                               **({"diagnostic": exc.diagnostic} if exc.diagnostic is not None else {})))
            print("Przekazanych danych nie można wycofać; zmiana nie zostanie ponowiona automatycznie.")
        except assistant_flow.Declined:
            print("Anulowano.")
        except KeyboardInterrupt:
            print("\nPrzerwano.")
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
        return recipient("Poza zasięgiem Claude: %d wpisów pominiętych przy skanie (szczegóły: --v).") % total if total else None

    def _answer(self, text, loop, decision):
        self._close_section()
        confirmed, rejected = loop.confirm_sources(decision["sources"])
        print(rule("Odpowiedź") + "\n")
        print(render_answer(decision["text"], output_format=self.args.format))
        notes = self._notes(loop, rejected)
        if self.args.details:                       # --v: the full account of sources and scope, every time
            print("\n" + rule("Źródła"))
            if confirmed:
                print(field("Potwierdzone", *[_source(p, a, b) for p, a, b in confirmed]))
            else:
                print(field("Potwierdzone", recipient("brak — Claude nie wskazał źródeł, które aplikacja mogłaby potwierdzić")))
            print()
            print(rule("Zakres"))
            print(self._received_field(loop))
            skipped = self._skipped_field()
            if skipped:
                print(skipped)
            for label, values in notes:
                print(field(label, *values))
            print(SEPARATOR)
            print("ASK: PASS")
        elif notes:                                  # default: silence unless there is something to be careful about
            print("\n" + rule("Uwaga"))
            for label, values in notes:
                print(field(label, *values))
            if self._skipped_line():
                print(field("Poza zasięgiem", "%d wpisów pominiętych przy skanie (szczegóły: --v)" % self._skipped_total()))
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
        print(rule("Zatrzymano bez odpowiedzi"))
        print(field("Co dalej", "Zawęź pytanie albo zadaj je ponownie (nowa zgoda).",
                    "Napisz np. „kontynuuj”, aby następne pytanie znało to, co już sprawdzono."))
        print(self._received_field(loop))
        if self.args.details:
            skipped = self._skipped_field()
            if skipped:
                print(skipped)
        for label, values in self._notes(loop):
            print(field(label, *values))
        if not self.args.details and self._skipped_line():
            print(field("Poza zasięgiem", "%d wpisów pominiętych przy skanie (szczegóły: --v)" % self._skipped_total()))
        print(SEPARATOR)
        print()
        received, size = loop.delivered_summary()
        self._remember_stop(received, "fragmenty %d plików (%d B)" % (len(received), size))

    # --- loop ----------------------------------------------------------------------------
    def run(self):
        stats = self.project.stats
        print()
        print("Projekt %s (%s): %d plików w indeksie%s. Przeskanowano lokalnie." % (
            self.name, self.project.project_id[:8], stats["indexed"],
            "" if self.project.complete else " (indeks niekompletny: %s)" % REASONS.get(self.project.reason, "limit")))
        print(recipient("Do Claude trafi tylko to, co zatwierdzisz."))
        print()
        print("Opcje:")
        print("/koniec    kończy")
        print("/pomoc     pokazuje opcjonalne polecenia")
        print()
        print("Co chcesz z nim zrobić?")
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
                print("Nie potwierdzono zatrzymania sesji. Uruchom ai-desktop stop.")

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
        elif command == "/pliki":
            self.files(rest)
        elif command == "/szukaj":
            self.search(rest)
        elif command == "/rejestr":
            self.ledger()
        elif command == "/odswiez":
            self._refresh(announce=True)
        else:
            print("Nieznane polecenie. /pomoc pokazuje dostępne.")
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
            print("Brak permissions.toml: katalog wymaga zakresu files.read. Zobacz config/permissions.example.toml.")
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
        print("Polecenie wymaga zwykłego użytkownika i terminala.")
        return 2
    session = ProjectSession(project, os.path.basename(project.root), policy, args, reader, clock=clock, sleep=sleep,
                             window=window, sender=sender)
    try:
        session.attach_conversation("project", project.root, project.manifest_sha256())
        return session.run()
    except (OSError, ValueError):
        print("Nie można użyć zapisu rozmowy. Projekt lub ustawienia mogły się zmienić; sprawdź ai-desktop sessions list.")
        return 2
    finally:
        session.close_conversation()


if __name__ == "__main__":
    raise SystemExit(main())
