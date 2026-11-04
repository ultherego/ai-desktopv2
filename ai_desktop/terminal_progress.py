"""UI-only waiting indicator: fixed labels, no model data, signals or input.

The caller starts it only AFTER managed_session has finished authorization.
It is stopped before returning to another prompt or rendering an answer.
It reports the request phase, not tool events, token progress or completion %.
"""
import os
import sys
import threading
import time

from .execution import UNSET
from .run_controls import recipient
from .terminal_style import paint

LABELS = {
    "assistant": "Analizowanie pytania",
    "web": "Szukanie i analizowanie źródeł",
    "question": "Opracowywanie odpowiedzi",
    "workspace": "Przygotowywanie propozycji",
    "cleanup": "Kończenie sesji",
    "task": "Claude pracuje",
}
INTERVAL = 0.3
CLEAR_LINE = "\r\x1b[2K"


class WaitingIndicator:
    """A disposable animation; any display failure leaves the request alone."""

    def __init__(self, *, output_format="auto", stream=None, config=UNSET):
        self.stream = sys.stdout if stream is None else stream
        # ContextVars are not inherited by the animation thread. Capture the
        # controller's validated task recipient before creating any thread.
        name = recipient(config=config)
        self._labels = {phase: (name + " pracuje" if phase == "task" else name + ": " + text)
                        for phase, text in LABELS.items()}
        self._enabled = False
        try:
            self._enabled = (output_format == "auto" and "NO_COLOR" not in os.environ
                and os.environ.get("TERM", "") not in ("", "dumb", "unknown")
                and self.stream.isatty() and os.isatty(self.stream.fileno()))
        except (OSError, ValueError, AttributeError):
            pass
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._label = ""
        self._started = None
        self._closed = False
        self._drawn = False

    def __enter__(self):
        # No drawing/thread until phase(): sudo and consent retain the terminal.
        return self

    def phase(self, name):
        if type(name) is not str or name not in LABELS:
            raise ValueError("unknown waiting phase")
        if not self._enabled or self._closed:
            return
        with self._lock:
            self._label = self._labels[name]
            if self._started is None:
                self._started = time.monotonic()
        self._paint()
        if self._thread is None and not self._stop.is_set():
            thread = threading.Thread(target=self._animate, name="ai-desktop-wait", daemon=True)
            try:
                thread.start()
            except RuntimeError:
                self._stop.set()
            else:
                self._thread = thread

    def _paint(self):
        with self._lock:
            if self._stop.is_set() or self._started is None:
                return
            try:
                elapsed = max(0, time.monotonic() - self._started)
                dots = "." * (int(elapsed / INTERVAL) % 3 + 1)
                text = f"{self._label}{dots:<3} {int(elapsed)} s"
                frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
                unicode_output = (getattr(self.stream, "encoding", None) or "").lower().replace("-", "") in ("utf8", "utf")
                frame = frames[int(elapsed / INTERVAL) % len(frames)] if unicode_output else ""
                try:
                    columns = os.get_terminal_size(self.stream.fileno()).columns
                except (OSError, ValueError, AttributeError):
                    columns = 80
                # Never wrap an animated line, including after terminal resize.
                room = max(0, min(160, columns or 80) - 1)
                if frame and room > 2:
                    text = paint(frame, "accent", "bold", stream=self.stream) + " " + paint(text[:room - 2], "muted", stream=self.stream)
                else:
                    text = text[:room]
                self.stream.write(CLEAR_LINE + text)
                self.stream.flush()
                self._drawn = True
            except (OSError, ValueError, UnicodeError):
                self._stop.set()

    def _animate(self):
        while not self._stop.wait(INTERVAL):
            self._paint()

    def stop(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        with self._lock:
            if self._drawn:
                try:
                    self.stream.write(CLEAR_LINE)
                    self.stream.flush()
                except (OSError, ValueError, UnicodeError):
                    pass
                self._drawn = False

    def __exit__(self, *_):
        self.stop()
        return False
