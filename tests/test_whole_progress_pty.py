"""Render progress through a real terminal FD, without a model, host data or input automation."""
import fcntl
import io
import os
import pty
import re
import select
import struct
import termios
import unittest

from ai_desktop.material_cli import WholeProgress


class Screen:
    """Small VT screen for the cursor movement/erase operations used by this frame.

    A PTY supplies real isatty/winsize/line-discipline behavior; this screen checks
    the resulting visible rows instead of tying the test to a particular CSI spelling.
    """
    def __init__(self, columns):
        self.columns = columns
        self.rows = [[" "] * columns for _ in range(12)]
        self.row = self.column = self.furthest_row = 0
        self.wrapped = False

    def feed(self, text):
        index = 0
        while index < len(text):
            if text.startswith("\x1b[", index):
                match = re.match(r"\x1b\[([0-9;]*)([A-Za-z])", text[index:])
                if match is None:
                    raise AssertionError("unexpected terminal control")
                raw, operation = match.groups()
                values = [int(value) if value else 0 for value in raw.split(";")]
                count = values[0] or 1
                if operation == "A":
                    self.row = max(0, self.row - count)
                elif operation == "B":
                    self.row += count
                elif operation == "m":
                    pass                      # colour/attributes do not move the cursor or change the cells
                elif operation == "G":
                    self.column = count - 1
                elif operation == "K":
                    mode = values[0]
                    start, end = ((0, self.columns) if mode == 2 else
                                  (0, self.column + 1) if mode == 1 else (self.column, self.columns))
                    self.rows[self.row][start:end] = [" "] * (end - start)
                elif operation == "J":
                    if values[0] == 2:
                        self.rows = [[" "] * self.columns for _ in self.rows]
                    elif values[0] == 0:
                        self.rows[self.row][self.column:] = [" "] * (self.columns - self.column)
                        for row in range(self.row + 1, len(self.rows)):
                            self.rows[row] = [" "] * self.columns
                    else:
                        raise AssertionError("unexpected erase mode")
                else:
                    raise AssertionError("unexpected terminal operation: " + operation)
                index += len(match.group(0))
                continue
            char = text[index]
            if char == "\r":
                self.column = 0
            elif char == "\n":
                self.row += 1
            elif char.isprintable():
                if self.column >= self.columns:
                    self.wrapped = True
                    self.row += 1
                    self.column = 0
                if self.row >= len(self.rows):
                    raise AssertionError("progress scrolled past its owned frame")
                self.rows[self.row][self.column] = char
                self.column += 1
            else:
                raise AssertionError("unexpected terminal byte")
            self.furthest_row = max(self.furthest_row, self.row)
            index += 1

    def visible(self):
        return ["".join(row).rstrip() for row in self.rows]


class WholeProgressPTYTests(unittest.TestCase):
    def terminal(self, columns=80):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        stream = os.fdopen(slave, "w", encoding="utf-8", buffering=1)
        self.addCleanup(stream.close)
        fcntl.ioctl(stream.fileno(), termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))
        self.assertTrue(stream.isatty())
        self.assertEqual(os.get_terminal_size(stream.fileno()).columns, columns)
        return master, stream

    @staticmethod
    def drain(master):
        output = []
        while select.select([master], [], [], 0.02)[0]:
            output.append(os.read(master, 65536))
        return b"".join(output).decode("utf-8")

    def test_stage_part_wait_and_synthesis_replace_one_frame_and_clear_for_the_prompt(self):
        master, stream = self.terminal()
        progress = WholeProgress(9, stages=3, stream=stream)
        progress.set_stage(1)
        self.assertEqual(self.drain(master), "")
        screen = Screen(80)
        progress.set_part(1, 1, 5406)
        screen.feed(self.drain(master))
        lines = screen.visible()
        self.assertIn("stage 1 of 3", lines[0])
        self.assertIn("Part 1/9", lines[1])
        self.assertIn("1–5406", lines[1])
        self.assertIn("analyz", lines[1])
        self.assertTrue(lines[2])
        progress.update(done=1)
        screen.feed(self.drain(master))
        self.assertIn("analyzed", screen.visible()[1])

        progress.set_stage(2)
        self.assertEqual(self.drain(master), "")
        progress.set_part(5, 21625, 27030)
        screen.feed(self.drain(master))
        self.assertIn("stage 2 of 3", screen.visible()[0])
        self.assertIn("Part 5/9", screen.visible()[1])
        self.assertIn("21625–27030", screen.visible()[1])
        self.assertNotIn("Part 1/9", "\n".join(screen.visible()))
        progress.update_wait(8)
        screen.feed(self.drain(master))
        self.assertIn("waiting 8", screen.visible()[1])
        progress.end_wait()
        screen.feed(self.drain(master))
        self.assertNotIn("waiting", screen.visible()[1])
        progress.update(synthesis=True)
        screen.feed(self.drain(master))
        self.assertIn("summary", screen.visible()[1].lower())
        self.assertFalse(screen.wrapped)
        self.assertLessEqual(screen.furthest_row, 2)

        progress.clear()
        screen.feed(self.drain(master))
        self.assertFalse(any(screen.visible()))
        progress.clear()
        self.assertEqual(self.drain(master), "")
        stream.write("> ")
        stream.flush()
        screen.feed(self.drain(master))
        self.assertEqual(screen.visible()[0], ">")
        self.assertFalse(any(screen.visible()[1:]))

    def test_narrow_terminal_never_wraps_or_accumulates_part_rows(self):
        master, stream = self.terminal(32)
        progress = WholeProgress(18, stages=3, stream=stream)
        screen = Screen(32)
        for index in range(1, 19):
            progress.set_stage((index - 1) // 8 + 1)
            progress.set_part(index, index * 2544, index * 2544 + 2543)
            progress.update_wait(7)
            progress.update(done=index)
            screen.feed(self.drain(master))
        self.assertFalse(screen.wrapped)
        self.assertLessEqual(screen.furthest_row, 2)
        self.assertTrue(all(len(line) <= 31 for line in screen.visible()))
        self.assertFalse(any(screen.visible()[3:]))
        progress.clear()
        screen.feed(self.drain(master))
        self.assertFalse(any(screen.visible()))

    def test_plain_output_has_one_static_block_and_no_cursor_controls(self):
        stream = io.StringIO()
        progress = WholeProgress(18, stages=3, output_format="plain", stream=stream)
        self.assertEqual(stream.getvalue(), "")
        progress.set_stage(1)
        progress.set_part(1, 1, 2544)
        initial = stream.getvalue()
        self.assertEqual(len(initial.splitlines()), 3)
        self.assertIn("stage 1 of 3", initial)
        self.assertIn("Part 1/18", initial)
        for number in range(1, 19):
            progress.set_stage((number - 1) // 8 + 1)
            progress.set_part(number, 1, 2544)
            progress.update(done=number)
            progress.update_wait(4)
            progress.end_wait()
        progress.clear()
        self.assertEqual(stream.getvalue(), initial)
        self.assertNotIn("\x1b", initial)
