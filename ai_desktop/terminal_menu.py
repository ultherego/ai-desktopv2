"""Small terminal-only selector. Input bytes are never printed or saved.

Only an explicit, unmodified Enter selects an option. Bracketed paste is
discarded as a bounded record, including any embedded Enter or arrow keys.
The caller decides what a selected option authorizes; this module starts no
process, performs no network access and keeps no file or command history.
"""
import copy
import os
import re
import select
import sys
import termios
import time
import unicodedata

from .question_input import _byte, _width, _without_locks
from .terminal_style import paint

MAX_OPTIONS = 12
MAX_LABEL_BYTES = 256
MAX_PASTE_BYTES = 8192
ESCAPE_SECONDS = 0.15
PASTE_SECONDS = 2
_PASTE_END = b"\x1b[201~"
_HINT = "↑/↓  Enter selects  Esc cancels"
_HINT_MARKS = "↑/↓  Space ticks  Enter selects  Esc cancels"


class MenuInputError(ValueError):
    """A finite refusal without input text, paths or terminal error details."""


def _label(value):
    try:
        valid = (type(value) is str and value.strip() and len(value.encode("utf-8")) <= MAX_LABEL_BYTES
                 and not any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in value))
    except UnicodeError:
        valid = False
    if not valid:
        raise MenuInputError("menu-label")
    return value


def _write(output, text):
    count = output.write(text)
    if type(count) is not int or count != len(text):
        raise MenuInputError("menu-output")
    output.flush()


def _clip(text, cells):
    if _width(text) <= cells:
        return text
    visible, used = [], 0
    for char in text:
        width = _width(char)
        if used + width > max(0, cells - 1):
            break
        visible.append(char)
        used += width
    return "".join(visible) + "…"


class _Frame:
    def __init__(self, output, title, options, hint=_HINT):
        self.output, self.title, self.options, self.hint = output, title, options, hint
        self.rows = len(options) + 2
        self.drawn = False

    def draw(self, selected):
        size = os.get_terminal_size(self.output.fileno())
        columns, rows = size.columns or 80, size.lines or 24
        if columns < 8 or rows < self.rows:
            raise MenuInputError("menu-size")
        lines = [self.title, *(('› ' if index == selected else '  ') + option
                              for index, option in enumerate(self.options)), self.hint]
        parts = ["\r"]
        if self.drawn:
            parts.append("\x1b[%dA" % (self.rows - 1))
        for index, line in enumerate(lines):
            style = ("bold",) if index == 0 else ("dim",) if index == len(lines) - 1 else \
                ("bold", "cyan") if index == selected + 1 else ()
            parts.append("\r\x1b[2K" + paint(_clip(line, columns - 1), *style, stream=self.output))
            if index + 1 < len(lines):
                parts.append("\r\n")
        # Cleanup must also clear a partially written frame on output failure.
        self.drawn = True
        _write(self.output, "".join(parts))

    def clear(self):
        if not self.drawn:
            return
        parts = ["\r\x1b[%dA" % (self.rows - 1)]
        for index in range(self.rows):
            parts.append("\r\x1b[2K")
            if index + 1 < self.rows:
                parts.append("\x1b[1B")
        parts.append("\x1b[%dA\r" % (self.rows - 1))
        _write(self.output, "".join(parts))
        self.drawn = False


def _paste(fd):
    tail = b""
    deadline = time.monotonic() + PASTE_SECONDS
    for _ in range(MAX_PASTE_BYTES + len(_PASTE_END)):
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            raise MenuInputError("menu-paste")
        tail = (tail + _byte(fd))[-len(_PASTE_END):]
        if tail == _PASTE_END:
            return
    raise MenuInputError("menu-paste")


def _modified_key(code, modifiers=1, event=1):
    held = (modifiers - 1) & ~(64 | 128)
    if event != 1:
        return "ignore"
    if held == 0 and code == 13:
        return "enter"
    if held == 0 and code == 27:
        return "cancel"
    if held == 4 and code in (99, 100):
        return "cancel"
    return "ignore"


def _escape(fd):
    if not select.select([fd], [], [], ESCAPE_SECONDS)[0]:
        return "cancel"
    first = _byte(fd)
    if first == b"O":
        if not select.select([fd], [], [], ESCAPE_SECONDS)[0]:
            raise MenuInputError("menu-key")
        return {b"A": "up", b"B": "down"}.get(_byte(fd), "ignore")
    if first != b"[":
        raise MenuInputError("menu-key")
    body = b""
    for _ in range(32):
        if not select.select([fd], [], [], ESCAPE_SECONDS)[0]:
            raise MenuInputError("menu-key")
        byte = _byte(fd)
        if not 0x20 <= byte[0] <= 0x7E:
            raise MenuInputError("menu-key")
        body += byte
        if 0x40 <= byte[0] <= 0x7E:
            break
    else:
        raise MenuInputError("menu-key")
    key = _without_locks(b"[" + body)
    if key == b"[200~":
        _paste(fd)
        return "ignore"
    if key in (b"[A", b"[B"):
        return "up" if key == b"[A" else "down"
    match = re.fullmatch(rb"\[(\d+)(?:;(\d+)(?::([123]))?)?u", key)
    if match:
        return _modified_key(int(match[1]), int(match[2] or 1), int(match[3] or 1))
    match = re.fullmatch(rb"\[27;(\d+);(\d+)~", key)
    if match:
        return _modified_key(int(match[2]), int(match[1]))
    return "ignore"


def choose(title, options, *, default=0, marks=None, markable=0):
    """Choose a trusted option by arrow keys and Enter; Escape/Ctrl+C/EOF cancel.

    No terminal is touched for an invalid label or redirected stream. Input
    already queued when the menu starts cancels it: discarding a queued paste
    prefix must not turn a later pasted newline into an authorization. Pending
    bytes are flushed again before returning to a subsequent login prompt.
    """
    title = _label(title)
    if type(options) not in (list, tuple) or not 1 <= len(options) <= MAX_OPTIONS:
        raise MenuInputError("menu-options")
    options = tuple(_label(option) for option in options)
    # `marks` (a set of indexes, changed in place) lets Space tick the first `markable` options as checkboxes. The
    # caller decides what the ticks mean; Enter still only selects the highlighted option.
    if marks is not None and (type(marks) is not set or type(markable) is not int
                              or not 0 <= markable <= len(options)):
        raise MenuInputError("menu-options")

    def shown():
        if marks is None:
            return options
        return tuple(("[x] " if index in marks else "[ ] ") + option if index < markable else option
                     for index, option in enumerate(options))
    if type(default) is not int or not 0 <= default < len(options):
        raise MenuInputError("menu-default")
    original = None
    fd = None
    frame = None
    failure = False
    answer = None
    output = sys.stdout
    try:
        if not sys.stdin.isatty() or not output.isatty():
            raise MenuInputError("menu-terminal")
        fd = sys.stdin.fileno()
        out_fd = output.fileno()
        if not os.isatty(fd) or not os.isatty(out_fd) or os.fstat(fd).st_rdev != os.fstat(out_fd).st_rdev:
            raise MenuInputError("menu-terminal")
        original = termios.tcgetattr(fd)
        raw = copy.deepcopy(original)
        raw[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INLCR | termios.IGNCR
                    | termios.INPCK | termios.ISTRIP | termios.IXON)
        raw[1] &= ~termios.OPOST
        raw[2] = (raw[2] & ~(termios.CSIZE | termios.PARENB)) | termios.CS8
        raw[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
        raw[6][termios.VMIN], raw[6][termios.VTIME] = 1, 0
        termios.tcsetattr(fd, termios.TCSANOW, raw)
        pending = bool(select.select([fd], [], [], 0)[0])
        # Do not flush an apparently empty queue here: a paste prefix arriving
        # just after select() must remain available to the escape parser.
        # The cancellation path and every completed decision flush in finally.
        if not pending:
            _write(output, "\x1b[?2004h\x1b[?25l")
            frame = _Frame(output, title, shown(), _HINT if marks is None else _HINT_MARKS)
            selected = default
            frame.draw(selected)
            while True:
                byte = _byte(fd)
                key = ("enter" if byte in (b"\r", b"\n") else "space" if byte == b" " else
                       "cancel" if byte == b"\x04" else _escape(fd) if byte == b"\x1b" else "ignore")
                if key == "cancel":
                    break
                if key == "enter":
                    answer = selected
                    break
                if key in ("up", "down"):
                    selected = (selected + (-1 if key == "up" else 1)) % len(options)
                    frame.draw(selected)
                if key == "space" and marks is not None and selected < markable:
                    marks.symmetric_difference_update({selected})
                    frame.options = shown()
                    frame.draw(selected)
    except (EOFError, KeyboardInterrupt):
        answer = None
    except (MenuInputError, OSError, termios.error, ValueError, UnicodeError, AttributeError, TypeError):
        failure = True
    finally:
        if original is not None:
            # Each cleanup step runs even if a previous one fails. A failed
            # restoration must never return a selected/authorized index.
            cleanup = ([frame.clear] if frame is not None else []) + [
                lambda: _write(output, "\x1b[?2004l\x1b[?25h"),
                lambda: termios.tcflush(fd, termios.TCIFLUSH),
                lambda: termios.tcsetattr(fd, termios.TCSANOW, original),
            ]
            for action in cleanup:
                try:
                    action()
                except (Exception, KeyboardInterrupt):
                    failure = True
    if failure:
        raise MenuInputError("menu-input") from None
    return answer
