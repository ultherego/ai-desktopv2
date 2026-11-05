"""Opt-in local conversation memory; never a source of grants or credentials.

Records contain the saved questions, answers and context metadata selected by
``--session``. The store does not import source files, grants, plans, OAuth or a
native client's session. Every resume must pass the ordinary policy, current
binding and fresh consent again.
Advisory locks serialize cooperating controllers; hostile same-UID writers are
outside the filesystem guarantees, as elsewhere in AI Desktop.
"""
from contextlib import contextmanager
import copy
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import time
import unicodedata

from . import execution

MAX_FILE = 1024 * 1024
MAX_SESSIONS = 128
MAX_HISTORY = 32                # `ask` keeps the last eight turns; records written with up to four still open
LEGACY_HISTORY = 4              # material/project sessions keep their previous window
MAX_PATHS = 32
_ID = re.compile(r"[a-f0-9]{24}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_KEYS = {"schema", "id", "revision", "kind", "path", "binding", "execution", "history", "created", "updated"}
# Optional, written only for `ask` conversations: task memory plus the metadata needed to revoke what depended on a
# source that is no longer allowed. Older records simply lack them.
_OPTIONAL = {"memory", "paths", "turns", "meta", "title"}
MAX_TITLE = 80


class SessionError(ValueError):
    """A closed refusal code, without private text or filesystem names."""


def _id(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise SessionError("session-id")
    return value


def _text(value, limit, *, empty=False):
    if type(value) is not str or (not empty and not value.strip()):
        raise SessionError("session-text")
    try:
        if len(value.encode("utf-8")) > limit:
            raise SessionError("session-text")
    except UnicodeError:
        raise SessionError("session-text") from None
    if any(c not in "\n\t" and unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in value):
        raise SessionError("session-text")
    return value


def _cut(value, limit):
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value
    return raw[:limit].decode("utf-8", "ignore") + " …[skrócono]"


def _binding(kind, path, binding):
    if type(kind) is not str or kind not in ("ask", "material", "project"):
        raise SessionError("session-kind")
    if type(path) is not str or type(binding) is not str:
        raise SessionError("session-binding")
    if kind == "ask":
        if path != "" or binding != "":
            raise SessionError("session-binding")
    else:
        _text(path, 4096)
        if (not os.path.isabs(path) or path != os.path.normpath(path)
                or "\n" in path or "\t" in path or not _HASH.fullmatch(binding or "")):
            raise SessionError("session-binding")


def _execution(value):
    try:
        return execution.validate(value)
    except ValueError:
        raise SessionError("session-execution") from None


def _record(value):
    if (type(value) is not dict or not _KEYS <= set(value) <= _KEYS | _OPTIONAL or value["schema"] != 1
            or type(value["schema"]) is not int):
        raise SessionError("session-schema")
    _id(value["id"])
    for key in ("revision", "created", "updated"):
        if type(value[key]) is not int or not 0 <= value[key] < 2**53:
            raise SessionError("session-schema")
    if value["updated"] < value["created"]:
        raise SessionError("session-schema")
    _binding(value["kind"], value["path"], value["binding"])
    _execution(value["execution"])
    history = value["history"]
    if type(history) is not list or len(history) > MAX_HISTORY:
        raise SessionError("session-history")
    for item in history:
        if type(item) is not dict or set(item) != {"question", "answer"}:
            raise SessionError("session-history")
        _text(item["question"], execution.HISTORY_QUESTION_BYTES)
        _text(item["answer"], 8192 + 64)
    if "title" in value:
        _text(value["title"], MAX_TITLE * 4)
        if len(value["title"]) > MAX_TITLE or "\n" in value["title"] or "\t" in value["title"]:
            raise SessionError("session-text")
    if "memory" in value:
        _extras(value)
    return copy.deepcopy(value)


def _extras(value):
    """Validate the optional ask-only part: memory, source table, per-turn taint and per-entry origin/taint."""
    from . import conversation_memory
    try:
        conversation_memory.validate(value["memory"])
    except ValueError:
        raise SessionError("session-memory") from None
    paths, turns, meta = value.get("paths"), value.get("turns"), value.get("meta")
    if (type(paths) is not list or len(paths) > MAX_PATHS or len(set(paths)) != len(paths)
            or type(turns) is not list or len(turns) != len(value["history"])
            or type(meta) is not dict or set(meta) != {"user", "taints"}):
        raise SessionError("session-memory")
    for path in paths:
        _text(path, 512)

    def indexes(items):
        if (type(items) is not list or len(items) > MAX_PATHS
                or any(type(i) is not int or isinstance(i, bool) or not 0 <= i < len(paths) for i in items)):
            raise SessionError("session-memory")

    for turn in turns:
        if (type(turn) is not dict or set(turn) != {"n", "taint", "redacted"} or type(turn["n"]) is not int
                or not 1 <= turn["n"] < 10**4 or type(turn["redacted"]) is not bool):
            raise SessionError("session-memory")
        indexes(turn["taint"])
    ids = {entry["id"] for entry in value["memory"]["entries"]}
    if (type(meta["user"]) is not list or any(ident not in ids for ident in meta["user"])
            or type(meta["taints"]) is not dict or any(ident not in ids for ident in meta["taints"])):
        raise SessionError("session-memory")
    for items in meta["taints"].values():
        indexes(items)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise SessionError("session-schema")
        result[key] = value
    return result


def _file_info(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise SessionError("session-file")
    return info


def _same(left, right):
    return ((left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns, left.st_ctime_ns)
            == (right.st_dev, right.st_ino, right.st_size, right.st_mtime_ns, right.st_ctime_ns))


class Store:
    def __init__(self, home=None):
        # Ignore XDG/HOME overrides inherited from an untrusted invocation.
        self.home = Path(pwd.getpwuid(os.geteuid()).pw_dir if home is None else home)
        if not self.home.is_absolute() or str(self.home) != os.path.normpath(str(self.home)):
            raise SessionError("session-home")
        self.path = self.home / ".local/state/ai-desktop/conversations"

    @contextmanager
    def _root(self, create=False, optional=False):
        fd = os.open("/", _DIR_FLAGS)
        try:
            home_parts = self.home.parts[1:]
            parts = (*home_parts, ".local", "state", "ai-desktop", "conversations")
            for index, part in enumerate(parts):
                if create and index >= len(home_parts):
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                try:
                    next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    if optional:
                        yield None
                        return
                    raise
                os.close(fd)
                fd = next_fd
                info = os.fstat(fd)
                allowed_uids = {os.geteuid()} if index >= len(home_parts) - 1 else {0, os.geteuid()}
                if info.st_uid not in allowed_uids or info.st_mode & 0o022:
                    raise SessionError("session-directory")
                if index == len(parts) - 1 and stat.S_IMODE(info.st_mode) != 0o700:
                    raise SessionError("session-directory")
            yield fd
        except OSError:
            raise SessionError("session-file") from None
        finally:
            os.close(fd)

    @contextmanager
    def _locked(self, root):
        fd = os.open(".sessions.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     0o600, dir_fd=root)
        try:
            info = _file_info(fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            if not _same(info, os.stat(".sessions.lock", dir_fd=root, follow_symlinks=False)):
                raise SessionError("session-conflict")
            yield
        finally:
            os.close(fd)

    def _read(self, root, ident):
        fd = os.open(_id(ident) + ".json", _FILE_FLAGS, dir_fd=root)
        try:
            info = _file_info(fd)
            if not 0 < info.st_size <= MAX_FILE:
                raise SessionError("session-size")
            raw = os.read(fd, MAX_FILE + 1)
            if (len(raw) != info.st_size or not _same(info, _file_info(fd))
                    or not _same(info, os.stat(ident + ".json", dir_fd=root, follow_symlinks=False))):
                raise SessionError("session-conflict")
            try:
                value = _record(json.loads(raw, object_pairs_hook=_pairs))
            except (ValueError, UnicodeError, RecursionError):
                raise SessionError("session-schema") from None
            if value["id"] != ident:
                raise SessionError("session-id")
            return value, info
        finally:
            os.close(fd)

    def _publish(self, root, value, old_info=None):
        value = _record(value)
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if len(raw) > MAX_FILE:
            raise SessionError("session-size")
        temporary = ".new-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=root)
        try:
            view = memoryview(raw)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise SessionError("session-file")
                view = view[count:]
            os.fsync(fd)
            destination = value["id"] + ".json"
            if old_info is None:
                # link() is the no-replace publication primitive for this same
                # filesystem; a concurrent destination can never be clobbered.
                os.link(temporary, destination, src_dir_fd=root, dst_dir_fd=root, follow_symlinks=False)
                os.unlink(temporary, dir_fd=root)
            else:
                current = os.stat(destination, dir_fd=root, follow_symlinks=False)
                if not _same(old_info, current):
                    raise SessionError("session-conflict")
                os.replace(temporary, destination, src_dir_fd=root, dst_dir_fd=root)
            os.fsync(root)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=root)
            except FileNotFoundError:
                pass

    def _lease(self, root, ident, *, create=True):
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        if create:
            flags |= os.O_CREAT
        fd = os.open(_id(ident) + ".lease", flags, 0o600, dir_fd=root)
        try:
            info = _file_info(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not _same(info, os.stat(ident + ".lease", dir_fd=root, follow_symlinks=False)):
                raise SessionError("session-conflict")
            return fd
        except BlockingIOError:
            os.close(fd)
            raise SessionError("session-active") from None
        except BaseException:
            os.close(fd)
            raise

    def create(self, kind, path="", binding="", execution_config=None):
        _binding(kind, path, binding)
        selected = _execution(execution_config)
        with self._root(create=True) as root, self._locked(root):
            ids = self._ids(root)
            if len(ids) >= MAX_SESSIONS:
                raise SessionError("session-limit")
            now = int(time.time())
            value = {"schema": 1, "id": secrets.token_hex(12), "revision": 0, "kind": kind,
                     "path": path, "binding": binding, "execution": selected, "history": [],
                     "created": now, "updated": now}
            self._publish(root, value)
            lease = self._lease(root, value["id"])
        return Conversation(self, value, lease)

    def open(self, ident, kind, path="", binding="", execution_config=None):
        _id(ident)
        _binding(kind, path, binding)
        selected = _execution(execution_config)
        with self._root() as root, self._locked(root):
            value, info = self._read(root, ident)
            if ((value["kind"], value["path"], value["binding"]) != (kind, path, binding)
                    or not same_agent(value["execution"], selected)):
                raise SessionError("session-binding")
            if value["execution"] != selected:
                value["execution"] = selected
                value["revision"] += 1
                self._publish(root, value, info)
            lease = self._lease(root, ident)
        return Conversation(self, value, lease)

    def rebind(self, ident, selected, expected_revision):
        """Record a changed model, effort or instruction set of the same agent; a different recipient stays refused."""
        selected = _execution(selected)
        with self._root() as root, self._locked(root):
            value, info = self._read(root, ident)
            if value["revision"] != expected_revision:
                raise SessionError("session-conflict")
            if not same_agent(value["execution"], selected):
                raise SessionError("session-binding")
            value["execution"] = selected
            value["revision"] = expected_revision + 1
            value["updated"] = max(value["updated"], int(time.time()))
            self._publish(root, value, info)
        return value

    def update(self, value, expected_revision):
        value = _record(value)
        if type(expected_revision) is not int:
            raise SessionError("session-conflict")
        with self._root() as root, self._locked(root):
            old, info = self._read(root, value["id"])
            if old["revision"] != expected_revision:
                raise SessionError("session-conflict")
            immutable = ("id", "schema", "kind", "path", "binding", "execution", "created")
            if any(old[key] != value[key] for key in immutable):
                raise SessionError("session-binding")
            value["revision"] = expected_revision + 1
            value["updated"] = max(old["updated"], int(time.time()))
            self._publish(root, value, info)
        return value

    def _ids(self, root):
        names = []
        with os.scandir(root) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > 3 * MAX_SESSIONS + 8:
                    raise SessionError("session-limit")
        result = sorted(name[:-5] for name in names if name.endswith(".json") and _ID.fullmatch(name[:-5]))
        if len(result) > MAX_SESSIONS:
            raise SessionError("session-limit")
        return result

    def read(self, ident):
        with self._root() as root:
            return self._read(root, ident)[0]

    def list(self):
        """Readable records only: one damaged file must not hide all the others (see damaged)."""
        return self._scan()[0]

    def damaged(self):
        """Ids of records that cannot be read, so the user can see and remove them."""
        return self._scan()[1]

    def _scan(self):
        good, bad = [], []
        with self._root(optional=True) as root:
            if root is None:
                return good, bad
            for ident in self._ids(root):
                try:
                    good.append(self._read(root, ident)[0])
                except (SessionError, OSError):
                    bad.append(ident)
        return good, bad

    def remove_damaged(self, ident):
        """Remove a record that cannot be read. A readable one still needs remove(), with its review and conflict checks."""
        with self._root() as root, self._locked(root):
            try:
                self._read(root, ident)
            except SessionError as exc:
                if str(exc) == "session-conflict":          # changing right now: not proven damaged
                    raise
            except OSError:
                pass
            else:
                raise SessionError("session-conflict")
            lease = self._lease(root, ident)
            try:
                os.unlink(ident + ".json", dir_fd=root)     # never follows a link; removes the entry itself
                os.unlink(ident + ".lease", dir_fd=root)
                os.fsync(root)
            finally:
                os.close(lease)

    def active(self, ident):
        try:
            with self._root() as root, self._locked(root):
                self._read(root, ident)
                try:
                    fd = self._lease(root, ident, create=False)
                except FileNotFoundError:
                    return False
                except SessionError as exc:
                    if str(exc) == "session-active":
                        return True
                    raise
                os.close(fd)
                return False
        except OSError:
            raise SessionError("session-file") from None

    def remove(self, ident, *, expected=None):
        reviewed = None if expected is None else _record(expected)
        with self._root() as root, self._locked(root):
            value, info = self._read(root, ident)
            if reviewed is not None and value != reviewed:
                raise SessionError("session-conflict")
            lease = self._lease(root, ident)
            try:
                if not _same(info, os.stat(ident + ".json", dir_fd=root, follow_symlinks=False)):
                    raise SessionError("session-conflict")
                os.unlink(ident + ".json", dir_fd=root)
                os.unlink(ident + ".lease", dir_fd=root)
                os.fsync(root)
            finally:
                os.close(lease)

    def resume_args(self, ident):
        value = self.read(ident)
        args = [value["kind"]]
        if value["kind"] != "ask":
            args.append(value["path"])
        args.extend(("--session", value["id"]))
        if value["execution"] is not None:
            args.extend(("--agent", value["execution"]["profile"], "--model", value["execution"]["model"],
                         "--effort", value["execution"]["effort"]))
        return args


def same_agent(saved, selected):
    """What binds a saved conversation: who receives it (service), the role and its access. Model, effort and Skills may
    change, so editing a Skill or switching a model in a conversation does not strand the conversation."""
    def key(value):
        return ("claude", "default", "changes") if value is None else (value["provider"], value["profile"], value["access"])
    return key(saved) == key(selected)


class Conversation:
    def __init__(self, store, value, lease):
        self.store, self._record, self._lease = store, _record(value), lease
        self.session_id = value["id"]

    @property
    def history(self):
        from .material_loop import history_turn
        return [history_turn(item["question"], item["answer"]) for item in self._record["history"]]

    @property
    def record(self):
        return copy.deepcopy(self._record)

    def rebind(self, selected):
        if self._lease is None:
            raise SessionError("session-closed")
        self._record = self.store.rebind(self.session_id, selected, self._record["revision"])

    def save_state(self, exported):
        """Replace history and task memory of an `ask` conversation with the controller's exported state."""
        if self._lease is None:
            raise SessionError("session-closed")
        if self._record["kind"] != "ask":
            raise SessionError("session-kind")
        value = self.record
        value.update(exported)
        # The record limit is fixed: if it would be exceeded the OLDEST verbatim turns leave the file first; the task
        # memory and the revocation metadata always stay.
        while len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")) > MAX_FILE \
                and value["history"]:
            value["history"] = value["history"][1:]
            value["turns"] = value["turns"][1:]
        self._record = self.store.update(value, self._record["revision"])

    def state(self):
        """The persisted ask state (may be absent for records written before task memory existed)."""
        return copy.deepcopy({key: self._record[key] for key in ("history", *_OPTIONAL) if key in self._record})

    @property
    def title(self):
        return self._record.get("title")

    def append(self, question, answer):
        if self._lease is None:
            raise SessionError("session-closed")
        _text(question, execution.HISTORY_QUESTION_BYTES)
        _text(answer, 1024 * 1024)
        value = self.record
        limit = MAX_HISTORY if value["kind"] == "ask" else LEGACY_HISTORY
        value["history"] = (value["history"] + [{"question": question, "answer": _cut(answer, 8192)}])[-limit:]
        self._record = self.store.update(value, self._record["revision"])

    def close(self):
        if self._lease is not None:
            os.close(self._lease)
            self._lease = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def load_session(args, kind, path="", binding="", execution_config=None, *, store=None, **kwargs):
    # ``execution`` alias makes the controller hook natural without shadowing
    # this module's execution validator.
    if "execution" in kwargs:
        if execution_config is not None or len(kwargs) != 1:
            raise SessionError("session-execution")
        execution_config = kwargs["execution"]
    elif kwargs:
        raise SessionError("session-schema")
    selected = getattr(args, "session", None)
    if selected is None:
        return None
    store = Store() if store is None else store
    if selected == "new":
        return store.create(kind, path, binding, execution_config)
    return store.open(selected, kind, path, binding, execution_config)


def resume_args(ident, *, store=None):
    return (Store() if store is None else store).resume_args(ident)
