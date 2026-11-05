"""Colour for the program's OWN prompts and status lines; never applied to model or command text.

Off when the stream is not a terminal, NO_COLOR is set or TERM is dumb. The text handed in must already be safe
(terminal_text); this module only wraps it in fixed SGR sequences.
"""
import os
import sys

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}
# The theme: 24-bit colour where the terminal declares it, the nearest basic colour elsewhere.
_THEME = {"accent": ((94, 234, 255), "96"), "violet": ((180, 140, 255), "95"), "muted": ((132, 140, 160), "90")}
_RULE_FROM, _RULE_TO = (94, 234, 255), (180, 140, 255)
RULE_WIDTH = 48


def enabled(stream=None):
    stream = sys.stdout if stream is None else stream
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):
        return False
    return tty and "NO_COLOR" not in os.environ and os.environ.get("TERM", "") not in ("", "dumb", "unknown")


def _truecolor():
    return os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")


def _code(name):
    if name in _THEME:
        rgb, basic = _THEME[name]
        return "38;2;%d;%d;%d" % rgb if _truecolor() else basic
    return _CODES.get(name)


def paint(text, *styles, stream=None):
    codes = [code for code in map(_code, styles) if code]
    if not codes or not enabled(stream):
        return text
    return "\x1b[%sm%s\x1b[0m" % (";".join(codes), text)


def _unicode(stream=None):
    stream = sys.stdout if stream is None else stream
    return (getattr(stream, "encoding", None) or "").lower().replace("-", "") in ("utf8", "utf")


def symbol(name, stream=None):
    """Status marks; plain ASCII when the terminal cannot show them."""
    marks = {"ok": ("✔", "+"), "fail": ("✘", "x"), "arrow": ("→", "->"), "dot": ("·", "-"), "pointer": ("›", ">"),
             "skip": ("–", "-")}
    fancy, plain = marks[name]
    return fancy if _unicode(stream) else plain


def greeting(text, stream=None):
    """The program's own line that opens a conversation: it has to be the most visible text on the screen."""
    if not enabled(stream):
        return text
    mark = "◆ " if _unicode(stream) else ""
    return paint(mark, "violet", "bold", stream=stream) + paint(text, "accent", "bold", stream=stream)


def rule(stream=None):
    """The line around an answer: plain without colour, a colour gradient where the terminal can show one."""
    plain = "─" * RULE_WIDTH
    if not enabled(stream) or not _unicode(stream):
        return plain
    if not _truecolor():
        return paint(plain, "accent", stream=stream)
    span = RULE_WIDTH - 1
    cells = []
    for index in range(RULE_WIDTH):
        rgb = [round(a + (b - a) * index / span) for a, b in zip(_RULE_FROM, _RULE_TO)]
        cells.append("\x1b[38;2;%d;%d;%dm─" % tuple(rgb))
    return "".join(cells) + "\x1b[0m"


def size_label(count):
    """A byte count as a person reads it."""
    if count < 1024:
        return "%d B" % count
    return "%.1f KiB" % (count / 1024)


def _fit(text, room):
    return text if len(text) <= room else text[:max(room - 1, 1)] + "…"


def card(title, rows, *, tone="cyan", width=None, stream=None):
    """A framed block: a title in the top edge, one row per line. Text must already be safe (terminal_text)."""
    stream = sys.stdout if stream is None else stream
    if width is None:
        try:
            width = os.get_terminal_size(stream.fileno()).columns
        except (OSError, ValueError, AttributeError):
            width = 80
    width = max(36, min(width - 2, 96))
    inner = width - 4
    if _unicode(stream):
        top_l, top_r, bottom_l, bottom_r, side, line = "╭", "╮", "╰", "╯", "│", "─"
    else:
        top_l, top_r, bottom_l, bottom_r, side, line = "+", "+", "+", "+", "|", "-"
    head = _fit(title, inner - 2)
    lines = [paint(top_l + line + " " + head + " " + line * (width - 5 - len(head)) + top_r, tone, "bold", stream=stream)]
    for row in rows:
        # Never truncate a row: the owner approves exactly what is shown, so a long command wraps onto more lines.
        for start in range(0, max(len(row), 1), inner):
            cell = row[start:start + inner]
            lines.append(paint(side, tone, stream=stream) + " " + cell + " " * (inner - len(cell)) + " "
                         + paint(side, tone, stream=stream))
    lines.append(paint(bottom_l + line * (width - 2) + bottom_r, tone, stream=stream))
    return lines
