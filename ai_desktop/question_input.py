"""Bounded one-line TTY editor, without history, readline or user init files.

Only known editing keys become terminal operations. User bytes are decoded and
validated before display; pasted controls never become terminal output or a
later confirmation. Consent continues to use its separate canonical reader.
"""
from bisect import bisect_left, bisect_right
import os
import re
import select
import sys
import termios
import time
import unicodedata

from .analysis_contract import MAX_QUESTION_BYTES, validate_question
from .terminal_style import greeting, paint


PROMPT = "W czym mogę pomóc?\n> "
PASTE_TEXT_BYTES = 32 * 1024          # one pasted block kept as an attachment (project mode)
PASTES_TOTAL_BYTES = 32 * 1024        # all attachments of one prompt
MAX_PASTES = 9
ATTACH_MIN_BYTES = 400                # shorter single-line pastes simply become typed text
HISTORY_LIMIT = 50
CTRL_C_HINT = "(Naciśnij Ctrl+C jeszcze raz, aby zakończyć)"
FOLLOWUP_PROMPT = "> "
_PASTE_START = b"[200~"
_PASTE_END = b"\x1b[201~"
_KEYS = {b"[D": "left", b"OD": "left", b"[C": "right", b"OC": "right",
         b"[H": "home", b"OH": "home", b"[1~": "home", b"[7~": "home",
         b"[F": "end", b"OF": "end", b"[4~": "end", b"[8~": "end",
         b"[3~": "delete", b"[A": "ignore", b"[B": "ignore",
         b"OA": "ignore", b"OB": "ignore"}


_TOKEN = r"\[wklejka #\d: \d+ wierszy, [0-9, ]+(?:B|KiB)\]"
_TOKEN_END = re.compile(_TOKEN + r"\Z")
_TOKEN_START = re.compile(_TOKEN)


class QuestionInputError(ValueError):
    """A finite input refusal; never contains the input itself."""


def paste_token(number, text):
    size = len(text.encode("utf-8"))
    return "[wklejka #%d: %d wierszy, %s]" % (number, text.count("\n") + 1,
                                              ("%.1f KiB" % (size / 1024)).replace(".", ",") if size >= 1024 else "%d B" % size)


class PromptSession:
    """What one interactive command shares between its prompts: input history, the double Ctrl+C rule,
    and the pasted blocks of the prompt being edited. Nothing is written to disk."""

    def __init__(self):
        self.history, self._armed_until, self._pastes = [], 0.0, []
        self._clock = time.monotonic

    ARM_SECONDS = 3.0

    @property
    def armed(self):
        """True while a second Ctrl+C (at the prompt, a consent question or a wait) ends the whole command."""
        return self._clock() < self._armed_until

    @armed.setter
    def armed(self, value):
        self._armed_until = self._clock() + self.ARM_SECONDS if value else 0.0

    def remember(self, text):
        if text.strip() and (not self.history or self.history[-1] != text):
            self.history.append(text)
            del self.history[:-HISTORY_LIMIT]

    def _add_paste(self, text):
        used = sum(len(t.encode("utf-8")) for _, t in self._pastes)
        size = len(text.encode("utf-8"))
        if len(self._pastes) >= MAX_PASTES or size > PASTE_TEXT_BYTES or used + size > PASTES_TOTAL_BYTES:
            raise QuestionInputError("question paste")
        number = max((n for n, _ in self._pastes), default=0) + 1
        self._pastes.append((number, text))
        return number

    def reset_pastes(self):
        self._pastes = []

    def take_pastes(self, line):
        """Pastes whose marker is still in the submitted text (deleting the marker drops the block)."""
        kept = [(n, t) for n, t in self._pastes if paste_token(n, t) in line]
        self._pastes = []
        return kept


def _width(text):
    return sum(0 if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me")
               else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def _safe_text(raw, newlines=False):
    try:
        text = raw.decode("utf-8", errors="strict")
        if newlines:
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        else:
            validate_question(text)
        if any(unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp") and not (newlines and char in "\n\t")
               for char in text):
            raise ValueError()
        return text
    except (UnicodeError, ValueError):
        raise QuestionInputError("question text") from None


def _window(shown, cursor, available):
    """The part of one row that fits `available` cells around the cursor, and the cursor's offset inside it."""
    widths = [0]
    for char in shown:
        widths.append(widths[-1] + _width(char))
    start = bisect_left(widths, max(0, widths[cursor] - available), 0, cursor + 1)
    end = bisect_right(widths, widths[start] + available) - 1
    return shown[start:end], widths[cursor] - widths[start]


class _Line:
    def __init__(self, limit):
        self.text, self.cursor, self.limit = "", 0, limit
        self.rows_total, self.row_drawn = 1, 0         # what is on the screen now (for redrawing a multi-row prompt)

    def insert(self, text):
        result = self.text[:self.cursor] + text + self.text[self.cursor:]
        if len(result.encode("utf-8")) > self.limit:
            raise QuestionInputError("question limit")
        self.text = result
        self.cursor += len(text)

    def edit(self, key):
        if key == "left":
            self.cursor = max(0, self.cursor - 1)
        elif key == "right":
            self.cursor = min(len(self.text), self.cursor + 1)
        elif key == "home":
            self.cursor = 0
        elif key == "end":
            self.cursor = len(self.text)
        elif key == "backspace" and self.cursor:
            marker = _TOKEN_END.search(self.text[:self.cursor])
            start = marker.start() if marker else self.cursor - 1
            self.text = self.text[:start] + self.text[self.cursor:]      # a paste marker is deleted as one unit
            self.cursor = start
        elif key == "delete":
            marker = _TOKEN_START.match(self.text, self.cursor)
            end = marker.end() if marker else self.cursor + 1
            self.text = self.text[:self.cursor] + self.text[end:]
        elif key in ("up", "down"):
            before = self.text[:self.cursor]
            start = before.rfind("\n") + 1
            column = self.cursor - start
            if key == "up" and start:
                previous = before.rfind("\n", 0, start - 1) + 1
                self.cursor = previous + min(column, start - 1 - previous)
            elif key == "down":
                following = self.text.find("\n", self.cursor)
                if following != -1:
                    end = self.text.find("\n", following + 1)
                    end = len(self.text) if end == -1 else end
                    self.cursor = following + 1 + min(column, end - following - 1)

    def view(self, columns):
        # Keep the prompt, content and cursor inside one physical terminal row.
        # The remaining byte limit still applies to the complete uncut value.
        shown = self.text.replace("\n", "⏎")             # single-row view: a line break is one visible cell
        visible, offset = _window(shown, self.cursor, max(1, columns - 3))
        return visible, 2 + offset

    def layout(self, columns):
        """Rows to draw for a multi-line prompt: (rows, cursor_row, cursor_cell). Each row is cut around its own cursor."""
        available = max(1, columns - 3)
        before = self.text[:self.cursor]
        row = before.count("\n")
        column = len(before) - (before.rfind("\n") + 1)
        rows, cell = [], 2
        for index, logical in enumerate(self.text.split("\n")):
            if index == row:
                visible, offset = _window(logical, column, available)
                cell = 2 + offset
            else:
                visible, _ = _window(logical, 0, available)
            rows.append(visible)
        return rows, row, cell


def _byte(fd):
    value = os.read(fd, 1)
    if not value:
        raise EOFError()
    if value == b"\x03":
        raise KeyboardInterrupt()
    return value


def _escape(fd):
    result = b""
    for _ in range(6):
        # A lone/unrecognised Escape must not leave the editor waiting forever.
        if not select.select([fd], [], [], .15)[0]:
            break
        result += _byte(fd)
        if result in _KEYS or result == _PASTE_START:
            return result
        if not any(key.startswith(result) for key in (*_KEYS, _PASTE_START)):
            break
    raise QuestionInputError("question key")


_CTRL_LETTERS = {97: b"[H", 98: b"[D", 99: b"ctrl-c", 100: b"ctrl-d", 101: b"[F", 102: b"[C"}
_KITTY = None          # whether this terminal answered the keyboard-protocol query (asked once per process)


def _kitty_query(fd, output):
    """Ask once whether the terminal speaks the kitty keyboard protocol (then Shift+Enter is distinguishable)."""
    global _KITTY
    if _KITTY is None:
        output.write("\x1b[?u")
        output.flush()
        reply = b""
        while len(reply) < 32 and select.select([fd], [], [], .15 if not reply else .03)[0]:
            reply += os.read(fd, 1)
            if reply.endswith(b"u") and reply.startswith(b"\x1b[?"):
                break
        _KITTY = reply.startswith(b"\x1b[?") and reply.endswith(b"u")
    return _KITTY


_LOCKS = 64 | 128                                              # Caps Lock and Num Lock bits of the modifier field


def _without_locks(key):
    """`ESC[1;129A` (Up with Num Lock on) is plain Up: keys with only lock bits set are normalised to their bare form."""
    match = re.fullmatch(rb"\[(?:\d+;)?(\d+)([ABCDHF])", key)
    if match:
        return b"[" + match.group(2) if ((int(match.group(1)) - 1) & ~_LOCKS) == 0 else b"[ignored"
    match = re.fullmatch(rb"\[(\d+);(\d+)~", key)
    if match and match.group(1) != b"27":
        return b"[" + match.group(1) + b"~" if ((int(match.group(2)) - 1) & ~_LOCKS) == 0 else b"[ignored"
    return key


def _escape_ext(fd):
    """One escape sequence for a PromptSession: known keys, `newline` (Alt/Shift/Ctrl+Enter), `ctrl-c`/`ctrl-d`
    reported by the keyboard protocol, otherwise `ignore`. Unknown keys never abort the prompt."""
    if not select.select([fd], [], [], .15)[0]:
        return b"ignore"                                       # a lone Escape
    first = os.read(fd, 1)
    if first in (b"\r", b"\n"):
        return b"newline"                                      # Alt+Enter
    if first == b"O":
        second = os.read(fd, 1) if select.select([fd], [], [], .15)[0] else b""
        return first + second if first + second in _KEYS else b"ignore"
    if first != b"[":
        return b"ignore"
    body = b""
    while len(body) < 24 and select.select([fd], [], [], .15)[0]:
        byte = os.read(fd, 1)
        if not byte:
            raise EOFError()
        body += byte
        if 0x40 <= byte[0] <= 0x7E:
            break
    key = b"[" + body
    key = _without_locks(key)
    if key in _KEYS or key == _PASTE_START:
        return key
    match = re.fullmatch(rb"\[(\d+)(?:;(\d+))?u", key) or re.fullmatch(rb"\[27;(\d+);(\d+)~", key)
    if match:
        code, mods = (int(match.group(1)), int(match.group(2) or 1)) if key.endswith(b"u") else (int(match.group(2)), int(match.group(1)))
        held = (mods - 1) & ~(64 | 128)                        # Shift 1, Alt 2, Ctrl 4, Super 8 ... without Caps/Num Lock
        if code == 13 and held:
            return b"newline"                                  # Shift/Alt/Ctrl+Enter
        if held == 4 and code in _CTRL_LETTERS:
            return _CTRL_LETTERS[code]                         # Ctrl+A/B/C/D/E/F as the keyboard protocol reports them
    return b"ignore"


def _paste(fd, remaining, newlines=False):
    value = bytearray()
    while len(value) <= remaining + len(_PASTE_END):
        value.extend(_byte(fd))
        if value.endswith(_PASTE_END):
            raw = bytes(value[:-len(_PASTE_END)])
            if len(raw) > remaining:
                break
            return _safe_text(raw, newlines)
        # ESC is permitted only as the prefix of the final paste delimiter.
        escape = value.find(b"\x1b")
        if escape >= 0 and not _PASTE_END.startswith(bytes(value[escape:])):
            break
        if value[-1] < 32 and value[-1] != 27 or value[-1] == 127:
            if not (newlines and value[-1] in (9, 10, 13)):
                break
    raise QuestionInputError("question paste")


def _character(fd, first):
    number = first[0]
    length = 1 if number < 128 else 2 if 0xC2 <= number <= 0xDF else 3 if 0xE0 <= number <= 0xEF else 4 if 0xF0 <= number <= 0xF4 else 0
    if not length:
        raise QuestionInputError("question encoding")
    return _safe_text(first + b"".join(_byte(fd) for _ in range(length - 1)))


def _draw(line, output):
    try:
        columns = os.get_terminal_size(output.fileno()).columns
    except OSError:
        columns = 80
    columns = max(4, columns)
    if "\n" not in line.text and line.rows_total == 1:
        visible, cursor = line.view(columns)
        # Every escape here is generated by us. No escape from user input survives.
        output.write("\r\x1b[2K" + _pointer(output) + visible + "\r\x1b[" + str(cursor) + "C")
        output.flush()
        return
    rows, cursor_row, cursor_cell = line.layout(columns)
    parts = ["\x1b[%dA" % line.row_drawn] if line.row_drawn else []
    parts.append("\r\x1b[J")                                 # from the first row down: clear the whole old block
    for index, visible in enumerate(rows):
        parts.append((_pointer(output) if index == 0 else "  ") + visible + ("\r\n" if index < len(rows) - 1 else ""))
    if len(rows) - 1 - cursor_row:
        parts.append("\x1b[%dA" % (len(rows) - 1 - cursor_row))
    parts.append("\r\x1b[%dC" % cursor_cell)
    output.write("".join(parts))
    output.flush()
    line.rows_total, line.row_drawn = len(rows), cursor_row


def _wait(fd, idle):
    """Call the idle callback once if no key arrives before the caller's deadline; then block normally."""
    if idle is None:
        return None
    seconds, callback = idle
    left = seconds()
    if left is not None and left > 0 and not select.select([fd], [], [], left)[0]:
        callback()
        return None
    return idle


def _pointer(output):
    return paint(">", "accent", "bold", stream=output) + " "


def _styled_prompt(prompt, output):
    """Colour only: the visible cells of the input row stay the two of "> ", so cursor positions do not change."""
    head, tail = prompt[:-2], prompt[-2:]
    if tail != "> ":
        return prompt
    first, newline, rest = head.partition("\n")
    return (greeting(first, stream=output) + newline + rest if first else head) + _pointer(output)


def read_question(*, max_bytes=MAX_QUESTION_BYTES, followup=False, prompt=None, session=None, attach=False, idle=None,
                  lf_breaks=True):
    """Read a fresh bounded UTF-8 question and restore the original TTY state.

    With a `PromptSession`: Up/Down walk the history of this command, a first Ctrl+C clears the line (or, on an empty
    line, asks for a second one) and only the second ends the command. With `attach=True` a multi-line or large paste
    becomes a marked attachment instead of being refused; the caller collects it with `session.take_pastes`."""
    if type(followup) is not bool or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_QUESTION_BYTES:
        raise QuestionInputError("question limit")
    if ((session is not None and type(session) is not PromptSession) or type(attach) is not bool
            or (attach and session is None) or prompt not in (None, FOLLOWUP_PROMPT, PROMPT)):
        raise QuestionInputError("question limit")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise QuestionInputError("question terminal")
    fd, output = sys.stdin.fileno(), sys.stdout
    original = termios.tcgetattr(fd)
    if not original[3] & termios.ICANON:
        raise QuestionInputError("question terminal")
    edited = original[:]
    edited[6] = original[6][:]
    edited[0] &= ~(termios.ICRNL | termios.INLCR | termios.IGNCR | termios.IXON | termios.IXOFF)
    edited[3] &= ~(termios.ICANON | termios.ECHO | termios.IEXTEN | termios.ISIG)
    edited[6][termios.VMIN], edited[6][termios.VTIME] = 1, 0
    termios.tcsetattr(fd, termios.TCSANOW, edited)
    pushed, line = None, None
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
        line = _Line(max_bytes)
        if session is not None:
            session.reset_pastes()
            # Asked before the prompt is drawn (and only once per process) so that no typed key can be mistaken for the reply.
            if _kitty_query(fd, output):
                output.write("\x1b[>1u")
                pushed = "kitty"
            else:
                output.write("\x1b[>4;2m")                          # xterm modifyOtherKeys; terminals without it ignore this
                pushed = "xterm"
        output.write("\x1b[?2004h" + _styled_prompt(prompt if prompt is not None else FOLLOWUP_PROMPT if followup else PROMPT, output))
        output.flush()
        position, draft = None, ""
        while True:
            idle = _wait(fd, idle)
            try:
                value = _byte(fd)
                extended = None
                if value == b"\x1b" and session is not None:
                    extended = _escape_ext(fd)
                    if extended == b"ctrl-c":
                        raise KeyboardInterrupt()
            except KeyboardInterrupt:
                if session is None:
                    raise
                if line.text:
                    line.text, line.cursor, session.armed = "", 0, False
                    session.reset_pastes()
                    _draw(line, output)
                    continue
                if session.armed:
                    session.armed = False
                    raise
                session.armed = True
                output.write("\r\x1b[2K" + CTRL_C_HINT + "\r\n" + _pointer(output))
                output.flush()
                continue
            if session is not None and value != b"\x1b":
                session.armed = False
            if value == b"\n" and attach and lf_breaks:
                line.insert("\n")                                   # Ctrl+J / a terminal binding that sends LF: break the line
                _draw(line, output)
                continue
            if value in (b"\r", b"\n"):
                if attach and line.text.endswith("\\") and line.cursor == len(line.text):
                    line.text = line.text[:-1]                      # backslash + Enter: a line break instead of sending
                    line.cursor -= 1
                    line.insert("\n")
                    _draw(line, output)
                    continue
                text = validate_question(line.text)
                if session is not None:
                    session.armed = False
                    session.remember(text)
                return text
            if value == b"\x04":
                if not line.text:
                    raise EOFError()
                line.edit("delete")
            elif value in (b"\x08", b"\x7f"):
                line.edit("backspace")
            elif value in (b"\x01", b"\x05", b"\x02", b"\x06"):
                line.edit({b"\x01": "home", b"\x05": "end", b"\x02": "left", b"\x06": "right"}[value])
            elif value == b"\x1b":
                key = extended if session is not None else _escape(fd)
                if session is not None:
                    session.armed = False
                if key == b"ctrl-d":
                    if not line.text:
                        raise EOFError()
                    line.edit("delete")
                elif key == b"newline":
                    if attach:
                        line.insert("\n")
                elif key == b"ignore":
                    pass
                elif key == _PASTE_START and attach:
                    block = _paste(fd, PASTE_TEXT_BYTES + 1024, newlines=True)
                    if "\n" in block or len(block.encode("utf-8")) >= ATTACH_MIN_BYTES:
                        line.insert(paste_token(session._add_paste(block), block))
                    else:
                        line.insert(block)
                elif key == _PASTE_START:
                    line.insert(_paste(fd, max_bytes - len(line.text.encode("utf-8"))))
                elif key in (b"[A", b"OA", b"[B", b"OB") and session is not None and "\n" in line.text:
                    line.edit("up" if key in (b"[A", b"OA") else "down")
                elif key in (b"[A", b"OA", b"[B", b"OB") and session is not None:
                    if session.history:
                        older = key in (b"[A", b"OA")
                        if position is None:
                            position, draft = len(session.history), line.text
                        position = max(0, position - 1) if older else min(len(session.history), position + 1)
                        line.text = draft if position == len(session.history) else session.history[position]
                        line.cursor = len(line.text)
                else:
                    line.edit(_KEYS[key])
            else:
                line.insert(_character(fd, value))
            _draw(line, output)
    finally:
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, original)
            finally:
                below = max(0, line.rows_total - 1 - line.row_drawn) if line is not None else 0
                output.write(("\x1b[%dB" % below if below else "")
                             + ("\x1b[<u" if pushed == "kitty" else "\x1b[>4;0m" if pushed == "xterm" else "")
                             + "\x1b[?2004l\n")
                output.flush()
