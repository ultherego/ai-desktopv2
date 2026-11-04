"""Recoverable deletion for approved project files: files are MOVED into the private task state directory, never unlinked.

A deletion is journaled first (intent), then each file is renamed (atomic, same filesystem) into
`.ai-desktop-tasks/trash-<plan>/`, then the journal is marked. `restore` moves each file back only when its original
name is free and the stored file still has the recorded identity and size, so a later edit or a recreated file is never
overwritten (it is reported as a conflict). A crash between any two steps leaves either the file in place or in the
trash, and `restore` decides from the filesystem, not from the journal alone. Same-UID hostile renames are outside the
boundary, as for the other task executors.
"""
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time

from . import task_contract as contract
from . import task_workspace as tw
from .analysis_contract import canonical, digest
from .e2_ipc import _unique_pairs
from .fs_publish import rename_noreplace as _rename_noreplace
from .workspace_executor import ExecutionDenied, _parent

MAX_DELETE = 8
MAX_FILE_BYTES = 16 * 1024 * 1024
_PLAN = re.compile(r"[a-f0-9]{32}\Z")
_STASH = re.compile(r"[0-9]\Z")

@dataclass(frozen=True)
class DeleteAuthorization:
    sha256: str


class DeletePlan:
    """Immutable version snapshot; the trusted UI issues one approval after its preview."""
    def __init__(self, value, *, clock=time.monotonic):
        self._payload = canonical(value)
        self.sha256 = digest(self._payload)
        self._clock, self._expires = clock, clock() + 180
        self._issued, self._used = None, False

    def data(self):
        return json.loads(self._payload)

    def authorize(self):
        if self._used or self._issued is not None or self._clock() > self._expires:
            raise ExecutionDenied("TRASH: REFUSED approval")
        self._issued = DeleteAuthorization(self.sha256)
        return self._issued

    def consume(self, authorization):
        valid = (not self._used and self._issued is not None and authorization is self._issued
                 and self._clock() <= self._expires)
        self._used = True
        if not valid:
            raise ExecutionDenied("TRASH: REFUSED approval")
        return self.data()


class TrashRecoveryRequired(ExecutionDenied):
    """Some effects may remain; retain the exact journal ID even if undo or journaling failed."""
    def __init__(self, plan):
        self.plan = plan
        super().__init__("TRASH: recovery required")



def _journal_name(plan):
    return plan + ".trash.json"


def _stash_dir(plan):
    return "trash-" + plan


def _fingerprint(parent, leaf):
    """Identity, size, mode and SHA-256 of one regular file this user owns; refuses anything unusual."""
    fd = os.open(leaf, tw.READ, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or info.st_mode & 0o7000 or info.st_size > MAX_FILE_BYTES):
            raise ValueError("file policy")
        digest, total = hashlib.sha256(), 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise ValueError("file grew")
            digest.update(chunk)
        after = os.fstat(fd)
        if total != info.st_size or after.st_mtime_ns != info.st_mtime_ns or after.st_ino != info.st_ino:
            raise ValueError("changed file")
        return {"dev": info.st_dev, "ino": info.st_ino, "size": info.st_size,
                "mode": stat.S_IMODE(info.st_mode), "sha256": digest.hexdigest()}
    finally:
        os.close(fd)


def _check_journal(value, name):
    entries = value.get("entries") if type(value) is dict else None
    if (type(value) is not dict or set(value) != {"schema", "plan", "workspace", "entries", "status"}
            or value["schema"] != 1 or type(value["plan"]) is not str or not _PLAN.match(value["plan"])
            or _journal_name(value["plan"]) != name or type(value["workspace"]) is not str
            or value["status"] not in ("prepared", "trashed", "restored", "conflict")
            or type(entries) is not list or not 1 <= len(entries) <= MAX_DELETE):
        raise ValueError("trash journal")
    seen = set()
    for index, item in enumerate(entries):
        if (type(item) is not dict or set(item) != {"path", "stash", "dev", "ino", "size", "mode", "sha256", "state"}
                or contract.path(item["path"]) in seen or item["stash"] != str(index)
                or item["state"] not in ("prepared", "trashed", "restored")
                or any(type(item[k]) is not int or isinstance(item[k], bool) or item[k] < 0 for k in ("dev", "ino", "size", "mode"))
                or item["mode"] & ~0o7777 or type(item["sha256"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            raise ValueError("trash journal entry")
        seen.add(item["path"])
    return value


def _load(state_fd, name):
    fd = os.open(name, tw.READ, dir_fd=state_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1 or info.st_size > 256 * 1024):
            raise ValueError("trash journal file")
        raw = os.read(fd, 256 * 1024 + 1)
        if len(raw) != info.st_size:
            raise ValueError("trash journal file")
        return _check_journal(json.loads(raw, object_pairs_hook=_unique_pairs), name)
    finally:
        os.close(fd)


def _selection(names):
    try:
        names = [contract.path(n) for n in names]
        if not 1 <= len(names) <= MAX_DELETE or len(set(names)) != len(names):
            raise ValueError("selection")
    except ValueError:
        raise ExecutionDenied("TRASH: REFUSED selection") from None
    return names


def _inspect(root, names):
    entries = []
    for index, name in enumerate(names):
        parent, leaf = _parent(root, name)
        try:
            entries.append(dict(_fingerprint(parent, leaf), path=name, stash=str(index), state="prepared",
                                parent=tw.identity(os.fstat(parent))))
        finally:
            os.close(parent)
    return entries


def prepare(workspace, names, *, clock=time.monotonic):
    """Capture versions BEFORE the deletion question. No journal or filesystem mutation."""
    names = _selection(names)
    root = tw._root(workspace)
    try:
        return DeletePlan({"workspace": workspace, "root": tw.identity(os.fstat(root)),
                           "entries": _inspect(root, names), "plan": secrets.token_hex(16)}, clock=clock)
    except (OSError, ValueError):
        raise ExecutionDenied("TRASH: REFUSED file") from None
    finally:
        os.close(root)


def trash(workspace, names):
    """Low-level convenience for trusted callers; interactive UI uses prepare -> approval -> execute."""
    plan = prepare(workspace, names)
    return execute(plan, plan.authorize())


def execute(prepared, authorization):
    """Consume one approval and move only the versions shown by the trusted UI."""
    if type(prepared) is not DeletePlan:
        raise ExecutionDenied("TRASH: REFUSED plan")
    before = prepared.consume(authorization)
    workspace, plan = before["workspace"], before["plan"]
    root = tw._root(workspace)
    moved = []
    try:
        fcntl.flock(root, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if tw.identity(os.fstat(root)) != before["root"]:
            raise ValueError("changed root")
        # Check all versions under the task lock BEFORE creating recovery state.
        if _inspect(root, [e["path"] for e in before["entries"]]) != before["entries"]:
            raise ValueError("changed approved files")
        state = tw._state_dir(root, create=True)
        try:
            entries = [{k: v for k, v in entry.items() if k != "parent"} for entry in before["entries"]]
            journal_name = _journal_name(plan)
            journal = {"schema": 1, "plan": plan, "workspace": workspace, "entries": entries, "status": "prepared"}
            tw._save(state, journal_name, journal, first=True)          # intent is durable before any file moves
            os.mkdir(_stash_dir(plan), 0o700, dir_fd=state)
            os.fsync(state)
            stash = os.open(_stash_dir(plan), tw.DIR, dir_fd=state)
            try:
                for entry, expected in zip(entries, before["entries"]):
                    parent, leaf = _parent(root, entry["path"])
                    try:
                        now = _fingerprint(parent, leaf)
                        if (tw.identity(os.fstat(parent)) != expected["parent"]
                                or now != {k: entry[k] for k in ("dev", "ino", "size", "mode", "sha256")}):
                            raise ValueError("changed since the scan")
                        os.rename(leaf, entry["stash"], src_dir_fd=parent, dst_dir_fd=stash)
                        # Include the move in recovery before any later operation can fail.
                        moved.append(entry)
                        os.fsync(parent)
                        os.fsync(stash)
                        # Another editor may replace the source between fingerprint and
                        # rename. Inspect the detached entry before accepting this move.
                        detached = _fingerprint(stash, entry["stash"])
                        if detached != now:
                            # Recovery concerns the file we actually detached, never
                            # an older version that another editor already replaced.
                            entry.update(detached)
                            raise ValueError("changed during move")
                        entry["state"] = "trashed"
                        tw._save(state, journal_name, journal)
                    finally:
                        os.close(parent)
                journal["status"] = "trashed"
                tw._save(state, journal_name, journal)
            except (OSError, ValueError):
                conflicts = _restore_entries(root, state, stash, plan, moved)
                for entry in entries:
                    if not any(entry is done for done in moved):
                        entry["state"] = "restored"   # never moved; later user edits are not our recovery work
                journal["status"] = "conflict" if conflicts else "restored"
                try:
                    tw._save(state, journal_name, journal)
                except (OSError, ValueError):
                    conflicts += 1
                if conflicts:
                    raise TrashRecoveryRequired(plan) from None
                raise ExecutionDenied("TRASH: REFUSED changed file or filesystem") from None
            finally:
                os.close(stash)
        finally:
            os.close(state)
    except ExecutionDenied:
        raise
    except (OSError, ValueError):
        raise ExecutionDenied("TRASH: REFUSED workspace") from None
    finally:
        os.close(root)
    return plan


def _restore_entries(root, state, stash, plan, entries):
    """Put each trashed entry back if its name is free and the stored file is the recorded one; count conflicts."""
    conflicts = 0
    for entry in reversed(entries):
        try:
            parent, leaf = _parent(root, entry["path"])
        except (OSError, ExecutionDenied):
            conflicts += 1
            continue
        try:
            try:
                os.lstat(leaf, dir_fd=parent)
            except FileNotFoundError:
                pass
            else:
                # The name exists. If it is the recorded file itself (the move never happened) there is nothing to
                # restore; anything else is a conflict and is never overwritten.
                try:
                    here = _fingerprint(parent, leaf)
                except (OSError, ValueError):
                    here = None
                if here is not None and {k: here[k] for k in ("dev", "ino", "size", "sha256")} == \
                        {k: entry[k] for k in ("dev", "ino", "size", "sha256")}:
                    entry["state"] = "restored"
                else:
                    conflicts += 1
                continue
            try:
                held = _fingerprint(stash, entry["stash"])
            except (OSError, ValueError):
                conflicts += 1
                continue
            if {k: held[k] for k in ("dev", "ino", "size", "sha256")} != {k: entry[k] for k in ("dev", "ino", "size", "sha256")}:
                conflicts += 1
                continue
            _rename_noreplace(stash, entry["stash"], parent, leaf)
            os.fsync(parent)
            entry["state"] = "restored"
        except OSError:
            conflicts += 1
        finally:
            os.close(parent)
    return conflicts


def restore(workspace, plan):
    """Undo one deletion. Returns True when every file is back; False (journal kept) on any conflict."""
    if type(plan) is not str or not _PLAN.match(plan):
        raise ExecutionDenied("TRASH: REFUSED plan")
    root = tw._root(workspace)
    try:
        fcntl.flock(root, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = tw._state_dir(root)
        try:
            name = _journal_name(plan)
            journal = _load(state, name)
            if journal["workspace"] != workspace:
                raise ValueError("workspace")
            pending = [e for e in journal["entries"] if e["state"] != "restored"]
            try:
                stash = os.open(_stash_dir(plan), tw.DIR, dir_fd=state)
            except FileNotFoundError:
                stash = None
            try:
                if stash is None:
                    conflicts = len(pending)
                else:
                    conflicts = _restore_entries(root, state, stash, plan, pending)
            finally:
                if stash is not None:
                    os.close(stash)
            journal["status"] = "conflict" if conflicts else "restored"
            tw._save(state, name, journal)
            if not conflicts:
                try:
                    os.rmdir(_stash_dir(plan), dir_fd=state)
                except OSError:
                    pass
            return not conflicts
        finally:
            os.close(state)
    except ExecutionDenied:
        raise
    except (OSError, ValueError):
        raise ExecutionDenied("TRASH: REFUSED journal") from None
    finally:
        os.close(root)


def recovery_records(workspace):
    """Expose deletion recovery even after a crash before the UI printed its plan ID."""
    root = tw._root(workspace)
    try:
        try:
            state = tw._state_dir(root)
        except FileNotFoundError:
            return []
        try:
            names = sorted(n for n in os.listdir(state) if re.fullmatch(r"[a-f0-9]{32}\.trash\.json", n))
            if len(names) > tw.MAX_JOURNALS:
                raise ValueError("trash journal count")
            result = []
            for name in names:
                value = _load(state, name)
                if value["workspace"] != workspace:
                    raise ValueError("trash workspace")
                result.append({"plan": value["plan"], "status": value["status"], "kind": "trash",
                               "files": [{"path": e["path"], "action": "restore"} for e in value["entries"]],
                               "journal_sha256": tw.sha(canonical(value).encode("utf-8"))})
            return result
        finally:
            os.close(state)
    finally:
        os.close(root)
