"""Real PTY input, line discipline and screen checks; no native login or AI."""
from contextlib import contextmanager
import fcntl
import io
import os
import pty
import re
import select
import struct
import termios
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import terminal_menu as menu
from tests.test_whole_progress_pty import Screen

OPTIONS = ("Claude", "ChatGPT / Codex", "Anuluj")


class TerminalMenuTests(unittest.TestCase):
    def test_redirected_streams_refuse_without_inspection_or_processes(self):
        for stdin, stdout in ((io.StringIO(), io.StringIO()),):
            with patch.object(menu.sys, "stdin", stdin), patch.object(menu.sys, "stdout", stdout), \
                    patch.object(menu.termios, "tcgetattr") as inspect, \
                    patch("subprocess.run") as run, patch("subprocess.Popen") as process:
                with self.assertRaises(menu.MenuInputError):
                    menu.choose("Logowanie", OPTIONS)
            inspect.assert_not_called()
            run.assert_not_called()
            process.assert_not_called()
            self.assertEqual(stdout.getvalue(), "")

    def test_unsafe_labels_options_and_default_refuse_before_terminal_access(self):
        cases = (("Logowanie\x1b[2J", OPTIONS, 0), ("Login\n", OPTIONS, 0), ("Login\u202e", OPTIONS, 0),
                 ("\ud800", OPTIONS, 0), ("x" * 257, OPTIONS, 0), ("Login", ("Claude\x07",), 0),
                 ("Login", (), 0), ("Login", "Claude", 0), ("Login", OPTIONS, True),
                 ("Login", OPTIONS, -1), ("Login", OPTIONS, 3))
        for title, options, default in cases:
            with self.subTest(title=repr(title), default=default), patch.object(menu.termios, "tcgetattr") as inspect:
                with self.assertRaises(menu.MenuInputError):
                    menu.choose(title, options, default=default)
            inspect.assert_not_called()


class TerminalMenuPTYTests(unittest.TestCase):
    @contextmanager
    def terminal(self, *, columns=80, options=OPTIONS, default=0, queued=b"", output_failure=False,
                 next_prompt=False, guard_side_effects=False):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))
        original = termios.tcgetattr(slave)
        source = os.fdopen(os.dup(slave), "r", encoding="utf-8")
        target = os.fdopen(os.dup(slave), "w", encoding="utf-8", buffering=1)
        result, done = {}, threading.Event()
        output = bytearray()
        if queued:
            os.write(master, queued)
        class BrokenOutput:
            def isatty(self): return target.isatty()
            def fileno(self): return target.fileno()
            def write(self, value): raise OSError("SYNTHETIC_OUTPUT_FAILURE")
            def flush(self): pass
        selected_output = BrokenOutput() if output_failure else target

        def choose():
            try:
                result["value"] = menu.choose("Logowanie", options, default=default)
                if next_prompt:
                    target.write("NEXT> ")
                    target.flush()
            except BaseException as exc:
                result["error"] = exc
            finally:
                done.set()

        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.object(menu.sys, "stdin", source))
            stack.enter_context(patch.object(menu.sys, "stdout", selected_output))
            if guard_side_effects:
                # PTY descriptors are already open. Menu use must not acquire
                # files, saved history, a network socket or a child process.
                stack.enter_context(patch("builtins.open", side_effect=AssertionError("file access")))
                stack.enter_context(patch("os.open", side_effect=AssertionError("file access")))
                stack.enter_context(patch("socket.socket", side_effect=AssertionError("network")))
                stack.enter_context(patch("subprocess.run", side_effect=AssertionError("process")))
                stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("process")))
            thread = threading.Thread(target=choose, daemon=True)
            thread.start()
            state = SimpleNamespace(master=master, slave=slave, original=original, source=source,
                                    target=target, result=result, done=done, output=output, thread=thread)
            try:
                yield state
            finally:
                if thread.is_alive():
                    os.write(master, b"\x03")
                    thread.join(timeout=3)
                target.close()
                source.close()
                os.close(slave)
                os.close(master)
                if thread.is_alive():
                    raise AssertionError("menu thread did not terminate")

    def read(self, state, *, marker=None, count=1, seconds=2):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if marker is not None and state.output.count(marker) >= count:
                return
            if select.select([state.master], [], [], .01)[0]:
                state.output.extend(os.read(state.master, 65536))
            elif marker is None and state.done.is_set():
                return
        if marker is not None:
            self.fail("missing menu output: " + repr(marker) + " in " + repr(bytes(state.output)))

    def ready(self, state):
        self.read(state, marker=b"Esc anuluje")

    def finish(self, state, expected):
        self.assertTrue(state.done.wait(3), "menu did not finish")
        state.thread.join(timeout=1)
        self.read(state, seconds=.03)
        self.assertNotIn("error", state.result, repr(state.result.get("error")))
        self.assertEqual(state.result.get("value"), expected)
        self.assertEqual(termios.tcgetattr(state.slave), state.original)
        self.assertIn(b"\x1b[?2004l", state.output)
        self.assertIn(b"\x1b[?25h", state.output)

    def test_csi_ss3_and_lock_modified_arrows_select_only_on_enter(self):
        for arrow, expected in ((b"\x1b[B", 1), (b"\x1b[A", 2), (b"\x1bOB", 1), (b"\x1bOA", 2),
                                (b"\x1b[1;129B", 1), (b"\x1b[1;65A", 2)):
            with self.subTest(arrow=arrow), self.terminal() as state:
                self.ready(state)
                os.write(state.master, arrow)
                self.read(state, marker=b"\r\x1b[2K", count=10)
                self.assertFalse(state.done.is_set())
                os.write(state.master, b"\r")
                self.finish(state, expected)

    def test_enter_and_kitty_enter_select_default(self):
        for enter in (b"\r", b"\n", b"\x1b[13u", b"\x1b[13;129u", b"\x1b[27;65;13~"):
            with self.subTest(enter=enter), self.terminal(default=1) as state:
                self.ready(state)
                os.write(state.master, enter)
                self.finish(state, 1)

    def test_escape_ctrl_c_and_eof_cancel_and_restore_original_mode(self):
        for key in (b"\x1b", b"\x03", b"\x04", b"\x1b[99;5u", b"\x1b[27u"):
            with self.subTest(key=key), self.terminal() as state:
                self.ready(state)
                os.write(state.master, key)
                self.finish(state, None)

    def test_bracketed_paste_cannot_select_or_change_option(self):
        with self.terminal() as state:
            self.ready(state)
            os.write(state.master, b"\x1b[200~Claude\n\r\x1b[B\x1b[13u\x1b[201~")
            self.read(state, seconds=.15)
            self.assertFalse(state.done.is_set())
            os.write(state.master, b"\r")
            self.finish(state, 0)

    def test_prequeued_paste_prefix_cancels_instead_of_authorizing_its_later_newline(self):
        with self.terminal(queued=b"\x1b[200~") as state:
            self.assertTrue(state.done.wait(2), "prequeued prefix must cancel before a menu is shown")
            os.write(state.master, b"pasted text\n\x1b[201~")
            self.finish(state, None)
            self.assertNotIn(b"Logowanie", state.output)

    def test_paste_prefix_arriving_after_initial_pending_check_is_not_flushed(self):
        original_select = select.select
        original_write = os.write
        injected = []
        ready = threading.Event()
        proceed = threading.Event()
        def inspected(read, write, errors, timeout=None):
            result = original_select(read, write, errors, timeout)
            if timeout == 0 and not injected:
                # Hold the empty-queue observation while the parent supplies a
                # new paste prefix. It must reach the parser after the frame.
                injected.append(True)
                ready.set()
                if not proceed.wait(2):
                    raise AssertionError("paste race synchronization failed")
            return result
        with patch.object(menu.select, "select", side_effect=inspected), self.terminal() as state:
            self.assertTrue(ready.wait(2))
            original_write(state.master, b"\x1b[200~")
            proceed.set()
            self.ready(state)
            original_write(state.master, b"pasted text\n\x1b[201~")
            self.read(state, seconds=.1)
            self.assertFalse(state.done.is_set())
            original_write(state.master, b"\x03")
            self.finish(state, None)

    def test_paste_is_bounded_and_error_restores_terminal(self):
        with self.terminal() as state:
            self.ready(state)
            os.write(state.master, b"\x1b[200~" + b"x" * (menu.MAX_PASTE_BYTES + len(b"\x1b[201~")))
            self.assertTrue(state.done.wait(3))
            self.read(state, seconds=.03)
            self.assertIsInstance(state.result.get("error"), menu.MenuInputError)
            self.assertNotIn("value", state.result)
            self.assertEqual(termios.tcgetattr(state.slave), state.original)
            self.assertIn(b"\x1b[?25h", state.output)

    def test_pending_after_selection_is_flushed_before_next_prompt(self):
        with self.terminal(next_prompt=True) as state:
            self.ready(state)
            os.write(state.master, b"\r" + b"not-a-login-answer\n" * 16)
            self.finish(state, 0)
            self.assertFalse(select.select([state.source.fileno()], [], [], .05)[0])
            self.assertIn(b"NEXT> ", state.output)

    def test_narrow_frame_updates_without_wrapping_or_adding_rows_and_clears_for_next_prompt(self):
        options = ("Claude " + "a" * 100, "ChatGPT / Codex " + "b" * 100, "Anuluj")
        with self.terminal(columns=32, options=options, next_prompt=True) as state:
            self.ready(state)
            os.write(state.master, b"\x1b[B" * 40)
            self.read(state, marker=b"\r\x1b[2K", count=5 * 41)
            screen = Screen(32)
            clean = re.sub(rb"\x1b\[\?(?:2004|25)[hl]", b"", bytes(state.output)).decode()
            screen.feed(clean)
            self.assertFalse(screen.wrapped)
            self.assertEqual(screen.furthest_row, 4)
            self.assertTrue(all(len(line) <= 31 for line in screen.visible()))
            self.assertIn("› ChatGPT / Codex", screen.visible()[2])
            self.assertFalse(any(screen.visible()[5:]))
            before = len(state.output)
            os.write(state.master, b"\r")
            self.finish(state, 1)
            clean = re.sub(rb"\x1b\[\?(?:2004|25)[hl]", b"", bytes(state.output[before:])).decode()
            screen.feed(clean)
            self.assertEqual(screen.visible()[0], "NEXT>")
            self.assertFalse(any(screen.visible()[1:]))

    def test_output_failure_cannot_return_a_selection_and_still_restores_mode(self):
        with self.terminal(output_failure=True) as state:
            self.assertTrue(state.done.wait(2))
            self.assertIsInstance(state.result.get("error"), menu.MenuInputError)
            self.assertNotIn("value", state.result)
            self.assertEqual(termios.tcgetattr(state.slave), state.original)

    def test_menu_uses_only_existing_terminal_fds_without_files_network_or_processes(self):
        with self.terminal(guard_side_effects=True) as state:
            self.ready(state)
            os.write(state.master, b"\r")
            self.finish(state, 0)


if __name__ == "__main__":
    unittest.main()
