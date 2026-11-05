"""Descriptor-relative selected-file edits with durable undo records.

No commands, recursive reading, deletion of user files or root operation.
Same-UID malicious directory renames and hardware power-loss are not claimed
safe. Each published replacement has its inode recorded BEFORE publication.
Recovery never overwrites a file changed since publication.
"""
import hashlib
import fcntl
import json
import os
import pwd
import re
import secrets
import stat

from .analysis_contract import canonical
from .e2_ipc import _unique_pairs
from . import fs_identity, task_contract as contract, permissions
from .fs_publish import rename_exchange, rename_noreplace
from .workspace_executor import ExecutionDenied, _parent
from .task_plan import TaskPlan

STATE = ".ai-desktop-tasks"
READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
DIR = READ | os.O_DIRECTORY
CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
MAX_JOURNAL = 2 * 1024 * 1024
MAX_JOURNALS = 128
MAX_LEGACY_PARENT_ENTRIES = 4096


class NewTargetExists(ValueError):
    """Creation collided before content was read; not permission to overwrite."""

    def __init__(self, path):
        self.path = contract.path(path)
        super().__init__("new target exists")


class JournalLimit(ValueError):
    """Finite refusal: preserve existing undo history instead of pruning it."""


def _root(workspace):
    """Task-only root policy; legacy create-only executor remains unchanged."""
    try:
        uid = os.geteuid()
        if uid <= 0 or uid != os.getuid() or os.getgid() != os.getegid():
            raise ValueError("identity")
        home = pwd.getpwuid(uid).pw_dir
        permissions.validate_write_path(workspace, home, managed=True)
        if workspace in ("/", "/home", "/root", "/tmp"):
            raise ValueError("root scope")
        fd = os.open("/", DIR)
        try:
            for component in workspace.split("/")[1:]:
                nxt = os.open(component, DIR, dir_fd=fd)
                os.close(fd)
                fd = nxt
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o7022:
                raise ValueError("root policy")
            return fd
        except BaseException:
            os.close(fd)
            raise
    except (OSError, ValueError, TypeError, KeyError):
        raise ExecutionDenied("TASK: REFUSED workspace") from None


def _absent(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False


def _matches(parent, name, expected):
    return _absent(parent, name) if expected is None else read_file(parent, name) == expected


def _stage_name(taken):
    for _ in range(8):
        name = ".ai-task-" + secrets.token_hex(12)
        if name not in taken:
            taken.add(name)
            return name
    raise ValueError("stage collision")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def identity(info):
    return [info.st_dev, info.st_ino]


def read_file(parent, name):
    try:
        fd = os.open(name, READ, dir_fd=parent)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o7022 or info.st_size > contract.MAX_FILE):
            raise ValueError("file policy")
        raw = os.read(fd, contract.MAX_FILE + 1)
        after = os.fstat(fd)
        if (len(raw) != info.st_size or info.st_mtime_ns != after.st_mtime_ns
                or info.st_ctime_ns != after.st_ctime_ns):
            raise ValueError("changed file")
        content = contract.text(raw.decode("utf-8"))
        return {"inode": identity(info), "mode": stat.S_IMODE(info.st_mode),
                "sha256": sha(raw), "content": content}
    finally:
        os.close(fd)


def snapshot(workspace, names, *, must_be_absent=()):
    root = _root(workspace)
    try:
        if len(names) != len(set(names)) or not 1 <= len(names) <= contract.MAX_FILES:
            raise ValueError("selection")
        absent = set(must_be_absent)
        if not absent.issubset(names):
            raise ValueError("absence selection")
        files, total = {}, 0
        home = pwd.getpwuid(os.geteuid()).pw_dir
        for name in names:
            contract.path(name)
            permissions.validate_write_path(workspace + "/" + name, home, managed=True)
            parent, leaf = _parent(root, name)
            try:
                if name in absent and not _absent(parent, leaf):
                    raise NewTargetExists(name)
                content = None if name in absent else read_file(parent, leaf)
                total += len(content["content"].encode("utf-8")) if content else 0
                if total > contract.MAX_TOTAL:
                    raise ValueError("selection total")
                files[name] = {"parent": identity(os.fstat(parent)), "file": content}
            finally:
                os.close(parent)
        # `fs` is None where no durable identity exists; then only st_dev binds history.
        return {"workspace": workspace, "root": identity(os.fstat(root)), "files": files,
                "fs": fs_identity.identity(root)}
    finally:
        os.close(root)


def unchanged(before):
    absent = [name for name, item in before["files"].items() if item["file"] is None]
    if snapshot(before["workspace"], list(before["files"]), must_be_absent=absent) != before:
        raise ValueError("workspace changed")


def _state_dir(root, create=False):
    if create:
        try:
            os.mkdir(STATE, 0o700, dir_fd=root)
            os.fsync(root)
        except FileExistsError:
            pass
    fd = os.open(STATE, DIR, dir_fd=root)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise ValueError("journal directory")
    try:
        # Keep private undo contents out of ordinary git add/status without
        # touching the project's own .gitignore or .git configuration.
        if create:
            try:
                _write(fd, ".gitignore", b"*\n")
                os.fsync(fd)
            except FileExistsError:
                pass
        guard = read_file(fd, ".gitignore")
        if guard is None or guard["content"] != "*\n" or guard["mode"] != 0o600:
            raise ValueError("journal privacy")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write(parent, name, data, mode=0o600):
    fd = os.open(name, CREATE, mode, dir_fd=parent)
    try:
        offset = 0
        while offset < len(data):
            count = os.write(fd, data[offset:])
            if count <= 0:
                raise OSError("short write")
            offset += count
        os.fchmod(fd, mode)
        os.fsync(fd)
        return identity(os.fstat(fd))
    finally:
        os.close(fd)


def _save(fd, name, value, *, first=False):
    raw = canonical(value).encode("utf-8")
    if len(raw) > MAX_JOURNAL:
        raise ValueError("journal limit")
    if first:
        _write(fd, name, raw)
    else:
        temp = ".pending-" + secrets.token_hex(12)
        _write(fd, temp, raw)
        os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
    os.fsync(fd)


def _valid_identity(value):
    if (type(value) is not list or len(value) != 2
            or any(type(n) is not int or not 0 <= n < 2 ** 64 for n in value)):
        raise ValueError("identity")


def _record_size(record):
    if record is None:
        return 0
    if (type(record) is not dict or set(record) != {"inode", "mode", "sha256", "content"}
            or type(record["mode"]) is not int or record["mode"] & ~0o755
            or record["mode"] & 0o022):
        raise ValueError("record")
    _valid_identity(record["inode"])
    raw = contract.text(record["content"]).encode("utf-8")
    if sha(raw) != record["sha256"]:
        raise ValueError("record")
    return len(raw)


def _load(fd, name):
    source = os.open(name, READ, dir_fd=fd)
    try:
        info = os.fstat(source)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size > MAX_JOURNAL):
            raise ValueError("journal")
        raw = os.read(source, MAX_JOURNAL + 1)
        if len(raw) != info.st_size:
            raise ValueError("journal")
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
        if (type(value) is not dict or set(value) != {"schema", "plan", "before", "entries", "status"}
                or type(value["schema"]) is not int or value["schema"] not in (1, 2, 3)
                or type(value["plan"]) is not str or value["plan"] + ".json" != name
                or value["status"] not in ("prepared", "applied", "rolled-back", "conflict")):
            raise ValueError("journal")
        before, entries = value["before"], value["entries"]
        keys = {"workspace", "root", "files"} | ({"fs"} if value["schema"] == 3 else set())
        if (type(before) is not dict or set(before) != keys
                or type(before["files"]) is not dict or not 1 <= len(before["files"]) <= contract.MAX_FILES
                or type(entries) is not list or not 1 <= len(entries) <= min(len(before["files"]), contract.MAX_CHANGED_FILES)):
            raise ValueError("journal")
        _valid_identity(before["root"])
        if value["schema"] == 3 and not fs_identity.valid(before["fs"]):
            raise ValueError("journal filesystem identity")
        source_total = 0
        for path, selected in before["files"].items():
            contract.path(path)
            if type(selected) is not dict or set(selected) != {"parent", "file"}:
                raise ValueError("journal source")
            _valid_identity(selected["parent"])
            source_total += _record_size(selected["file"])
        if source_total > contract.MAX_TOTAL:
            raise ValueError("journal source total")
        seen, stage_names = set(), set()
        after_total, undo_total = 0, 0
        for item in entries:
            keys = {"path", "after", "published", "temp", "undo"}
            if value["schema"] >= 2:
                keys.add("undo_temp")
            if (type(item) is not dict or set(item) != keys
                    or contract.path(item["path"]) not in before["files"] or item["path"] in seen
                    or type(item["published"]) is not bool):
                raise ValueError("journal")
            for stage in (item["temp"],) + ((item["undo_temp"],) if value["schema"] >= 2 else ()):
                if (type(stage) is not str or not re.fullmatch(r"\.ai-task-[a-f0-9]{24}", stage)
                        or stage in stage_names):
                    raise ValueError("journal stage")
                stage_names.add(stage)
            seen.add(item["path"])
            after_total += _record_size(item["after"])
            undo_total += _record_size(item["undo"])
        if after_total > contract.MAX_TOTAL or undo_total > contract.MAX_TOTAL:
            raise ValueError("journal total")
        return value
    finally:
        os.close(source)


def _binding(root, value):
    """How a saved journal relates to the workspace opened now.

    'same'     identical runtime identity (and durable identity, if recorded)
    'rebound'  durable filesystem/subvolume identity and inode match while only
               the anonymous st_dev changed (e.g. after a remount or reboot)
    'detached' anything else that shares path and inode: another subvolume,
               a snapshot, or a legacy journal that cannot prove its origin
    A different root inode is not a binding question and stays a hard refusal.
    """
    before = value["before"]
    saved, current = before["root"], identity(os.fstat(root))
    if saved[1] != current[1]:
        raise ValueError("journal root")
    durable = before.get("fs")
    if durable is None:
        return "same" if saved[0] == current[0] else "detached"
    if fs_identity.identity(root) != durable:
        return "detached"
    return "same" if saved[0] == current[0] else "rebound"


def _rebase(value, device):
    """Refresh only the runtime device number, in memory, after proof of binding.

    Inodes, paths, modes and bytes are untouched and are still compared later.
    Identities on another device (e.g. a nested subvolume) are left alone, so
    they fail the later comparison instead of being silently adopted.
    """
    before = value["before"]
    old = before["root"][0]

    def fix(item):
        if item[0] == old:
            item[0] = device
    fix(before["root"])
    for selected in before["files"].values():
        fix(selected["parent"])
        if selected["file"] is not None:
            fix(selected["file"]["inode"])
    for entry in value["entries"]:
        for key in ("after", "undo"):
            if entry[key] is not None:
                fix(entry[key]["inode"])


DETACHED_HISTORY_HINT = (
    "Zachowano zakończoną historię z innym numerem urządzenia. "
    "Nie użyjemy jej do cofania; nowe zmiany wymagają świeżego planu i zgody na diff. "
    "Nie usuwaj ani nie poprawiaj ręcznie dzienników."
)


def _detached_completed(root, value):
    """Classify historical evidence, never rebind it or grant rollback rights.

    Only a device-only mismatch with the same lexical root and inode reaches
    here. We do NOT infer that this is the same filesystem (Btrfs snapshots can
    share inode numbers). A terminal record has no unfinished work to recover.
    Its bytes and identity remain unchanged; rollback keeps its strict checks.
    Any unfinished/malformed state or residual staging object still blocks.
    """
    if value["status"] not in ("applied", "rolled-back"):
        raise ValueError("journal root")
    if value["status"] == "applied" and any(
            not entry["published"] or entry["after"] is None or entry["undo"] is not None
            for entry in value["entries"]):
        raise ValueError("journal completion")
    # Metadata only, relative to the verified root. Do not read target contents,
    # follow symlinks, clean stage files, migrate IDs, or silently archive data.
    for entry in value["entries"]:
        parent, _ = _parent(root, entry["path"])
        try:
            stages = [entry["temp"]]
            if value["schema"] >= 2:
                stages.append(entry["undo_temp"])
            else:
                # Legacy schema 1 never recorded undo-stage names. Its absence
                # cannot be proved by this narrow check; retain fail-closed.
                raise ValueError("journal legacy binding")
            if any(not _absent(parent, stage) for stage in stages):
                raise ValueError("recovery required")
        finally:
            os.close(parent)


# Entries of the recoverable-deletion feature (workspace_trash) share this private directory but are not task journals.
_PENDING = re.compile(r"\.pending-[a-f0-9]+")
_TRASH_ENTRY = re.compile(r"[a-f0-9]{32}\.trash\.json|trash-[a-f0-9]{32}")


def _conflict_settled(root, value, *, rebound=False):
    """True only when every recorded stage of this journal is provably gone, in the very directories it recorded
    (schema 2+; older journals and a moved or remounted directory stay blocking; a proven Btrfs rebound keeps its files)."""
    try:
        if value["schema"] < 2:
            return False
        for entry in value["entries"]:
            parent, _ = _parent(root, entry["path"])
            try:
                if not rebound and identity(os.fstat(parent)) != value["before"]["files"][entry["path"]]["parent"]:
                    return False
                if not (_absent(parent, entry["temp"]) and _absent(parent, entry["undo_temp"])):
                    return False
            finally:
                os.close(parent)
        return True
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return False


def recovery_records(workspace):
    """Read validated recovery metadata without exposing saved file contents."""
    root = _root(workspace)
    try:
        try:
            fd = _state_dir(root)
        except FileNotFoundError:
            return []
        try:
            # A .pending file is an unfinished temporary write (full disk, Ctrl-C): never authoritative, so it blocks nothing.
            names = [name for name in os.listdir(fd) if name != ".gitignore" and not _TRASH_ENTRY.fullmatch(name)
                     and not _PENDING.fullmatch(name)]
            if len(names) > MAX_JOURNALS:
                raise ValueError("journal count")
            result = []
            for name in sorted(names):
                if not re.fullmatch(r"[a-f0-9]{64}\.json", name):
                    raise ValueError("incomplete journal")
                value = _load(fd, name)
                if value["before"]["workspace"] != workspace:
                    raise ValueError("journal root")
                binding = _binding(root, value)
                detached = binding == "detached"
                if detached:
                    _detached_completed(root, value)
                record = {"plan": value["plan"],
                          "status": "detached" if detached else value["status"],
                          "files": [] if detached else [{"path": item["path"],
                                     "action": "restore" if value["before"]["files"][item["path"]]["file"] is not None else "remove"}
                                    for item in value["entries"]],
                          "journal_sha256": sha(canonical(value).encode("utf-8")),
                          "modified": os.stat(name, dir_fd=fd).st_mtime}
                if binding == "rebound":
                    record["rebound"] = True
                if record["status"] == "conflict":
                    record["settled"] = _conflict_settled(root, value, rebound=binding == "rebound")
                result.append(record)
            return result
        finally:
            os.close(fd)
    finally:
        os.close(root)


def status(workspace):
    return [(record["plan"], record["status"]) for record in recovery_records(workspace)]


def _same_content(current, recorded):
    """Rollback restores bytes and mode, so those decide. A new inode with the same bytes (an earlier rollback, an editor
    that saves by rename) is the same content; a parent directory and hard links are still checked by the callers."""
    if current is None or recorded is None:
        return current is recorded
    return current["mode"] == recorded["mode"] and current["sha256"] == recorded["sha256"]


def _can_apply(workspace):
    records = recovery_records(workspace)
    if len(records) >= MAX_JOURNALS:
        raise JournalLimit("journal limit")
    # A conflict with no stage left is a finished refusal: rollback kept newer data and nothing is in progress, so it stays
    # visible in `task status` and can be retried, but it does not stop other writes. A leftover stage still blocks.
    if any(record["status"] == "prepared" or (record["status"] == "conflict" and not record.get("settled"))
           for record in records):
        raise ValueError("recovery required")
    # Detached terminal evidence still counts toward MAX_JOURNALS. It is never
    # an input to a new TaskPlan and cannot authorize changing an existing file.
    return sum(record["status"] == "detached" for record in records)


def _journal_capacity(value, replacements):
    """Budget the largest apply/undo state before any journal or target write."""
    maximum = dict(value, status="rolled-back", entries=[])
    # Linux dev_t/ino_t are bounded by unsigned 64-bit values. Actual native
    # identities cannot need more decimal digits than this reservation.
    largest_inode = [2 ** 64 - 1, 2 ** 64 - 1]
    for entry, replacement in zip(value["entries"], replacements):
        original = value["before"]["files"][entry["path"]]["file"]
        after = {"inode": largest_inode, "mode": original["mode"] if original else 0o600,
                 "sha256": sha(replacement["content"].encode("utf-8")), "content": replacement["content"]}
        undo = dict(original, inode=largest_inode) if original else None
        maximum["entries"].append(dict(entry, after=after, undo=undo, published=False))
    if len(canonical(maximum).encode("utf-8")) > MAX_JOURNAL:
        raise JournalLimit("journal capacity")


def apply(session, authorization, *, write_guard=None):
    """Consume the exact trusted UI approval; never accept model commands."""
    if type(session) is not TaskPlan:
        raise ValueError("plan")
    if write_guard is not None and not callable(write_guard):
        raise ValueError("write guard")
    data = session.consume(authorization)
    before, replacements, plan = data["before"], data["proposal"]["files"], session.sha256
    _can_apply(before["workspace"])
    unchanged(before)
    entries, stage_names = [], set()
    for item in replacements:
        name = contract.path(item["path"])
        if name not in before["files"] or any(e["path"] == name for e in entries):
            raise ValueError("selection")
        contract.text(item["content"])
        entries.append({"path": name, "after": None, "published": False,
                        "temp": _stage_name(stage_names), "undo": None,
                        "undo_temp": _stage_name(stage_names)})
    if not 1 <= len(entries) <= contract.MAX_CHANGED_FILES:
        raise ValueError("entries")
    root = _root(before["workspace"])
    try:
        if identity(os.fstat(root)) != before["root"]:
            raise ValueError("root")
        fcntl.flock(root, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _can_apply(before["workspace"])
        unchanged(before)
        name = plan + ".json"
        value = {"schema": 3, "plan": plan, "before": before, "entries": entries, "status": "prepared"}
        _journal_capacity(value, replacements)
        if write_guard is not None:
            write_guard()
        fd = _state_dir(root, create=True)
        try:
            _save(fd, name, value, first=True)
            for entry, replacement in zip(entries, replacements):
                parent, leaf = _parent(root, entry["path"])
                try:
                    saved = before["files"][entry["path"]]
                    if identity(os.fstat(parent)) != saved["parent"] or not _matches(parent, leaf, saved["file"]):
                        raise ValueError("changed target")
                    raw = replacement["content"].encode("utf-8")
                    mode = saved["file"]["mode"] if saved["file"] is not None else 0o600
                    inode = _write(parent, entry["temp"], raw, mode)
                    entry["after"] = {"inode": inode, "mode": mode, "sha256": sha(raw), "content": replacement["content"]}
                    _save(fd, name, value)
                    # Repeat immediately before rename; hostile same-UID races
                    # are outside the boundary, normal concurrent edits refuse.
                    if not _matches(parent, leaf, saved["file"]):
                        raise ValueError("changed target")
                    if saved["file"] is None:
                        # Absence is a condition of creation, not permission to replace a
                        # file another editor created after our last state check.
                        rename_noreplace(parent, entry["temp"], parent, leaf)
                    else:
                        # Swap instead of replace: whatever sat at the name is now at the stage name, where it is
                        # compared with what this plan saw. An editor that wrote in the meantime is put back, never
                        # silently destroyed. (A write into an inode someone already holds open can still land after
                        # this check; see docs/KNOWN_LIMITATIONS.md.)
                        rename_exchange(parent, entry["temp"], parent, leaf)
                        try:
                            displaced = read_file(parent, entry["temp"])
                            if displaced != saved["file"]:
                                raise ValueError("changed target")
                        except BaseException:
                            rename_exchange(parent, entry["temp"], parent, leaf)
                            # Their file is back; what is left under the stage name is ours, so it can go.
                            try:
                                os.unlink(entry["temp"], dir_fd=parent)
                            except OSError:
                                pass
                            raise
                        os.unlink(entry["temp"], dir_fd=parent)
                    os.fsync(parent)
                    entry["published"] = True
                    _save(fd, name, value)
                finally:
                    os.close(parent)
            value["status"] = "applied"
            _save(fd, name, value)
        finally:
            os.close(fd)
    finally:
        os.close(root)


def _upgrade_legacy(root, fd, name, value):
    """Refuse ambiguous old stages; migrate before creating any undo file.

    Schema 1 did not record undo filenames. Only directory entry names in
    already selected parents are inspected, never unknown file contents.
    Unknown stages are preserved for explicit recovery, not adopted or deleted.
    """
    if value["schema"] >= 2:
        return True
    known = {}
    for entry in value["entries"]:
        parent_id = tuple(value["before"]["files"][entry["path"]]["parent"])
        known.setdefault(parent_id, set()).add(entry["temp"])
    checked = set()
    for entry in value["entries"]:
        parent_id = tuple(value["before"]["files"][entry["path"]]["parent"])
        if parent_id in checked:
            continue
        parent, _ = _parent(root, entry["path"])
        try:
            if tuple(identity(os.fstat(parent))) != parent_id:
                return False
            with os.scandir(parent) as names:
                for count, candidate in enumerate(names, 1):
                    if (count > MAX_LEGACY_PARENT_ENTRIES
                            or (candidate.name.startswith(".ai-task-") and candidate.name not in known[parent_id])):
                        return False
            checked.add(parent_id)
        finally:
            os.close(parent)
    stage_names = {entry["temp"] for entry in value["entries"]}
    # Generate the whole extension before touching schema-1 entries. A failed
    # allocation must not leave a mixed journal that a retry cannot validate.
    undo_names = [_stage_name(stage_names) for _ in value["entries"]]
    for entry, undo_name in zip(value["entries"], undo_names):
        entry["undo_temp"] = undo_name
    value["schema"] = 2
    _save(fd, name, value)
    return True


def rollback(workspace, plan, *, expected_journal=None):
    if not re.fullmatch(r"[a-f0-9]{64}", plan):
        raise ValueError("plan")
    if expected_journal is not None and (type(expected_journal) is not str
                                        or not re.fullmatch(r"[a-f0-9]{64}", expected_journal)):
        raise ValueError("journal approval")
    root = _root(workspace)
    try:
        fcntl.flock(root, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd = _state_dir(root)
        try:
            name = plan + ".json"
            value = _load(fd, name)
            before = value["before"]
            if before["workspace"] != workspace:
                raise ValueError("root")
            binding = _binding(root, value)
            if binding == "detached":
                raise ValueError("root")
            if expected_journal is not None and sha(canonical(value).encode("utf-8")) != expected_journal:
                raise ValueError("journal changed")
            if binding == "rebound":
                _rebase(value, os.fstat(root).st_dev)
            try:
                upgraded = _upgrade_legacy(root, fd, name, value)
            except (OSError, ValueError):
                upgraded = False
            if not upgraded:
                value["status"] = "conflict"
                _save(fd, name, value)
                return False
            conflicts = 0
            for entry in reversed(value["entries"]):
                try:
                    parent, leaf = _parent(root, entry["path"])
                except (OSError, RuntimeError):
                    conflicts += 1
                    continue
                try:
                    saved = before["files"][entry["path"]]
                    if identity(os.fstat(parent)) != saved["parent"]:
                        conflicts += 1
                        continue
                    current = read_file(parent, leaf)
                    original = saved["file"]
                    if _same_content(current, original) or (entry["undo"] is not None
                                                            and _same_content(current, entry["undo"])):
                        pass
                    elif entry["after"] is not None and _same_content(current, entry["after"]):
                        if original is None:
                            # Move away first and look at what actually moved: a file an editor saved after the
                            # comparison above goes back to its name instead of being deleted.
                            rename_noreplace(parent, leaf, parent, entry["temp"])
                            moved = read_file(parent, entry["temp"])
                            if not _same_content(moved, entry["after"]):
                                rename_noreplace(parent, entry["temp"], parent, leaf)
                                raise ValueError("changed undo target")
                            os.unlink(entry["temp"], dir_fd=parent)
                        else:
                            # The name is durable before creation; the exact
                            # identity is durable before publication. Unknown
                            # partial files/collisions are retained as conflicts.
                            undo_temp = entry["undo_temp"]
                            staged = read_file(parent, undo_temp)
                            if entry["undo"] is None:
                                if staged is not None:
                                    raise ValueError("unknown undo stage")
                                inode = _write(parent, undo_temp, original["content"].encode("utf-8"), original["mode"])
                                entry["undo"] = dict(original, inode=inode)
                                _save(fd, name, value)
                            elif staged != entry["undo"]:
                                raise ValueError("changed undo stage")
                            if read_file(parent, undo_temp) != entry["undo"]:
                                raise ValueError("changed undo stage")
                            if read_file(parent, leaf) != current:
                                raise ValueError("changed undo target")
                            # Swap, as a write does: what was displaced is compared, and a newer save is put back.
                            rename_exchange(parent, undo_temp, parent, leaf)
                            try:
                                displaced = read_file(parent, undo_temp)
                                if not _same_content(displaced, entry["after"]):
                                    raise ValueError("changed undo target")
                            except BaseException:
                                rename_exchange(parent, undo_temp, parent, leaf)
                                raise
                            os.unlink(undo_temp, dir_fd=parent)
                        os.fsync(parent)
                    else:
                        conflicts += 1
                        continue
                    # Both apply and undo stages have durable names. Never
                    # claim success while either is present but unrecognized.
                    # After a crash between a swap and its unlink a stage holds the version that was displaced.
                    for stage_name, known in ((entry["temp"], (entry["after"], original)),
                                              (entry["undo_temp"], (entry["undo"], entry["after"]))):
                        staged = read_file(parent, stage_name)
                        if staged is not None:
                            if not any(fingerprint is not None and _same_content(staged, fingerprint) for fingerprint in known):
                                raise ValueError("unknown stage")
                            os.unlink(stage_name, dir_fd=parent)
                            os.fsync(parent)
                except (OSError, ValueError):
                    conflicts += 1
                finally:
                    os.close(parent)
            value["status"] = "conflict" if conflicts else "rolled-back"
            _save(fd, name, value)
            return not conflicts
        finally:
            os.close(fd)
    finally:
        os.close(root)
