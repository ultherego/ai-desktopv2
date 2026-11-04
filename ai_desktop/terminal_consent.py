"""Short, explicit terminal decisions; exact authorization stays in the caller.

This helper never creates a plan or an authorization. The trusted caller binds
yes to the shown plan or to one question and its captured permissions. Task
write/rollback approvals retain their exact previews and independent checks.
"""
import os
import sys
import termios

from .execution import UNSET
from .run_controls import recipient, recipient_config
from .terminal_style import paint


PROMPTS = {
    "ask-question": "Przekazać żądanie do Claude [t/n]: ",
    "files-read": "Odczytać te pliki do pytania? [t/n]: ",
    "save-thread": "Zapisać tę rozmowę lokalnie, żeby do niej wrócić? [t/n]: ",
    "run-change": "Ta ZMIANA może zmienić stan systemu. Wpisz tak, aby ją wykonać (albo n): ",
    "run-undo": "Test niezaliczony. Wykonać wycofanie pokazane wyżej? Wpisz tak (albo n): ",
    "systemd-read": "Zezwolić na ten odczyt systemd? [t/n]: ",
    "weather": "Pobrać pogodę z Open-Meteo? [t/n]: ",
    "ask-transfer": "Wysłać do Claude? [t/n]: ",
    "transfer": "Wysłać pokazane dane do Claude? [t/n]: ",
    "material-transfer": "Przekazać do Claude? [t/n]: ",
    "material-whole": "Przekazać cały plik do Claude w tych żądaniach? [t/n]: ",
    "material-task": "Przekazać do Claude? [t/n]: ",
    "project-task": "Przekazać do Claude? [t/n]: ",
    "project-delete": "Usunąć te pliki (trafią do kosza zadania, można je przywrócić)? [t/n]: ",
    "restore": "Przywrócić usunięte pliki? [t/n]: ",
    "project-continue": "Kontynuować analizę z Claude (kolejny etap)? [t/n]: ",
    "project-edit": "Przekazać te pliki do Claude i przygotować diff? [t/n]: ",
    "write": "Zapisać pokazane zmiany? [t/n]: ",
    "rollback": "Cofnąć pokazane zmiany? [t/n]: ",
}
MAX_ANSWER_BYTES = 64


def fresh_input(prompt, *, max_bytes=MAX_ANSWER_BYTES):
    """Read one new canonical TTY line, without Python's input read-ahead.

    Clearing the kernel queue before the prompt prevents a pasted second yes
    from approving a later action that has not been shown yet. Reading bytes
    directly also avoids leaving a second line in a Python userspace buffer.
    This is not a defense against another process controlling the same TTY.
    """
    if type(max_bytes) is not int or not 1 <= max_bytes <= 4096:
        raise ValueError("terminal answer limit")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("terminal required")
    fd = sys.stdin.fileno()
    try:
        attributes = termios.tcgetattr(fd)
        if not attributes[3] & termios.ICANON:
            raise ValueError("canonical terminal required")
        termios.tcflush(fd, termios.TCIFLUSH)
    except termios.error:
        raise OSError("terminal input unavailable") from None
    sys.stdout.write(prompt)
    sys.stdout.flush()
    answer = bytearray()
    while len(answer) <= max_bytes:
        byte = os.read(fd, 1)
        if not byte:
            raise EOFError()
        if byte == b"\n":
            return answer.decode("utf-8")
        answer.extend(byte)
    raise ValueError("terminal answer too long")


STRICT = frozenset({"run-change", "run-undo"})        # only the whole word "tak" counts, never "t"


def confirm(action, *, reader=input, config=UNSET):
    """Require an explicit yes/no; retries do not renew authorization."""
    if type(action) is not str or action not in PROMPTS:
        raise ValueError("consent action")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    # Retries ask about the same task recipient. A reader callback or another
    # controller scope cannot relabel this pending decision as another service.
    selected = recipient_config(config)
    while True:
        # Every prompt, including a correction, requires a fresh terminal line.
        prompt = recipient(PROMPTS[action], config=selected)
        answer = fresh_input(paint(prompt, "bold", "red" if action in STRICT else "yellow")) \
            if reader is input else reader(prompt)
        if type(answer) is not str:
            raise ValueError("terminal answer type")
        answer = answer.strip().casefold()
        if answer == "tak" or (answer == "t" and action not in STRICT):
            return True
        if answer in ("n", "nie"):
            return False
        if answer:                       # a bare Enter just asks again; it never approves
            print("Wpisz tak albo n." if action in STRICT else "Wpisz t albo n.", flush=True)
