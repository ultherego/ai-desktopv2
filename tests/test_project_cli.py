"""`ai-desktop project`: real policy and files; fake A+ session, transport and clock. No real Claude."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
import contextlib
import io
import json
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import permissions as p
from ai_desktop import project_cli as pcli
from ai_desktop import project_loop as pl
from ai_desktop import project_scope as ps
from ai_desktop import project_step as pstep
from ai_desktop.analysis_contract import AnalysisConsent, AnalysisDenied, parse_request
from ai_desktop import material_cli as mcli
from ai_desktop.terminal_consent import PROMPTS
from tests.test_material_cli import Clock, FakeSession, script

APP = "".join("row %04d filler text for the application module\n" % i for i in range(1, 2001))
APP = APP.replace("row 0120 ", "row 0120 ERROR connection refused by backend ")


class Base(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.geteuid(), 0)
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ai-project-cli-", dir=real_home)))
        # Keep baseline policy/controller tests independent of the runner's
        # private settings; actual profile storage is exercised separately.
        self.enterContext(patch("ai_desktop.agent_profiles.resolve", return_value={
            "provider": "claude", "model": "default", "effort": "default", "instructions": "",
            "profile": "default", "access": "changes"}))
        self.enterContext(patch.object(p, "_identity", return_value=(os.getuid(), str(self.home))))
        self.config = self.home / ".config/ai-desktop/permissions.toml"
        self.config.parent.mkdir(parents=True, mode=0o700)
        self.config.write_text('version = 1\n[files]\nread = ["~/proj/**"]\ndeny = ["~/proj/secret/**"]\n[network]\nclaude = true\n')
        self.config.chmod(0o600)
        self.proj = self.home / "proj"
        for rel, text in (("src/app.py", APP), ("src/util.py", "def helper():\n    return 1\n"),
                          ("docs/readme.txt", "Project notes\n"), ("secret/key.txt", "TOP-SECRET-CONTENT\n")):
            target = self.proj / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        self.clock = Clock()
        self.fake = FakeSession(self.clock)
        self.sent, self.model, self.ready = [], None, 0

    def sender(self, approval):
        self.sent.append(approval)
        if getattr(self, "send_error", None):
            raise self.send_error
        return self.model(json.loads(approval.payload))

    def payloads(self):
        return [json.loads(a.payload) for a in self.sent]

    def run_cli(self, commands, answers=(), argv=None, tty=True):
        commands, answers, self.prompts = iter(commands), iter(answers), []

        def reader(prompt):
            self.prompts.append(prompt)
            value = next(commands if prompt == "> " else answers)
            if isinstance(value, BaseException):
                raise value
            return value

        def ready():
            self.ready += 1
        out = io.StringIO()
        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(patch.object(pcli.sys.stdin, "isatty", return_value=tty))
        stack.enter_context(patch.object(pcli.sys.stdout, "isatty", return_value=tty))
        stack.enter_context(patch.object(mcli, "installation_ready", side_effect=ready))
        with stack:
            window = mcli.SessionWindow(quiet=True, clock=self.clock, opener=self.fake.opener)
            code = pcli.main(argv or [str(self.proj)], reader=reader, clock=self.clock, sleep=self.clock.sleep,
                             sender=self.sender, window=window)
        self.output = out.getvalue()
        return code


class RootAndLocalTests(Base):
    def test_bad_roots_and_missing_terminal_are_refused_before_anything_is_sent(self):
        (self.home / "other").mkdir()
        os.symlink(self.proj / "src", self.proj / "alias")
        for argv in ([str(self.home / "other")], [str(self.home)], [str(self.proj / "alias")], [str(self.proj / "none")]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli(["/exit"], argv=argv), 2)
        self.assertEqual(self.run_cli(["/exit"], tty=False), 2)
        self.config.unlink()
        self.assertEqual(self.run_cli(["/exit"]), 2)
        self.assertEqual((self.sent, self.fake.opens, self.ready), ([], 0, 0))

    def test_local_commands_send_nothing_and_never_show_denied_names_or_content(self):
        self.assertEqual(self.run_cli(["/files", "/files src", "/search ERROR", "/search TOP-SECRET", "/log", "/exit"]), 0)
        self.assertIn("src/app.py", self.output)
        self.assertIn("No matches", self.output)
        self.assertNotIn("TOP-SECRET", self.output)
        self.assertNotIn("secret/", self.output)
        self.assertIn("Nothing was handed", self.output)
        self.assertEqual((self.sent, self.fake.opens), ([], 0))

    def test_the_banner_and_the_consent_preview_use_short_lines(self):
        self.model = script()
        self.run_cli(["co to za projekt?", "/exit"], ["no"])
        lines = self.output.splitlines()
        self.assertEqual(lines[0], "")
        self.assertTrue(lines[1].startswith("Project proj ("))
        self.assertEqual(lines[2], "Only what you approve goes to Claude.")
        self.assertEqual(lines[3:8], ["", "Options:", "/exit      quits", "/help      shows optional commands", ""])
        self.assertEqual(lines[8], "What do you want to do with it?")
        # Compact by default: one line says what leaves the computer, nothing repeated from the banner.
        self.assertIn("To Claude: the question, file names and parts from proj (3 file(s), 1 entry skipped)", lines)
        for hidden in ("── To send", "Skipped", "Without ", "Content"):
            self.assertFalse([l for l in lines if hidden in l], hidden)
        self.assertIn("Send to Claude? [y/n]", PROMPTS["project-task"])

    def test_verbose_shows_the_full_consent_card(self):
        self.model = script()
        self.run_cli(["co to za projekt?", "/exit"], ["no"], argv=[str(self.proj), "--v"])
        lines = self.output.splitlines()
        self.assertIn("── To send " + "─" * 37, lines)
        self.assertRegex(self.output, r"Skipped\s+denied or out of scope: 1")
        self.assertRegex(self.output, r"Without\s+Internet, file writes or running code · time limit 600 s")
        self.assertRegex(self.output, r"Data\s+the question, plus the file names, matches and parts Claude asks for")

    def test_verbose_adds_the_explanation_of_what_is_sent(self):
        self.model = script()
        self.run_cli(["co to za projekt?", "/exit"], ["no"], argv=[str(self.proj), "--v"])
        self.assertRegex(self.output, r"Content\s+Claude will ask")
        self.assertIn("File names, matches and parts are sent as data", self.output)

    def test_the_bare_word_exit_ends_the_session_instead_of_asking_claude(self):
        self.model = script()
        self.assertEqual(self.run_cli(["exit"]), 0)
        self.assertEqual((self.sent, self.fake.opens), ([], 0))

    def test_no_network_permission_blocks_questions_but_not_local_commands(self):
        self.config.write_text('version = 1\n[files]\nread = ["~/proj/**"]\ndeny = []\n[network]\nclaude = false\n')
        self.config.chmod(0o600)
        self.assertEqual(self.run_cli(["co tu jest?", "/files", "/exit"]), 0)
        self.assertIn("network.claude=false", self.output)
        self.assertEqual(self.sent, [])


class QuestionTests(Base):
    def ask(self, *decisions, question="Gdzie jest błąd połączenia?", answers=("yes",), extra=(), verbose=False):
        self.model = script(*decisions)
        argv = [str(self.proj), "--v"] if verbose else None
        return self.run_cli([question, *extra, "/exit"], list(answers), argv=argv)

    def test_progress_is_framed_in_its_own_section_between_the_prompt_and_the_answer(self):
        self.ask({"action": "list", "prefix": "", "cursor": 0}, {"action": "answer", "text": "Gotowe.", "sources": []})
        lines = self.output.splitlines()
        head = lines.index("── Preparing " + "─" * 35)
        self.assertEqual(lines[head - 1], "")                       # a blank line after the consent answer
        self.assertTrue(any(l.startswith("  Step 1/8") for l in lines[head:]))
        foot = lines.index("─" * 48, head)
        self.assertEqual(lines[foot + 1], "")
        self.assertTrue(all(not l.startswith("  Step") for l in lines[foot:]))
        self.assertLess(foot, lines.index("── Answer " + "─" * 38))     # the answer comes after the section is closed

    def test_a_task_that_stops_closes_the_section_before_the_summary(self):
        self.ask({"action": "find", "fragment": "util", "cursor": 0}, '{"action":"list"')
        lines = self.output.splitlines()
        head = next(i for i, l in enumerate(lines) if l.startswith("── Preparing"))
        foot = lines.index("─" * 48, head)
        self.assertLess(foot, next(i for i, l in enumerate(lines) if l.startswith("── Stopped without an answer")))

    def test_a_direct_answer_has_no_empty_progress_frame_and_no_waiting_lines(self):
        self.ask({"action": "answer", "text": "Od razu.", "sources": []})
        self.assertNotIn("── Preparing", self.output)
        self.assertNotIn("Waiting for", self.output)
        self.assertIn("── Answer", self.output)

    def test_two_ctrl_c_anywhere_end_the_session_and_one_only_cancels_the_step(self):
        self.model = script({"action": "answer", "text": "Odpowiedź.", "sources": []})
        # Ctrl+C at the consent question cancels it and arms the next one; the second (also at consent) ends the session.
        self.run_cli(["pierwsze", "drugie", "nigdy"], [KeyboardInterrupt(), KeyboardInterrupt()])
        self.assertEqual(self.prompts.count("> "), 2)
        self.assertIn("(Press Ctrl+C again within 3 s to quit)", self.output)
        self.assertEqual(self.sent, [])
        # A single Ctrl+C does not end the session: the next question still works.
        self.run_cli(["pierwsze", "drugie", "/exit"], [KeyboardInterrupt(), "yes"])
        self.assertEqual(self.prompts.count("> "), 3)
        self.assertIn("Odpowiedź.", self.output)

    def test_ctrl_c_while_waiting_for_claude_interrupts_only_the_request_and_a_second_one_ends_the_session(self):
        self.send_error = KeyboardInterrupt()
        self.model = script({"action": "answer", "text": "x", "sources": []})
        self.run_cli(["pierwsze", "drugie", "nigdy"], ["yes", "yes"])
        self.assertEqual(self.prompts.count("> "), 2)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Interrupted.", self.output)

    def test_declining_consent_sends_nothing_and_the_short_preview_has_no_long_explanation(self):
        self.model = script()
        self.run_cli(["Gdzie jest błąd?", "/exit"], ["no"])
        self.assertIn("Cancelled. No data was sent.", self.output)
        self.assertNotIn("File names, matches and parts", self.output)
        self.assertIn("To Claude: the question, file names and parts from proj", self.output)
        self.assertEqual((self.sent, self.fake.opens), ([], 0))
        self.assertIn(PROMPTS["project-task"], self.prompts)

    def test_a_multi_step_investigation_reads_only_what_was_asked_and_confirms_sources(self):
        self.ask({"action": "list", "prefix": "", "cursor": 0},
                 {"action": "search", "needle": "ERROR", "continue": False},
                 {"action": "read", "path": "src/app.py", "ranges": [{"first": 118, "last": 122}]},
                 {"action": "answer", "text": "Błąd w src/app.py wiersz 120.",
                  "sources": [{"path": "src/app.py", "first": 120, "last": 120}, {"path": "src/util.py", "first": 1, "last": 2}]}, verbose=True)
        self.assertEqual(len(self.sent), 4)
        first, last = self.payloads()[0], self.payloads()[-1]
        self.assertEqual(first["mode"], "project")
        self.assertEqual(first["project"]["project"]["files"], 3)
        self.assertEqual(first["project"]["project"]["skipped"]["denied"], 1)
        kinds = [o["kind"] for o in last["project"]["observations"]]
        self.assertEqual(kinds, ["overview", "tree", "search", "read"])
        everything = json.dumps(self.payloads())
        self.assertNotIn("TOP-SECRET", everything)
        self.assertNotIn("secret/", everything)
        self.assertNotIn("filler text for the application module\\nrow 1500", everything)    # unread parts never sent
        self.assertIn("Step 1/8: Claude asked for the directory contents of the project", self.output)
        self.assertRegex(self.output, r"Confirmed\s+src/app.py:120")
        self.assertIn("src/util.py:1–2", self.output)                  # listed as NOT received
        self.assertIn("Claude cited sources it did not receive", self.output)
        self.assertIn("src/app.py: lines 118–122", self.output)
        self.assertRegex(self.output, r"Out of reach\s+denied or out of scope: 1")
        self.assertIn("the rest of the project was not sent", self.output)

    def test_search_hits_with_whole_lines_count_as_received_sources(self):
        self.ask({"action": "search", "needle": "def helper", "continue": False},
                 {"action": "answer", "text": "Pomocnik w util.py.",
                  "sources": [{"path": "src/util.py", "first": 1, "last": 1}]}, verbose=True)
        self.assertRegex(self.output, r"Confirmed\s+src/util.py:1\n")
        self.assertNotIn("no file lines", self.output)
        self.assertIn("search results only; the rest of the project was not sent", self.output)
        self.assertNotIn("0 B of parts read", self.output)

    def test_the_default_answer_is_followed_only_by_the_prompt_unless_there_is_something_to_watch(self):
        self.ask({"action": "read", "path": "src/util.py", "ranges": [{"first": 1, "last": 2}]},
                 {"action": "answer", "text": "Pomocnik.", "sources": [{"path": "src/util.py", "first": 1, "last": 2}]})
        for header in ("── Sources", "── Scope", "── Note"):
            self.assertNotIn(header, self.output)
        self.assertIn("Out of reach for Claude: 1 entry skipped during the scan (details: --v).", self.output)
        self.assertNotIn("denied or out of scope", self.output.split("── Answer")[1])   # after the answer: a count only
        self.assertNotIn("secret", self.output.split("── Answer")[1].lower())
        self.ask({"action": "answer", "text": "Zmyślone.", "sources": [{"path": "src/app.py", "first": 1, "last": 1}]})
        self.assertIn("── Note ", self.output)
        self.assertRegex(self.output, r"Rejected\s+src/app.py:1")

    def test_the_skipped_count_is_reported_once_per_session_unless_it_changes(self):
        self.model = script({"action": "answer", "text": "Jeden.", "sources": []}, {"action": "answer", "text": "Dwa.", "sources": []})
        self.run_cli(["pierwsze", "drugie", "/exit"], ["yes", "yes"])
        self.assertEqual(self.output.count("Out of reach for Claude: 1 entry"), 1)

    def test_a_partial_search_is_called_out_even_without_verbose(self):
        for i in range(30):
            (self.proj / "docs" / ("n%02d.txt" % i)).write_text("NEEDLE\n" * 3)
        self.ask({"action": "search", "needle": "NEEDLE", "continue": False},
                 {"action": "answer", "text": "Tylko pierwsza strona.", "sources": []})
        self.assertRegex(self.output, r"Incomplete\s+search")

    def test_one_json_action_after_a_short_sentence_is_executed_not_refused(self):
        self.ask('Read README.md first.  {"action":"read","path":"src/util.py","ranges":[{"first":1,"last":9}]}',
                 {"action": "answer", "text": "Przeczytane.", "sources": []})
        self.assertIn("Step 1/8: Claude asked for src/util.py", self.output)
        self.assertNotIn("is not in the required format", self.output)
        for raw in ('a {"action":"overview"} b {"action":"overview"}', 'Najpierw {"action": zepsute', "x" * 700 + ' {"action":"overview"}'):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                pstep.parse_decision(raw)

    def test_an_answer_object_that_only_fails_the_strict_schema_is_still_shown(self):
        self.ask('{"action":"answer","text":"Analiza plik po pliku nie jest wykonalna.","note":"extra"}')
        self.assertIn("Analiza plik po pliku nie jest wykonalna.", self.output)
        self.assertRegex(self.output, r"Format\s+Claude answered in plain text")
        self.assertNotIn("is not in the required format", self.output)

    def test_a_deep_task_continues_in_stages_with_coverage_and_a_small_yes_per_stage(self):
        self.model = script(
            {"action": "read", "path": "src/util.py", "ranges": [{"first": 1, "last": 2}]},
            {"action": "answer", "text": "Częściowo: util.py.", "sources": [], "progress": "Zrobione: util.py. Dalej: docs.", "continue": True},
            {"action": "read", "path": "docs/readme.txt", "ranges": [{"first": 1, "last": 1}]},
            {"action": "answer", "text": "Komplet.", "sources": [], "continue": False})
        self.run_cli(["Zrób głęboką analizę", "/exit"], ["yes", "yes"])
        first, second, third, fourth = self.payloads()
        self.assertEqual((first["project"]["project"]["stage"], first["project"]["project"]["visited"]), (1, []))
        self.assertEqual(second["project"]["project"]["stage"], 1)
        self.assertEqual((third["project"]["project"]["stage"], third["project"]["project"]["visited"]), (2, ["src/util.py"]))
        self.assertEqual(third["project"]["project"]["progress"], "Zrobione: util.py. Dalej: docs.")
        self.assertIn(PROMPTS["project-continue"], self.prompts)
        self.assertIn("Partial analysis, stage 1 of 5: Claude read 1 of 3 files", self.output)
        self.assertIn("── Stage 2 of 5", self.output)
        self.assertIn("Komplet.", self.output)

    def test_declining_the_next_stage_or_reaching_the_cap_ends_the_deep_task(self):
        partial = {"action": "answer", "text": "Kolejny kawałek.", "sources": [], "continue": True}
        self.model = script(partial)
        self.run_cli(["Analiza", "/exit"], ["yes", "no"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Stopped after stage 1 of 5; partial conclusions are above.", self.output)
        self.model = script(*[partial] * 5)
        self.run_cli(["Analiza", "/exit"], ["yes"] * 5)
        self.assertEqual(len(self.sent), 6)                                # 1 earlier + 5 stages
        self.assertIn("Reached the limit of 5 stages", self.output)

    def test_prose_instead_of_an_action_is_shown_with_a_format_warning_and_no_sources(self):
        self.ask("To jest zwykły tekst z **markdownem**.")
        self.assertIn("To jest zwykły tekst", self.output)
        self.assertRegex(self.output, r"Format\s+Claude answered in plain text instead of the agreed action")
        self.assertNotIn("is not in the required format", self.output)

    def test_a_cut_search_hit_does_not_count_as_a_received_line(self):
        (self.proj / "docs/long.txt").write_text("a" * 400 + " LONGNEEDLE\n")
        self.ask({"action": "search", "needle": "LONGNEEDLE", "continue": False},
                 {"action": "answer", "text": "Jest w long.txt.",
                  "sources": [{"path": "docs/long.txt", "first": 1, "last": 1}]}, verbose=True)
        self.assertIn("Claude cited sources it did not receive", self.output)
        self.assertIn("no file lines", self.output)

    def test_an_end_past_the_last_line_is_cut_and_a_start_past_it_is_refused_with_an_explanation(self):
        self.ask({"action": "read", "path": "src/util.py", "ranges": [{"first": 1, "last": 500}]},
                 {"action": "read", "path": "src/util.py", "ranges": [{"first": 300, "last": 400}]},
                 {"action": "answer", "text": "Gotowe.", "sources": [{"path": "src/util.py", "first": 1, "last": 2}]}, verbose=True)
        self.assertIn("Step 1/8: Claude asked for src/util.py, lines 1–2 (", self.output)
        self.assertIn("the file has 2 lines", self.output)
        self.assertIn("Step 2/8: Claude asked for src/util.py, lines 300–400 — refused: line range outside the file (the file has 2 lines)", self.output)
        self.assertNotIn("asked for refused", self.output)
        self.assertRegex(self.output, r"Confirmed\s+src/util.py:1–2")

    def test_a_malformed_answer_shows_a_safe_preview_of_what_came_back(self):
        self.ask("To nie jest JSON \x1b[31mczerwony")
        self.assertIn("is not in the required format", self.output)
        self.assertIn("Start of the answer: To nie jest JSON", self.output)
        self.assertNotIn("\x1b[31m", self.output)

    def test_unknown_paths_traversal_and_denied_files_are_refused_back_to_the_model(self):
        self.ask({"action": "read", "path": "secret/key.txt", "ranges": [{"first": 1, "last": 1}]},
                 {"action": "read", "path": "../outside", "ranges": [{"first": 1, "last": 1}]},
                 {"action": "answer", "text": "Nie mogłem tego odczytać.", "sources": []})
        # The second decision is rejected by the strict schema (no "..") and ends the task without a retry.
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.payloads()[1]["project"]["observations"][1:], [{"kind": "refused", "action": "read", "code": "PATH"}])
        self.assertIn("is not in the required format", self.output)
        self.assertNotIn("TOP-SECRET", json.dumps(self.payloads()))

    def test_a_repeated_request_a_bad_format_and_the_step_budget_stop_the_task_honestly(self):
        self.ask({"action": "list", "prefix": "", "cursor": 0}, {"action": "list", "prefix": "", "cursor": 0})
        self.assertIn("repeated an earlier request", self.output)
        self.ask('{"action":"list"')
        self.assertIn("is not in the required format", self.output)
        self.ask(*[{"action": "find", "fragment": "app%d" % i, "cursor": 0} for i in range(8)])
        self.assertIn("did not answer in the last step", self.output)
        self.assertIn("Stopped without an answer", self.output)

    def test_a_file_that_changes_between_reads_is_reported_and_repinned(self):
        read = {"action": "read", "path": "src/util.py", "ranges": [{"first": 1, "last": 2}]}
        reread = {"action": "read", "path": "src/util.py", "ranges": [{"first": 1, "last": 1}]}
        changer = {"n": 0}
        base = script(read, reread, {"action": "answer", "text": "Plik się zmienił.",
                                     "sources": [{"path": "src/util.py", "first": 1, "last": 1}]})

        def model(payload):
            changer["n"] += 1
            if changer["n"] == 2:
                (self.proj / "src/util.py").write_text("def helper():\n    return 2\n")
            return base(payload)
        self.model = model
        self.run_cli(["Co robi helper?", "/exit"], ["yes"], argv=[str(self.proj), "--v"])
        observed = self.payloads()[2]["project"]["observations"]
        self.assertIn({"kind": "refused", "action": "read", "code": "CHANGED"}, observed)
        self.assertEqual([o["kind"] for o in observed if o["kind"] in ("read", "dropped")], ["dropped"])
        self.assertRegex(self.output, r"Changed\s+src/util.py \(during the task or after the scan\)")
        self.assertRegex(self.output, r"Confirmed\s+none — Claude named no sources")

    def test_the_index_is_refreshed_before_every_question_and_on_request(self):
        self.model = script({"action": "answer", "text": "Jeden.", "sources": []},
                            {"action": "answer", "text": "Dwa.", "sources": []})
        commands = ["Pierwsze pytanie?", "Drugie pytanie?", "/refresh", "/exit"]
        added = {"done": False}

        def reader_hook(payload):
            if not added["done"]:
                added["done"] = True
                (self.proj / "src/new_module.py").write_text("def fresh():\n    return 1\n")
            return self.model_inner(payload)
        self.model_inner = self.model
        self.model = reader_hook
        self.run_cli(commands, ["yes", "yes"])
        first, second = self.payloads()
        self.assertEqual(second["project"]["project"]["files"], first["project"]["project"]["files"] + 1)
        self.assertIn("Index refreshed: 4 file(s) (was 3)", self.output)
        self.assertIn("Index refreshed: 4 file(s).", self.output)

    def test_a_stopped_task_is_remembered_so_the_next_question_can_continue(self):
        self.model = script({"action": "read", "path": "src/util.py", "ranges": [{"first": 1, "last": 2}]},
                            *[{"action": "find", "fragment": "app%d" % i, "cursor": 0} for i in range(7)],
                            {"action": "answer", "text": "Kontynuacja.", "sources": []})
        self.run_cli(["Duże zadanie?", "kontynuuj", "/exit"], ["yes", "yes"])
        self.assertIn('Type e.g. "continue"', self.output)
        history = self.payloads()[-1]["project"]["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["answer"], "[Task stopped without an answer. Claude received: parts of 1 file(s) (%d B)]"
                         % len("def helper():\n    return 1\n"))
        self.assertNotIn("util.py", history[0]["answer"])            # never quotes file names (they are untrusted)
        self.assertEqual(history[0]["question"], "Duże zadanie?")

    def test_a_stopped_task_with_nothing_delivered_leaves_no_history_turn(self):
        self.model = script(*[{"action": "find", "fragment": "app%d" % i, "cursor": 0} for i in range(8)],
                            {"action": "answer", "text": "Dalej.", "sources": []})
        self.run_cli(["Zadanie?", "jeszcze raz", "/exit"], ["yes", "yes"])
        self.assertEqual(self.payloads()[-1]["project"]["history"], [])

    def test_a_multi_page_search_is_not_mistaken_for_a_repeat(self):
        for i in range(30):
            (self.proj / "docs" / ("n%02d.txt" % i)).write_text("NEEDLE\n" * 3)
        self.ask({"action": "search", "needle": "NEEDLE", "continue": False},
                 {"action": "search", "needle": "NEEDLE", "continue": True},
                 {"action": "search", "needle": "NEEDLE", "continue": True},
                 {"action": "answer", "text": "Trzy strony.", "sources": []})
        self.assertNotIn("repeated", self.output)
        self.assertEqual(len(self.sent), 4)

    def test_a_read_refused_as_changed_may_be_retried_once_and_a_same_size_edit_is_flagged(self):
        target = self.proj / "src/util.py"
        read = {"action": "read", "path": "src/util.py", "ranges": [{"first": 1, "last": 2}]}
        other = {"action": "read", "path": "src/util.py", "ranges": [{"first": 1, "last": 1}]}
        state = {"n": 0}
        base = script(read, other, other, {"action": "answer", "text": "Gotowe.", "sources": []})

        def model(payload):
            state["n"] += 1
            if state["n"] == 2:
                target.write_text("def helper():\n    return 2\n")         # same size, different bytes
            return base(payload)
        self.model = model
        self.run_cli(["Co robi helper?", "/exit"], ["yes"])
        self.assertNotIn("repeated", self.output)
        self.assertEqual(len(self.sent), 4)
        self.assertRegex(self.output, r"Changed\s+src/util.py \(during the task or after the scan\)")

    def test_access_revoked_after_an_answer_stops_the_next_question_and_leaks_no_names(self):
        revoked = 'version = 1\n[files]\nread = []\ndeny = ["~/proj/"]\n[network]\nclaude = false\n'
        state = {"n": 0}

        def model(payload):
            state["n"] += 1
            if state["n"] == 1:                                          # answered, then access is withdrawn
                self.config.write_text(revoked)
                self.config.chmod(0o600)
            return '{"action":"answer","text":"Gotowe.","sources":[]}'
        self.model = model
        self.run_cli(["Pierwsze?", "Drugie?", "/exit"], ["yes", "yes"])
        self.assertEqual(len(self.sent), 1)                              # only the request approved before the change
        self.assertNotIn("util.py", json.dumps(self.payloads()[1:]))

    def test_local_helpers_rescan_and_report_a_changed_policy_correctly(self):
        def commands():
            yield "/files"
            (self.proj / "src/late.py").write_text("late marker\n")
            yield "/search late marker"
            yield "/exit"
        self.run_cli(commands())
        self.assertIn("Index refreshed: 4 file(s) (was 3)", self.output)
        self.assertIn("src/late.py:1: late marker", self.output)

    def test_ledger_and_conversation_memory_cover_the_task(self):
        self.model = script({"action": "answer", "text": "Odpowiedź 1.", "sources": []},
                            {"action": "answer", "text": "Odpowiedź 2.", "sources": []})
        self.run_cli(["Pytanie pierwsze?", "Pytanie drugie?", "/log", "/exit"], ["yes", "yes"])
        self.assertEqual(len(self.payloads()[1]["project"]["history"]), 1)
        self.assertIn("state: answered", self.output)

    def test_delivery_failure_marks_unknown_and_is_not_retried(self):
        self.send_error = AnalysisDenied("network")
        self.model = script({"action": "list", "prefix": "", "cursor": 0})
        self.run_cli(["Pytanie?", "/log", "/exit"], ["yes"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("state: unknown", self.output)
        self.assertIn("will not be retried", self.output)

    def test_the_models_working_notes_come_back_with_the_next_question(self):
        self.model = script({"action": "answer", "text": "Zrobione.", "sources": [], "progress": "Krok 1 gotowy; dalej: testy."},
                            {"action": "answer", "text": "Kontynuuję.", "sources": []})
        self.run_cli(["Zacznij", "rób dalej", "/exit"], ["yes", "yes"])
        first, second = self.payloads()
        self.assertEqual(first["project"]["project"]["progress"], "")
        self.assertEqual(second["project"]["project"]["progress"], "Krok 1 gotowy; dalej: testy.")

    def test_oversized_or_non_text_progress_is_refused(self):
        for progress in ("x" * 2049, "bad\x1b", 5):
            raw = json.dumps({"action": "answer", "text": "a", "sources": [], "progress": progress})
            with self.subTest(progress=str(progress)[:10]), self.assertRaises(ValueError):
                pstep.parse_decision(raw)

    def test_a_pasted_block_travels_as_a_marked_observation_and_is_listed_in_the_preview(self):
        from ai_desktop import question_input as qi
        code = "def helper():\n    return 42\n"

        class Seeded(qi.PromptSession):
            def __init__(self):
                super().__init__()
                self._add_paste(code)
        marker = qi.paste_token(1, code)
        self.model = script({"action": "answer", "text": "Funkcja zwraca 42.", "sources": []})
        with patch.object(pcli, "PromptSession", Seeded):
            self.run_cli(["co robi ten kod? " + marker, "/exit"], ["yes"])
        (request,) = self.payloads()
        self.assertEqual(request["question"], "co robi ten kod? " + marker)
        pasted = [o for o in request["project"]["observations"] if o["kind"] == "pasted"]
        self.assertEqual(pasted, [{"kind": "pasted", "id": 1, "lines": 3, "text": code}])
        self.assertIn("pastes: #1 (3 lines, 28 B)", self.output)
        self.model = script({"action": "answer", "text": "Bez wklejki.", "sources": []})
        with patch.object(pcli, "PromptSession", Seeded):
            self.run_cli(["co robi ten kod? (usunięto znacznik)", "/exit"], ["yes"])
        self.assertEqual([o for o in self.payloads()[-1]["project"]["observations"] if o["kind"] == "pasted"], [])

    def test_pasted_observations_are_validated_and_never_dropped_by_compaction(self):
        project = ps.Project(p.load_policy(), str(self.proj)).scan()
        loop = pl.Loop(project, pasted=[(1, "a\nb")])
        for i in range(30):
            loop.observations.append({"kind": "search", "needle": "n%d" % i, "complete": True, "more": False, "hits": [],
                                      "skipped": {"large": 0, "binary": 0, "unreadable": 0}})
        loop._compact()
        self.assertTrue(any(o["kind"] == "pasted" for o in loop.observations))
        pstep.validate_input(json.loads(json.dumps(loop.request_input(1, 1000))))
        for broken in ({"kind": "pasted", "id": 0, "lines": 1, "text": "a"}, {"kind": "pasted", "id": 1, "lines": 5, "text": "a"},
                       {"kind": "pasted", "id": 1, "lines": 1, "text": "a\x1b"}, {"kind": "pasted", "id": 1, "lines": 1, "text": "x" * 40000}):
            request = json.loads(json.dumps(loop.request_input(1, 1000)))
            request["observations"] = [broken]
            with self.subTest(broken=str(broken)[:40]), self.assertRaises(ValueError):
                pstep.validate_input(request)

    def test_the_first_request_already_carries_a_map_of_the_whole_project(self):
        (self.proj / "README.md").write_text("# Projekt\n")
        self.ask({"action": "answer", "text": "Mapa.", "sources": []})
        (view,) = self.payloads()[0]["project"]["observations"]
        self.assertEqual(view["kind"], "overview")
        self.assertEqual(view["files"], 4)                                            # README, app, util, readme.txt (secret/ is denied)
        self.assertEqual({e["name"] for e in view["top"]}, {"README.md", "docs", "src"})
        self.assertTrue(next(e for e in view["top"] if e["name"] == "src")["dir"])
        self.assertIn({"ext": ".py", "files": 2}, view["extensions"])
        self.assertIn("README.md", view["key_files"])
        self.assertNotIn("secret", json.dumps(view).lower())                          # denied names never reach the model
        self.assertNotIn("TOP-SECRET", json.dumps(view))

    def test_listing_a_directory_returns_one_level_with_aggregates_and_a_repeated_overview_is_a_repeat(self):
        self.ask({"action": "list", "prefix": "src", "cursor": 0}, {"action": "overview"},
                 {"action": "answer", "text": "Gotowe.", "sources": []})
        self.assertIn("repeated", self.output)                                       # overview was preloaded, asking again is no progress
        tree = [o for o in self.payloads()[-1]["project"]["observations"] if o["kind"] == "tree"][0]
        self.assertEqual(tree["prefix"], "src")
        self.assertEqual([(e["name"], e["dir"]) for e in tree["items"]], [("app.py", False), ("util.py", False)])

    def test_pagination_cursor_and_search_continuation_are_enforced_by_the_controller(self):
        for i in range(130):
            (self.proj / "docs").mkdir(exist_ok=True)
            (self.proj / "docs" / ("n%03d.txt" % i)).write_text("x\n")
        self.ask({"action": "list", "prefix": "docs", "cursor": 0}, {"action": "list", "prefix": "docs", "cursor": 100},
                 {"action": "search", "needle": "zzz", "continue": True},
                 {"action": "answer", "text": "Gotowe.", "sources": []})
        observed = self.payloads()[-1]["project"]["observations"]
        trees = [o for o in observed if o["kind"] == "tree"]
        self.assertEqual([len(o["items"]) for o in trees], [100, 31])
        self.assertEqual(trees[0]["next"], 100)
        self.assertIsNone(trees[1]["next"])
        self.assertEqual(observed[-1], {"kind": "refused", "action": "search", "code": "LIMIT"})


class EditTests(Base):
    """Edit proposals map onto the owner's write policy and hand over to the proven ask/task pipeline (mocked here)."""

    def setUp(self):
        super().setUp()
        self.config.write_text('version = 1\n[files]\nread = ["~/proj/**"]\nwrite = ["~/proj/**"]\ndeny = ["~/proj/secret/**"]\n'
                               '[network]\nclaude = true\n')
        self.config.chmod(0o600)
        self.calls = []

    def snapshot(self):
        return pcli.assistant_flow.capture_access()

    def plan(self, **decision):
        project = ps.Project(p.load_policy(), str(self.proj)).scan()
        base = {"files": [], "create": [], "check": "text"}
        return pcli.build_edit_plan(project, self.snapshot(), dict(base, **decision))

    def test_a_change_is_mapped_by_the_controller_onto_configured_scopes(self):
        plan, existing, created = self.plan(files=["src/util.py"], create=["docs/new.txt"], check="python-syntax")
        self.assertEqual(plan, {"action": "edit", "write": "w0", "files": [{"id": "f0", "path": "src/util.py"}],
                                "create": ["docs/new.txt"], "check": "python-syntax"})
        self.assertEqual((existing, created), (["src/util.py"], ["docs/new.txt"]))

    def test_unmappable_proposals_are_refused_with_a_reason(self):
        for decision, reason in (({"files": ["src/nope.py"]}, "not in the project index"),
                                 ({"create": ["src/util.py"]}, "already exists"),
                                 ({"create": ["missing/new.py"]}, "does not exist"),
                                 ({"files": ["secret/key.txt"]}, "not in the project index")):
            with self.subTest(decision=decision), self.assertRaises(pcli.EditRefused) as caught:
                self.plan(**decision)
            self.assertIn(reason, str(caught.exception))
        (self.proj / "src/big.txt").write_text("x" * (8 * 1024 * 1024 + 1))
        with self.assertRaises(pcli.EditRefused) as caught:
            self.plan(files=["src/big.txt"])
        self.assertIn("8 MiB", str(caught.exception))
        deep = self.proj / "a/b/c/d/e"
        deep.mkdir(parents=True)
        (deep / "f.py").write_text("x\n")
        with self.assertRaises(pcli.EditRefused) as caught:
            self.plan(files=["a/b/c/d/e/f.py"])
        self.assertIn("5 levels", str(caught.exception))

    def test_no_write_scope_for_the_project_means_no_edit(self):
        self.config.write_text('version = 1\n[files]\nread = ["~/proj/**"]\ndeny = []\n[network]\nclaude = true\n')
        self.config.chmod(0o600)
        with self.assertRaises(pcli.EditRefused) as caught:
            self.plan(create=["docs/new.txt"])
        self.assertIn("files.write", str(caught.exception))

    def run_edit(self, answers, edit_decision=None):
        edit_decision = edit_decision or {"action": "edit", "files": ["src/util.py"], "create": [], "check": "python-syntax"}
        self.model = script({"action": "read", "path": "src/util.py", "ranges": [{"first": 1, "last": 2}]}, edit_decision)
        prepared = pcli.ask_edit.PreparedEdit({}, {"files": []}, "python-syntax", str(self.proj), object())
        with patch.object(pcli.ask_edit, "prepare", side_effect=lambda *a, **k: self.calls.append(("prepare", a[0])) or prepared), \
                patch.object(pcli.ask_cli, "request", side_effect=lambda *a, **k: self.calls.append(("request", a[0])) or "{}"), \
                patch.object(pcli.ask_edit, "finish", side_effect=lambda *a, **k: self.calls.append(("finish", None)) or pcli.ask_edit.CompletedTask(0)):
            self.run_cli(["Dodaj komentarz w util.py", "/exit"], answers)

    def test_the_outcome_appears_in_history_within_the_same_session(self):
        self.model = script({"action": "read", "path": "src/util.py", "ranges": [{"first": 1, "last": 2}]},
                            {"action": "edit", "files": ["src/util.py"], "create": [], "check": "text"},
                            {"action": "answer", "text": "Dalej.", "sources": [], "progress": "Zrobiono: komentarz w util.py. Dalej: testy."})
        prepared = pcli.ask_edit.PreparedEdit({}, {"files": []}, "text", str(self.proj), object())
        with patch.object(pcli.ask_edit, "prepare", return_value=prepared), patch.object(pcli.ask_cli, "request", return_value="{}"), \
                patch.object(pcli.ask_edit, "finish", return_value=pcli.ask_edit.CompletedTask(0)):
            self.run_cli(["Dodaj komentarz", "tak, i rób dalej", "/exit"], ["yes", "yes", "yes"])
        followup = self.payloads()[-1]["project"]
        self.assertEqual(followup["history"][0]["answer"], "[change saved after the user's consent]")

    def test_the_real_consent_chain_accepts_the_edit_request_without_a_planner_round(self):
        """prepare + request run for real (only transport, session and the executor's finish are replaced)."""
        from contextlib import nullcontext
        seen = []

        def transport(approval):
            seen.append(json.loads(approval.payload))
            return json.dumps({"summary": "Dodano plik.", "files": [{"path": "hello.py", "content": "print('hello world')\n"}]})

        class Indicator:
            def __init__(self, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def phase(self, name): pass
        finished = []
        self.model = script({"action": "edit", "files": [], "create": ["hello.py"], "check": "python-syntax"})
        with patch.object(pcli.ask_cli, "managed_session", lambda **k: nullcontext()), \
                patch.object(pcli.ask_cli, "send_once", transport), patch.object(pcli.ask_cli, "installation_ready"), \
                patch.object(pcli.ask_cli, "WaitingIndicator", Indicator), \
                patch.object(pcli.ask_edit, "finish", side_effect=lambda *a, **k: finished.append(a[1]) or pcli.ask_edit.CompletedTask(0)):
            self.run_cli(["utwórz hello.py który wypisze hello world", "/exit"], ["yes", "yes"])
        self.assertNotIn("onsent expired", self.output)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["mode"], "workspace")                       # straight to the workspace request
        self.assertEqual(seen[0]["task"]["instruction"], "utwórz hello.py który wypisze hello world")
        self.assertEqual([f["path"] for f in seen[0]["task"]["files"]], ["hello.py"])
        self.assertEqual(len(finished), 1)
        self.assertFalse((self.proj / "hello.py").exists())                  # nothing is written by `project` itself

    def run_delete(self, answers, paths=("docs/readme.txt",)):
        self.model = script({"action": "edit", "files": [], "create": [], "delete": list(paths), "check": "text"})
        self.run_cli(["usuń readme", "/exit"], answers)

    def test_deleting_moves_the_file_to_the_trash_after_the_owners_own_yes_and_it_can_be_restored(self):
        target = self.proj / "docs/readme.txt"
        self.run_delete(["yes", "yes"])
        self.assertFalse(target.exists())
        self.assertIn(PROMPTS["project-delete"], self.prompts)
        self.assertRegex(self.output, r"To delete\s+docs/readme.txt \(14 B\)")
        self.assertIn("Deleted 1 file(s) (in the task trash).", self.output)
        restore = [l for l in self.output.splitlines() if l.startswith("Restore: ai-desktop task restore")][0]
        self.assertIn(str(self.proj), restore)
        plan = restore.rsplit(" ", 1)[1]
        self.assertTrue(pcli.workspace_trash.restore(str(self.proj), plan))
        self.assertEqual(target.read_text(), "Project notes\n")
        self.assertEqual(len(self.sent), 1)                                   # nothing about the deletion went to Claude

    def test_declining_a_deletion_or_an_unmappable_one_removes_nothing(self):
        target = self.proj / "docs/readme.txt"
        self.run_delete(["yes", "no"])
        self.assertTrue(target.exists())
        self.assertIn("Cancelled. Nothing was deleted.", self.output)
        self.run_delete(["yes"], paths=("docs/missing.txt",))
        self.assertRegex(self.output, r"Refused\s+Claude named a file that is not in the project index")
        self.run_delete(["yes"], paths=("secret/key.txt",))
        self.assertTrue((self.proj / "secret/key.txt").exists())
        self.assertNotIn(PROMPTS["project-delete"], self.prompts)

    def test_locations_the_write_policy_always_protects_cannot_be_deleted_even_if_readable(self):
        (self.proj / "backup").mkdir()
        (self.proj / "backup/old.txt").write_text("keep\n")
        self.run_delete(["yes"], paths=("backup/old.txt",))
        self.assertRegex(self.output, r"Refused\s+This deletion is outside the scopes")
        self.assertTrue((self.proj / "backup/old.txt").exists())
        self.assertNotIn(PROMPTS["project-delete"], self.prompts)

    def test_the_task_restore_command_brings_the_files_back_only_after_a_yes(self):
        from ai_desktop import task_cli
        self.run_delete(["yes", "yes"])
        plan = [l for l in self.output.splitlines() if l.startswith("Restore:")][0].rsplit(" ", 1)[1]
        argv = ["restore", "--workspace", str(self.proj), "--plan", plan]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(task_cli.sys.stdin, "isatty", return_value=True), \
                patch.object(task_cli.sys.stdout, "isatty", return_value=True):
            self.assertEqual(task_cli.main(argv, reader=lambda prompt: "no"), 2)
        self.assertFalse((self.proj / "docs/readme.txt").exists())
        with contextlib.redirect_stdout(out), patch.object(task_cli.sys.stdin, "isatty", return_value=True), \
                patch.object(task_cli.sys.stdout, "isatty", return_value=True):
            self.assertEqual(task_cli.main(argv, reader=lambda prompt: "yes"), 0)
        self.assertTrue((self.proj / "docs/readme.txt").exists())
        self.assertIn("Deleted files restored.", out.getvalue())

    def test_delete_is_exclusive_with_other_edit_fields_and_bounded(self):
        for raw in ('{"action":"edit","files":["a"],"create":[],"delete":["b"],"check":"text"}',
                    '{"action":"edit","files":[],"create":["a"],"delete":["b"],"check":"text"}',
                    '{"action":"edit","files":[],"create":[],"delete":["a/../b"],"check":"text"}',
                    '{"action":"edit","files":[],"create":[],"delete":%s,"check":"text"}' % json.dumps(["f%d" % i for i in range(9)]),
                    '{"action":"edit","files":[],"create":[],"delete":"a","check":"text"}'):
            with self.subTest(raw=raw[:60]), self.assertRaises(ValueError):
                pstep.parse_decision(raw)
        decision = pstep.parse_decision('{"action":"edit","files":[],"create":[],"delete":["x.py"],"check":"text"}')
        self.assertEqual(decision["delete"], ["x.py"])

    def test_the_pasted_text_itself_travels_in_the_edit_request_not_just_its_marker(self):
        from ai_desktop import question_input as qi
        body = "Lorem ipsum dolor sit amet, consectetur adipiscing elit."

        class Seeded(qi.PromptSession):
            def __init__(self):
                super().__init__()
                self._add_paste(body)
        marker = qi.paste_token(1, body)
        self.model = script({"action": "edit", "files": [], "create": ["a.txt"], "check": "text"})
        prepared = pcli.ask_edit.PreparedEdit({}, {"files": []}, "text", str(self.proj), object())
        with patch.object(pcli, "PromptSession", Seeded), \
                patch.object(pcli.ask_edit, "prepare", return_value=prepared) as prepare, \
                patch.object(pcli.ask_cli, "request", return_value="{}") as request, \
                patch.object(pcli.ask_edit, "finish", return_value=pcli.ask_edit.CompletedTask(0)):
            self.run_cli(["stwórz a.txt i wpisz do niego: " + marker, "/exit"], ["yes"])
        instruction = request.call_args.args[0]
        self.assertEqual(instruction, "stwórz a.txt i wpisz do niego: " + body)
        self.assertNotIn("paste", instruction)
        prepare.assert_called_once()

    def test_a_paste_too_big_for_an_edit_request_is_refused_before_anything_is_sent(self):
        from ai_desktop import question_input as qi
        body = "x" * 2100

        class Seeded(qi.PromptSession):
            def __init__(self):
                super().__init__()
                self._add_paste(body)
        self.model = script({"action": "edit", "files": [], "create": ["a.txt"], "check": "text"})
        with patch.object(pcli, "PromptSession", Seeded), patch.object(pcli.ask_cli, "request") as request, \
                patch.object(pcli.task_contract, "MAX_INSTRUCTION", 2048):
            self.run_cli(["wpisz: " + qi.paste_token(1, body), "/exit"], ["yes"])
        request.assert_not_called()
        self.assertRegex(self.output, r"Refused\s+The file change request with its paste is over 2048 B")

    def test_an_unfinished_earlier_task_is_named_instead_of_a_generic_failure(self):
        with patch.object(pcli.task_workspace, "_can_apply", side_effect=ValueError("recovery required")), \
                patch.object(pcli.ask_cli, "request") as request:
            self.run_edit(["yes"], {"action": "edit", "files": [], "create": ["docs/new.txt"], "check": "text"})
        self.assertRegex(self.output, r"Refused\s+An earlier file change task is unfinished")
        self.assertIn("ai-desktop task status", self.output)
        request.assert_not_called()
        with patch.object(pcli.task_workspace, "_can_apply", side_effect=pcli.task_workspace.JournalLimit("journal limit")):
            self.run_edit(["yes"], {"action": "edit", "files": [], "create": ["docs/new.txt"], "check": "text"})
        self.assertIn("limit of saved tasks", self.output)

    def test_declining_the_edit_of_a_file_claude_has_not_seen_sends_nothing_and_writes_nothing(self):
        self.run_edit(["yes", "no"], {"action": "edit", "files": ["docs/readme.txt"], "create": [], "check": "text"})
        self.assertEqual(self.calls, [])
        self.assertIn("── File change ", self.output)
        self.assertRegex(self.output, r"To change\s+docs/readme.txt")
        self.assertIn("Cancelled. File contents were not sent. Nothing was saved.", self.output)
        self.assertIn(PROMPTS["project-edit"], self.prompts)

    def test_a_new_file_or_a_file_already_read_whole_needs_no_second_question(self):
        self.run_edit(["yes"])                                                  # util.py was read in full during the task
        self.assertEqual([c[0] for c in self.calls], ["prepare", "request", "finish"])
        self.assertNotIn(PROMPTS["project-edit"], self.prompts)
        self.assertIn("File change — to change: src/util.py · check: python-syntax. Preparing the diff…", self.output)
        self.calls.clear()
        self.run_edit(["yes"], {"action": "edit", "files": [], "create": ["docs/new.txt"], "check": "text"})
        self.assertEqual([c[0] for c in self.calls], ["prepare", "request", "finish"])
        self.assertNotIn(PROMPTS["project-edit"], self.prompts)

    def test_accepting_hands_the_mapped_plan_to_the_existing_edit_pipeline_with_the_users_own_words(self):
        self.run_edit(["yes", "yes"])
        self.assertEqual([c[0] for c in self.calls], ["prepare", "request", "finish"])
        self.assertEqual(self.calls[0][1]["write"], "w0")                  # the plan argument of prepare
        self.assertEqual(self.calls[1][1], "Dodaj komentarz w util.py")     # the instruction is the user's question, verbatim
        self.assertEqual(len(self.sent), 2)                                  # only the investigation steps used the project transport

    def test_an_edit_the_policy_cannot_express_never_reaches_the_pipeline(self):
        self.run_edit(["yes"], {"action": "edit", "files": [], "create": ["missing/new.py"], "check": "text"})
        self.assertEqual(self.calls, [])
        self.assertRegex(self.output, r"Refused\s+Directory missing does not exist")

    def test_a_malformed_edit_decision_ends_the_task_without_a_retry(self):
        for raw in ('{"action":"edit","files":[],"create":[],"check":"text"}',
                    '{"action":"edit","files":["../x"],"create":[],"check":"text"}',
                    '{"action":"edit","files":["a"],"create":[],"check":"rm -rf"}',
                    '{"action":"edit","files":["a"],"create":["a"],"check":"text"}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                pstep.parse_decision(raw)


class ContractTests(Base):
    def test_requests_are_strict_on_both_sides_and_modes_never_mix(self):
        project = ps.Project(p.load_policy(), str(self.proj)).scan()
        loop = pl.Loop(project)
        payload = AnalysisConsent(None, question="Pytanie?", project=loop.request_input(1, 1000)).payload
        parse_request(payload.encode("utf-8"))
        data = json.loads(payload)
        for mutate in (lambda d: d["project"].update(extra=1), lambda d: d["project"]["task"].update(step=0),
                       lambda d: d["project"]["observations"].append({"kind": "names"}),
                       lambda d: d["project"]["observations"].append(
                           {"kind": "read", "path": "../x", "first": 1, "last": 1, "lines": 1,
                            "ref": {}, "text": "x"})):
            broken = json.loads(payload)
            mutate(broken)
            with self.subTest(), self.assertRaises(Exception):
                parse_request(json.dumps(broken, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        with self.assertRaises(ValueError):
            AnalysisConsent(None, question="Pytanie?", project=data["project"], material=data["project"])
        with self.assertRaises(ValueError):
            AnalysisConsent(None, question="", project=data["project"])

    def test_stage_visited_and_continue_are_validated(self):
        project = ps.Project(p.load_policy(), str(self.proj)).scan()
        good = json.loads(json.dumps(pl.Loop(project, stage=2, visited=["src/util.py"]).request_input(1, 1000)))
        pstep.validate_input(good)
        for mutate in (lambda d: d["project"].update(stage=6), lambda d: d["project"].update(stage=0),
                       lambda d: d["project"].update(visited=["../x"]), lambda d: d["project"].update(visited=["a"] * 61),
                       lambda d: d["project"].pop("visited")):
            broken = json.loads(json.dumps(good))
            mutate(broken)
            with self.assertRaises(ValueError):
                pstep.validate_input(broken)
        with self.assertRaises(ValueError):
            pstep.parse_decision('{"action":"answer","text":"x","sources":[],"continue":"yes"}')
        self.assertTrue(pstep.parse_decision('{"action":"answer","text":"x","sources":[],"continue":true}')["continue"])
        self.assertFalse(pstep.parse_decision('{"action":"answer","text":"x","sources":[]}')["continue"])

    def test_the_instruction_limits_edits_to_explicit_requests(self):
        from ai_desktop.analysis_contract import SYSTEM_PROMPT
        self.assertIn("Use edit only when the user explicitly asks to change", SYSTEM_PROMPT)
        self.assertIn("'continue' or 'keep going' concern the analysis", SYSTEM_PROMPT)

    def test_decisions_are_a_closed_set(self):
        good = ['{"action":"list","prefix":"","cursor":0}', '{"action":"find","fragment":"a","cursor":0}',
                '{"action":"search","needle":"a","continue":false}',
                '{"action":"read","path":"a/b.py","ranges":[{"first":1,"last":2}]}',
                '{"action":"answer","text":"x","sources":[{"path":"a.py","first":1,"last":1}]}',
                '{"action":"ask_user","text":"?"}']
        for raw in good:
            with self.subTest(raw=raw):
                pstep.parse_decision(raw)
        bad = ['{"action":"whole"}', '{"action":"read","path":"/etc/passwd","ranges":[{"first":1,"last":1}]}',
               '{"action":"read","path":"a/../b","ranges":[{"first":1,"last":1}]}',
               '{"action":"read","path":"a","ranges":[]}', '{"action":"list","prefix":"..","cursor":0}',
               '{"action":"search","needle":"a","continue":"yes"}', '{"action":"list","prefix":"","cursor":-1}',
               '{"action":"list","action":"find"}', '{"action":"exec","cmd":"id"}', "NaN", '{"action":"list","prefix":"","cursor":0,"x":1}']
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                pstep.parse_decision(raw)

    def test_oversize_or_tampered_observations_are_refused(self):
        project = ps.Project(p.load_policy(), str(self.proj)).scan()
        loop = pl.Loop(project)
        loop.execute({"action": "read", "path": "src/util.py", "ranges": [(1, 2)]})
        good = loop.request_input(1, 1000)
        pstep.validate_input(json.loads(json.dumps(good)))
        tampered = json.loads(json.dumps(good))
        tampered["observations"][0]["text"] = "def helper():\n    return 9\n"
        with self.assertRaises(ValueError):
            pstep.validate_input(tampered)


if __name__ == "__main__":
    unittest.main()
