"""Offline indicator tests, including a real pseudo-terminal; no AI or sudo."""
from contextlib import ExitStack
import io
import os
import pty
import select
import time
import unittest
from unittest.mock import Mock, patch

from ai_desktop import terminal_progress as progress


class TTY(io.StringIO):
    def isatty(self):
        return True

    def fileno(self):
        return 1


class WaitingIndicatorTests(unittest.TestCase):
    def enabled(self, stream=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True))
        stack.enter_context(patch.object(progress.os, "isatty", return_value=True))
        stack.enter_context(patch.object(progress.os, "get_terminal_size", return_value=os.terminal_size((80, 24))))
        thread = stack.enter_context(patch.object(progress.threading, "Thread"))
        clock = stack.enter_context(patch.object(progress.time, "monotonic", return_value=10.0))
        output = stream if stream is not None else TTY()
        return progress.WaitingIndicator(stream=output), output, thread, clock

    def test_context_stays_idle_until_authorization_has_finished(self):
        indicator, out, thread, _ = self.enabled()
        with indicator:
            self.assertEqual(out.getvalue(), "")
            thread.assert_not_called()
        self.assertEqual(out.getvalue(), "")

    def test_fixed_phases_elapsed_seconds_and_dots_without_percent(self):
        indicator, out, thread, clock = self.enabled()
        with indicator:
            indicator.phase("assistant")
            self.assertIn("Analyzing the question.   0 s", out.getvalue())
            clock.return_value = 11.0
            indicator.phase("web")
            self.assertIn("Searching and analyzing sources", out.getvalue())
            self.assertIn("1 s", out.getvalue())
            clock.return_value = 11.6
            indicator._paint()
            indicator.phase("cleanup")
        self.assertIn("Ending the session", out.getvalue())
        self.assertNotIn("%", out.getvalue())
        self.assertTrue(out.getvalue().endswith(progress.CLEAR_LINE))
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()
        thread.return_value.join.assert_called_once()

    def test_plain_markdown_no_color_dumb_and_nonterminal_never_start_thread(self):
        for fmt, term, no_color, stream in (
            ("plain", "xterm", False, TTY()), ("markdown", "xterm", False, TTY()),
            ("auto", "dumb", False, TTY()), ("auto", "", False, TTY()),
            ("auto", "xterm", True, TTY()), ("auto", "xterm", False, io.StringIO()),
        ):
            env = {"TERM": term}
            if no_color:
                env["NO_COLOR"] = "1"
            with self.subTest(fmt=fmt, term=term, no_color=no_color), \
                    patch.dict(os.environ, env, clear=True), patch.object(progress.os, "isatty", return_value=True), \
                    patch.object(progress.threading, "Thread") as thread:
                with progress.WaitingIndicator(stream=stream, output_format=fmt) as indicator:
                    indicator.phase("web")
                self.assertEqual(stream.getvalue(), "")
                thread.assert_not_called()

    def test_fake_tty_without_descriptor_and_closed_stream_are_not_animated(self):
        for stream in (io.StringIO(), TTY()):
            stream.isatty = lambda: True
            if isinstance(stream, TTY):
                stream.close()
            with patch.dict(os.environ, {"TERM": "xterm"}), patch.object(progress.os, "isatty", return_value=False), \
                    patch.object(progress.threading, "Thread") as thread:
                with progress.WaitingIndicator(stream=stream) as indicator:
                    indicator.phase("web")
                thread.assert_not_called()

    def test_unknown_label_cannot_inject_model_text(self):
        indicator, out, thread, _ = self.enabled()
        for value in ("SECRET\x1b[31m", {}, None, True):
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                indicator.phase(value)
        self.assertEqual(out.getvalue(), "")
        thread.assert_not_called()

    def test_resize_never_wraps_animated_line(self):
        indicator, out, _, _ = self.enabled()
        for columns in (1, 7, 20, 80, 160, 1000):
            with patch.object(progress.os, "get_terminal_size", return_value=os.terminal_size((columns, 24))):
                indicator.phase("web")
            frame = out.getvalue().split(progress.CLEAR_LINE)[-1]
            self.assertLessEqual(len(frame), min(160, columns) - 1)
            self.assertNotIn("\n", frame)
        indicator.stop()

    def test_exception_and_ctrl_c_stop_without_swallowing_or_printing_exception(self):
        for error in (RuntimeError("SECRET"), KeyboardInterrupt()):
            indicator, out, thread, _ = self.enabled()
            with self.assertRaises(type(error)):
                with indicator:
                    indicator.phase("web")
                    raise error
            thread.return_value.join.assert_called_once()
            self.assertTrue(out.getvalue().endswith(progress.CLEAR_LINE))
            self.assertNotIn("SECRET", out.getvalue())

    def test_stop_is_idempotent_and_late_phase_cannot_restart(self):
        indicator, out, thread, _ = self.enabled()
        indicator.phase("web")
        indicator.stop()
        rendered = out.getvalue()
        indicator.stop()
        indicator.phase("cleanup")
        indicator._paint()
        self.assertEqual(out.getvalue(), rendered)
        thread.return_value.join.assert_called_once()

    def test_failed_write_or_thread_start_does_not_fail_request(self):
        for fault in ("write", "start"):
            indicator, out, thread, _ = self.enabled()
            with self.subTest(fault=fault):
                if fault == "write":
                    out.write = Mock(side_effect=OSError("SECRET"))
                else:
                    thread.return_value.start.side_effect = RuntimeError("SECRET")
                with indicator:
                    indicator.phase("web")
                    indicator.phase("cleanup")
                self.assertNotIn("SECRET", out.getvalue())

    def test_real_pty_animates_and_has_no_output_after_stop(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        stream = os.fdopen(slave, "w", encoding="utf-8", buffering=1)
        self.addCleanup(stream.close)
        with patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            with progress.WaitingIndicator(stream=stream) as indicator:
                indicator.phase("web")
                time.sleep(progress.INTERVAL * 2.5)
            data = b""
            while select.select([master], [], [], 0)[0]:
                data += os.read(master, 8192)
            self.assertGreaterEqual(data.count(progress.CLEAR_LINE.encode()), 3)
            self.assertIsNotNone(indicator._thread)
            self.assertFalse(indicator._thread.is_alive())
            self.assertFalse(select.select([master], [], [], progress.INTERVAL * 1.2)[0])
