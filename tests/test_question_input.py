"""Real PTY editing and refusal paths; no Claude, history or user files."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import threading
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_desktop import question_input as editor


ROOT = Path(__file__).resolve().parents[1]


from unittest.mock import patch as _patch
_COLOUR = None


def setUpModule():
    # These tests check editing and flow by exact bytes; colour has its own tests in test_terminal_theme.py.
    global _COLOUR
    _COLOUR = _patch.dict(os.environ, {"NO_COLOR": "1"})
    _COLOUR.start()


def tearDownModule():
    _COLOUR.stop()


class RefusedPasteTests(unittest.TestCase):
    def pipe(self, payload):
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)
        os.write(write_end, payload)
        return read_end, write_end

    def leftover(self, fd):
        return os.read(fd, 65536) if select.select([fd], [], [], 0)[0] else b""

    def test_nothing_of_an_oversized_paste_reaches_the_next_prompt(self):
        fd, write_end = self.pipe(b"x" * 500 + b"\ndruga linia\n" + editor._PASTE_END + b"nowe")
        self.addCleanup(os.close, write_end)
        with self.assertRaisesRegex(editor.QuestionInputError, "question paste"):
            editor._paste(fd, 100, newlines=True)
        self.assertEqual(self.leftover(fd), b"")

    def test_a_forbidden_byte_inside_a_paste_also_throws_away_the_rest(self):
        fd, write_end = self.pipe(b"ok\x07reszta\n" + editor._PASTE_END)
        self.addCleanup(os.close, write_end)
        with self.assertRaises(editor.QuestionInputError):
            editor._paste(fd, 100, newlines=True)
        self.assertEqual(self.leftover(fd), b"")

    def test_a_paste_without_an_end_marker_cannot_hang_the_prompt(self):
        fd, write_end = self.pipe(b"x" * 500)
        self.addCleanup(os.close, write_end)
        started = time.monotonic()
        with self.assertRaises(editor.QuestionInputError):
            editor._paste(fd, 100, newlines=True)
        self.assertLess(time.monotonic() - started, 5)

    def test_an_end_marker_split_between_reads_is_still_found(self):
        marker = editor._PASTE_END
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)
        self.addCleanup(os.close, write_end)
        def later():
            os.write(write_end, b"y" * 200 + marker[:3])
            time.sleep(0.05)
            os.write(write_end, marker[3:])
        worker = threading.Thread(target=later)
        worker.start()
        started = time.monotonic()
        editor._discard_paste(read_end, idle=2)
        worker.join()
        self.assertLess(time.monotonic() - started, 1.5)                 # ended at the marker, not at the idle limit


class QuestionLineTests(unittest.TestCase):
    def test_prompt_and_unicode_editing_preserve_complete_utf8(self):
        self.assertEqual(editor.PROMPT, "W czym mogę pomóc?\n> ")
        line = editor._Line(20)
        line.insert("ż界ółć")
        line.edit("left")
        line.edit("backspace")
        line.insert("a")
        self.assertEqual(line.text, "ż界óać")
        line.edit("home")
        line.edit("delete")
        line.edit("end")
        line.insert("!")
        self.assertEqual(line.text, "界óać!")

    def test_byte_limit_and_horizontal_view_do_not_truncate_stored_text(self):
        line = editor._Line(8)
        line.insert("ż界abc")
        self.assertEqual(len(line.text.encode("utf-8")), 8)
        with self.assertRaises(editor.QuestionInputError):
            line.insert("x")
        self.assertEqual(line.text, "ż界abc")
        self.assertEqual(line.view(7), ("abc", 5))
        line.edit("home")
        self.assertEqual(line.view(7), ("ż界a", 2))
        self.assertEqual(editor._width("ą界e\u0301"), 4)

    def test_invalid_limit_refuses_before_terminal_access(self):
        with patch.object(editor.sys.stdin, "isatty") as tty:
            for maximum in (0, -1, (128 * 1024 + 1), True, "2048", None):
                with self.subTest(maximum=maximum), self.assertRaises(editor.QuestionInputError):
                    editor.read_question(max_bytes=maximum)
            tty.assert_not_called()

    def test_invalid_text_never_becomes_display_output(self):
        for raw in (b"\xff", b"\x1b]52;PRIVATE", b"\x00", b"\n", b"\t", b"\r",
                    "\u202ePRIVATE".encode(), "\u2028".encode()):
            with self.subTest(raw=raw), self.assertRaises(editor.QuestionInputError) as caught:
                editor._safe_text(raw)
            self.assertNotIn("PRIVATE", str(caught.exception))


class QuestionTerminalTests(unittest.TestCase):
    @contextmanager
    def child(self, maximum=2048, columns=40, extra="", *, home_contains_repo=False, audit_probe=False, followup=False):
        script = f"""
import json, os, pwd, sys, sysconfig, termios
sys.path.insert(0, {str(ROOT)!r})
personal_roots = tuple(os.path.abspath(path) for path in
                       (os.environ['HOME'], pwd.getpwuid(os.getuid()).pw_dir))
code_roots = tuple(os.path.abspath(path) for path in
                   ({str(ROOT / 'ai_desktop')!r}, sysconfig.get_path('stdlib'), sysconfig.get_path('platstdlib')))
def under(path, root):
    return path == root or path.startswith(root + os.sep)
def audit(event, args):
    if event != 'open' or not isinstance(args[0], (str, bytes)):
        return
    path = os.path.abspath(os.fsdecode(args[0]))
    if any(name in path.split(os.sep) for name in ('.inputrc', '.history', '.python_history')):
        raise AssertionError('unexpected personal file access')
    if any(under(path, root) for root in personal_roots):
        # CI checks the repository out inside the runner's HOME. Keep the
        # audit active during import, allowing only trusted Python code reads.
        writes = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
        code_read = (path.endswith(('.py', '.pyc')) and any(under(path, root) for root in code_roots)
                     and type(args[2]) is int and not args[2] & writes)
        if not code_read:
            raise AssertionError('unexpected personal file access')
sys.addaudithook(audit)
from ai_desktop.question_input import read_question, QuestionInputError
if {audit_probe!r}:
    forbidden = [(os.path.join(personal_roots[0], '.inputrc'), os.O_RDONLY),
                 (os.path.join(personal_roots[0], '.python_history'), os.O_RDONLY),
                 (os.path.join(personal_roots[1], '.history'), os.O_RDONLY),
                 (os.path.join(personal_roots[1], 'personal.txt'), os.O_RDONLY),
                 ({str(ROOT / 'ai_desktop' / 'personal.json')!r}, os.O_RDONLY),
                 ({str(ROOT / 'ai_desktop' / 'editor.py')!r}, os.O_WRONLY)]
    for path, flags in forbidden:
        try:
            audit('open', (path, 'r' if flags == os.O_RDONLY else 'w', flags))
        except AssertionError:
            continue
        raise AssertionError('personal access audit was weakened')
    print('AUDIT_GUARDS=True', flush=True)
original = termios.tcgetattr(0)
{extra}
try:
    result = read_question(max_bytes={maximum}, followup={followup!r})
    print('RESULT=' + json.dumps(result, ensure_ascii=True), flush=True)
except (QuestionInputError, ValueError, EOFError, KeyboardInterrupt, OSError) as error:
    print('REFUSED=' + type(error).__name__, flush=True)
finally:
    print('RESTORED=' + str(termios.tcgetattr(0) == original), flush=True)
    print('READLINE=' + str('readline' in sys.modules), flush=True)
"""
        with tempfile.TemporaryDirectory(prefix="ai-question-home-") as personal:
            env = dict(os.environ, HOME=str(ROOT.parent) if home_contains_repo else personal)
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))
            process = subprocess.Popen([sys.executable, "-I", "-B", "-c", script], cwd=ROOT, env=env,
                                       stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            try:
                yield master, process, bytearray()
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
                os.close(master)

    def until(self, master, process, output, marker, timeout=5):
        deadline = time.monotonic() + timeout
        while marker not in output.replace(b"\r\n", b"\n") and time.monotonic() < deadline:
            if select.select([master], [], [], .03)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                output.extend(data)
            elif process.poll() is not None:
                break
        self.assertIn(marker, output.replace(b"\r\n", b"\n"), output.decode(errors="replace"))

    def completed(self, payload, *, maximum=2048, columns=40):
        with self.child(maximum, columns) as (master, process, output):
            self.until(master, process, output, editor.PROMPT.encode())
            os.write(master, payload)
            self.until(master, process, output, b"READLINE=False")
            self.assertEqual(process.wait(timeout=2), 0, output.decode(errors="replace"))
            self.assertIn(b"RESTORED=True", output)
            self.assertIn(b"\x1b[?2004l", output)
            return bytes(output)

    def test_arrows_home_end_backspace_delete_edit_the_question(self):
        # abc -> aXbc -> aXc -> !aXc? -> !aX?
        output = self.completed(b"abc\x1b[D\x1b[DX\x1b[3~\x1b[H!\x1b[F?\x1b[D\x7f\n")
        self.assertIn(b'RESULT="!aX?"', output)
        self.assertNotIn(b"^[[D", output)

    def test_followup_terminal_has_only_bare_prompt_and_restores_state(self):
        self.assertEqual(editor.FOLLOWUP_PROMPT, "> ")
        for payload, marker in (("edytuj istniejący\n".encode(), b"RESULT="),
                                (b"\x03", b"REFUSED=KeyboardInterrupt"),
                                (b"\x04", b"REFUSED=EOFError")):
            with self.subTest(payload=payload), self.child(followup=True) as (master, process, output):
                self.until(master, process, output, b"\x1b[?2004h> ")
                os.write(master, payload)
                self.until(master, process, output, b"READLINE=False")
                self.assertEqual(process.wait(timeout=2), 0)
                self.assertIn(marker, output)
                self.assertIn(b"RESTORED=True", output)
                self.assertNotIn(editor.PROMPT.encode(), output.replace(b"\r\n", b"\n"))
                self.assertNotIn("Co mam zrobić zamiast tego?".encode(), output)

    def test_application_cursor_keys_and_unicode_preserve_characters(self):
        output = self.completed("ż界ć".encode() + b"\x1bOD\x7f" + "ół".encode()
                                + b"\x1bOH!\x1bOF?\n")
        self.assertIn(("RESULT=" + json.dumps("!żółć?", ensure_ascii=True)).encode(), output)

    def test_horizontal_scroll_keeps_long_question_and_utf8_budget(self):
        question = "ż" * 1024
        output = self.completed(b"\x1b[200~" + question.encode() + b"\x1b[201~\x1b[H!\x7f\x1b[F\n",
                                columns=14)
        # Adding an extra byte at the limit is refused, never silently clipped.
        self.assertIn(b"REFUSED=QuestionInputError", output)
        self.assertNotIn(b"RESULT=", output)
        output = self.completed(b"\x1b[200~" + question.encode() + b"\x1b[201~\x1b[H\x1b[F\n",
                                columns=14)
        self.assertIn(("RESULT=" + json.dumps(question, ensure_ascii=True)).encode(), output)

    def test_plain_bracketed_paste_is_not_history_or_an_automatic_submission(self):
        with self.child() as (master, process, output):
            self.until(master, process, output, editor.PROMPT.encode())
            os.write(master, b"\x1b[200~abc\x1b[201~")
            self.until(master, process, output, b"> abc")
            self.assertNotIn(b"RESULT=", output)
            self.assertIsNone(process.poll())
            os.write(master, b"\x1b[A\x1b[B\n")
            self.until(master, process, output, b"READLINE=False")
            self.assertIn(b'RESULT="abc"', output)
            self.assertIn(b"RESTORED=True", output)

    def test_ctrl_c_eof_and_disallowed_input_restore_terminal(self):
        cases = ((b"\x03", b"KeyboardInterrupt"), (b"\x04", b"EOFError"),
                 (b"\x1b]52;c;PRIVATE\x07", b"QuestionInputError"),
                 (b"\x1b[200~PRIVATE\nnext\x1b[201~", b"QuestionInputError"),
                 (b"\x1b[200~\x1b[2JPRIVATE\x1b[201~", b"QuestionInputError"),
                 ("\u202ePRIVATE".encode(), b"QuestionInputError"),
                 (b"\xffPRIVATE", b"QuestionInputError"),
                 (b"\x1b", b"QuestionInputError"))
        for payload, reason in cases:
            with self.subTest(reason=reason, payload=payload):
                output = self.completed(payload)
                self.assertIn(b"REFUSED=" + reason, output)
                self.assertNotIn(b"RESULT=", output)
                self.assertNotIn(b"PRIVATE", output)
                self.assertNotIn(b"\x1b]52", output)

    def test_noncanonical_input_refuses_without_prompt(self):
        extra = "changed = original[:]; changed[3] &= ~termios.ICANON; termios.tcsetattr(0, termios.TCSANOW, changed); original = termios.tcgetattr(0)"
        with self.child(extra=extra) as (master, process, output):
            self.until(master, process, output, b"READLINE=False")
            self.assertIn(b"REFUSED=QuestionInputError", output)
            self.assertNotIn(editor.PROMPT.encode(), output.replace(b"\r\n", b"\n"))
            self.assertIn(b"RESTORED=True", output)

    def test_repo_inside_home_allows_only_trusted_code_imports_and_retains_audit(self):
        with self.child(home_contains_repo=True, audit_probe=True) as (master, process, output):
            self.until(master, process, output, editor.PROMPT.encode())
            self.assertIn(b"AUDIT_GUARDS=True", output)
            os.write(master, b"abc\x1b[D!\n")
            self.until(master, process, output, b"READLINE=False")
            self.assertIn(b'RESULT="ab!c"', output)
            self.assertIn(b"RESTORED=True", output)
            self.assertEqual(process.wait(timeout=2), 0, output.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()


class PromptSessionTerminalTests(unittest.TestCase):
    """Shared prompt behaviour of ask/task/material/project: history, double Ctrl+C, attachments, idle hook (real PTY)."""

    def run_script(self, body, keystrokes, *, columns=60, delay=0.15):
        script = (f"import json, sys, termios\nsys.path.insert(0, {str(ROOT)!r})\n"
                  "from ai_desktop.question_input import (read_question, PromptSession, QuestionInputError, paste_token)\n"
                  "import ai_desktop.question_input as q\nq._KITTY = False\n"
                  "original = termios.tcgetattr(0)\nsession = PromptSession()\nlog = []\n" + body +
                  "\nprint('RESTORED=' + str(termios.tcgetattr(0) == original), flush=True)\nprint('DONE', flush=True)\n")
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", script], cwd=ROOT, stdin=slave, stdout=slave,
                                   stderr=slave, close_fds=True)
        os.close(slave)
        output = bytearray()
        try:
            # Wait for the prompt itself: on a busy machine a fixed delay sends keys before the editor reads them.
            ready = time.monotonic() + 15
            while b"\x1b[?2004h" not in output and time.monotonic() < ready and process.poll() is None:
                if select.select([master], [], [], .05)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        break
            for chunk in keystrokes:
                time.sleep(delay)
                os.write(master, chunk)
            deadline = time.monotonic() + 6
            while b"DONE" not in output and time.monotonic() < deadline:
                if select.select([master], [], [], .05)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    output.extend(data)
                elif process.poll() is not None:
                    break
            process.wait(timeout=3)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            os.close(master)
        return output.replace(b"\r\n", b"\n").decode(errors="replace")

    def results(self, output):
        return [json.loads(line[len("R="):]) for line in output.splitlines() if line.startswith("R=")]

    READS = ("for _ in range({count}):\n"
             "    try:\n"
             "        value = read_question(max_bytes=2048, prompt='> ', session=session, attach={attach})\n"
             "        print('R=' + json.dumps([value, session.take_pastes(value)], ensure_ascii=False), flush=True)\n"
             "    except (KeyboardInterrupt, EOFError, QuestionInputError) as error:\n"
             "        print('R=' + json.dumps(['ERR', type(error).__name__]), flush=True)\n"
             "        break\n")

    def test_up_and_down_walk_the_history_of_this_command_only(self):
        output = self.run_script(self.READS.format(attach=False, count=3),
                                 [b"pierwsze\n", b"drugie\n", b"\x1b[A\x1b[A\n"])
        self.assertEqual([r[0] for r in self.results(output)], ["pierwsze", "drugie", "pierwsze"])
        self.assertIn("RESTORED=True", output)

    def test_the_first_ctrl_c_clears_or_warns_and_only_the_second_ends_the_command(self):
        output = self.run_script(self.READS.format(attach=False, count=3), [b"cos wpisane\x03", b"\x03", b"zostaje\n", b"\x03", b"\x03"])
        results = self.results(output)
        self.assertEqual(results[0][0], "zostaje")                      # Ctrl+C on text cleared it, the 2nd one only warned
        self.assertEqual(results[1], ["ERR", "KeyboardInterrupt"])
        self.assertIn("(Naciśnij Ctrl+C jeszcze raz, aby zakończyć)", output)
        self.assertIn("RESTORED=True", output)

    def test_any_other_key_disarms_the_second_ctrl_c(self):
        output = self.run_script(self.READS.format(attach=False, count=1), [b"\x03", b"x", b"\x03", b"\n"])
        self.assertEqual(self.results(output)[0][0], "")                # x was cleared by the next Ctrl+C, not an exit

    def test_a_multi_line_paste_becomes_a_marked_attachment_and_the_marker_travels_with_the_question(self):
        paste = b"\x1b[200~def a():\n    return 1\n\x1b[201~"
        output = self.run_script(self.READS.format(attach=True, count=1), [b"co robi ten kod? ", paste, b"\r"])
        (value, pastes), = self.results(output)
        token = "[wklejka #1: 3 wierszy, 22 B]"
        self.assertEqual(value, "co robi ten kod? " + token)
        self.assertEqual(pastes, [[1, "def a():\n    return 1\n"]])
        self.assertNotIn("\n    return 1", output.split("R=")[0].replace("\x1b", ""))        # content is not echoed to the screen

    def test_deleting_the_marker_drops_the_attachment(self):
        paste = b"\x1b[200~a\nb\x1b[201~"
        output = self.run_script(self.READS.format(attach=True, count=1), [paste, b"\x7f", b"\r"])
        (value, pastes), = self.results(output)
        self.assertNotIn("wklejka #1", value)
        self.assertEqual(pastes, [])

    def test_oversized_attachments_are_refused_and_without_attach_a_newline_paste_is_still_refused(self):
        big = b"\x1b[200~" + b"x\n" * (17 * 1024) + b"\x1b[201~"
        output = self.run_script(self.READS.format(attach=True, count=3), [big, b"\r"], delay=0.3)
        self.assertEqual(self.results(output)[0], ["ERR", "QuestionInputError"])
        output = self.run_script(self.READS.format(attach=False, count=3), [b"\x1b[200~a\nb\x1b[201~", b"\n"])
        self.assertEqual(self.results(output)[0], ["ERR", "QuestionInputError"])

    def test_the_idle_hook_fires_once_when_no_key_arrives_in_time_and_typing_still_works(self):
        body = ("calls = []\n"
                "value = read_question(max_bytes=2048, prompt='> ', session=session, idle=(lambda: .4, lambda: calls.append(1)))\n"
                "print('R=' + json.dumps([value, len(calls)]), flush=True)\n")
        output = self.run_script(body, [b"", b"po czasie\n"], delay=0.9)
        self.assertEqual(self.results(output), [["po czasie", 1]])

    def test_invalid_session_arguments_refuse_before_any_terminal_access(self):
        for kwargs in ({"attach": True}, {"session": object()}, {"prompt": "inny> "}, {"attach": 1, "session": editor.PromptSession()}):
            with self.subTest(kwargs=kwargs), patch.object(editor.sys.stdin, "isatty") as tty, \
                    self.assertRaises(editor.QuestionInputError):
                editor.read_question(**kwargs)
            tty.assert_not_called()

    def test_alt_enter_shift_enter_and_backslash_enter_make_a_line_break_in_project_mode(self):
        for label, keys in (("alt", b"a\x1b\rb\r"), ("kitty shift", b"a\x1b[13;2ub\r"), ("xterm shift", b"a\x1b[27;2;13~b\r"),
                            ("backslash", b"a\\\rb\r"), ("ctrl-j / LF binding", b"a\nb\r")):
            with self.subTest(label=label):
                output = self.run_script(self.READS.format(attach=True, count=1), [keys])
                self.assertEqual(self.results(output)[0][0], "a\nb")
                self.assertNotIn("⏎", output)                        # a real second row, not a symbol
                self.assertIn("> a\r\n  b", output.replace("\n", "\r\n") if "\r\n" not in output else output)

    def test_without_attachments_a_line_break_key_is_ignored_and_enter_still_sends(self):
        output = self.run_script(self.READS.format(attach=False, count=1), [b"a\x1b\rb\n"])
        self.assertEqual(self.results(output)[0][0], "ab")

    def test_unknown_keys_are_ignored_instead_of_aborting_the_prompt(self):
        output = self.run_script(self.READS.format(attach=False, count=1), [b"a\x1b[15~b\x1b[1;5Cc\x1b[Zd\x1bOPe\n"])
        self.assertEqual(self.results(output)[0][0], "abcde")

    def test_keyboard_protocol_ctrl_c_counts_as_ctrl_c(self):
        output = self.run_script(self.READS.format(attach=False, count=1), [b"\x1b[99;5u", b"\x1b[99;5u"])
        self.assertEqual(self.results(output)[0], ["ERR", "KeyboardInterrupt"])
        self.assertIn("(Naciśnij Ctrl+C jeszcze raz, aby zakończyć)", output)

    def test_the_keyboard_protocol_is_pushed_only_where_the_terminal_announced_it_and_always_popped(self):
        for flag, expected in ((True, True), (False, False)):
            with self.subTest(flag=flag):
                output = self.run_script("import ai_desktop.question_input as q\nq._KITTY = %r\n" % flag
                                         + self.READS.format(attach=False, count=1), [b"x\n"])
                self.assertEqual("\x1b[>1u" in output, expected)
                self.assertEqual("\x1b[<u" in output, expected)
                self.assertIn("RESTORED=True", output)

    def test_a_multi_line_prompt_is_drawn_as_rows_and_ctrl_c_clears_the_whole_block(self):
        keys = [b"pierwsza", b"\x1b[13;2u", b"druga", b"\x1b[13;2u", b"trzecia", b"\x03", b"po czyszczeniu\r"]
        output = self.run_script(self.READS.format(attach=True, count=1), keys)
        raw = output.replace("\r\n", "\n")
        self.assertIn("> pierwsza\n  druga\n  trzecia", raw)               # three rows, each on its own line
        self.assertEqual(self.results(output)[0][0], "po czyszczeniu")        # the block was cleared by the first Ctrl+C
        self.assertIn("\x1b[2A", output)                                     # the redraw went back to the first row

    def test_up_and_down_move_between_the_rows_of_a_multi_line_prompt_instead_of_the_history(self):
        keys = [b"x\r", b"ab\x1b[13;2ucd", b"\x1b[A", b"1", b"\x1b[B", b"2", b"\r"]
        output = self.run_script(self.READS.format(attach=True, count=2), keys)
        self.assertEqual(self.results(output)[1][0], "ab1\ncd2")

    def test_lock_state_bits_in_keyboard_protocol_modifiers_do_not_hide_ctrl_c_or_shift_enter(self):
        for label, ctrl_c, newline in (("num lock", b"\x1b[99;133u", b"\x1b[13;130u"), ("caps lock", b"\x1b[99;69u", b"\x1b[13;66u"),
                                       ("both", b"\x1b[99;197u", b"\x1b[13;194u"), ("xterm num lock", b"\x1b[27;133;99~", b"\x1b[27;130;13~")):
            with self.subTest(label=label):
                output = self.run_script(self.READS.format(attach=True, count=1), [b"a" + newline + b"b", ctrl_c, b"\r"])
                self.assertEqual(self.results(output)[0][0], "")             # newline accepted, Ctrl+C cleared the two rows

    def test_the_exact_bytes_a_terminal_with_num_lock_on_sends_are_understood(self):
        """Captured with scripts/keys_probe.py on a real terminal: Shift+Enter, Ctrl+C and Up all carry the Num Lock bit."""
        output = self.run_script(self.READS.format(attach=True, count=2), [b"x\r", b"a\x1b[13;130ub", b"\x1b[1;129A", b"1", b"\x1b[1;129B", b"2", b"\r"])
        self.assertEqual(self.results(output)[1][0], "a1\nb2")                 # Shift+Enter broke the line; Up/Down moved within it
        output = self.run_script(self.READS.format(attach=True, count=1), [b"text\x1b[99;133u", b"\x1b[99;133u", b"\x1b[99;133u"])
        self.assertEqual(self.results(output)[0], ["ERR", "KeyboardInterrupt"])        # 1st clears the text, 2nd warns, 3rd ends
        self.assertIn("(Naciśnij Ctrl+C jeszcze raz, aby zakończyć)", output)

    def test_history_and_cursor_keys_work_with_lock_bits_and_other_modifiers_are_ignored(self):
        output = self.run_script(self.READS.format(attach=False, count=2),
                                 [b"pierwsze\r", b"\x1b[1;129A\x1b[1;129D\x1b[3;129~\x1b[1;5C\x1b[1;2A\r"])
        # Up (num lock) recalls, Left moves, Delete removes one character, Ctrl+Right and Shift+Up are ignored
        self.assertEqual([r[0] for r in self.results(output)], ["pierwsze", "pierwsz"])


class KeyDecodingMatrixTests(unittest.TestCase):
    """Every key is decoded the same with and without Num Lock / Caps Lock bits (a compositor may keep Num Lock on)."""

    LOCKS = {"no lock": 0, "num lock": 128, "caps lock": 64, "num+caps lock": 192}

    def decode(self, body):
        read, write = os.pipe()
        try:
            os.write(write, body)
            return editor._escape_ext(read)
        finally:
            os.close(read)
            os.close(write)

    def test_cursor_keys_decode_to_their_plain_form_with_or_without_lock_bits(self):
        for name, lock in self.LOCKS.items():
            for final, expected in ((b"A", b"[A"), (b"B", b"[B"), (b"C", b"[C"), (b"D", b"[D"), (b"H", b"[H"), (b"F", b"[F")):
                with self.subTest(lock=name, key=final):
                    forms = [b"[" + final] + ([b"[1;%d%s" % (1 + lock, final)] if lock else [])
                    for form in forms:
                        self.assertEqual(self.decode(form), expected)
            for number, expected in ((b"3", b"[3~"), (b"1", b"[1~"), (b"4", b"[4~")):
                with self.subTest(lock=name, tilde=number):
                    form = b"[" + number + (b";%d" % (1 + lock) if lock else b"") + b"~"
                    self.assertEqual(self.decode(form), expected)

    def test_shift_enter_in_every_encoding_and_lock_state_is_a_line_break(self):
        for name, lock in self.LOCKS.items():
            with self.subTest(lock=name):
                self.assertEqual(self.decode(b"[13;%du" % (2 + lock)), b"newline")              # kitty: Shift
                self.assertEqual(self.decode(b"[13;%du" % (3 + lock)), b"newline")              # kitty: Alt
                self.assertEqual(self.decode(b"[27;%d;13~" % (2 + lock)), b"newline")           # xterm modifyOtherKeys
        self.assertEqual(self.decode(b"\r"), b"newline")                                        # Alt+Enter as ESC CR
        self.assertEqual(self.decode(b"[13u"), b"ignore")                                       # plain Enter in CSI u form is not a break

    def test_ctrl_letters_decode_with_or_without_lock_bits_and_other_modifiers_are_ignored(self):
        letters = {b"97": b"[H", b"98": b"[D", b"99": b"ctrl-c", b"100": b"ctrl-d", b"101": b"[F", b"102": b"[C"}
        for name, lock in self.LOCKS.items():
            for code, expected in letters.items():
                with self.subTest(lock=name, code=code):
                    self.assertEqual(self.decode(b"[%s;%du" % (code, 5 + lock)), expected)
                    self.assertEqual(self.decode(b"[27;%d;%s~" % (5 + lock, code)), expected)
            with self.subTest(lock=name, other="ctrl+shift+c, alt+c, ctrl+left"):
                self.assertEqual(self.decode(b"[99;%du" % (6 + lock)), b"ignore")
                self.assertEqual(self.decode(b"[99;%du" % (3 + lock)), b"ignore")
                self.assertEqual(self.decode(b"[1;%dD" % (5 + lock)), b"ignore")
        self.assertEqual(self.decode(b"[15~"), b"ignore")                                      # F5
        self.assertEqual(self.decode(b"[?0u"), b"ignore")                                      # a late keyboard-protocol reply
