"""`ai-desktop material`: real policy and files; fake A+ session, transport and clock."""
import contextlib
import io
import json
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import material_cli as mcli
from ai_desktop.material_whole import WholePlan
from ai_desktop.materials import import_bytes
from ai_desktop import permissions as p
from ai_desktop.analysis_contract import AnalysisDenied
from ai_desktop import analysis_contract as ac
from ai_desktop.terminal_consent import PROMPTS

LINES = ["line %03d filler text" % i for i in range(1, 201)]
LINES[49] = "line 050 SECRET-MARKER-IN-LINE-50"
LINES[119] = "line 120 ERROR failure happened here"
LINES[121] = "line 122 ERROR second failure"
TEXT = "\n".join(LINES) + "\n"


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class FakeSession:
    def __init__(self, clock, fail=None):
        self.opens = self.closes = 0
        self.clock, self.fail, self.active = clock, fail, 0

    def opener(self, *, quiet):
        outer = self

        @contextlib.contextmanager
        def manager():
            if outer.fail:
                raise outer.fail
            outer.opens += 1
            try:
                yield
            finally:
                outer.closes += 1
        return manager()


class Base(unittest.TestCase):
    network = "claude = true\n"

    def setUp(self):
        self.assertNotEqual(os.geteuid(), 0)
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ai-material-cli-", dir=real_home)))
        # Profile storage has its own filesystem tests. This baseline material
        # harness must not depend on the runner's real agent selection.
        self.enterContext(patch("ai_desktop.agent_profiles.resolve", return_value={
            "provider": "claude", "model": "default", "effort": "default", "instructions": "",
            "profile": "default", "access": "changes"}))
        self.enterContext(patch.object(p, "_identity", return_value=(os.getuid(), str(self.home))))
        self.config = self.home / ".config/ai-desktop/permissions.toml"
        self.config.parent.mkdir(parents=True, mode=0o700)
        self.write_policy()
        (self.home / "data").mkdir(mode=0o700)
        self.file = self.home / "data/log.txt"
        self.file.write_text(TEXT)
        self.clock = Clock()
        self.fake = FakeSession(self.clock)
        self.sent = []
        self.ready = 0
        self.model = None

    def write_policy(self, read='["~/data/*"]'):
        self.config.write_text("version = 1\n[files]\nread = %s\ndeny = []\n[network]\n%s" % (read, self.network))
        self.config.chmod(0o600)

    def sender(self, approval):
        self.sent.append(approval)
        if getattr(self, "send_error", None):
            raise self.send_error
        if self.model is not None:
            return self.model(json.loads(approval.payload))
        return getattr(self, "answer", '{"action":"answer","text":"Odpowiedź syntetyczna.","sources":[]}')

    def run_cli(self, commands, answers=(), argv=None, tty=True, window=True):
        commands, answers, self.prompts = iter(commands), iter(answers), []

        def reader(prompt):
            self.prompts.append(prompt)
            source = commands if prompt == "> " else answers
            value = next(source)
            if isinstance(value, BaseException):
                raise value
            return value

        def ready():
            self.ready += 1
        out = io.StringIO()
        module = mcli
        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(patch.object(module.sys.stdin, "isatty", return_value=tty))
        stack.enter_context(patch.object(module.sys.stdout, "isatty", return_value=tty))
        stack.enter_context(patch.object(module, "installation_ready", side_effect=ready))
        with stack:
            kwargs = dict(reader=reader, clock=self.clock, sleep=self.clock.sleep, sender=self.sender)
            if window:
                kwargs["window"] = mcli.SessionWindow(quiet=True, clock=self.clock, opener=self.fake.opener)
                self.window = kwargs["window"]
            code = mcli.main(argv or [str(self.file)], **kwargs)
        self.output = out.getvalue()
        return code


class FileAcceptanceTests(Base):
    def refused(self, hint_part):
        code = self.run_cli(["/koniec"])
        self.assertEqual(code, 2, self.output)
        self.assertIn(hint_part, self.output)
        self.assertEqual((self.sent, self.fake.opens, self.ready), ([], 0, 0))

    def test_limit_encoding_controls_and_empty_files_are_refused_with_closed_hints(self):
        cases = [(b"a" * (5 * 1024 * 1024 + 1), "Plik przekracza 5242880 bajtów (5 MiB, limit file_size)"), (b"\xff\xfe\xfa", "UTF-8"),
                 (b"\x1b[31mred\x1b[0m\n", "sterujące"), (b"", "pusty"), ("﻿bom".encode(), "sterujące")]
        for raw, hint in cases:
            with self.subTest(hint=hint, size=len(raw)):
                self.file.write_bytes(raw)
                self.refused(hint)
                self.assertNotIn("65536", self.output)           # never the 64 KiB limit of ordinary reads

    def test_well_known_binary_formats_are_named_instead_of_a_vague_encoding_error(self):
        for raw, kind in ((b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj", "PDF"), (b"PK\x03\x04\x14\x00\x06\x00", "archiwum ZIP"),
                          (b"\x89PNG\r\n\x1a\n\x00\x00", "obraz PNG"), (b"\xff\xd8\xff\xe0\x00\x10JFIF", "obraz JPEG"),
                          (b"\x7fELF\x02\x01\x01", "program (ELF)")):
            with self.subTest(kind=kind):
                self.file.write_bytes(raw)
                self.assertEqual(self.run_cli(["/koniec"]), 2)
                self.assertIn("To wygląda na plik: %s" % kind, self.output)
                self.assertIn("Nic nie zostało wysłane.", self.output)
        self.file.write_bytes(b"%PDF-1.4\n\xff\xfe")
        self.run_cli(["/koniec"])
        self.assertIn("pdftotext plik.pdf plik.txt", self.output)
        self.file.write_bytes(b"\xff\xfe\xfa")                           # unknown binary: the generic hint stays
        self.run_cli(["/koniec"])
        self.assertIn("nie jest poprawnym tekstem UTF-8", self.output)

    def test_exact_limit_is_accepted(self):
        self.file.write_bytes(b"x" * (5 * 1024 * 1024))
        self.assertEqual(self.run_cli(["/koniec"]), 0)
        self.assertIn("5242880 B", self.output)

    def test_policy_symlink_hardlink_directory_and_private_key_are_refused(self):
        outside = self.home / "outside.txt"
        outside.write_text("not allowed\n")
        (self.home / "data/sub").mkdir()
        for path, hint in ((outside, "nie jest dozwolony"), (self.home / "data/none.txt", "nie istnieje"),
                           (self.home / "data/sub", "zwykłym plikiem")):
            code = self.run_cli(["/koniec"], argv=[str(path)])
            self.assertEqual(code, 2)
            self.assertIn(hint, self.output)
        (self.home / "data/link").symlink_to(self.file)
        os.link(self.file, self.home / "data/hard")
        for name in ("link", "hard"):
            self.assertEqual(self.run_cli(["/koniec"], argv=[str(self.home / "data" / name)]), 2)
        (self.home / "data/hard").unlink()                     # a single link again
        self.file.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n")
        self.refused("klucza prywatnego")

    def test_missing_policy_or_invalid_policy_stops_before_reading(self):
        self.config.unlink()
        self.assertEqual(self.run_cli(["/koniec"]), 2)
        self.assertIn("Brak permissions.toml", self.output)
        self.config.write_text("INVALID")
        self.config.chmod(0o600)
        self.assertEqual(self.run_cli(["/koniec"]), 2)
        self.assertEqual((self.sent, self.fake.opens), ([], 0))

    def test_root_and_missing_terminal_are_refused(self):
        self.assertEqual(self.run_cli(["/koniec"], tty=False), 2)
        with patch.object(mcli.os, "geteuid", return_value=0):
            self.assertEqual(self.run_cli(["/koniec"]), 2)
        self.assertEqual(self.sent, [])


class FileSizeSettingTests(Base):
    """The owner's `file_size` in [files] decides the largest file `material` imports (default 5 MiB, ceiling 16 MiB)."""

    def policy_with(self, literal):
        self.config.write_text('version = 1\n[files]\nread = ["~/data/*"]\ndeny = []\nfile_size = %s\n[network]\nclaude = true\n' % literal)
        self.config.chmod(0o600)

    def test_a_smaller_limit_refuses_with_a_hint_that_names_the_setting(self):
        self.policy_with('"100KiB"')
        self.file.write_bytes(b"x" * (100 * 1024 + 1))
        self.assertEqual(self.run_cli(["/koniec"]), 2)
        self.assertIn("Plik przekracza 102400 bajtów (100 KiB, limit file_size)", self.output)
        self.assertIn("zwiększ file_size w [files] w permissions.toml (maksymalnie 16 MiB)", self.output)
        self.file.write_bytes(b"x" * (100 * 1024))
        self.assertEqual(self.run_cli(["/koniec"]), 0)

    def test_a_larger_limit_accepts_a_file_over_the_default_and_the_ceiling_still_holds(self):
        self.file.write_bytes(b"x" * (6 * 1024 * 1024))
        self.assertEqual(self.run_cli(["/koniec"]), 2)                       # default 5 MiB
        self.policy_with('"16MiB"')
        self.assertEqual(self.run_cli(["/koniec"]), 0)
        self.assertIn("%d B" % (6 * 1024 * 1024), self.output)
        self.file.write_bytes(b"x" * (16 * 1024 * 1024 + 1))
        self.assertEqual(self.run_cli(["/koniec"]), 2)
        self.assertIn("16 MiB", self.output)

    def test_the_request_schema_accepts_sizes_up_to_the_hard_ceiling_only(self):
        from ai_desktop import material_step as ms, materials
        base = {"material": {"id": "0" * 16, "version": "a" * 64, "size": 16 * 1024 * 1024, "lines": 10},
                "task": {"step": 1, "max_steps": 8, "bytes_left": 1000}, "observations": [], "history": []}
        ms.validate_input(json.loads(json.dumps(base)))
        base["material"]["size"] = materials.MAX_PROJECT_FILE_BYTES + 1
        with self.assertRaises(ValueError):
            ms.validate_input(base)


class LocalCommandTests(Base):
    def test_find_and_show_are_local_and_never_open_a_session(self):
        code = self.run_cli(["/znajdz ERROR", "/pokaz 119-121", "/znajdz NO-SUCH-TEXT", "/rejestr", "/koniec"])
        self.assertEqual(code, 0)
        self.assertIn("wiersz 120: line 120 ERROR failure happened here", self.output)
        self.assertIn("wiersz 122: line 122 ERROR second failure", self.output)
        self.assertIn("120: line 120 ERROR", self.output)
        self.assertIn("Brak trafień w całym materiale.", self.output)
        self.assertIn("Nic nie zostało przekazane", self.output)
        self.assertEqual((self.sent, self.fake.opens, self.ready), ([], 0, 0))
        self.assertEqual(self.prompts.count(PROMPTS["material-transfer"]), 0)

    def test_terminal_controls_in_the_material_are_shown_escaped(self):
        self.file.write_bytes(b"first\r\nsecond\tword\r\n")
        self.run_cli(["/pokaz 1-2", "/znajdz second", "/koniec"])
        self.assertNotIn("\r", self.output)
        self.assertIn("\\u000d", self.output)

    def test_invalid_local_commands_explain_themselves(self):
        self.run_cli(["", "/nic", "/pokaz", "/pokaz 0-3", "/pokaz 9999", "/znajdz", "/znajdz " + "x" * 300, "/pomoc", "/koniec"])
        for text in ("Nieznane polecenie", "Podaj zakres wierszy", "Podaj niepusty tekst"):
            self.assertIn(text, self.output)

    def test_end_of_input_and_ctrl_c_leave_cleanly(self):
        self.assertEqual(self.run_cli([EOFError()]), 0)
        self.assertEqual(self.run_cli([KeyboardInterrupt()]), 0)
        self.assertEqual(self.fake.opens, 0)


def decision(**fields):
    return json.dumps(fields, ensure_ascii=False)


def script(*items):
    """A fake model that returns the next decision (dict -> JSON, str -> raw) and records payloads."""
    queue, seen = list(items), []

    def model(payload):
        seen.append(payload)
        item = queue.pop(0)
        return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
    model.seen = seen
    return model


BIG_LINES = ["row %05d filler text to make this log long enough" % i for i in range(1, 6001)]
BIG_LINES[119] = "row 00120 ERROR connection refused by the backend"
BIG_LINES[2399] = "row 02400 ERROR second failure with code E-4711"
BIG_LINES[49] = "row 00050 SECRET-MARKER-NOT-ASKED-FOR"
BIG_TEXT = "\n".join(BIG_LINES) + "\n"


class NaturalTests(Base):
    def big(self):
        self.file.write_text(BIG_TEXT)
        self.assertGreater(len(BIG_TEXT), 72 * 1024)

    def payloads(self):
        return [json.loads(a.payload) for a in self.sent]

    # --- small file: the whole file after consent ---------------------------------------
    def test_a_plain_sentence_about_a_small_file_sends_the_whole_file_after_consent(self):
        self.model = script({"action": "answer", "text": "Plik wygląda poprawnie.", "sources": [{"first": 1, "last": 3}]})
        code = self.run_cli(["przeanalizuj ten plik i wskaż możliwe błędy", "/rejestr", "/koniec"], ["tak"],
                            argv=[str(self.file), "--v"])
        self.assertEqual(code, 0, self.output)
        for text in ("Wczytano lokalnie.", "Co chcesz z nim zrobić?", "cały plik", "Plik wygląda poprawnie.", "stan: answered"):
            self.assertIn(text, self.output)
        self.assertRegex(self.output, r"Bez\s+Internetu, zapisu plików i uruchamiania kodu")           # the full card is --v
        self.assertRegex(self.output, r"Potwierdzone\s+wiersze 1–3")
        self.assertRegex(self.output, r"Claude dostał\s+wiersze 1–200")
        self.assertEqual(self.prompts.count(PROMPTS["material-transfer"]), 1)
        (payload,) = self.payloads()
        self.assertEqual(payload["mode"], "material")
        self.assertEqual(payload["question"], "przeanalizuj ten plik i wskaż możliwe błędy")
        text = "".join(o["text"] for o in payload["material"]["observations"] if o["kind"] == "read")
        self.assertEqual(text, TEXT)                                         # exactly the file, nothing more
        self.assertEqual(payload["material"]["history"], [])

    def test_any_wording_is_a_question_nothing_is_parsed_for_ranges_or_keywords(self):
        sentences = ["przeanalizuj ten plik", "5 powodów, dla których to się wywala?", "/pytaj przeanalizuj ten plik",
                     "Jaki jest sens wiersza 12-14?", "what does this file do"]
        self.model = script(*[{"action": "answer", "text": "ok", "sources": []}] * len(sentences))
        self.run_cli(sentences + ["/koniec"], ["tak"] * len(sentences))
        asked = [p["question"] for p in self.payloads()]
        self.assertEqual(asked, [x.replace("/pytaj ", "", 1) for x in sentences])
        for payload in self.payloads():
            self.assertEqual(len([o for o in payload["material"]["observations"] if o["kind"] == "read"]), 1)

    def test_declined_consent_sends_nothing_and_opens_no_session(self):
        for answers in (["nie"], [EOFError()], [KeyboardInterrupt()]):
            with self.subTest(answers=str(answers)):
                self.sent.clear()
                code = self.run_cli(["przeanalizuj ten plik", "/rejestr", "/koniec"], answers)
                self.assertEqual(code, 0)
                self.assertEqual((self.sent, self.ready, self.fake.opens), ([], 0, 0))
                self.assertIn("Anulowano. Dane nie zostały wysłane.", self.output)
                self.assertIn("Nic nie zostało przekazane", self.output)

    def test_blocked_claude_policy_keeps_local_commands_and_refuses_questions(self):
        self.network = "claude = false\n"
        self.write_policy()
        self.run_cli(["/znajdz ERROR", "przeanalizuj ten plik", "/koniec"], [])
        self.assertIn("wiersz 120", self.output)
        self.assertIn("network.claude=false", self.output)
        self.assertEqual((self.sent, self.prompts.count(PROMPTS["material-transfer"])), ([], 0))

    def test_invalid_questions_never_reach_consent(self):
        self.run_cli(["x" * 3000, "pytanie\x1b[0m", "/koniec"], [])
        self.assertEqual((self.sent, self.ready), ([], 0))
        self.assertEqual(self.output.count("jedną linią zwykłego tekstu"), 2)
        self.assertIn("jedną linią zwykłego tekstu", self.output)

    # --- /pytaj A-B: deliberate narrowing -------------------------------------------------
    def test_narrowed_question_sends_only_those_lines_and_gets_one_step(self):
        self.model = script({"action": "answer", "text": "Te wiersze to wypełniacz.", "sources": [{"first": 10, "last": 12}]})
        self.run_cli(["/pytaj 10-12 Co to jest?", "/koniec"], ["tak"])
        (payload,) = self.payloads()
        reads = [o for o in payload["material"]["observations"] if o["kind"] == "read"]
        self.assertEqual("".join(o["text"] for o in reads), "\n".join(LINES[9:12]) + "\n")
        self.assertEqual(payload["material"]["task"]["max_steps"], 1)
        self.assertNotIn("SECRET-MARKER-IN-LINE-50", self.sent[0].payload)
        self.assertIn("pytanie i wskazane wiersze z log.txt", self.output)

    def test_the_banner_and_the_consent_line_are_compact_and_the_card_is_verbose_only(self):
        self.model = script()
        self.run_cli(["co tu jest?", "/koniec"], ["nie"])
        lines = self.output.splitlines()
        self.assertEqual(lines[0], "")
        self.assertTrue(lines[1].startswith("Plik log.txt ("))
        self.assertEqual(lines[2], "Do Claude trafi tylko to, co zatwierdzisz.")
        self.assertEqual(lines[3:8], ["", "Opcje:", "/koniec    kończy", "/pomoc     pokazuje opcjonalne polecenia", ""])
        self.assertEqual(lines[8], "Co chcesz z nim zrobić?")
        self.assertIn("Do Claude: pytanie i cały plik z log.txt (200 wierszy, %d B)" % len(TEXT.encode()), lines)
        for hidden in ("── Do wysłania", "Bez ", "Treść"):
            self.assertFalse([l for l in lines if hidden in l], hidden)
        self.run_cli(["co tu jest?", "/koniec"], ["nie"], argv=[str(self.file), "--v"])
        self.assertIn("── Do wysłania " + "─" * 33, self.output.splitlines())

    def test_narrowed_question_cannot_widen_itself(self):
        self.model = script({"action": "read", "ranges": [{"first": 1, "last": 200}]})
        self.run_cli(["/pytaj 10-12 Co to jest?", "/koniec"], ["tak"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Zatrzymano bez odpowiedzi", self.output)
        self.assertIn("wiersze 10–12", self.output)

    def test_invalid_or_oversize_scope_is_refused_before_consent(self):
        self.big()
        self.run_cli(["/pytaj 0-5 Pytanie?", "/pytaj 7000-7001 Pytanie?", "/pytaj 1-6000 Pytanie?", "/koniec"], [])
        self.assertEqual((self.sent, self.prompts.count(PROMPTS["material-transfer"])), ([], 0))
        self.assertIn("nieprawidłowy albo zbyt duży", self.output)

    # --- large file: the program finds the fragments ---------------------------------------
    def test_large_file_question_runs_the_loop_with_one_consent_and_visible_steps(self):
        self.big()
        self.model = script({"action": "search", "needle": "ERROR"},
                            {"action": "read", "ranges": [{"first": 118, "last": 122}]},
                            {"action": "answer", "text": "Backend odmówił połączenia (wiersz 120).",
                             "sources": [{"first": 119, "last": 121}]})
        self.run_cli(["Dlaczego pojawia się błąd?", "/rejestr", "/koniec"], ["tak"], argv=[str(self.file), "--v"])
        self.assertEqual(self.prompts.count(PROMPTS["material-task"]), 1)       # consent once for the whole task
        self.assertEqual(len(self.sent), 3)
        for text in ("Claude sam poprosi o wyszukanie frazy lub odczyt wierszy", "Krok 1/8: Claude poprosił o wyszukano „ERROR”",
                     "Krok 2/8: Claude poprosił o odczyt wierszy 118–122", "Backend odmówił połączenia",
                     "reszta pliku nie została wysłana"):
            self.assertIn(text, self.output)
        self.assertRegex(self.output, r"Potwierdzone\s+wiersze 119–121")
        self.assertRegex(self.output, r"Claude dostał\s+wiersze 118–122")
        self.assertRegex(self.output, r"Wyszukiwania\s+„ERROR”")
        everything = "".join(a.payload for a in self.sent)
        self.assertNotIn("SECRET-MARKER-NOT-ASKED-FOR", everything)           # never requested, never sent
        first, second, third = self.payloads()
        self.assertEqual(first["material"]["observations"], [])
        self.assertEqual([o["kind"] for o in second["material"]["observations"]], ["search"])
        self.assertEqual([o["kind"] for o in third["material"]["observations"]], ["search", "read"])
        self.assertEqual([p["material"]["task"]["step"] for p in self.payloads()], [1, 2, 3])
        self.assertEqual(self.output.count("stan: answered"), 3)                # the ledger saw every request

    def test_claimed_sources_that_were_never_delivered_are_not_shown_as_sources(self):
        self.big()
        self.model = script({"action": "read", "ranges": [{"first": 120, "last": 121}]},
                            {"action": "answer", "text": "Coś.", "sources": [{"first": 120, "last": 120}, {"first": 900, "last": 910}]})
        self.run_cli(["Dlaczego błąd?", "/koniec"], ["tak"])
        self.assertIn("── Uwaga ", self.output)                                  # by default only what needs care is shown
        self.assertRegex(self.output, r"Odrzucone\s+wiersze 900–910")
        self.assertIn("(Claude podał źródła, których nie otrzymał)", self.output)
        self.assertNotIn("Potwierdzone", self.output)
        self.model = script({"action": "read", "ranges": [{"first": 120, "last": 121}]},
                            {"action": "answer", "text": "Coś.", "sources": [{"first": 120, "last": 120}, {"first": 900, "last": 910}]})
        self.run_cli(["Dlaczego błąd?", "/koniec"], ["tak"], argv=[str(self.file), "--v"])
        self.assertRegex(self.output, r"Potwierdzone\s+wiersz 120")
        self.assertNotIn("wiersze 900", self.output.split("Zakres")[0].split("Potwierdzone")[1])

    def test_a_malformed_decision_stops_the_task_with_no_retry(self):
        self.big()
        for reply in ('{"action":"whole"', 'Oto {"action":"answer"} w tekście',
                      '{"action":"search","needle":"x","extra":1}', '{"action":"read","ranges":[{"first":1,"last":99999}]}'):
            with self.subTest(reply=reply[:30]):
                self.sent.clear()
                self.model = script(reply)
                self.run_cli(["Pytanie?", "/koniec"], ["tak"])
                self.assertEqual(len(self.sent), 1)
                self.assertIn("nie ma wymaganego formatu", self.output)

    def test_prose_instead_of_an_action_is_shown_as_an_unsourced_answer_and_a_fenced_action_is_accepted(self):
        self.big()
        self.model = script("Oto odpowiedź zwykłym tekstem.")
        self.run_cli(["Pytanie?", "/koniec"], ["tak"])
        self.assertIn("Oto odpowiedź zwykłym tekstem.", self.output)
        self.assertIn("odpowiedział zwykłym tekstem zamiast ustalonej akcji", self.output)
        self.assertNotIn("Źródła potwierdzone", self.output)
        self.model = script('```json\n{"action":"answer","text":"Z ogrodzenia.","sources":[]}\n```')
        self.run_cli(["Pytanie?", "/koniec"], ["tak"])
        self.assertIn("Z ogrodzenia.", self.output)
        self.assertNotIn("zwykłym tekstem zamiast", self.output)

    def test_a_repeated_request_ends_the_task_for_lack_of_progress(self):
        self.big()
        self.model = script({"action": "search", "needle": "ERROR"}, {"action": "search", "needle": "ERROR"})
        self.run_cli(["Pytanie?", "/koniec"], ["tak"])
        self.assertEqual(len(self.sent), 2)
        self.assertIn("powtórzył wcześniejszą prośbę bez postępu", self.output)

    def test_the_budget_ends_the_task_honestly_after_eight_requests(self):
        self.big()
        needles = ["row 001", "row 002", "row 003", "row 004", "row 005", "row 006", "row 007", "row 008"]
        self.model = script(*[{"action": "search", "needle": n} for n in needles])
        self.run_cli(["Pytanie, na które nie ma końca?", "/koniec"], ["tak"])
        self.assertEqual(len(self.sent), 8)
        self.assertIn("nie odpowiedział w ostatnim kroku", self.output)
        self.assertIn("Zatrzymano bez odpowiedzi", self.output)
        self.assertRegex(self.output, r"Claude dostał\s+wiersze 100–119")       # whole lines that arrived inside search hits count
        self.assertIn("Zawęź pytanie albo zadaj je ponownie (nowa zgoda)", self.output)

    def test_a_stopped_task_is_remembered_so_the_next_question_can_continue(self):
        self.big()
        needles = ["row 001", "row 002", "row 003", "row 004", "row 005", "row 006", "row 007", "row 008"]
        self.model = script(*[{"action": "search", "needle": n} for n in needles],
                            {"action": "answer", "text": "Kontynuacja.", "sources": []})
        self.run_cli(["Duże zadanie?", "kontynuuj", "/koniec"], ["tak", "tak"])
        history = self.payloads()[-1]["material"]["history"]
        self.assertEqual(len(history), 1)
        self.assertIn("Zadanie przerwane bez odpowiedzi", history[0]["answer"])

    def test_pytaj_with_only_a_range_asks_for_the_question_instead_of_sending_the_range_as_one(self):
        self.big()
        self.run_cli(["/pytaj 120-160", "/koniec"])
        self.assertIn("Dopisz pytanie po zakresie", self.output)
        self.assertEqual(self.sent, [])

    def test_oversize_model_reads_are_refused_back_to_the_model_not_crashed(self):
        self.big()
        self.model = script({"action": "read", "ranges": [{"first": 1, "last": 6000}]},
                            {"action": "answer", "text": "Nie mogę przeczytać tyle naraz.", "sources": []})
        self.run_cli(["Pytanie?", "/koniec"], ["tak"])
        second = self.payloads()[1]["material"]["observations"]
        self.assertEqual(second, [{"kind": "refused", "action": "read", "code": "TOO_LARGE"}])

    def test_ask_user_returns_to_the_prompt_and_is_remembered(self):
        self.big()
        self.model = script({"action": "ask_user", "text": "Jaki jest komunikat błędu z konsoli?"},
                            {"action": "answer", "text": "Dziękuję.", "sources": []})
        self.run_cli(["Dlaczego to się wywala?", "Komunikat to: timeout", "/koniec"], ["tak", "tak"])
        self.assertIn("Claude pyta: Jaki jest komunikat błędu z konsoli?", self.output)
        history = self.payloads()[1]["material"]["history"]
        self.assertEqual(history[0]["question"], "Dlaczego to się wywala?")
        self.assertIn("Claude zapytał", history[0]["answer"])

    # --- conversation memory (LC3, bounded) -------------------------------------------------
    def test_earlier_answers_travel_with_later_questions_bounded_and_shortened(self):
        long_answer = "Pierwsza odpowiedź. " + "dłuższy tekst " * 400
        self.model = script({"action": "answer", "text": long_answer, "sources": []},
                            {"action": "answer", "text": "Druga.", "sources": []},
                            {"action": "answer", "text": "Trzecia.", "sources": []})
        self.run_cli(["Pierwsze pytanie?", "Wyjaśnij pierwszy punkt poprzedniej odpowiedzi", "A teraz trzecie?", "/koniec"],
                     ["tak"] * 3)
        first, second, third = self.payloads()
        self.assertEqual(first["material"]["history"], [])
        (turn,) = second["material"]["history"]
        self.assertEqual(turn["question"], "Pierwsze pytanie?")
        self.assertTrue(turn["answer"].startswith("Pierwsza odpowiedź."))
        self.assertTrue(turn["answer"].endswith("…[skrócono]"))
        self.assertLess(len(turn["answer"].encode()), 1700)
        self.assertEqual([t["question"] for t in third["material"]["history"]],
                         ["Pierwsze pytanie?", "Wyjaśnij pierwszy punkt poprzedniej odpowiedzi"])

    def test_history_is_limited_to_the_last_four_turns(self):
        self.model = script(*[{"action": "answer", "text": "odp %d" % i, "sources": []} for i in range(7)])
        self.run_cli(["pytanie %d?" % i for i in range(7)] + ["/koniec"], ["tak"] * 7)
        last = self.payloads()[-1]["material"]["history"]
        self.assertEqual([t["question"] for t in last], ["pytanie 2?", "pytanie 3?", "pytanie 4?", "pytanie 5?"])

    def test_a_declined_or_failed_question_leaves_no_phantom_answer_in_the_history(self):
        self.model = script({"action": "answer", "text": "ok", "sources": []})
        self.run_cli(["odrzucone?", "przyjęte?", "/koniec"], ["nie", "tak"])
        self.assertEqual(self.payloads()[0]["material"]["history"], [])

    def test_access_revoked_between_questions_stops_every_later_hand_off(self):
        for label, revoke in (("service off", lambda: self.write_policy_with(read='["~/data/*"]', network="claude = false\n")),
                              ("read scope removed", lambda: self.write_policy_with(read="[]", network=self.network)),
                              ("deny added", lambda: self.write_policy_with(read='["~/data/*"]', network=self.network,
                                                                            deny='["~/data/"]'))):
            with self.subTest(label):
                self.write_policy()
                answer = '{"action":"answer","text":"Pierwsza.","sources":[]}'

                def first_then_revoke(payload, revoke=revoke):
                    revoke()
                    return answer
                self.sent.clear()
                self.model = first_then_revoke
                self.run_cli(["Pierwsze?", "Drugie?", "/koniec"], ["tak", "tak"])
                self.assertEqual(len(self.sent), 1)                       # only the request approved before the change
                self.assertIn("Uprawnienia w permissions.toml zmieniły się", self.output)
                self.assertIn("nie zostały wysłane", self.output)

    def write_policy_with(self, *, read, network, deny="[]"):
        self.config.write_text("version = 1\n[files]\nread = %s\ndeny = %s\n[network]\n%s" % (read, deny, network))
        self.config.chmod(0o600)

    # --- failures, session, pacing, details -------------------------------------------------
    def test_failure_after_hand_off_stops_the_loop_is_unknown_and_never_retried(self):
        self.big()
        calls = {"n": 0}

        def flaky(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                return '{"action":"search","needle":"ERROR"}'
            raise AnalysisDenied("timeout")
        self.model = flaky
        self.run_cli(["Pytanie?", "/rejestr", "/koniec"], ["tak"])
        self.assertEqual(len(self.sent), 2)
        self.assertIn("stan: unknown", self.output)
        self.assertIn("nie można wycofać", self.output)
        self.assertEqual(self.fake.opens, self.fake.closes)

    def test_ctrl_c_during_a_step_is_unknown_and_returns_to_the_prompt(self):
        self.send_error = KeyboardInterrupt()
        self.run_cli(["przeanalizuj", "/rejestr", "/koniec"], ["tak"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Przerwano.", self.output)
        self.assertIn("stan: unknown", self.output)

    def test_pacing_waits_instead_of_exceeding_the_activation_limit(self):
        self.big()
        self.model = script(*[{"action": "search", "needle": "row %03d" % i} for i in range(5)],
                            {"action": "answer", "text": "gotowe", "sources": []})
        self.run_cli(["Pytanie?", "/koniec"], ["tak"])
        self.assertEqual(len(self.sent), 6)
        self.assertTrue(self.clock.slept)
        self.assertIn("Limit usługi (4 aktywacje na minutę)", self.output)
        self.assertTrue(all(step <= 1.0 for step in self.clock.slept))        # countdown ticks every second at most

    def test_pacer_announces_every_second_and_finishes_once(self):
        clock, notes, ends = Clock(), [], []
        pacer = mcli.Pacer(clock=clock, sleep=clock.sleep)
        for _ in range(mcli.MAX_HANDOFFS_PER_MINUTE):
            pacer.record()
        pacer.wait_turn(notes.append, lambda: ends.append(1))
        self.assertGreaterEqual(len(notes), 59)
        self.assertEqual(notes, sorted(notes, reverse=True))
        self.assertEqual(len(set(notes)), len(notes))
        self.assertEqual(ends, [1])

    def test_the_model_reported_by_the_client_is_shown_only_in_details_mode(self):
        meta = ac.extract_meta({"modelUsage": {"claude-sonnet-5-5": {"inputTokens": 10, "outputTokens": 5, "contextWindow": 200000}}})
        self.answer = ac.ModelText('{"action":"answer","text":"Odpowiedź syntetyczna.","sources":[]}', meta)
        self.assertIsNot(type(self.answer), str)
        self.run_cli(["Pytanie?", "/koniec"], ["tak"], argv=[str(self.file), "--v"])
        self.assertIn("MODEL: claude-sonnet-5-5 · okno 200000 · tokeny we/wy 10/5", self.output)
        self.run_cli(["Pytanie?", "/koniec"], ["tak"])
        self.assertNotIn("MODEL:", self.output)

    def test_details_mode_prints_the_first_payload_hashes_per_request_and_timing(self):
        self.big()
        self.model = script({"action": "search", "needle": "ERROR"}, {"action": "answer", "text": "ok", "sources": []})
        self.run_cli(["Pytanie?", "/koniec"], ["tak"], argv=[str(self.file), "--v"])
        self.assertIn("=== DOKŁADNY ŁADUNEK (żądanie 1) ===", self.output)
        self.assertIn("ŻĄDANIE 1: SHA256 " + self.sent[0].sha256, self.output)
        self.assertIn("ŻĄDANIE 2: SHA256 " + self.sent[1].sha256, self.output)
        self.assertIn("CZAS:", self.output)
        self.assertIn("ASK: PASS", self.output)


HUGE_LINES = ["row %06d filler text to make this log long enough to need many parts" % i for i in range(1, 50001)]
HUGE_LINES[199] = "row 000200 ERROR first failure"
HUGE_LINES[49799] = "row 049800 ERROR last failure near the very end"
HUGE_TEXT = "\n".join(HUGE_LINES) + "\n"
PARTS = len(WholePlan(import_bytes(HUGE_TEXT.encode(), "file")).parts)
SECOND_STAGE_MAPS = PARTS - 8


def whole_model(notes_for=None, fail_part=None, reduce_reply=None):
    """First request asks for the whole file; map requests return notes; reduce returns an answer."""
    seen = []

    def model(payload):
        seen.append(payload)
        task = payload["material"]["task"]
        if task.get("phase") == "map":
            index = task["part"]["index"]
            if fail_part is not None and index == fail_part:
                return "to nie jest JSON"
            return json.dumps({"action": "notes", "text": "część %d: ERROR w wierszach tej części" % index})
        if task.get("phase") == "reduce":
            if reduce_reply is not None:
                return reduce_reply
            return json.dumps({"action": "answer", "text": "Podsumowanie całego pliku.",
                               "sources": [{"first": 200, "last": 200}]})
        return json.dumps({"action": "whole"})
    model.seen = seen
    return model


class WholeTests(Base):
    def setUp(self):
        super().setUp()
        self.file.write_text(HUGE_TEXT)

    def run_whole(self, answers, model, commands=("Przeanalizuj cały plik", "/rejestr", "/koniec"), argv=None):
        self.model = model
        self.run_cli(list(commands), answers, argv=argv)
        return getattr(model, "seen", None)

    def test_a_full_analysis_covers_every_byte_and_the_controller_says_so(self):
        seen = self.run_whole(["tak", "tak"], whole_model())
        phases = [p["material"]["task"].get("phase") for p in seen]
        self.assertEqual(phases[0], None)                                     # the loop's first request
        self.assertEqual(phases.count("map"), PARTS)
        self.assertEqual(phases.count("reduce"), 1)
        self.assertEqual(self.prompts.count(PROMPTS["material-task"]), 1)
        self.assertEqual(self.prompts.count(PROMPTS["material-whole"]), 1)    # ONE consent for every stage
        self.assertNotIn("\nEtap ", self.output)                          # no separate stage banners
        self.assertEqual(self.output.count("Analiza całego pliku Etap "), 1)  # one static block outside a real TTY
        self.assertNotIn("przeanalizowana.", self.output)
        self.assertNotIn("Limit usługi", self.output)
        self.assertIn("To pytanie wymaga przeanalizowania CAŁEGO pliku.", self.output)
        self.assertIn("Analiza całego pliku Etap 1 z", self.output)
        maps = [p for p in seen if p["material"]["task"].get("phase") == "map"]
        text = "".join("".join(o["text"] for o in p["material"]["observations"]) for p in maps)
        self.assertEqual(text, HUGE_TEXT)                                     # every byte, once, in order
        self.assertEqual([p["material"]["task"]["step"] for p in maps], list(range(1, 9)) + list(range(1, SECOND_STAGE_MAPS + 1)))
        reduce = [p for p in seen if p["material"]["task"].get("phase") == "reduce"][0]
        self.assertEqual({o["kind"] for o in reduce["material"]["observations"]}, {"notes"})
        self.assertEqual(reduce["material"]["task"]["part"], {"index": PARTS, "total": PARTS})
        self.assertIn("Podsumowanie całego pliku.", self.output)
        for hidden in ("Pokrycie (liczy aplikacja)", "Źródła potwierdzone przez aplikację", "nie, że Claude znalazł wszystkie problemy"):
            self.assertNotIn(hidden, self.output)
        self.assertNotIn("ANALIZA CZĘŚCIOWA", self.output)
        self.assertEqual(self.output.count("stan: answered"), 1 + PARTS + 1)     # loop request + every part + synthesis

    def test_the_task_consent_expires_between_stages_and_is_not_renewed(self):
        base = whole_model()
        calls = {"maps": 0}

        def slow(payload):
            if payload["material"]["task"].get("phase") == "map":
                calls["maps"] += 1
                if calls["maps"] == 8:                                      # the first stage ends; the 30 minutes are gone
                    self.clock.now += mcli.TASK_SECONDS
            return base(payload)
        slow.seen = base.seen
        seen = self.run_whole(["tak", "tak"], slow)
        phases = [p["material"]["task"].get("phase") for p in seen]
        self.assertEqual((phases.count("map"), phases.count("reduce")), (8, 0))
        self.assertEqual(self.prompts.count(PROMPTS["material-whole"]), 1)    # no second question, no silent renewal
        for expected in ("Zgoda na to zadanie wygasła", "Bez wniosków końcowych", "Pokrycie (liczy aplikacja): 8 z %d części" % PARTS,
                         "ANALIZA CZĘŚCIOWA. Nieprzeanalizowane wiersze:"):
            self.assertIn(expected, self.output)
        self.assertNotIn("Podsumowanie całego pliku.", self.output)
        self.assertNotIn("(100%)", self.output)

    def test_the_optional_512_kib_profile_runs_end_to_end_inside_the_budget(self):
        self.config.write_text('version = 1\n[files]\nread = ["~/data/*"]\ndeny = []\npart_size = "512KiB"\n[network]\n%s' % self.network)
        self.config.chmod(0o600)
        seen = self.run_whole(["tak", "tak"], whole_model())
        maps = [a for a in self.sent if json.loads(a.payload)["material"]["task"].get("phase") == "map"]
        sizes = [len(a.payload.encode()) for a in maps]
        self.assertTrue(all(ac.MAX_PACKET < n <= ac.MAX_PACKET_LARGE for n in sizes[:-1]), sizes)    # bigger parts than the default packet
        for approval in maps:
            ac.parse_request(approval.payload.encode())                        # the real parser accepts every one
        text = "".join("".join(o["text"] for o in p["material"]["observations"])
                       for p in seen if p["material"]["task"].get("phase") == "map")
        self.assertEqual(text, HUGE_TEXT)
        self.assertLess(len(maps), PARTS)                                       # fewer requests than the 240 KiB default
        self.assertEqual(self.prompts.count(PROMPTS["material-whole"]), 1)
        self.assertNotIn("Pokrycie (liczy aplikacja)", self.output)
        self.assertNotIn("ANALIZA CZĘŚCIOWA", self.output)

    def test_the_session_request_cap_counts_every_hand_off_and_stops_without_retry(self):
        seen = self.run_whole(["tak", "tak"], whole_model(), argv=[str(self.file), "--max-requests", "3"])
        self.assertEqual(len(self.sent), 3)                                 # the loop's first request + two maps
        self.assertEqual(len(seen), 3)
        for expected in ("Limit 3 żądań w tej sesji jest wyczerpany", "ANALIZA CZĘŚCIOWA", "Pokrycie (liczy aplikacja): 2 z %d części" % PARTS):
            self.assertIn(expected, self.output)
        self.assertNotIn("Podsumowanie całego pliku.", self.output)

    def test_an_unknown_result_still_spends_the_cap_and_is_never_retried(self):
        base = whole_model()
        calls = {"n": 0}

        def flaky(payload):
            calls["n"] += 1
            if calls["n"] == 3:
                raise AnalysisDenied("timeout")
            return base(payload)
        flaky.seen = base.seen
        self.run_whole(["tak", "tak"], flaky, argv=[str(self.file), "--max-requests", "3"])
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(self.sent), 3)

    def test_only_whole_stops_before_any_unplanned_transfer(self):
        model = script({"action": "read", "ranges": [{"first": 1, "last": 20}]}, {"action": "answer", "text": "Nie.", "sources": []})
        self.model = model
        self.run_cli(["Przeanalizuj cały plik", "/koniec"], ["tak"], argv=[str(self.file), "--only-whole"])
        self.assertEqual(len(self.sent), 1)                                  # only the initial request was handed over
        self.assertIn("Tryb odbioru (--only-whole)", self.output)
        self.assertIn("zatrzymano przed kolejnym przekazaniem", self.output)
        self.assertNotIn("Nie.", self.output)

    def test_only_whole_lets_the_planned_whole_file_path_run_normally(self):
        seen = self.run_whole(["tak", "tak"], whole_model(), argv=[str(self.file), "--only-whole"])
        self.assertEqual([x["material"]["task"].get("phase") for x in seen].count("reduce"), 1)
        self.assertNotIn("--only-whole", self.output)

    def test_the_meta_log_records_every_hand_off_as_closed_json_without_any_text(self):
        meta = ac.extract_meta({"modelUsage": {"claude-sonnet-5-5": {"inputTokens": 7, "outputTokens": 3, "contextWindow": 200000}}})
        base = whole_model()
        log = self.home / "meta.jsonl"
        self.model = lambda payload: ac.ModelText(base(payload), meta)
        self.run_cli(["Przeanalizuj cały plik", "/koniec"], ["tak", "tak"], argv=[str(self.file), "--meta-log", str(log)])
        lines = [json.loads(line) for line in log.read_text().splitlines()]
        records, final = lines[:-1], lines[-1]
        self.assertEqual(final, {"result": {"outcome": "complete", "covered": PARTS, "total": PARTS, "synthesis": "answer"}})
        self.assertEqual(oct(log.stat().st_mode & 0o777), "0o600")
        self.assertEqual([r["n"] for r in records], list(range(1, len(records) + 1)))
        self.assertEqual(records[0]["phase"], "loop")
        self.assertEqual(sum(r["phase"] == "map" for r in records), PARTS)
        self.assertEqual(records[-1]["phase"], "reduce")                       # the synthesis request is recorded as well
        self.assertTrue(all(r["models"] == [{"model": "claude-sonnet-5-5", "window": 200000, "input": 7, "output": 3}] for r in records))
        self.assertEqual({k for r in records for k in r}, {"n", "phase", "part", "models"})   # nothing else: no text, no payload

    def test_a_missing_metadata_is_logged_as_unknown_and_an_existing_file_is_never_overwritten(self):
        log = self.home / "meta2.jsonl"
        self.run_whole(["tak", "tak"], whole_model(), argv=[str(self.file), "--meta-log", str(log)])
        self.assertTrue(all(json.loads(line)["models"] == [None] for line in log.read_text().splitlines()[:-1]))
        before = log.read_text()
        self.assertEqual(mcli.main([str(self.file), "--meta-log", str(log)]), 2)
        self.assertEqual(log.read_text(), before)
        (self.home / "link").symlink_to(log)
        self.assertEqual(mcli.main([str(self.file), "--meta-log", str(self.home / "link")]), 2)

    def test_first_question_runs_exactly_one_question_and_ends_the_session_without_a_prompt(self):
        question = "Wypisz wszystkie zdarzenia incident."
        seen = self.run_whole(["tak", "tak"], whole_model(), commands=(), argv=[str(self.file), "--first-question", question])
        self.assertTrue(all(x["question"] == question if "question" in x else True for x in map(lambda a: json.loads(a.payload), self.sent)))
        self.assertEqual({json.loads(a.payload)["question"] for a in self.sent}, {question})
        self.assertEqual(self.prompts.count("> "), 0)                       # no command prompt: nothing to mistype or paste
        self.assertEqual(self.prompts.count(PROMPTS["material-whole"]), 1)  # the consents are still asked (and answered by the user)
        self.assertNotIn("Pokrycie (liczy aplikacja)", self.output)

    def test_answer_file_receives_the_answer_and_the_notes_in_new_private_files_and_never_overwrites(self):
        target = self.home / "answer.txt"
        self.run_whole(["tak", "tak"], whole_model(), commands=(), argv=[str(self.file), "--first-question", "Pytanie?", "--answer-file", str(target)])
        self.assertEqual(target.read_text(), "Podsumowanie całego pliku.")
        notes = Path(str(target) + ".notes").read_text().splitlines()
        self.assertEqual(len(notes), PARTS)
        self.assertTrue(notes[0].startswith("wiersze 1–"))
        self.assertEqual(oct(target.stat().st_mode & 0o777), "0o600")
        self.assertEqual(mcli.main([str(self.file), "--answer-file", str(target)]), 2)          # an existing file is never overwritten

    def test_a_failed_synthesis_still_leaves_the_notes_and_no_answer_file(self):
        target = self.home / "a2.txt"
        self.run_whole(["tak", "tak"], whole_model(fail_part=None, reduce_reply='{"action":"unknown"}'), commands=(),
                       argv=[str(self.file), "--first-question", "Pytanie?", "--answer-file", str(target)])
        self.assertFalse(target.exists())
        self.assertEqual(len(Path(str(target) + ".notes").read_text().splitlines()), PARTS)

    def test_a_synthesis_that_only_breaks_the_strict_schema_is_shown_instead_of_lost(self):
        sources = [{"first": n, "last": n} for n in range(1, 12)]            # more than the 8 sources the strict schema allows
        reply = json.dumps({"action": "answer", "text": "Wnioski z syntezy.", "sources": sources})
        self.run_whole(["tak", "tak"], whole_model(reduce_reply=reply))
        self.assertIn("Wnioski z syntezy.", self.output)
        self.assertIn("niestandardowym formacie", self.output)
        self.assertNotIn("Źródła potwierdzone", self.output)               # the dropped sources are not confirmed
        self.assertNotIn("odpowiedź w złym formacie", self.output)

    def test_the_cap_must_be_a_positive_integer(self):
        for bad in ("0", "-2", "x"):
            with self.subTest(bad), self.assertRaises(SystemExit):
                mcli.build_parser().parse_args([str(self.file), "--max-requests", bad])

    def whole_with(self, text, part_size=None):
        self.file.write_text(text)
        if part_size:
            self.config.write_text('version = 1\n[files]\nread = ["~/data/*"]\ndeny = []\npart_size = "%s"\n[network]\n%s' % (part_size, self.network))
            self.config.chmod(0o600)
        seen = self.run_whole(["tak", "tak"], whole_model())
        phases = [x["material"]["task"].get("phase") for x in seen]
        self.assertEqual(phases.count("reduce"), 1)
        self.assertEqual(self.prompts.count(PROMPTS["material-whole"]), 1)        # no extra confirmation for any stage
        self.assertNotIn("Pokrycie (liczy aplikacja)", self.output)
        self.assertNotIn("ANALIZA CZĘŚCIOWA", self.output)
        self.assertNotIn("nie przechodzi walidacji", self.output)
        return seen

    @staticmethod
    def rows(size, width=63):
        return "\n".join(("r%07d " % i).ljust(width, "x") for i in range(size // (width + 1))) + "\n"

    def test_a_synthesis_only_stage_completes_without_extra_confirmations(self):
        text = self.rows(2 * 1024 * 1024)                                  # exactly 2 MiB: the maps fill stage 1, the synthesis is alone
        self.assertEqual(len(text.encode()), 2 * 1024 * 1024)
        self.assertEqual(WholePlan(import_bytes(text.encode(), "file"), part_bytes=512 * 1024).stages()[-1], [("reduce", None)])
        seen = self.whole_with(text, "512KiB")
        self.assertEqual(sum(1 for x in seen if x["material"]["task"].get("phase") == "map"), 5)

    def test_a_small_tail_and_the_synthesis_complete_in_the_last_stage(self):
        text = self.rows(2 * 1024 * 1024 - 600 * 1024)
        plan = WholePlan(import_bytes(text.encode(), "file"), part_bytes=512 * 1024)
        last = plan.stages()[-1]
        self.assertTrue(last[-1] == ("reduce", None) and any(kind == "map" for kind, _ in last))   # tail + synthesis together
        seen = self.whole_with(text, "512KiB")
        self.assertEqual(sum(1 for x in seen if x["material"]["task"].get("phase") == "map"), len(plan.parts))

    def test_a_whole_small_plan_below_256_kib_completes(self):
        seen = self.whole_with(self.rows(230 * 1024))                      # too big to preload, one part, budget < 256 KiB
        self.assertEqual(sum(1 for x in seen if x["material"]["task"].get("phase") == "map"), 1)

    def test_a_plan_whose_grants_cannot_be_built_is_refused_before_the_consent_and_any_transfer(self):
        with patch.object(mcli.WholePlan, "stage_budget", return_value=0):
            seen = self.run_whole(["tak", "tak"], whole_model())
        self.assertEqual(len(seen), 1)                                     # only the loop's first request
        self.assertEqual(self.prompts.count(PROMPTS["material-whole"]), 0)
        self.assertIn("nie przechodzi walidacji; nic nie zostało wysłane", self.output)

    def test_default_plan_has_one_sentence_before_the_single_consent(self):
        self.run_whole(["tak", "nie"], whole_model())
        for expected in ("To pytanie wymaga przeanalizowania CAŁEGO pliku. Do Claude zostanie wysłany cały plik (%d B)." % len(HUGE_TEXT.encode()),
                         "Zadanie nie zostało zatwierdzone"):
            self.assertIn(expected, self.output)
        for hidden in ("Plan:", "Twardy budżet", "ważna 30 min", "żądania w", "bez pytań o etapy"):
            self.assertNotIn(hidden, self.output)

    def test_verbose_retains_part_and_stage_details_without_extra_consents(self):
        self.run_whole(["tak", "tak"], whole_model(), argv=[str(self.file), "--v"])
        self.assertIn("Etap 1 z", self.output)
        self.assertIn("Część 1/%d (wiersze" % PARTS, self.output)
        self.assertIn("Twardy budżet całego zadania", self.output)
        self.assertIn("Źródła potwierdzone przez aplikację", self.output)
        self.assertIn("Pokrycie (liczy aplikacja)", self.output)
        self.assertIn("nie, że Claude znalazł wszystkie problemy", self.output)
        self.assertEqual(self.prompts.count(PROMPTS["material-whole"]), 1)

    def test_declining_the_whole_file_plan_sends_nothing_beyond_the_first_request(self):
        seen = self.run_whole(["tak", "nie"], whole_model())
        self.assertEqual(len(seen), 1)
        self.assertIn("Pokrycie (liczy aplikacja): 0 z %d części" % PARTS, self.output)
        self.assertIn("ANALIZA CZĘŚCIOWA", self.output)

    def test_a_part_with_a_malformed_note_is_not_counted_and_the_result_is_marked_partial(self):
        seen = self.run_whole(["tak", "tak"], whole_model(fail_part=3))
        self.assertIn("Część 3/%d" % PARTS, self.output)
        self.assertIn("NIE jest policzona jako przeanalizowana", self.output)
        reduce = [p for p in seen if p["material"]["task"].get("phase") == "reduce"][0]
        self.assertEqual(reduce["material"]["task"]["part"], {"index": PARTS - 1, "total": PARTS})
        self.assertEqual(len(reduce["material"]["observations"]), PARTS - 1)
        self.assertIn("Pokrycie (liczy aplikacja): %d z %d części" % (PARTS - 1, PARTS), self.output)
        self.assertIn("ANALIZA CZĘŚCIOWA", self.output)

    def test_model_claims_cannot_raise_coverage_or_confirm_unseen_sources(self):
        lie = json.dumps({"action": "answer", "text": "Przeanalizowałem 100% pliku, wszystko sprawdzone.",
                          "sources": [{"first": 200, "last": 200}, {"first": 49800, "last": 49800}]})
        seen = self.run_whole(["tak", "nie"], whole_model(reduce_reply=lie))
        # stage two was declined, so no reduce ran: the claim never even reaches the screen as a result
        self.assertNotIn("Przeanalizowałem 100%", self.output)
        self.assertEqual(self.output.count("Pokrycie (liczy aplikacja)"), 1)
        self.sent.clear()
        seen = self.run_whole(["tak", "tak"], whole_model(fail_part=PARTS, reduce_reply=lie))
        self.assertIn("Przeanalizowałem 100% pliku", self.output)              # shown as the model's text...
        self.assertIn("Pokrycie (liczy aplikacja): %d z %d części" % (PARTS - 1, PARTS), self.output)   # ...next to the real numbers
        self.assertIn("ANALIZA CZĘŚCIOWA", self.output)
        self.assertIn("Claude podał źródła, których nie otrzymał (pominięto): 49800–49800", self.output)

    def test_a_delivery_failure_stops_the_run_keeps_the_notes_and_never_retries(self):
        calls = {"n": 0}
        base = whole_model()

        def flaky(payload):
            calls["n"] += 1
            if calls["n"] == 5:
                raise AnalysisDenied("timeout")
            return base(payload)
        flaky.seen = base.seen
        self.run_whole(["tak", "tak"], flaky)
        self.assertEqual(calls["n"], 5)
        self.assertIn("stan: unknown", self.output)
        self.assertIn("Bez wniosków końcowych", self.output)
        self.assertIn("Pokrycie (liczy aplikacja): 3 z %d części" % PARTS, self.output)
        self.assertEqual(self.fake.opens, self.fake.closes)

    def test_a_malformed_final_answer_shows_the_notes_instead_of_inventing_a_synthesis(self):
        self.run_whole(["tak", "tak"], whole_model(reduce_reply='{"action":"unknown"}'))      # not salvageable: not an answer at all
        self.assertIn("Wnioski końcowe: odpowiedź w złym formacie", self.output)
        self.assertIn("Bez wniosków końcowych. Notatki z przeanalizowanych części:", self.output)
        self.assertIn("Pokrycie (liczy aplikacja): %d z %d części" % (PARTS, PARTS), self.output)

    def test_a_prose_synthesis_is_shown_as_an_unsourced_answer_with_a_format_warning(self):
        self.run_whole(["tak", "tak"], whole_model(reduce_reply="Zwykły tekst bez JSON."))
        self.assertIn("Zwykły tekst bez JSON.", self.output)
        self.assertIn("niestandardowym formacie", self.output)
        self.assertNotIn("Źródła potwierdzone", self.output)

    def test_ask_user_in_the_final_step_returns_to_the_prompt_and_is_remembered(self):
        reply = json.dumps({"action": "ask_user", "text": "Który błąd cię interesuje?"})
        self.run_whole(["tak", "tak"], whole_model(reduce_reply=reply))
        self.assertIn("Claude pyta: Który błąd cię interesuje?", self.output)
        self.assertNotIn("Pokrycie (liczy aplikacja)", self.output)

    def test_the_synthesis_is_remembered_with_its_coverage_for_follow_up_questions(self):
        original = whole_model()
        seen = []

        def both(payload):
            seen.append(payload)
            if payload["material"]["task"].get("phase") is None and payload["question"].startswith("Wyjaśnij"):
                return json.dumps({"action": "answer", "text": "Wyjaśnienie.", "sources": []})
            return original(payload)
        self.run_whole(["tak", "tak", "tak"], both,
                       commands=("Przeanalizuj cały plik", "Wyjaśnij pierwszy punkt poprzedniej odpowiedzi", "/koniec"))
        history = seen[-1]["material"]["history"]
        self.assertEqual(history[0]["question"], "Przeanalizuj cały plik")
        self.assertIn("Podsumowanie całego pliku.", history[0]["answer"])
        self.assertNotIn("analiza częściowa", history[0]["answer"])           # complete coverage is not labelled partial


if __name__ == "__main__":
    unittest.main()
