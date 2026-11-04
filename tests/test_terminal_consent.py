"""Short consent remains explicit, terminal-only and specific to an action."""
from contextlib import contextmanager
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, call, patch

from ai_desktop import terminal_consent as consent

ROOT = Path(__file__).resolve().parents[1]


class TerminalConsentTests(unittest.TestCase):
    def test_yes_or_no_and_their_initials_complete_the_decision(self):
        with patch.object(consent.sys.stdin, "isatty", return_value=True), \
                patch.object(consent.sys.stdout, "isatty", return_value=True), \
                patch.object(consent.sys.stdout, "write"):
            for action, prompt in consent.PROMPTS.items():
                # A change or its undo needs the whole word: a bare "t" would loop (covered in test_change_plan).
                for answer in (("tak", "TAK", " Tak ") if action in consent.STRICT
                               else ("t", "T", "tak", "TAK", " Tak ")):
                    with self.subTest(action=action, answer=answer):
                        reader = Mock(return_value=answer)
                        self.assertTrue(consent.confirm(action, reader=reader))
                        reader.assert_called_once_with(prompt)
                for answer in ("n", "N", "nie", "NIE", " Nie "):
                    with self.subTest(action=action, answer=answer):
                        reader = Mock(return_value=answer)
                        self.assertFalse(consent.confirm(action, reader=reader))
                        reader.assert_called_once_with(prompt)
                for answer in ("", " ", "?", "yes", "true", "tak\nnie",
                               "ANALIZUJ", "ZAPISZ", "COFNI", "a" * 64):
                    with self.subTest(action=action, answer=answer):
                        reader = Mock(side_effect=[answer, "nie"])
                        self.assertFalse(consent.confirm(action, reader=reader))
                        self.assertEqual(reader.call_args_list, [call(prompt), call(prompt)])
                for answer in (None, True):
                    reader = Mock(return_value=answer)
                    with self.assertRaises(ValueError):
                        consent.confirm(action, reader=reader)
                    reader.assert_called_once_with(prompt)

    def test_either_redirected_stream_refuses_without_reading(self):
        for stdin, stdout in ((False, True), (True, False), (False, False)):
            with patch.object(consent.sys.stdin, "isatty", return_value=stdin), \
                    patch.object(consent.sys.stdout, "isatty", return_value=stdout):
                reader = Mock(return_value="tak")
                self.assertFalse(consent.confirm("transfer", reader=reader))
                reader.assert_not_called()

    def test_eof_interrupt_and_read_error_are_not_retried(self):
        with patch.object(consent.sys.stdin, "isatty", return_value=True), \
                patch.object(consent.sys.stdout, "isatty", return_value=True):
            for failure in (EOFError, KeyboardInterrupt, OSError):
                reader = Mock(side_effect=failure)
                with self.assertRaises(failure):
                    consent.confirm("write", reader=reader)
                reader.assert_called_once()

    def test_unknown_or_untrusted_action_never_prompts(self):
        for action in ("send", "transfer\nwrite", None, []):
            reader = Mock(return_value="tak")
            with self.assertRaises(ValueError):
                consent.confirm(action, reader=reader)
            reader.assert_not_called()

    def test_custom_question_limit_is_bounded_before_terminal_access(self):
        with patch.object(consent.sys.stdin, "isatty") as terminal:
            for limit in (0, -1, 4097, True, None, "2048"):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    consent.fresh_input("Question? ", max_bytes=limit)
            terminal.assert_not_called()


class FreshTerminalConsentTests(unittest.TestCase):
    @contextmanager
    def child(self, code):
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, "-B", "-c", code], cwd=ROOT,
                                   stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        try:
            yield master, process, bytearray()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            os.close(master)

    def read_until(self, master, process, output, marker, timeout=5, occurrences=1):
        deadline = time.monotonic() + timeout
        while output.count(marker) < occurrences and time.monotonic() < deadline:
            if select.select([master], [], [], .05)[0]:
                try:
                    data = os.read(master, 8192)
                except OSError:
                    break
                if not data:
                    break
                output.extend(data)
            elif process.poll() is not None:
                break
        self.assertGreaterEqual(output.count(marker), occurrences, output.decode(errors="replace"))

    def test_single_paste_cannot_approve_later_write(self):
        code = """
from ai_desktop.terminal_consent import confirm
print('TRANSFER=' + str(confirm('transfer')), flush=True)
print('DIFF: synthetic change', flush=True)
print('WRITE=' + str(confirm('write')), flush=True)
"""
        for paste in (b"t\nt\n", b"tak\ntak\n"):
            with self.subTest(paste=paste), self.child(code) as (master, process, output):
                self.read_until(master, process, output, consent.PROMPTS["transfer"].encode())
                os.write(master, paste)
                self.read_until(master, process, output, consent.PROMPTS["write"].encode())
                self.assertIn(b"TRANSFER=True", output)
                # Give the child time to consume any accidentally buffered yes.
                deadline = time.monotonic() + .2
                while time.monotonic() < deadline:
                    if select.select([master], [], [], .02)[0]:
                        try:
                            output.extend(os.read(master, 8192))
                        except OSError:
                            break
                self.assertIsNone(process.poll(), output.decode(errors="replace"))
                self.assertNotIn(b"WRITE=", output)
                os.write(master, b"nie\n")
                self.read_until(master, process, output, b"WRITE=False")
                self.assertEqual(process.wait(timeout=2), 0)

    def test_fresh_answers_can_approve_two_distinct_prompts(self):
        code = """
from ai_desktop.terminal_consent import confirm
print('TRANSFER=' + str(confirm('transfer')), flush=True)
print('WRITE=' + str(confirm('write')), flush=True)
"""
        with self.child(code) as (master, process, output):
            self.read_until(master, process, output, consent.PROMPTS["transfer"].encode())
            os.write(master, b"TAK\n")
            self.read_until(master, process, output, consent.PROMPTS["write"].encode())
            os.write(master, b"Tak\n")
            self.read_until(master, process, output, b"WRITE=True")
            self.assertIn(b"TRANSFER=True", output)
            self.assertEqual(process.wait(timeout=2), 0)

    def test_empty_or_unknown_answers_reprompt_until_explicit_decision(self):
        code = """
from ai_desktop.terminal_consent import confirm
print('DECISION=' + str(confirm('transfer')), flush=True)
"""
        for final, result in ((b"N", b"False"), (b"T", b"True")):
            with self.subTest(final=final), self.child(code) as (master, process, output):
                prompt = consent.PROMPTS["transfer"].encode()
                self.read_until(master, process, output, prompt)
                for number, answer in enumerate((b"", b"?", b"yes", b"unknown"), 2):
                    os.write(master, answer + b"\n")
                    self.read_until(master, process, output, prompt, occurrences=number)
                    self.assertNotIn(b"DECISION=", output)
                    self.assertIsNone(process.poll())
                os.write(master, final + b"\n")
                self.read_until(master, process, output, b"DECISION=" + result)
                self.assertEqual(process.wait(timeout=2), 0)

    def test_reprompt_discards_queued_pasted_approval_too(self):
        code = """
from ai_desktop.terminal_consent import confirm
print('DECISION=' + str(confirm('transfer')), flush=True)
"""
        with self.child(code) as (master, process, output):
            prompt = consent.PROMPTS["transfer"].encode()
            self.read_until(master, process, output, prompt)
            os.write(master, b"?\ntak\n")
            self.read_until(master, process, output, prompt, occurrences=2)
            deadline = time.monotonic() + .2
            while time.monotonic() < deadline:
                if select.select([master], [], [], .02)[0]:
                    try:
                        output.extend(os.read(master, 8192))
                    except OSError:
                        break
            self.assertIsNone(process.poll(), output.decode(errors="replace"))
            self.assertNotIn(b"DECISION=", output)
            os.write(master, b"nie\n")
            self.read_until(master, process, output, b"DECISION=False")
            self.assertEqual(process.wait(timeout=2), 0)

    def test_ctrl_d_and_overlong_answer_refuse(self):
        code = """
from ai_desktop.terminal_consent import confirm
try:
    result = confirm('transfer')
except (EOFError, OSError, ValueError):
    print('REFUSED', flush=True)
    raise SystemExit(2)
print('UNEXPECTED_RESULT=' + str(result), flush=True)
"""
        for answer in (b"\x04", b"t" * (consent.MAX_ANSWER_BYTES + 1) + b"\n"):
            with self.subTest(answer=answer), self.child(code) as (master, process, output):
                self.read_until(master, process, output, consent.PROMPTS["transfer"].encode())
                os.write(master, answer)
                self.read_until(master, process, output, b"REFUSED")
                self.assertNotIn(b"UNEXPECTED_RESULT", output)
                self.assertEqual(process.wait(timeout=2), 2)

    def test_noncanonical_terminal_refuses_before_prompt(self):
        code = """
import sys, termios
from ai_desktop.terminal_consent import confirm
attributes = termios.tcgetattr(sys.stdin.fileno())
attributes[3] &= ~termios.ICANON
termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, attributes)
try:
    confirm('transfer')
except ValueError:
    print('REFUSED', flush=True)
    raise SystemExit(2)
raise SystemExit(1)
"""
        with self.child(code) as (master, process, output):
            self.read_until(master, process, output, b"REFUSED")
            self.assertNotIn(consent.PROMPTS["transfer"].encode(), output)
            self.assertEqual(process.wait(timeout=2), 2)
