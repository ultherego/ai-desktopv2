"""`ai-desktop material PLIK`: one local material, explored locally, asked about on request.

The file is read once under the owner's policy (permissions.toml), kept in memory
and never sent as a whole. `/znajdz` and `/pokaz` are local only (no session, no
sudo, nothing leaves the computer). `/pytaj A-B ...` builds one exact packet of
the chosen lines, shows what will be sent, needs its own yes, and only then
hands it to the official client over the existing A+ transport. The answer is
printed with controller-written sources (what was really handed over), never
with references the model claims.

The A+ session is reused by quick consecutive questions: the root-owned helper
holds a session for 240 s at most and one request may take 150 s, so a session
is reused only while it is younger than SESSION_REUSE_SECONDS and is closed
when that window ends. Longer persistence would need a privileged change.
"""
import argparse
import collections
from contextlib import ExitStack
import json
import os
import re
import select
import sys
import time

from .run_controls import recipient, failure_hint, network_hint
from .execution import UNSET
from . import materials, permissions
from .analysis_cli import HINTS
from .analysis_contract import (AnalysisConsent, AnalysisDenied, MAX_QUESTION_BYTES, terminal_text,
                                validate_question)
from .analysis_service import send_once
from .analysis_session import installation_ready, managed_session
from .analysis_terminal import render_answer
from .file_context import _read_hint
from .file_context_contract import contains_private_key
from .material_grant import GrantDenied, MaterialGrant, TransferSession
from .material_loop import Loop, history_turn, visible_lines
from .material_whole import STAGE_REQUESTS, WholeError, WholePlan
from .material_whole import MAX_PARTS as _MAX_PARTS
from .material_step import DATA_BYTES as MAX_DATA_BYTES, MAX_HISTORY, MAX_STEPS, TOTAL_BYTES as MAX_TOTAL_BYTES, parse_decision, plain_answer
from .terminal_progress import WaitingIndicator
from .question_input import FOLLOWUP_PROMPT, PromptSession, QuestionInputError, read_question
from .terminal_consent import confirm

SESSION_REUSE_SECONDS = 80      # helper cap (SESSION_SECONDS) minus the longest request wait (300 s), exactly
MAX_HANDOFFS_PER_MINUTE = 3     # the socket allows 4 activations per 60 s
GRANT_SECONDS = 600
TASK_SECONDS = 1800                 # validity of the ONE consent for a whole-file task (stage grants never outlive it)
PREVIEW_LINES = 40
PREVIEW_COLUMNS = 160
SEPARATOR = "─" * 48
SECTION_WIDTH = 48


FIELD_WIDTH = 16


def field(label, value, *more):
    """Aligned "  Label        value" line; further values continue under the first one."""
    lines = ["  %-*s%s" % (FIELD_WIDTH, label, value)]
    lines += ["  %-*s%s" % (FIELD_WIDTH, "", extra) for extra in more]
    return "\n".join(lines)


def rule(title):
    """"── Tytuł ────…" with a fixed width: the same look for the progress section and the answer."""
    head = "── %s " % title
    return head + "─" * max(2, SECTION_WIDTH - len(head))

IMPORT_HINTS = {
    "IMPORT_EMPTY": "Plik jest pusty.",
    "IMPORT_LIMIT": "",          # worded at run time from the owner's file_size, see limit_hint()
    "IMPORT_ENCODING": "Plik nie jest poprawnym tekstem UTF-8. Nie zmieniam kodowania po cichu.",
    "IMPORT_CONTROL": "Plik zawiera znaki sterujące lub formatujące (np. sekwencje kolorów ANSI, BOM). Usuń je; nie przekształcam treści po cichu.",
}
GRANT_HINTS = {
    "GRANT_EXPIRED": "Termin zgody minął. Zadaj pytanie ponownie i zatwierdź nowy podgląd.",
    "BUDGET_EXHAUSTED": "Budżet tego zadania został wyczerpany.",
    "PACKET_LIMIT": "Pytanie lub zakres przekracza limit jednego żądania (96 KiB danych, pytanie do 2048 B). Zawęź zakres.",
    "SCOPE_DENIED": "Zakres jest niedozwolony lub nie mieści się w materiale.",
    "SOURCE_CHANGED": "Materiał zmienił się; zadanie zatrzymano.",
    "STATE_INVALID": "Nieprawidłowy stan żądania; zadanie zatrzymano.",
    "GRANT_INVALID": "Nieprawidłowy grant; zadanie zatrzymano.",
}
HELP = """Zadaj pytanie o plik zwykłym zdaniem; program sam dobierze potrzebne fragmenty.
Opcjonalnie (polecenia lokalne, nic nie wysyłają):
  /pytaj A-B PYTANIE  zawęź pytanie do wierszy A-B (zwykły tekst bez / też jest pytaniem)
  /znajdz TEKST   szukaj dosłownie w pliku (numery wierszy)
  /pokaz A-B      pokaż wiersze A-B (do 40 wierszy)
  /rejestr        co aplikacja przekazała do Claude w tej sesji
  /koniec         zakończ (też Ctrl+D)"""


Delivery = collections.namedtuple("Delivery", "answer reused opening waiting meta", defaults=(None,))



def step_parts():
    return _MAX_PARTS


class SessionWindow:
    """Lazily opened A+ session, reused only while young enough to finish a request."""

    def __init__(self, *, quiet, clock=time.monotonic, opener=managed_session):
        self._quiet, self._clock, self._opener = quiet, clock, opener
        self._stack, self._opened = None, 0.0

    @property
    def is_open(self):
        return self._stack is not None

    def seconds_left(self):
        return max(0.0, SESSION_REUSE_SECONDS - (self._clock() - self._opened)) if self._stack else 0.0

    def ensure(self):
        if self._stack is not None and self.seconds_left() <= 0:
            self.close()
        if self._stack is None:
            stack = ExitStack()
            try:
                stack.enter_context(self._opener(quiet=self._quiet))
            except BaseException:
                stack.close()
                raise
            self._stack, self._opened = stack, self._clock()

    def close(self):
        stack, self._stack = self._stack, None
        if stack is not None:
            stack.close()


def _countdown(seconds):
    """Waiting notice refreshed every second in place on a terminal (one line per second otherwise)."""
    text = "  Limit usługi (4 aktywacje na minutę): czekam %d s…" % seconds
    if sys.stdout.isatty():
        print("\r\x1b[K" + text, end="", flush=True)
    else:
        print(text, flush=True)


def _countdown_end():
    if sys.stdout.isatty():
        print("\r\x1b[K  Limit usługi: koniec oczekiwania.", flush=True)


class WholeProgress:
    """One owned three-row terminal frame; one static frame in plain output. No input or threads."""
    def __init__(self, total, stages=1, *, output_format="auto", stream=None):
        self.total, self.done, self.stages, self.stage = total, 0, stages, 1
        self.part, self.first, self.last = 0, 0, 0
        self.complete, self.synthesis = False, False
        self.stream = sys.stdout if stream is None else stream
        self.drawn, self._plain_started = False, False
        try:
            self.live = output_format == "auto" and self.stream.isatty() and os.isatty(self.stream.fileno())
        except (OSError, ValueError, AttributeError):
            self.live = False

    def set_stage(self, number):
        self.stage = number

    def set_part(self, index, first, last):
        self.part, self.first, self.last = index, first, last
        self.complete, self.synthesis = False, False
        self.update()

    def update(self, done=None, *, waiting=None, synthesis=False):
        if done is not None:
            self.done = done
            self.complete = True
        if synthesis:
            self.synthesis = True
        state = "przeanalizowana." if self.complete else "analizuję…"
        if waiting is not None:
            state = "czekam %d s…" % waiting
        body = ("  Podsumowanie: " + (state if waiting is not None else "przygotowuję…") if self.synthesis else
                "  Część %d/%d (wiersze %d–%d): %s" % (self.part, self.total, self.first, self.last, state))
        rows = [rule("Analiza całego pliku Etap %d z %d" % (self.stage, self.stages)), body, SEPARATOR]
        if not self.live:
            if not self._plain_started:
                print("\n".join(rows), file=self.stream, flush=True)
                self._plain_started = True
            return
        try:
            columns = os.get_terminal_size(self.stream.fileno()).columns
        except (OSError, ValueError, AttributeError):
            columns = 80
        self.clear()
        width = max(0, min(160, columns or 80) - 1)
        self.stream.write("\n".join("\r\x1b[2K" + row[:width] for row in rows))
        self.stream.flush()
        self.drawn = True

    def clear(self):
        if self.drawn:
            self.stream.write("\r\x1b[2A\x1b[2K\n\r\x1b[2K\n\r\x1b[2K\x1b[2A\r")
            self.stream.flush()
            self.drawn = False

    def update_wait(self, seconds):
        self.update(waiting=seconds)

    def end_wait(self):
        self.update()


class Pacer:
    """Client-side guard so a quick series never trips the socket's activation limit."""

    def __init__(self, *, clock=time.monotonic, sleep=time.sleep):
        self._clock, self._sleep, self._times = clock, sleep, collections.deque()

    def wait_turn(self, announce, finish=None):
        """Block until a hand-off fits the limit; announce(seconds_left) is called every second."""
        waited = False
        while True:
            now = self._clock()
            while self._times and now - self._times[0] >= 60:
                self._times.popleft()
            if len(self._times) < MAX_HANDOFFS_PER_MINUTE:
                if waited and finish is not None:
                    finish()
                return
            delay = 60 - (now - self._times[0]) + 0.5
            announce(int(delay) + 1)
            waited = True
            self._sleep(min(1.0, delay))

    def record(self):
        self._times.append(self._clock())


def _positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("musi być liczbą całkowitą >= 1")
    return number


def build_parser():
    from .run_controls import add_arguments
    parser = argparse.ArgumentParser(prog="ai-desktop material",
                                     description="Pracuj z jednym lokalnym plikiem tekstowym (do 5 MiB)")
    parser.add_argument("path", help="Plik UTF-8 w zakresie files.read z permissions.toml")
    add_arguments(parser, session=True)
    parser.add_argument("--format", choices=("auto", "plain", "markdown"), default="auto")
    parser.add_argument("--v", "--verbose", "-v", "--details", dest="details", action="store_true",
                        help="Pokaż dokładny ładunek i diagnostykę")
    parser.add_argument("--max-requests", dest="max_requests", type=_positive, default=None, metavar="N",
                        help="Twardy limit żądań przekazanych do wybranego asystenta w tej sesji (liczy też przekazania z nieznanym wynikiem)")
    parser.add_argument("--only-whole", dest="only_whole", action="store_true",
                        help="Odbiór: jeśli pierwsza decyzja modelu nie jest analizą całego pliku, zatrzymaj przed kolejnym przekazaniem")
    parser.add_argument("--first-question", dest="first_question", default=None, metavar="TEKST",
                        help="Odbiór: zadaj dokładnie to jedno pytanie (zgody nadal wpisujesz Ty), potem zakończ sesję")
    parser.add_argument("--answer-file", dest="answer_file", default=None, metavar="PLIK",
                        help="Odbiór: zapisz końcową odpowiedź do nowego pliku 0600 (oraz PLIK.notes z notatkami części)")
    parser.add_argument("--meta-log", dest="meta_log", default=None, metavar="PLIK",
                        help="Dopisz po jednej linii JSON na przekazanie: faza, model, okno, liczniki (nowy plik 0600, bez treści)")
    return parser


def _line_label(first, last):
    return "wiersz %d" % first if first == last else "wiersze %d–%d" % (first, last)


def _safe(text, limit=PREVIEW_COLUMNS):
    text = terminal_text(text.rstrip("\n"))
    return text if len(text) <= limit else text[:limit] + "…"


BINARY_KINDS = ((b"%PDF-", "PDF"), (b"PK\x03\x04", "archiwum ZIP (np. DOCX, ODT, XLSX)"), (b"\x89PNG", "obraz PNG"),
                (b"\xff\xd8\xff", "obraz JPEG"), (b"GIF8", "obraz GIF"), (b"\x7fELF", "program (ELF)"), (b"\x1f\x8b", "archiwum gzip"),
                (b"7z\xbc\xaf", "archiwum 7z"), (b"Rar!", "archiwum RAR"), (b"SQLite format 3", "baza SQLite"))


def binary_hint(raw):
    """Name a well-known binary format by its first bytes (nothing is parsed) and say what to do instead."""
    for magic, kind in BINARY_KINDS:
        if raw.startswith(magic):
            extra = " Możesz wyeksportować tekst (np. `pdftotext plik.pdf plik.txt`) i wskazać powstały plik .txt." if kind == "PDF" else ""
            return ("To wygląda na plik: %s, a nie tekst. `material` czyta tylko zwykły tekst UTF-8 i nie otwiera takich formatów.%s "
                    "Nic nie zostało wysłane." % (kind, extra))
    return None


def limit_hint(limit):
    human = "%.1f MiB" % (limit / 1048576) if limit >= 1048576 else "%d KiB" % (limit // 1024)
    human = human.replace(".0 MiB", " MiB")
    return ("Plik przekracza %d bajtów (%s, limit file_size). Wybierz mniejszy plik albo zwiększ file_size w [files] "
            "w permissions.toml (maksymalnie 16 MiB); nic nie zostało wysłane." % (limit, human))


def load_material(path, policy):
    """Read one policy-allowed file and import it, or raise ValueError(user hint)."""
    absolute = os.path.abspath(os.path.expanduser(path))
    limit = policy.file_size()
    try:
        selected, raw = permissions.read_bounded(policy, absolute, limit)
    except ValueError as exc:
        # files-size would quote the 64 KiB limit of ordinary file reads, not the owner's file_size.
        raise ValueError(limit_hint(limit) if exc.args == ("files-size",) else _read_hint(exc)) from None
    try:
        material = materials.import_bytes(raw, "file", limit=limit)
    except materials.MaterialError as exc:
        if exc.code == "IMPORT_LIMIT":
            raise ValueError(limit_hint(limit)) from None
        raise ValueError(binary_hint(raw) or IMPORT_HINTS[exc.code]) from None
    if contains_private_key(raw.decode("utf-8")):
        raise ValueError("Treść pliku zawiera rozpoznany nagłówek klucza prywatnego; nie przyjmuję takiego materiału.")
    return selected, material


class Session:
    _handoffs = 0                      # requests handed to the client in this session (the --max-requests cap counts them)
    _whole_progress = None
    def __init__(self, material, name, policy, args, reader, *, clock=time.monotonic, sleep=time.sleep,
                 window=None, sender=send_once):
        self.material, self.name, self.policy, self.args, self.reader = material, name, policy, args, reader
        self._clock, self._sender = clock, sender
        self.window = window or SessionWindow(quiet=not args.details, clock=clock)
        self.pacer = Pacer(clock=clock, sleep=sleep)
        self.tasks = []
        self.history = []
        self.prompt_session = PromptSession()
        self._current = None
        from .run_controls import provider_allowed
        self.can_send = provider_allowed(policy)

    conversation = None

    def remember(self, question, answer):
        """Persist only the explicitly selected conversation, after a result."""
        self.history.append(history_turn(question, answer))
        self.history = self.history[-MAX_HISTORY:]
        if self.conversation is not None:
            self.conversation.append(question, answer)

    def attach_conversation(self, kind, path, binding):
        from .conversation_sessions import load_session
        from .execution import current
        self.conversation = load_session(self.args, kind, path, binding, current())
        if self.conversation is not None:
            self.history = self.conversation.history
            print("Rozmowa: %s · zapis lokalny; do 4 ostatnich pytań w kontekście." % self.conversation.session_id)

    def close_conversation(self):
        if self.conversation is not None:
            self.conversation.close()

    # --- optional local helpers (no session, no sudo, nothing leaves the computer) ---
    def find(self, needle):
        try:
            page = materials.search_text(self.material, needle)
        except materials.MaterialError:
            print("Podaj niepusty tekst do 256 bajtów bez znaków sterujących.")
            return
        if not page.hits:
            print("Brak trafień w całym materiale." if page.complete else "Brak trafień w przeszukanym zakresie.")
            return
        for hit in page.hits:
            lo = self.material.line_starts[hit.line - 1]
            end = self.material.line_starts[hit.line] if hit.line < len(self.material.line_starts) else self.material.size
            line = self.material.raw[lo:min(end, lo + 4 * PREVIEW_COLUMNS)].decode("utf-8", "ignore")
            print("wiersz %d: %s" % (hit.line, _safe(line)))
        if not page.complete:
            print("Są kolejne trafienia (pokazano %d). Zawęź frazę." % len(page.hits))

    def _lines(self, spec):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", spec)
        if not match:
            raise materials.MaterialError("RANGE_INVALID")
        first = int(match.group(1))
        last = int(match.group(2) or first)
        return first, last, materials.line_range(self.material, first, last)

    def show(self, spec):
        try:
            first, last, _ = self._lines(spec)
        except materials.MaterialError:
            print("Podaj zakres wierszy w materiale, np. /pokaz 120-160 (materiał ma %d wierszy)." % self._line_count())
            return
        shown = min(last, first + PREVIEW_LINES - 1)
        for number in range(first, shown + 1):
            lo = self.material.line_starts[number - 1]
            hi = self.material.line_starts[number] if number < len(self.material.line_starts) else self.material.size
            print("%d: %s" % (number, _safe(self.material.raw[lo:hi].decode("utf-8", "ignore"))))
        if shown < last:
            print("(pokazano %d z %d wierszy)" % (shown - first + 1, last - first + 1))

    def _line_count(self):
        return visible_lines(self.material)

    def ledger(self):
        entries = [(task, e) for task in self.tasks for e in task["session"].ledger()]
        if not entries:
            print(recipient("Nic nie zostało przekazane do Claude w tej sesji."))
        for number, (task, entry) in enumerate(entries, 1):
            print("%d. %s, %d B, stan: %s, sha256 %s…" % (
                number, task["label"], entry["bytes"], entry["state"], entry["sha256"][:12]))
        print("Rejestr dotyczy przekazania do klienta A+, nie ruchu sieciowego ani odbioru przez dostawcę.")

    # --- a question in plain words ---------------------------------------------------
    def question(self, text, span=None):
        if not self.can_send:
            print(network_hint())
            return
        try:
            text = validate_question(text.strip())
        except ValueError:
            print("Pytanie ma być jedną linią zwykłego tekstu do %d bajtów." % MAX_QUESTION_BYTES)
            return
        if span is not None:
            loop = Loop(self.material, max_steps=1, history=self.history)
            if not loop.preload_range(*span):
                print("Wskazany zakres jest nieprawidłowy albo zbyt duży na jedno żądanie (limit ok. %d KiB)." % (MAX_DATA_BYTES // 1024 - 16))
                return
            whole = "range"
        else:
            loop = Loop(self.material, history=self.history)
            whole = "all" if loop.preload_whole() else None
        self._current = text
        now = self._clock()
        grant = MaterialGrant(self.material.material_id, self.material.sha256, text[:256], now + GRANT_SECONDS)
        session = TransferSession(self.material, grant, clock=self._clock)
        label = "%s: %s" % (self.name, _safe(text, 60))
        self.tasks.append({"session": session, "label": label})
        self._scope(text, whole, loop)
        try:
            approved = confirm("material-transfer" if whole else "material-task", reader=self.reader)
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
            self._run(text, loop, session, whole)
        finally:
            self._close_section()

    def _scope(self, text, whole, loop):
        what = {"all": "cały plik", "range": "wskazane wiersze"}.get(whole, recipient("fragmenty, o które poprosi Claude"))
        print()
        if not self.args.details:
            extra = " · kontekst: %d poprzednie pytania i odpowiedzi" % len(loop.history) if loop.history else ""
            print(recipient("Do Claude: pytanie i %s z %s (%d wierszy, %d B)%s") % (what, self.name, loop.lines, self.material.size, extra))
            return
        print(rule("Do wysłania"))
        print(field("Pytanie", _safe(text, 200)))
        print(field("Plik", "%s (%s), %d B, %d wierszy" % (self.name, self.material.material_id[:8], self.material.size, loop.lines)))
        if whole == "all":
            print(field("Treść", "cały plik (%d B) w jednym żądaniu" % self.material.size))
        elif whole == "range":
            print(field("Treść", "tylko wskazane wiersze (%d B) w jednym żądaniu" % loop.read_bytes))
        else:
            print(field("Treść", recipient("Claude sam poprosi o wyszukanie frazy lub odczyt wierszy z TEGO pliku —"),
                        "do %d żądań, łącznie do %d KiB danych; każdy krok zobaczysz na bieżąco;" % (MAX_STEPS, MAX_TOTAL_BYTES // 1024),
                        "reszta pliku nie jest wysyłana"))
        if loop.history:
            print(field("Kontekst", "%d wcześniejsze(-ych) pytań i odpowiedzi z tej sesji (skrócone)" % len(loop.history)))
        print(field("Bez", "Internetu, zapisu plików i uruchamiania kodu · limit czasu %d s" % GRANT_SECONDS))
        print(SEPARATOR)

    def _run(self, text, loop, session, whole):
        for number in range(1, loop.max_steps + 1):
            material_input = loop.request_input(number, session.bytes_left())
            try:
                packet = session.prepare_step(text, material_input, loop.data_bytes(), loop.request_ranges())
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
                decision = parse_decision(delivery.answer, loop.lines)
            except ValueError:
                decision = plain_answer(delivery.answer)          # prose instead of an action: shown, but without sources
                if decision is None:
                    print(recipient("Odpowiedź Claude nie ma wymaganego formatu; zadanie zatrzymano bez ponowienia."))
                    print("Początek odpowiedzi: %s" % _safe(str(delivery.answer).replace("\n", " "), 4000 if self.args.details else 160))
                    return self._stopped(loop)
                loop.format_note = True
            action = decision["action"]
            if getattr(self.args, "only_whole", False) and action != "whole":
                self._close_section()
                self._log_result("not-whole")
                print(recipient("Tryb odbioru (--only-whole): Claude nie wybrał analizy całego pliku; zatrzymano przed kolejnym przekazaniem."))
                print("Nic więcej nie zostało wysłane. Ten przebieg nie nadaje się do porównania.")
                return
            if action == "answer":
                return self._answer(text, loop, decision)
            if action == "whole":
                return self._whole(text)
            if action == "ask_user":
                self._close_section()
                print(recipient("Claude pyta: %s") % _safe(decision["text"], 500))
                self.remember(text, recipient("[Claude zapytał: %s]") % decision["text"])
                return
            if number == loop.max_steps:
                print(recipient("Claude nie odpowiedział w ostatnim kroku."))
                return self._stopped(loop)
            if decision["action"] not in ("search", "read"):
                # Valid in the whole-file flow only: file text must not be able to crash an ordinary question.
                print(recipient("Claude zwrócił krok, który nie pasuje do tego pytania; zadanie zatrzymano."))
                return self._stopped(loop)
            if loop.repeated(decision):
                print(recipient("Claude powtórzył wcześniejszą prośbę bez postępu; zadanie zatrzymano."))
                return self._stopped(loop)
            note = loop.execute(decision)
            self._line(recipient("  Krok %d/%d: Claude poprosił o %s") % (number, loop.max_steps, _safe(note, 200)))
        return self._stopped(loop)

    # --- the entire large file: parts, notes, synthesis, controller-owned coverage ------------
    _format_note = False

    def _whole(self, text):
        self._close_section()
        self._format_note = False
        try:
            plan = WholePlan(self.material, history=self.history, part_bytes=self.policy.part_size())
        except WholeError:
            need = -(-self.material.size // (step_parts() * 1024)) + 8
            print("\nPrzy part_size = %d KiB ten plik wymagałby zbyt wielu części (najwyżej %d, bo synteza dostaje po jednej notatce na część)."
                  % (self.policy.part_size() // 1024, step_parts()))
            if need * 1024 <= 512 * 1024:
                print("Zwiększ part_size w [files] permissions.toml do ok. %d KiB (najwyżej 512KiB) albo zadaj pytanie o fragment." % need)
            else:
                print("Zadaj pytanie o fragment pliku (/pytaj zakres pytanie) albo szukaj konkretnej frazy.")
            return
        stages = plan.stages()
        total = sum(len(stage) for stage in stages)
        if not self._grants_possible(text, plan, stages):
            return
        requests, budget = plan.task_budget()
        if self.args.details:
            print(recipient("\nTo pytanie wymaga przeanalizowania CAŁEGO pliku. Do Claude zostanie wysłany cały plik (%d B),") % self.material.size)
            print(recipient("po kolei w %d częściach (po ok. %d KiB), a potem zbiorcze wnioski z notatek Claude:") % (
                len(plan.parts), max(p.size for p in plan.parts) // 1024))
            print("  %d żądań w %d etapach (każdy do %d żądań i %d MiB danych); jedna zgoda obejmuje całe zadanie, bez pytań o etapy." % (
                total, len(stages), STAGE_REQUESTS, MAX_TOTAL_BYTES // 1048576))
            print("  Twardy budżet całego zadania: %d żądań i do %d KiB danych (plik %d KiB + notatki i historia dla syntezy)"
                  " · ważna %d min · Internet: wyłączony · zapis plików: brak." % (
                      requests, budget // 1024, self.material.size // 1024, TASK_SECONDS // 60))
        else:
            print(recipient("\nTo pytanie wymaga przeanalizowania CAŁEGO pliku. Do Claude zostanie wysłany cały plik (%d B).") % self.material.size)
        try:
            approved = confirm("material-whole", reader=self.reader)
        except EOFError:
            approved = False
        except KeyboardInterrupt:
            self._interrupted()
            approved = False
        if not approved:
            print("Zadanie nie zostało zatwierdzone. Dane z tego planu nie zostały wysłane.")
            return self._whole_result(text, plan, None)
        deadline = self._clock() + TASK_SECONDS
        reduced = None
        self._whole_progress = None if self.args.details else WholeProgress(len(plan.parts), len(stages), output_format=self.args.format)
        try:
            for number, stage in enumerate(stages, 1):
                if self._whole_progress:
                    self._whole_progress.set_stage(number)
                if self._clock() >= deadline:
                    if self._whole_progress:
                        self._whole_progress.clear()
                    print("\nZgoda na to zadanie wygasła po %d min; nie odnawiam jej sam. Dane kolejnych etapów nie zostały wysłane." % (
                        TASK_SECONDS // 60))
                    break
                if self.args.details:
                    print("\nEtap %d z %d: %s. Dotąd przeanalizowano %d z %d części." % (
                        number, len(stages), self._stage_label(plan, stage), len(plan.notes), len(plan.parts)))
                    self._open_section("Analiza całego pliku")
                try:
                    result = self._whole_stage(text, plan, stage, deadline)
                finally:
                    self._close_section()
                if result == "failed":
                    break
                if result is not None:
                    reduced = result
        finally:
            if self._whole_progress:
                self._whole_progress.clear()
            self._whole_progress = None
        return self._whole_result(text, plan, reduced)

    def _grants_possible(self, text, plan, stages):
        """Every stage grant must be constructible NOW, before the consent and the first transfer (not on entering stage N)."""
        now = self._clock()
        try:
            for stage in stages:
                TransferSession(self.material, MaterialGrant(self.material.material_id, self.material.sha256, text[:256],
                                now + GRANT_SECONDS, max_requests=len(stage), max_bytes=plan.stage_budget(stage)),
                                clock=self._clock)
        except GrantDenied as exc:
            print(GRANT_HINTS[exc.code])
            print("Plan tego zadania nie przechodzi walidacji; nic nie zostało wysłane.")
            return False
        return True

    @staticmethod
    def _stage_label(plan, stage):
        maps = [i for kind, i in stage if kind == "map"]
        parts = ("części %d–%d" % (maps[0] + 1, maps[-1] + 1) if len(maps) > 1 else "część %d" % (maps[0] + 1)) if maps else "bez nowych części"
        extra = " oraz wnioski końcowe" if stage[-1][0] == "reduce" else ""
        return "%d żądań (%s%s, ok. %d KiB danych)" % (len(stage), parts, extra, plan.stage_bytes(stage) // 1024)

    def _whole_stage(self, text, plan, stage, deadline):
        """One stage grant inside the task consent (never longer than it): returns the reduce decision,
        'failed' on a delivery failure, else None."""
        grant = MaterialGrant(self.material.material_id, self.material.sha256, text[:256],
                              min(self._clock() + GRANT_SECONDS, deadline),
                              max_requests=len(stage), max_bytes=plan.stage_budget(stage))
        session = TransferSession(self.material, grant, clock=self._clock)
        self.tasks.append({"session": session, "label": "%s: %s (cały plik)" % (self.name, _safe(text, 50))})
        for number, (kind, index) in enumerate(stage, 1):
            if kind == "map":
                part = plan.parts[index]
                material_input = plan.map_input(index, number, session.bytes_left())
                data = sum(len(o["text"].encode("utf-8")) for o in material_input["observations"])
                ranges = part.ranges()
                if self._whole_progress:
                    self._whole_progress.set_part(index + 1, part.first, part.last)
            else:
                if not plan.notes:
                    if self._whole_progress:
                        self._whole_progress.clear()
                    print("Brak notatek do podsumowania; pomijam wnioski końcowe.")
                    return None
                material_input = plan.reduce_input(number, session.bytes_left())
                data = sum(len(o["text"].encode("utf-8")) for o in material_input["observations"]) + sum(
                    len(t["question"].encode("utf-8")) + len(t["answer"].encode("utf-8")) for t in plan.history)
                ranges = []
            if self._whole_progress:
                self._whole_progress.update(synthesis=kind == "reduce")
            try:
                packet = session.prepare_step(text, material_input, data, ranges)
            except GrantDenied as exc:
                if self._whole_progress:
                    self._whole_progress.clear()
                print(GRANT_HINTS[exc.code])
                return "failed"
            if self.args.details:
                self._line("ŻĄDANIE %d/%d: SHA256 %s, %d B danych" % (number, len(stage), packet.sha256, packet.data_bytes))
            delivery = self._deliver(session, packet)
            if delivery is None:
                return "failed"
            self._timing(delivery)
            try:
                decision = parse_decision(delivery.answer, plan.lines)
            except ValueError:
                decision = None
                if kind == "reduce":                          # keep the synthesis the way the loop does: text shown, sources dropped
                    decision = plain_answer(delivery.answer)
                    if decision is not None:
                        self._format_note = True
                if decision is None and self.args.details:
                    self._line("Początek odpowiedzi w złym formacie: %s" % _safe(str(delivery.answer).replace("\n", " "), 4000))
            if kind == "map":
                if decision is not None and decision["action"] == "notes":
                    plan.record_note(index, decision["text"])
                    if self.args.details:
                        self._line("  Część %d/%d (wiersze %d–%d): przeanalizowana." % (index + 1, len(plan.parts), part.first, part.last))
                    elif self._whole_progress:
                        self._whole_progress.update(len(plan.notes))
                else:
                    if self._whole_progress:
                        self._whole_progress.clear()
                    self._line("  Część %d/%d (wiersze %d–%d): odpowiedź w złym formacie, część NIE jest policzona jako przeanalizowana."
                          % (index + 1, len(plan.parts), part.first, part.last))
            elif decision is not None and decision["action"] in ("answer", "ask_user"):
                return decision
            else:
                if self._whole_progress:
                    self._whole_progress.clear()
                print("Wnioski końcowe: odpowiedź w złym formacie; pokażę notatki z części bez syntezy.")
        return None

    def _whole_result(self, text, plan, decision):
        parts, total = len(plan.covered_parts()), len(plan.parts)
        covered, size = plan.covered_bytes(), self.material.size
        if decision is not None and decision["action"] == "ask_user":
            print(recipient("\nClaude pyta: %s") % _safe(decision["text"], 500))
            self.remember(text, recipient("[Claude zapytał: %s]") % decision["text"])
        elif decision is not None:
            confirmed, rejected = plan.confirm_sources(decision["sources"])
            print("\n" + rule("Odpowiedź") + "\n")
            print(render_answer(decision["text"], output_format=self.args.format))
            if self.args.details:
                print("\n" + SEPARATOR + "\n")
            if confirmed and self.args.details:
                print("Źródła potwierdzone przez aplikację: %s." % ", ".join(
                    "wiersze %d–%d" % s if s[0] != s[1] else "wiersz %d" % s[0] for s in confirmed))
            if rejected:
                print(recipient("Uwaga: Claude podał źródła, których nie otrzymał (pominięto): %s.") % ", ".join(
                    "%d–%d" % s for s in rejected))
            suffix = "" if plan.complete() else " [analiza częściowa: %d z %d części]" % (parts, total)
            self.remember(text, decision["text"] + suffix)
        else:
            print("\nBez wniosków końcowych. Notatki z przeanalizowanych części:")
            for index in plan.covered_parts():
                part = plan.parts[index]
                print("  wiersze %d–%d: %s" % (part.first, part.last, _safe(plan.notes[index], 300)))
        if self.args.details or not plan.complete() or decision is None:
            print("Pokrycie (liczy aplikacja): %d z %d części, %d z %d B (%d%%)." % (
                parts, total, covered, size, 100 * covered // size))
        if plan.complete() and self.args.details:
            print(recipient("Pokrycie znaczy, że wszystkie części zostały przekazane do analizy — nie, że Claude znalazł wszystkie problemy."))
        elif not plan.complete():
            spans = plan.spans(plan.missing_parts())
            print("ANALIZA CZĘŚCIOWA. Nieprzeanalizowane wiersze: %s. Wnioski dotyczą tylko przeanalizowanych części." % ", ".join(
                "%d–%d" % tuple(s) for s in spans[:12]) + (" …" if len(spans) > 12 else ""))
        if self._format_note and decision is not None:
            print(recipient("Uwaga: Claude odpowiedział w niestandardowym formacie — pokazano sam tekst, źródła nie są potwierdzone."))
        if decision is not None and decision["action"] == "answer":
            synthesis = "salvaged" if self._format_note else "answer"
        else:
            synthesis = "ask_user" if decision is not None else "none"
        outcome = ("complete" if plan.complete() and synthesis in ("answer", "salvaged")
                   else "ask_user" if plan.complete() and synthesis == "ask_user" else "partial")
        self._log_result(outcome, covered=parts, total=total, synthesis=synthesis)
        notes = "\n".join("wiersze %d–%d: %s" % (plan.parts[i].first, plan.parts[i].last, plan.notes[i]) for i in plan.covered_parts())
        self._save_answer(decision["text"] if decision is not None and decision["action"] == "answer" else None, notes or None)
        if self.args.details and decision is not None:
            print("ASK: PASS")

    def _stopped(self, loop):
        self._close_section()
        spans, size = loop.delivered_summary()
        print(rule("Zatrzymano bez odpowiedzi"))
        print(field("Co dalej", "Zawęź pytanie albo zadaj je ponownie (nowa zgoda).",
                    "Napisz np. „kontynuuj”, aby następne pytanie znało to, co już sprawdzono."))
        print(field(recipient("Claude dostał"), self._delivered_text(loop) + "; reszta pliku nie została wysłana"))
        print(SEPARATOR)
        print()
        self._remember_stop(spans, "wiersze " + self._delivered_text(loop).replace("wiersze ", "", 1))

    def _remember_stop(self, delivered, description):
        """A stopped task becomes one short controller-written history turn (only when something was delivered).

        The note never quotes file names or model text, so it cannot carry injected instructions."""
        if self._current and delivered:
            self.remember(self._current, recipient("[Zadanie przerwane bez odpowiedzi. Claude otrzymał: %s]") % description)

    def _delivered_text(self, loop):
        spans, size = loop.delivered_summary()
        if not spans:
            return "żadnych wierszy pliku"
        text = ", ".join("%d–%d" % (a, b) if a != b else str(a) for a, b in spans[:12])
        if len(spans) > 12:
            text += ", …"
        return "wiersze %s (%d B z %d B)" % (text, size, self.material.size)

    def _answer(self, text, loop, decision):
        self._close_section()
        confirmed, rejected = loop.confirm_sources(decision["sources"])
        print(rule("Odpowiedź") + "\n")
        print(render_answer(decision["text"], output_format=self.args.format))
        notes = []
        if rejected:
            notes.append(("Odrzucone", ["wiersze " + ", ".join("%d–%d" % s for s in rejected),
                                        recipient("(Claude podał źródła, których nie otrzymał)")]))
        if loop.format_note:
            notes.append(("Format", [recipient("Claude odpowiedział zwykłym tekstem zamiast ustalonej akcji — źródła nie są potwierdzone.")]))
        if self.args.details:                      # --v: the full account of sources and scope, every time
            print("\n" + rule("Źródła"))
            print(field("Potwierdzone", *(["wiersze %d–%d" % s if s[0] != s[1] else "wiersz %d" % s[0] for s in confirmed]
                                          or [recipient("brak — Claude nie wskazał źródeł, które aplikacja mogłaby potwierdzić")])))
            print()
            print(rule("Zakres"))
            print(field(recipient("Claude dostał"), self._delivered_text(loop), "(reszta pliku nie została wysłana)"))
            if loop.searches:
                print(field("Wyszukiwania", ", ".join("„%s”" % _safe(n, 40) for n in loop.searches)))
            for label, values in notes:
                print(field(label, *values))
            print(SEPARATOR)
            print("ASK: PASS")
        elif notes:                                # default: silence unless there is something to be careful about
            print("\n" + rule("Uwaga"))
            for label, values in notes:
                print(field(label, *values))
            print(SEPARATOR)
        print()
        self._save_answer(decision["text"])
        self.remember(text, decision["text"])

    def _open_section(self, title="Przygotowanie"):
        """Frame the progress of a task so it is not mixed with the preview or the answer. The frame appears only
        when there is something to show (the first step or wait), so a direct answer has no empty frame."""
        self._section_title, self._section_shown = title, False

    def _ensure_section(self):
        if getattr(self, "_section_title", None) and not getattr(self, "_section_shown", False):
            print()
            print(rule(self._section_title))
            self._section_shown = True

    def _line(self, text):
        if self._whole_progress:
            self._whole_progress.clear()
        self._ensure_section()
        print(text)

    def _close_section(self):
        if getattr(self, "_section_shown", False):
            print(SEPARATOR)
            print()
        self._section_title, self._section_shown = None, False

    def _gate_kwargs(self, packet):
        return dict(question=packet.question, context=list(packet.context) or None, material=packet.material)

    def _save_answer(self, text, notes=None):
        """Acceptance only: the final answer (and the parts' notes) to NEW 0600 files; never overwrites."""
        path = getattr(self.args, "answer_file", None)
        if not path:
            return
        for target, content in ((path, text), (path + ".notes", notes)):
            if content is None:
                continue
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
            except OSError:
                self._line("Uwaga: nie udało się zapisać --answer-file.")

    def _log_meta(self, packet, meta):
        """One closed JSON line per completed hand-off (phase, part, model, window, counters) - never text or payload."""
        path = getattr(self.args, "meta_log", None)
        if not path:
            return
        task = (packet.material or {}).get("task", {}) if isinstance(getattr(packet, "material", None), dict) else {}
        phase = task.get("phase") or "loop"
        part = task.get("part", {}).get("index") if isinstance(task.get("part"), dict) else None
        models = (meta or {}).get("models") or [None]
        record = {"n": self._handoffs, "phase": phase, "part": part, "models": [
            None if item is None else {k: item[k] for k in ("model", "window", "input", "output")} for item in models]}
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            self._line("Uwaga: nie udało się dopisać do --meta-log.")

    def _log_result(self, outcome, **fields):
        """The controller's own verdict on the task, as the last line of --meta-log (closed fields, no text)."""
        path = getattr(self.args, "meta_log", None)
        if not path:
            return
        record = {"result": dict(fields, outcome=outcome)}
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            self._line("Uwaga: nie udało się dopisać wyniku do --meta-log.")

    @staticmethod
    def model_line(meta):
        """One closed line from the client's reported metadata (never from the model's text)."""
        if not meta:
            return "MODEL: niedostępny (klient nie podał metadanych)"
        return "MODEL: " + "; ".join("%s · okno %s · tokeny we/wy %s/%s" % (
            item["model"], "?" if item["window"] is None else item["window"],
            "?" if item["input"] is None else item["input"], "?" if item["output"] is None else item["output"])
            for item in meta["models"])

    def _timing(self, delivery):
        if self.args.details:
            self._line(self.model_line(delivery.meta))
            self._line("CZAS: %s %.1f s; odpowiedź %.1f s (sudo i czas pisania pytań nie wliczone)" % (
                "sesja ponownie użyta" if delivery.reused else "nowa sesja", delivery.opening, delivery.waiting))

    def _deliver(self, session, packet, *, on_handoff=None):
        """Pace, gate, open/reuse the A+ session, hand off ONCE, send, record. None on any failure."""
        handed_off = False
        selected = UNSET
        cap = getattr(self.args, "max_requests", None)
        if cap is not None and self._handoffs >= cap:
            if self._whole_progress:
                self._whole_progress.clear()
            session.cancel(packet)
            print("Limit %d żądań w tej sesji jest wyczerpany; dane tego żądania nie zostały wysłane." % cap)
            return None
        try:
            # Pace first: waiting must not eat the consent TTL or the session reuse window.
            compact = getattr(self, "_whole_progress", None)
            self.pacer.wait_turn(compact.update_wait if compact else
                                 lambda seconds: (self._ensure_section(), _countdown(seconds)),
                                 compact.end_wait if compact else
                                 lambda: (self._ensure_section(), _countdown_end()))
            if compact:
                compact.clear()
            kwargs = self._gate_kwargs(packet)
            gate = AnalysisConsent(None, **kwargs)
            selected = json.loads(gate.payload).get("execution")
            if gate.payload != packet.payload:
                session.cancel(packet)
                print("Ładunek zmienił się po podglądzie; zadanie zatrzymano. Dane nie zostały wysłane.")
                return None
            gate.check(None, sha256=gate.sha256, nonce=gate.nonce, **kwargs)
            installation_ready()
            reused = self.window.is_open and self.window.seconds_left() > 0
            opening_started = self._clock()
            self.window.ensure()
            opening = self._clock() - opening_started
            approval = gate.authorize(None, sha256=gate.sha256, nonce=gate.nonce, **kwargs)
            sending_started = self._clock()
            if compact:
                compact.update()
            with WaitingIndicator(output_format="plain" if compact else getattr(self.args, "format", "auto"),
                                  config=selected) as progress:
                progress.phase("task")
                session.hand_off(packet)
                handed_off = True
                self._handoffs += 1                # unknown outcomes still spend the request
                self.pacer.record()
                if on_handoff is not None:
                    wire = json.loads(packet.payload)
                    on_handoff(wire[wire["mode"]])
                answer = self._sender(approval)
            session.record_answer(packet)
            meta = getattr(answer, "meta", None) or getattr(self._sender, "last_meta", None)
            self._log_meta(packet, meta)
            return Delivery(str(answer), reused, opening, self._clock() - sending_started, meta)
        except GrantDenied as exc:
            self._finish_failure(session, packet, handed_off)
            print(GRANT_HINTS[exc.code])
        except AnalysisDenied as exc:
            self._finish_failure(session, packet, handed_off)
            if self.args.details:
                print(str(exc))
            print(failure_hint(exc.reason, HINTS.get(exc.reason, "Nie udało się uzyskać odpowiedzi. Sprawdź ai-desktop status."),
                               config=selected,
                               **({"diagnostic": exc.diagnostic} if exc.diagnostic is not None else {})))
            if handed_off:
                print("Przekazanych danych nie można wycofać; pytanie nie zostanie ponowione automatycznie.")
        except KeyboardInterrupt:
            self._finish_failure(session, packet, handed_off)
            print("\nPrzerwano.")
            self._interrupted()
            if handed_off:
                print("Przekazanych danych nie można wycofać; wynik nieznany, bez automatycznego ponowienia.")
        return None

    def _finish_failure(self, session, packet, handed_off):
        compact = getattr(self, "_whole_progress", None)
        if compact:
            compact.clear()
        try:
            if handed_off:
                session.record_unknown(packet)
            else:
                session.cancel(packet)
        except GrantDenied:
            pass                      # already answered/cancelled; the ledger keeps its true state
        try:
            self.window.close()
        except AnalysisDenied:
            print("Nie potwierdzono zatrzymania sesji. Uruchom ai-desktop stop.")

    # --- loop ----------------------------------------------------------------------
    def run(self):
        print()
        print("Plik %s (%s): %d wierszy, %d B. Wczytano lokalnie." % (
            self.name, self.material.material_id[:8], self._line_count(), self.material.size))
        print(recipient("Do Claude trafi tylko to, co zatwierdzisz."))
        print()
        print("Opcje:")
        print("/koniec    kończy")
        print("/pomoc     pokazuje opcjonalne polecenia")
        print()
        print("Co chcesz z nim zrobić?")
        if not self.can_send:
            print(network_hint(short=True))
        try:
            if self.args.first_question is not None:        # acceptance: exactly this one question, consents stay with the user
                self.question(self.args.first_question)
                return 0
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

    _quit = False

    def _interrupted(self):
        """Ctrl+C outside the line editor (consent question, waiting for Claude). A second one within 3 s ends the session;
        the first only cancels what was in progress and arms the second."""
        if self.prompt_session.armed:
            self._quit = True
            return True
        self.prompt_session.armed = True
        print("  (Ctrl+C jeszcze raz w ciągu 3 s, aby zakończyć)")
        return False

    accepts_pastes = False            # `project` turns large and multi-line pastes into marked attachments

    def _read_command(self):
        if self.reader is input and sys.stdin.isatty() and sys.stdout.isatty():
            try:
                return read_question(max_bytes=MAX_QUESTION_BYTES, prompt=FOLLOWUP_PROMPT, session=self.prompt_session,
                                     attach=self.accepts_pastes,
                                     idle=(lambda: self.window.seconds_left() if self.window.is_open else None, self.window.close))
            except QuestionInputError:
                print("Tego wejścia nie przyjmuję: wklejka jest za duża (do 32 KiB, razem 32 KiB, pytanie do %d B) albo zawiera "
                      "niedozwolone znaki." % MAX_QUESTION_BYTES)
                return ""
        return self.reader("> ")

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
        elif command == "/znajdz":
            self.find(rest)
        elif command == "/pokaz":
            self.show(rest)
        elif command == "/rejestr":
            self.ledger()
        elif command == "/pytaj":
            if re.fullmatch(r"\d+(?:-\d+)?", rest):
                print("Dopisz pytanie po zakresie, np. /pytaj 120-160 co robi ten fragment?")
                return True
            match = re.fullmatch(r"(\d+)(?:-(\d+))?\s+(.+)", rest)
            if match:
                first = int(match.group(1))
                self.question(match.group(3), span=(first, int(match.group(2) or first)))
            else:
                self.question(rest)
        else:
            print("Nieznane polecenie. /pomoc pokazuje dostępne.")
        return True


from .run_controls import configured


@configured
def main(argv=None, *, reader=input, clock=time.monotonic, sleep=time.sleep, window=None, sender=send_once):
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.answer_file is not None and (os.path.lexists(args.answer_file) or os.path.lexists(args.answer_file + ".notes")):
        print("--answer-file: plik już istnieje; nie nadpisuję.")
        return 2
    if args.meta_log is not None:
        try:                                    # a NEW file (never overwrite), created before anything is read or sent
            os.close(os.open(args.meta_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
        except OSError:
            print("--meta-log: nie można utworzyć nowego pliku (istnieje już albo brak katalogu).")
            return 2
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
            print("Brak permissions.toml: plik wymaga zakresu files.read. Zobacz config/permissions.example.toml.")
            return 2
        selected, material = load_material(args.path, policy)
    except AnalysisDenied:
        print("Polecenie wymaga zwykłego użytkownika i terminala.")
        return 2
    except ValueError as exc:
        print(str(exc))
        return 2
    session = Session(material, os.path.basename(selected), policy, args, reader, clock=clock, sleep=sleep,
                      window=window, sender=sender)
    try:
        session.attach_conversation("material", selected, material.sha256)
        return session.run()
    except (OSError, ValueError):
        print("Nie można użyć zapisu rozmowy. Plik lub ustawienia mogły się zmienić; sprawdź ai-desktop sessions list.")
        return 2
    finally:
        session.close_conversation()


if __name__ == "__main__":
    raise SystemExit(main())
