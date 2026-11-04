"""E5 durable write-ahead journal for the create-only workspace executor.

The journal lives in the explicitly chosen workspace root, is written before
the first target is created and is replaced atomically (temp file, fsync,
rename, directory fsync). It records only the approved plan hash and the
approved relative paths/contents plus the inode of each file once created.
No secrets, host paths or model output are stored. Same-UID tampering is out
of scope (see workspace_executor); malformed or foreign journals are refused.
"""
import json
import os
import re
import secrets
import stat

from .typed_plan import MAX_CONTENT_BYTES, MAX_STEPS, _BLOCKED, _SEGMENT

PREFIX = ".ai-desktop-journal-"
_NAME = re.compile(r"\.ai-desktop-journal-[0-9a-f]{32}\.json\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
STATUSES = ("pending", "applied", "rolled_back")
_MAX_BYTES = 256 * 1024
# Temp file is reused after an interrupted write: truncate, never follow links.
_FILE_FLAGS = (os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW |
               os.O_CLOEXEC | os.O_NONBLOCK)
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class JournalError(RuntimeError):
    """Sanitized journal refusal."""


def new_name():
    # Random, because the plan hash is deterministic and one plan may be
    # legitimately applied again after a completed rollback.
    return PREFIX + secrets.token_hex(16) + ".json"


def is_journal_name(name):
    return type(name) is str and _NAME.fullmatch(name) is not None


def new_document(plan_sha256, operations):
    return {"schema": 1, "plan_sha256": plan_sha256, "status": "pending",
            "entries": [{"path": path, "content": content, "inode": None}
                        for path, content in operations]}


def validate(doc):
    """Strict schema check; returns the document or raises JournalError."""
    try:
        if (type(doc) is not dict or set(doc) != {"schema", "plan_sha256",
                                                    "status", "entries"}
                or doc["schema"] != 1 or doc["status"] not in STATUSES
                or type(doc["plan_sha256"]) is not str
                or not _HEX64.fullmatch(doc["plan_sha256"])
                or type(doc["entries"]) is not list
                or not 1 <= len(doc["entries"]) <= MAX_STEPS):
            raise JournalError("JOURNAL: REFUSED")
        seen = set()
        for entry in doc["entries"]:
            if type(entry) is not dict or set(entry) != {"path", "content", "inode"}:
                raise JournalError("JOURNAL: REFUSED")
            path, content, inode = entry["path"], entry["content"], entry["inode"]
            if type(path) is not str or not path or len(path) > 160 or path in seen:
                raise JournalError("JOURNAL: REFUSED")
            segments = path.split("/")
            if len(segments) > 5 or any(not _SEGMENT.fullmatch(s) or s in _BLOCKED
                                        for s in segments):
                raise JournalError("JOURNAL: REFUSED")
            if (type(content) is not str or not content
                    or len(content.encode("utf-8")) > MAX_CONTENT_BYTES):
                raise JournalError("JOURNAL: REFUSED")
            if inode is not None and (type(inode) is not int or inode <= 0):
                raise JournalError("JOURNAL: REFUSED")
            seen.add(path)
    except (UnicodeError, TypeError, KeyError):
        raise JournalError("JOURNAL: REFUSED") from None
    return doc


def read(root_fd, name):
    if not is_journal_name(name):
        raise JournalError("JOURNAL: REFUSED")
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=root_fd)
    except OSError:
        raise JournalError("JOURNAL: REFUSED") from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_size > _MAX_BYTES):
            raise JournalError("JOURNAL: REFUSED")
        raw = os.read(fd, info.st_size + 1)
        if len(raw) != info.st_size:
            raise JournalError("JOURNAL: REFUSED")
        doc = validate(json.loads(raw.decode("utf-8")))
        return doc
    except (OSError, ValueError, UnicodeError):
        raise JournalError("JOURNAL: REFUSED") from None
    finally:
        os.close(fd)


def exists(root_fd, name):
    try:
        os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def write(root_fd, name, doc):
    """Atomically replace/create the journal; durable once this returns."""
    validate(doc)
    raw = (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(raw) > _MAX_BYTES:
        raise JournalError("JOURNAL: REFUSED")
    tmp = name + ".tmp"
    try:
        try:
            info = os.stat(tmp, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_uid != os.geteuid()):
                raise OSError("unsafe journal temp file")
        fd = os.open(tmp, _FILE_FLAGS, 0o600, dir_fd=root_fd)
        try:
            with os.fdopen(os.dup(fd), "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(fd)
        os.rename(tmp, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
    except OSError:
        raise JournalError("JOURNAL: REFUSED") from None


def list_names(root_fd):
    try:
        names = os.listdir(root_fd)
    except OSError:
        raise JournalError("JOURNAL: REFUSED") from None
    return sorted(n for n in names if is_journal_name(n))
