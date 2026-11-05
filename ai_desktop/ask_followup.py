"""Bounded collision dialogue. User text is never a filesystem authorization.

Only an existing-target refusal reaches this UI. No original contents are read
or sent here; the next normal planner turn revalidates read/write permissions,
gets fresh transfer consent and still needs a separately approved tested diff.
"""
import json

from .analysis_contract import MAX_MESSAGE_BYTES, validate_question
from .permissions import _absolute
from .question_input import FOLLOWUP_PROMPT, QuestionInputError, read_question
from .task_contract import path as validate_path

MAX_FOLLOWUPS = 2
# Free text follows the user's language, so Polish cancel words stay next to the English ones.
CANCEL = frozenset({"anuluj", "anuluj.", "nie", "stop", "cancel", "wyjdź", "wyjdz", "rezygnuję", "rezygnuje",
                    "no", "quit", "exit"})


def clarify(question, path, *, workspace, reader=input):
    """Ask once, then return a bounded next question or explicit cancellation."""
    validate_question(question, MAX_MESSAGE_BYTES)
    validate_path(path)
    _absolute(workspace)
    # JSON quoting preserves a selected name as data, not a suggested command.
    label = json.dumps(path, ensure_ascii=False)
    prefix = ("The user's previous task:\n" + question +
              "\nThe controller refused to CREATE it because the given path already exists: " + label +
              ". Its contents were not read just because of the collision.\n"
              "The user's clarification (to replace it, select the existing file to read and edit; "
              "for a new name, select a new target without overwriting the old one):\n")
    remaining = MAX_MESSAGE_BYTES - len(prefix.encode("utf-8"))
    if remaining < 1:
        raise QuestionInputError("follow-up question limit")
    # Show the checked location locally; preserve the relative-only prefix
    # sent to the next planner. Do not resolve or read the colliding target.
    directory, filename = (workspace + "/" + path).rsplit("/", 1)
    location = json.dumps(directory, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    print("\nFile " + json.dumps(filename, ensure_ascii=False) + " exists in " + location + ".")
    print("Edit it, create a new one, or cancel?")
    reply = (read_question(max_bytes=remaining, followup=True) if reader is input
             else reader(FOLLOWUP_PROMPT))
    validate_question(reply)
    if len(reply.encode("utf-8")) > remaining:
        raise QuestionInputError("follow-up question limit")
    print()
    if not reply.strip() or reply.strip().casefold() in CANCEL:
        return None
    revised = prefix + reply.strip()
    validate_question(revised, MAX_MESSAGE_BYTES)
    print("I will prepare a new plan based on this answer.")
    return revised
