"""Local test-copy bounds, independent of material sent to a model.

This contract is deliberately not accepted by the model request parser. A
project copy is selected by the controller from the owner's file policy.
"""
import re

MAX_FILES = 20000
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
# UTF-8 text permitted by file_text can double in JSON (quotes, slashes,
# carriage returns and newlines); path and record metadata have their own cap.
MAX_PAYLOAD = 2 * MAX_TOTAL + MAX_FILES * 1024 + 1024
WORK_BYTES = MAX_TOTAL + 32 * 1024 * 1024
MEMORY_BYTES = 2 * 1024 * 1024 * 1024
_SEGMENT = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,254}\Z")


def path(value):
    """A local-only copy path; deeper than edit targets, never host authority."""
    if type(value) is not str or not 0 < len(value.encode("utf-8")) <= 512:
        raise ValueError("test path")
    parts = value.split("/")
    if len(parts) > 16 or any(
            not _SEGMENT.fullmatch(part) or part in (".", "..")
            or part.casefold() == ".ai-desktop-tasks" or part.casefold().startswith(".ai-task-")
            for part in parts):
        raise ValueError("test path")
    return value


def file_text(value):
    from .task_contract import file_text as validate
    return validate(value, MAX_FILE)


def validate_files(files):
    """Validate the entire copy before launching a process; never truncate it."""
    if type(files) is not dict or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("test files")
    total = 0
    for name, content in files.items():
        path(name)
        file_text(content)
        total += len(content.encode("utf-8"))
        if total > MAX_TOTAL:
            raise ValueError("test size")
    return total
