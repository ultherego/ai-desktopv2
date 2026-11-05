"""Saved conversation threads: saving with consent, picking one at the start of `ask`, ticking and deleting many."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
import fcntl
import io
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from ai_desktop import ask_cli, conversation_memory as cm, conversation_sessions as sessions, conversation_state as cs
from ai_desktop import session_cli, terminal_menu

ROOT = Path(__file__).resolve().parents[1]
PROFILE = {"provider": "claude", "model": "default", "effort": "default", "instructions": "", "profile": "default",
           "access": "changes"}
OTHER = dict(PROFILE, profile="other")


def talk(*questions):
    conversation = cs.Conversation()
    for text in questions:
        conversation.finish(text, "Odpowiedź na: " + text, None, [])
    return conversation


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aid-threads-", dir=Path.home())
        self.addCleanup(self.temporary.cleanup)
        self.store = sessions.Store(home=self.temporary.name)

    def thread(self, *questions, profile=PROFILE, kind="ask"):
        value = self.store.create(kind, "", "", profile)
        if kind == "ask":
            value.save_state(cs.export(talk(*questions)))
        ident = value.session_id
        value.close()
        return ident


class TitleAndLabelTests(StoreCase):
    def test_title_is_the_users_first_message_shortened_and_stored_with_the_record(self):
        ident = self.thread("Chcę zrobić undervolting dla gry. Korzystam z LACT.\nDrugi wiersz", "Druga")
        record = self.store.read(ident)
        self.assertEqual(record["title"], "Chcę zrobić undervolting dla gry. Korzystam z LACT. Drugi wiersz")
        self.assertIn("exchanges: 2", session_cli.thread_label(record))
        self.assertIn("Chcę zrobić undervolting", session_cli.thread_label(record))
        self.assertEqual(len(cs.title_of("a" * 500)), 80)

    def test_a_record_without_a_title_is_labelled_by_its_first_question(self):
        ident = self.thread("Pytanie bez tytułu")
        record = self.store.read(ident)
        record.pop("title")
        self.assertIn("Pytanie bez tytułu", session_cli.thread_label(record))

    def test_titles_with_control_characters_or_excess_length_are_refused(self):
        value = self.store.create("ask", "", "", PROFILE)
        for bad in ("a\nb", "a\tb", "x" * 81, "\x1b[31m"):
            record = value.record
            record["title"] = bad
            with self.subTest(title=repr(bad)), self.assertRaises(sessions.SessionError):
                self.store.update(record, record["revision"])
        value.close()


class SaveOfferTests(StoreCase):
    def offer(self, answer, conversation=None):
        phases = []
        with patch("ai_desktop.terminal_consent.confirm", return_value=answer) as confirm, \
                patch("ai_desktop.conversation_sessions.Store", return_value=self.store), \
                patch("ai_desktop.execution.current", return_value=PROFILE), redirect_stdout(io.StringIO()) as out:
            saved = ask_cli._offer_save(conversation or talk("Chcę ustalić, dlaczego start jest wolny"), phases.append)
        return saved, confirm, out.getvalue(), phases

    def test_a_yes_saves_the_whole_state_as_a_thread(self):
        saved, confirm, out, phases = self.offer(True)
        confirm.assert_called_once_with("save-thread")
        self.assertIsNotNone(saved)
        saved.close()
        record = self.store.read(saved.session_id)
        self.assertEqual(record["title"], "Chcę ustalić, dlaczego start jest wolny")
        self.assertTrue(record["memory"]["entries"])
        self.assertIn("saved locally", out)
        self.assertEqual(phases, ["session-save"])

    def test_a_no_writes_nothing_at_all(self):
        saved, confirm, out, _ = self.offer(False)
        self.assertIsNone(saved)
        self.assertEqual(self.store.list(), [])

    def test_a_failing_store_reports_and_does_not_end_the_command(self):
        with patch("ai_desktop.terminal_consent.confirm", return_value=True), \
                patch("ai_desktop.conversation_sessions.Store", side_effect=sessions.SessionError("session-file")), \
                patch("ai_desktop.execution.current", return_value=PROFILE), redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(ask_cli._offer_save(talk("x y"), lambda value: None))
        self.assertIn("Nothing was saved", out.getvalue())

    def test_the_prompt_never_names_a_provider(self):
        from ai_desktop.terminal_consent import PROMPTS
        self.assertNotIn("Claude", PROMPTS["save-thread"])
        self.assertNotIn("Codex", PROMPTS["save-thread"])


class PickThreadTests(StoreCase):
    def pick(self, choices):
        queue, seen = list(choices), []
        def fake(title, options, **kwargs):
            seen.append((title, tuple(options)))
            return queue.pop(0)
        with patch.object(session_cli, "choose", side_effect=fake), patch.object(session_cli, "_manage") as manage:
            result = session_cli.select_thread(self.store, PROFILE)
        return result, seen, manage

    def test_no_saved_threads_means_a_new_conversation_without_any_menu(self):
        result, seen, _ = self.pick([])
        self.assertEqual((result, seen), (("new", None), []))

    def test_only_threads_of_this_agent_and_kind_are_offered_newest_first(self):
        mine = self.thread("Moja pierwsza")
        self.thread("Cudza", profile=OTHER)
        newer = self.thread("Moja nowsza")
        result, seen, _ = self.pick([2])
        options = seen[0][1]
        self.assertEqual(options[0], "New conversation")
        self.assertEqual(options[-1], "Manage saved conversations")
        self.assertEqual(len(options), 4)
        self.assertNotIn("Cudza", " ".join(options))
        self.assertEqual(result[0], "resume")
        self.assertIn(result[1], (mine, newer))

    def test_new_cancel_and_manage_loop(self):
        self.thread("Jeden")
        self.assertEqual(self.pick([0])[0], ("new", None))
        self.assertIsNone(self.pick([None])[0])
        result, seen, manage = self.pick([2, 0])                              # manage, then 'new'
        manage.assert_called_once()
        self.assertEqual(result, ("new", None))
        self.assertEqual(len(seen), 2)


class StartupFallbackTests(unittest.TestCase):
    def test_any_trouble_with_the_store_or_the_menu_means_a_new_conversation(self):
        for error in (sessions.SessionError("session-file"), terminal_menu.MenuInputError("menu-terminal"), OSError("x")):
            with self.subTest(error=type(error).__name__), \
                    patch("ai_desktop.session_cli.select_thread", side_effect=error), \
                    patch("ai_desktop.execution.current", return_value=PROFILE):
                self.assertEqual(ask_cli._choose_thread(), ("new", None))


class ManageTests(StoreCase):
    def run_manage(self, steps):
        """steps: list of (marks to add as indexes, choice) for each menu call; confirmations are plain choices."""
        queue = list(steps)
        def fake(title, options, *, marks=None, markable=0, **kwargs):
            tick, choice = queue.pop(0)
            if marks is not None:
                marks.symmetric_difference_update(tick)
            return choice
        with patch.object(session_cli, "choose", side_effect=fake), redirect_stdout(io.StringIO()) as out:
            session_cli._manage(self.store)
        self.assertEqual(queue, [])
        return out.getvalue()

    def test_ticked_threads_are_deleted_after_a_confirmation_and_the_rest_stay(self):
        ids = [self.thread("Pytanie %d" % i) for i in range(4)]
        before = {value["id"]: value["updated"] for value in self.store.list()}
        newest_first = [v["id"] for v in session_cli._values(self.store)]
        # tick rows 0 and 2, go to the first action ('Delete selected (0)' label is rebuilt), confirm 'Yes'
        out = self.run_manage([({0, 2}, 4), (set(), 1), (set(), 3)])
        left = {value["id"] for value in self.store.list()}
        self.assertEqual(left, {newest_first[1], newest_first[3]})
        self.assertIn("Conversations deleted: 2", out)

    def test_declining_the_confirmation_or_leaving_deletes_nothing(self):
        self.thread("Jeden")
        self.thread("Dwa")
        self.run_manage([({0, 1}, 2), (set(), 0), (set(), 3)])                # delete -> 'No, go back', then 'Back'
        self.assertEqual(len(self.store.list()), 2)
        self.run_manage([({0}, None)])                                       # Esc
        self.assertEqual(len(self.store.list()), 2)

    def test_deleting_with_nothing_ticked_says_so(self):
        self.thread("Jeden")
        out = self.run_manage([(set(), 1), (set(), 2)])
        self.assertIn("Nothing selected", out)
        self.assertEqual(len(self.store.list()), 1)

    def test_an_active_thread_is_skipped_not_destroyed(self):
        ident = self.thread("Aktywna")
        other = self.thread("Zwykła")
        lease = self.store.open(ident, "ask", "", "", PROFILE)                # held like a running conversation
        try:
            order = [v["id"] for v in session_cli._values(self.store)]
            out = self.run_manage([({0, 1}, 2), (set(), 1), (set(), 2)])
        finally:
            lease.close()
        self.assertEqual({v["id"] for v in self.store.list()}, {ident})
        self.assertIn("skipped", out)


class MarkMenuPtyTests(unittest.TestCase):
    """The real terminal menu: Space ticks, arrows move, Enter on an action returns it, Esc cancels."""
    SCRIPT = r"""
from ai_desktop.terminal_menu import choose
marks = set()
choice = choose("Conversations", ["a", "b", "c", "Delete selected", "Back"], marks=marks, markable=3)
print("RESULT", choice, sorted(marks), flush=True)
"""

    def run_keys(self, keys):
        master, slave = pty.openpty()
        def own_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        before = termios.tcgetattr(slave)
        process = subprocess.Popen([sys.executable, "-B", "-c", self.SCRIPT], cwd=ROOT, stdin=slave, stdout=slave,
                                   stderr=slave, close_fds=True, preexec_fn=own_terminal)
        output, deadline = bytearray(), time.monotonic() + 10
        try:
            time.sleep(0.4)
            for key in keys:
                os.write(master, key)
                time.sleep(0.12)
            while time.monotonic() < deadline and process.poll() is None:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        output.extend(os.read(master, 8192))
                    except OSError:
                        break
            for _ in range(20):
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        output.extend(os.read(master, 8192))
                    except OSError:
                        break
            process.wait(timeout=3)
            return output.decode("utf-8", "replace"), termios.tcgetattr(slave) == before
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
            os.close(slave)

    def test_space_ticks_arrows_move_and_enter_on_the_action_returns_it(self):
        down = b"\x1b[B"
        text, restored = self.run_keys([down, b" ", down, b" ", down, b"\r"])
        self.assertIn("RESULT 3 [1, 2]", text)
        self.assertIn("[x] b", text)
        self.assertIn("Space ticks", text)
        self.assertTrue(restored)

    def test_space_on_an_action_row_does_not_tick_and_space_twice_unticks(self):
        down = b"\x1b[B"
        text, _ = self.run_keys([b" ", b" ", down, down, down, b" ", b"\r"])
        self.assertIn("RESULT 3 []", text)

    def test_escape_cancels_without_a_choice(self):
        text, restored = self.run_keys([b" ", b"\x1b"])
        self.assertIn("RESULT None [0]", text)
        self.assertTrue(restored)

    def test_without_marks_the_menu_behaves_as_before(self):
        self.assertEqual(terminal_menu._HINT, "↑/↓  Enter selects  Esc cancels")
        with self.assertRaises(terminal_menu.MenuInputError):
            terminal_menu.choose("T", ["a"], marks=[], markable=1)


if __name__ == "__main__":
    unittest.main()


class OrderAndStyleTests(unittest.TestCase):
    def test_saved_threads_are_offered_before_the_first_message_is_asked_for(self):
        events = []
        stream = io.StringIO()
        stream.isatty = lambda: True
        with patch.object(ask_cli.sys.stdin, "isatty", return_value=True), \
                patch.object(ask_cli.os, "geteuid", return_value=os.getuid()), \
                patch.object(ask_cli, "PromptSession"), \
                patch.object(ask_cli, "_choose_thread", side_effect=lambda: events.append("picker") or ("new", None)), \
                patch.object(ask_cli, "_read_message", side_effect=lambda *a, **k: events.append("message") or (_ for _ in ()).throw(KeyboardInterrupt())), \
                redirect_stdout(stream):
            ask_cli.main([])
        self.assertEqual(events, ["picker", "message"])

    def test_colour_only_on_a_real_colour_terminal(self):
        from ai_desktop import terminal_style
        tty = io.StringIO()
        tty.isatty = lambda: True
        with patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=False):
            os.environ.pop("NO_COLOR", None)
            self.assertEqual(terminal_style.paint("x", "red", stream=tty), "\x1b[31mx\x1b[0m")
            self.assertEqual(terminal_style.paint("x", "red", stream=io.StringIO()), "x")
            with patch.dict(os.environ, {"NO_COLOR": "1"}):
                self.assertEqual(terminal_style.paint("x", "red", stream=tty), "x")
        with patch.dict(os.environ, {"TERM": "dumb"}):
            self.assertEqual(terminal_style.paint("x", "red", stream=tty), "x")


class _Stream(io.StringIO):
    def __init__(self, encoding):
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self):
        return self._encoding


class CardTests(unittest.TestCase):
    def test_a_card_never_cuts_a_row_and_keeps_every_line_the_same_width(self):
        from ai_desktop import terminal_style
        command = "journalctl -b -u " + "x" * 200 + " --no-pager"
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            lines = terminal_style.card("Polecenie", [command, "program"], width=60, stream=_Stream("utf-8"))
        body = "".join(line[2:-2].rstrip() for line in lines[1:-1])
        self.assertIn(command, body)
        self.assertEqual(len({len(line) for line in lines}), 1)
        self.assertTrue(lines[0].startswith("╭") and lines[-1].startswith("╰"))

    def test_ascii_fallback_without_a_utf8_terminal(self):
        from ai_desktop import terminal_style
        stream = _Stream("ascii")
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            lines = terminal_style.card("x", ["y"], width=40, stream=stream)
            self.assertEqual(terminal_style.symbol("ok", stream), "+")
        self.assertTrue(lines[0].startswith("+") and lines[1].startswith("|"))
