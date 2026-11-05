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
    "ask-question": "Send this request to Claude? [y/n]: ",
    "files-read": "Read these files for the question? [y/n]: ",
    "save-thread": "Save this conversation locally so you can come back to it? [y/n]: ",
    "run-change": "This CHANGE can alter the system. Type yes to run it (or n): ",
    "run-undo": "The test failed. Run the undo shown above? Type yes (or n): ",
    "systemd-read": "Allow this systemd read? [y/n]: ",
    "weather": "Get the weather from Open-Meteo? [y/n]: ",
    "ask-transfer": "Send to Claude? [y/n]: ",
    "transfer": "Send the data shown to Claude? [y/n]: ",
    "material-transfer": "Send to Claude? [y/n]: ",
    "material-whole": "Send the whole file to Claude in these requests? [y/n]: ",
    "material-task": "Send to Claude? [y/n]: ",
    "project-task": "Send to Claude? [y/n]: ",
    "project-delete": "Delete these files (they go to the task trash and can be restored)? [y/n]: ",
    "restore": "Restore the deleted files? [y/n]: ",
    "project-continue": "Continue the analysis with Claude (next stage)? [y/n]: ",
    "project-edit": "Send these files to Claude and prepare a diff? [y/n]: ",
    "write": "Save the changes shown? [y/n]: ",
    "rollback": "Undo the changes shown? [y/n]: ",
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


STRICT = frozenset({"run-change", "run-undo"})        # only the whole word "yes" counts, never "y"


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
        if answer == "yes" or (answer == "y" and action not in STRICT):
            return True
        if answer in ("n", "no"):
            return False
        if answer:                       # a bare Enter just asks again; it never approves
            print("Type yes or n." if action in STRICT else "Type y or n.", flush=True)
