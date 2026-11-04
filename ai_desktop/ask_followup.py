"""Bounded collision dialogue. User text is never a filesystem authorization.

Only an existing-target refusal reaches this UI. No original contents are read
or sent here; the next normal planner turn revalidates read/write permissions,
gets fresh transfer consent and still needs a separately approved tested diff.
"""
import json

from .analysis_contract import MAX_QUESTION_BYTES, validate_question
from .permissions import _absolute
from .question_input import FOLLOWUP_PROMPT, QuestionInputError, read_question
from .task_contract import path as validate_path

MAX_FOLLOWUPS = 2
CANCEL = frozenset({"anuluj", "anuluj.", "nie", "stop", "cancel", "wyjdź", "wyjdz", "rezygnuję", "rezygnuje"})


def clarify(question, path, *, workspace, reader=input):
    """Ask once, then return a bounded next question or explicit cancellation."""
    validate_question(question)
    validate_path(path)
    _absolute(workspace)
    # JSON quoting preserves a selected name as data, not a suggested command.
    label = json.dumps(path, ensure_ascii=False)
    prefix = ("Poprzednie zadanie użytkownika:\n" + question +
              "\nKontroler odmówił UTWORZENIA, bo wskazana ścieżka już istnieje: " + label +
              ". Nie odczytano jej treści z samego powodu kolizji.\n"
              "Doprecyzowanie użytkownika (przy podmianie wybierz istniejący plik do odczytu i edycji; "
              "przy nowej nazwie wybierz nowy cel, bez nadpisywania starego):\n")
    remaining = MAX_QUESTION_BYTES - len(prefix.encode("utf-8"))
    if remaining < 1:
        raise QuestionInputError("follow-up question limit")
    # Show the checked location locally; preserve the relative-only prefix
    # sent to the next planner. Do not resolve or read the colliding target.
    directory, filename = (workspace + "/" + path).rsplit("/", 1)
    location = json.dumps(directory, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    print("\nPlik " + json.dumps(filename, ensure_ascii=False) + " istnieje w " + location + ".")
    print("Edytować, stworzyć nowy, anulować?")
    reply = (read_question(max_bytes=remaining, followup=True) if reader is input
             else reader(FOLLOWUP_PROMPT))
    validate_question(reply)
    if len(reply.encode("utf-8")) > remaining:
        raise QuestionInputError("follow-up question limit")
    print()
    if not reply.strip() or reply.strip().casefold() in CANCEL:
        return None
    revised = prefix + reply.strip()
    validate_question(revised)
    print("Przygotuję nowy plan według tej odpowiedzi.")
    return revised
