"""Display untrusted Markdown without commands, pagers, images or active links.

Rendering happens after the analysis session has stopped. Nothing here can
grant authority to model output. In raw/fallback mode all controls stay literal.
"""
import copy
import io
import os
import re
import sys

from .analysis_contract import MAX_TEXT, terminal_text

MAX_RENDERED = 262144


def fenced_code(text, *, language=""):
    """Frame literal text without wrapping it or trusting embedded delimiters."""
    if type(text) is not str or type(language) is not str or not re.fullmatch(r"[A-Za-z0-9_+.-]{0,32}", language):
        raise ValueError("invalid code block")
    clean = terminal_text(text)
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", clean)), default=0))
    return fence + language + "\n" + clean + ("" if clean.endswith("\n") else "\n") + fence


def _width(stream):
    try:
        columns = os.get_terminal_size(stream.fileno()).columns
        return max(20, min(160, columns)) if columns > 0 else 80
    except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
        return 80


def _reference_breaks(token):
    """Keep explicit [n] lists separate; never split inline citations or code."""
    if token.type != "inline" or not token.children:
        return
    lines = token.content.split("\n")
    entries = lines[1:] if lines[0] == "Źródła:" else lines
    if not 1 <= len(entries) <= 32 or len(lines) < 2:
        return
    if any(not re.fullmatch(r"\[" + str(index) + r"\] .+", line)
           for index, line in enumerate(entries, 1)):
        return
    children = []
    for child in token.children:
        if child.type == "softbreak":
            child.type = "hardbreak"
            children.append(copy.copy(child))
        children.append(child)
    token.children = children


def _render(text, *, width, styled):
    # Lazy UI-only dependency: never imported by the privileged helper/worker.
    from rich.console import Console
    from rich.markdown import CodeBlock, Markdown
    from rich.text import Text

    class LiteralCode(CodeBlock):
        def __rich_console__(self, console, options):
            # No lexer selected by model text, truncation or inserted linewrap
            # in command lines. The terminal can soft-wrap the original line.
            yield Text(self.text.plain.rstrip("\n"), no_wrap=True, overflow="ignore")

    class AnswerMarkdown(Markdown):
        elements = dict(Markdown.elements, fence=LiteralCode, code_block=LiteralCode)

    document = AnswerMarkdown(text, hyperlinks=False)
    pending, count, code = list(document.parsed), 0, {}
    marker = "@CODE"
    while marker in text:
        marker += "X"
    while pending:
        token = pending.pop()
        count += 1
        if count > 4096 or token.type == "html_block":
            raise ValueError("render complexity")
        # Entities are decoded by the Markdown parser. Sanitize AGAIN so e.g.
        # &#x1b; or a bidi entity cannot reintroduce terminal controls.
        token.content = terminal_text(token.content)
        _reference_breaks(token)
        if token.type in ("fence", "code_block"):
            key = marker + str(len(code)) + "@"
            # The label is display-only: it never selects a lexer, command or
            # renderer. Unrecognised info strings are omitted, not evaluated.
            label = token.info.strip()
            language = label if re.fullmatch(r"[A-Za-z0-9_+.-]{0,32}", label) else ""
            code[key] = fenced_code(token.content, language=language)
            token.content = key
        if token.type == "html_inline":
            token.type, token.tag = "text", ""
        if token.attrs:
            token.attrs = {key: terminal_text(str(value)) for key, value in token.attrs.items()}
        pending.extend(token.children or [])

    buffer = io.StringIO()
    console = Console(file=buffer, width=width, color_system="standard" if styled else None,
                      force_terminal=styled, force_jupyter=False, force_interactive=False,
                      no_color=not styled, markup=False, highlight=False, safe_box=True)
    console.print(document, crop=False)
    result = buffer.getvalue().rstrip("\n")
    # Lists and quotes may crop their children even when Text says no_wrap.
    # Expand code only AFTER all layout; a missing/cropped marker fails back
    # to the complete literal response rather than displaying a partial command.
    for key, literal in code.items():
        if result.count(key) != 1:
            raise ValueError("cropped code")
        result = "\n".join(literal if key in line else line for line in result.split("\n"))
    if len(result.encode("utf-8")) > MAX_RENDERED:
        raise ValueError("render size")
    return result


def render_answer(text, *, output_format="auto", stream=None):
    if type(text) is not str or len(text.encode("utf-8")) > MAX_TEXT * 6:
        raise ValueError("invalid output")
    if output_format not in ("auto", "plain", "markdown"):
        raise ValueError("invalid format")
    clean = terminal_text(text)
    if output_format == "markdown":
        return clean
    stream = sys.stdout if stream is None else stream
    styled = (output_format == "auto" and stream.isatty()
              and "NO_COLOR" not in os.environ
              and os.environ.get("TERM", "") not in ("dumb", "unknown"))
    try:
        return _render(clean, width=_width(stream), styled=styled)
    except Exception:
        # A missing renderer or malformed presentation must never cause an
        # automatic retry of a request, hide its answer or print exception data.
        return clean
