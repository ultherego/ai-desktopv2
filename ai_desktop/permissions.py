"""Private source and file authority for ask. This module never writes targets.

Selections are lexical until the UI grants the local read. Files and every
ancestor are opened relative to descriptors with O_NOFOLLOW; selected v2
directory roots may use the guarded same-parent alias exception below. A policy is bound
to the exact configuration inode, metadata and bytes until transfer approval.
"""
from contextlib import contextmanager
import errno
from dataclasses import dataclass
import json
import os
import pwd
import re
import stat
import tomllib
import unicodedata

from .file_context_contract import (MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES,
                                    contains_private_key, validate_context)

MAX_POLICY_BYTES = 16384
DEFAULT_FILE_SIZE = 5 * 1024 * 1024       # largest file `material` imports unless `file_size` says otherwise
MIN_FILE_SIZE = 4 * 1024
MAX_FILE_SIZE = 16 * 1024 * 1024          # hard ceiling in code (also the ceiling of the request schema)
MAX_ACCESS_BYTES = 8192
MAX_RULES = 64
MAX_DIRECTORY_ENTRIES = 1024
MAX_TOTAL_ENTRIES = 4096
MAX_DEPTH = 8
_POLICY_RELATIVE = ".config/ai-desktop/permissions.toml"
_BLOCKED_ROOTS = ("/proc", "/sys", "/dev", "/run", "/var/lib/ai-desktop",
                  "/etc/ssh", "/etc/ssl/private", "/etc/security")
_BLOCKED_COMPONENTS = frozenset({".claude", ".codex", ".gnupg", ".aws", ".azure", ".kube", ".pki",
                               ".password-store", ".docker", ".mozilla", ".ai-desktop-tasks"})
_BLOCKED_HOME = (".config/ai-desktop", ".local/state/ai-desktop", ".local/share/ai-desktop",
                 ".local/share/keyrings", ".config/gh", ".config/gcloud", ".config/rclone",
                 ".config/chromium", ".config/google-chrome", ".config/BraveSoftware")
_BLOCKED_NAMES = frozenset({"shadow", "gshadow", ".netrc", ".git-credentials", ".npmrc", ".pypirc",
                          ".env", ".credentials.json", "credentials", "credentials.json",
                          "token", "tokens.json", "auth.json", "secrets.toml",
                          "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk"})
_WRITE_BLOCKED_ROOTS = ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot",
                        "/var", "/opt", "/srv")
_WRITE_BLOCKED_COMPONENTS = frozenset({".ssh", ".git", ".local", ".ai-desktop-tasks",
                                     "oauth", "secrets", "backup", "backups"})


DEFAULT_PART_SIZE = 240 * 1024            # one portion of a whole-file analysis (one request)
MIN_PART_SIZE = 30 * 1024
MAX_PART_SIZE = 512 * 1024                # = the per-request data ceiling of the "map" phase (material_step.PART_DATA_BYTES)


def parse_size(value, minimum, maximum):
    """A size setting: bytes as an integer, or a string such as "512KiB" / "5 MiB" / "2097152". Bounded."""
    if type(value) is int and not isinstance(value, bool):
        size = value
    elif type(value) is str:
        match = re.fullmatch(r"\s*(\d{1,9})\s*(B|KiB|MiB)?\s*", value)
        if not match:
            raise ValueError("file-size")
        size = int(match.group(1)) * {None: 1, "B": 1, "KiB": 1024, "MiB": 1024 * 1024}[match.group(2)]
    else:
        raise ValueError("file-size")
    if not minimum <= size <= maximum:
        raise ValueError("file-size")
    return size


class _SettingError(Exception):
    pass


class _Problem(Exception):
    """A found defect of the owner's policy file: plain Polish lines that name the key and the entry, never a path."""
    def __init__(self, *lines):
        super().__init__("policy-problem")
        self.lines = tuple(lines)


def _shown(value, limit=70):
    """The owner's own text, made safe for one terminal line."""
    text = value if type(value) is str else type(value).__name__
    text = "".join(char if char.isprintable() else "?" for char in text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _repeated(items):
    seen, again = set(), []
    for item in items:
        if item in seen and item not in again:
            again.append(item)
        seen.add(item)
    return again


def parse_file_size(value):
    return parse_size(value, MIN_FILE_SIZE, MAX_FILE_SIZE)


def parse_part_size(value):
    return parse_size(value, MIN_PART_SIZE, MAX_PART_SIZE)


@dataclass(frozen=True)
class Limits:
    """Resource budgets of ONE question, set by the owner in `[files]`; never permissions.

    Defaults equal the former constants. Ceilings are hard limits in code that the request schema (client AND worker)
    also enforces, so a policy can lower a budget or raise it only up to the ceiling.
    """
    read_files: int = 64              # files read into one context
    read_file_size: int = 65536       # bytes per file
    read_total_size: int = 131072     # bytes of all file contents of one context
    listing_entries: int = 128        # entries per list/find step
    discovery_bytes: int = 131072     # bytes of all discovery steps sent to the model during one question
    search_results: int = 40          # matches per find step
    search_scan: int = 20000          # directory entries a question may examine while searching/listing
    discovery_rounds: int = 4         # list/find steps (extra model requests) per question
    plan_corrections: int = 2         # corrected read plans (extra model requests) per question
    command_rounds: int = 24          # commands the assistant may propose per question (0 = none run)
    command_output: int = 8192        # bytes of one command's output that go back to the model
    command_timeout: int = 20         # seconds one command may run


# name -> (kind, minimum, maximum); kind "count" is an integer, "size" accepts "24KiB"-style strings too
LIMIT_SPECS = {
    "read_files": ("count", 1, 128),
    "read_file_size": ("size", 1024, 131072),
    "read_total_size": ("size", 1024, 262144),
    "listing_entries": ("count", 16, 512),
    "discovery_bytes": ("size", 2048, 262144),
    "search_results": ("count", 5, 200),
    "search_scan": ("count", 100, 100000),
    "discovery_rounds": ("count", 1, 8),
    "plan_corrections": ("count", 0, 4),
    "command_rounds": ("count", 0, 32),
    "command_output": ("size", 1024, 131072),
    "command_timeout": ("count", 1, 60),
}


def parse_limits(files):
    """Strictly parse the optional budget keys of `[files]`; the key name (never the value) is reported on error."""
    values = {}
    for name, (kind, minimum, maximum) in LIMIT_SPECS.items():
        if name not in files:
            continue
        try:
            if kind == "size":
                values[name] = parse_size(files[name], minimum, maximum)
            else:
                value = files[name]
                if type(value) is not int or isinstance(value, bool) or not minimum <= value <= maximum:
                    raise ValueError("limit")
                values[name] = value
        except ValueError:
            raise _SettingError(name) from None
    limits = Limits(**values)
    if limits.read_file_size > limits.read_total_size:
        raise _SettingError("read_file_size")
    return limits


class _MissingParent(ValueError):
    pass


def _under(path, parent):
    return path == parent or path.startswith(parent + "/")


def _absolute(value):
    if (type(value) is not str or not value.startswith("/")
            or any(unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in value)
            or len(value.encode("utf-8")) > 512
            or any(c in value for c in "$~\\?[]{}*")
            or any(p in ("", ".", "..") for p in value.split("/")[1:])):
        raise ValueError("files-path")
    return value


def _home(uid):
    return pwd.getpwuid(uid).pw_dir


def _identity():
    uid = os.getuid()
    if uid == 0 or uid != os.geteuid() or os.getgid() != os.getegid():
        raise ValueError("policy-identity")
    try:
        return uid, _absolute(_home(uid))
    except (OSError, KeyError, ValueError):
        raise ValueError("policy-identity") from None


def _canonical(value, home):
    if type(value) is not str:
        raise ValueError("files-path")
    return _absolute(home + value[1:] if value.startswith("~/") else value)


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory(info, uid, secure):
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("files-directory")
    if secure and (info.st_uid not in (0, uid) or info.st_mode & 0o022):
        raise ValueError("policy-directory")


@contextmanager
def _open_directory(path, uid, *, secure=False):
    """Keep and verify the whole chain, including names changed during a read."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fds, links = [], []
    ready = False
    try:
        root = os.open("/", flags)
        fds.append(root)
        _directory(os.fstat(root), uid, secure)
        current = root
        if path != "/":
            _absolute(path)
            for name in path.split("/")[1:]:
                try:
                    child = os.open(name, flags, dir_fd=current)
                except OSError as exc:
                    # A no-follow directory open commonly reports ENOTDIR for
                    # a symlink. Inspect only that already-selected name in the
                    # same open parent; never follow it or retry the read.
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        try:
                            failed = os.stat(name, dir_fd=current, follow_symlinks=False)
                        except OSError:
                            pass
                        else:
                            if stat.S_ISLNK(failed.st_mode):
                                raise ValueError("policy-directory" if secure else "files-symlink") from None
                            if not stat.S_ISDIR(failed.st_mode):
                                raise ValueError("policy-directory" if secure else "files-directory") from None
                    raise
                fds.append(child)
                info = os.fstat(child)
                _directory(info, uid, secure)
                # Directory size/times change for unrelated children. Identity
                # and security metadata, plus each path link, are what matter.
                links.append((current, name, child, (info.st_dev, info.st_ino,
                              info.st_mode, info.st_uid, info.st_gid)))
                current = child

        def recheck():
            _directory(os.fstat(root), uid, secure)
            for parent, name, child, expected in links:
                for actual in (os.fstat(child), os.stat(name, dir_fd=parent, follow_symlinks=False)):
                    _directory(actual, uid, secure)
                    if (actual.st_dev, actual.st_ino, actual.st_mode,
                            actual.st_uid, actual.st_gid) != expected:
                        raise ValueError("files-changed")
        # Private immutable snapshot for the narrowly supported directory alias.
        # Ordinary callers still receive the same descriptor/recheck pair.
        root_info = os.fstat(root)
        recheck.snapshot = ((root_info.st_dev, root_info.st_ino, root_info.st_mode,
                             root_info.st_uid, root_info.st_gid),
                            *(expected for _, _, _, expected in links))
        recheck()
        ready = True
        yield current, recheck
        recheck()
    except FileNotFoundError:
        if not ready:
            raise _MissingParent("files-missing") from None
        raise ValueError("files-changed") from None
    except PermissionError:
        raise ValueError("policy-access" if secure else "files-permission") from None
    except OSError as exc:
        raise ValueError("files-symlink" if not secure and exc.errno == errno.ELOOP else "files-access") from None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def trusted_owner(info, uid, *, system):
    """The user's own file, or - for a version 2 policy - a root-owned file nobody else can write (system config)."""
    return info.st_uid == uid or (system and info.st_uid == 0 and not info.st_mode & 0o022)


def _read(path, uid, limit, *, policy=False, optional=False, system=False):
    parent, name = path.rsplit("/", 1)
    try:
        with _open_directory(parent or "/", uid, secure=policy) as (directory, recheck):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=directory)
            except FileNotFoundError:
                if optional:
                    return None
                raise ValueError("policy-missing" if policy else "files-missing") from None
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("policy-file" if policy else "files-type")
                if not trusted_owner(before, uid, system=system and not policy):
                    raise ValueError("policy-file" if policy else "files-owner")
                if before.st_nlink != 1:
                    raise ValueError("policy-file" if policy else "files-links")
                if before.st_mode & 0o6000 or (policy and stat.S_IMODE(before.st_mode) != 0o600):
                    raise ValueError("policy-file" if policy else "files-mode")
                if before.st_size > limit:
                    raise ValueError("policy-size" if policy else "files-size")
                chunks, total = [], 0
                while True:
                    chunk = os.read(fd, min(4096, limit + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("policy-size" if policy else "files-size")
                after = os.fstat(fd)
                named = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if _fingerprint(before) != _fingerprint(after) or _fingerprint(after) != _fingerprint(named):
                    raise ValueError("files-changed")
                recheck()
                raw = b"".join(chunks)
                if len(raw) != before.st_size:
                    raise ValueError("files-changed")
                return raw, _fingerprint(after)
            finally:
                os.close(fd)
    except _MissingParent:
        if policy and optional:
            return None
        raise ValueError("policy-missing" if policy else "files-missing") from None
    except OSError as exc:
        code = "policy-access" if policy else "files-access"
        if not policy and exc.errno == errno.ELOOP:
            code = "files-symlink"
        elif not policy and exc.errno in (errno.EACCES, errno.EPERM):
            code = "files-permission"
        raise ValueError(code) from None


@dataclass(frozen=True)
class Scope:
    index: int
    pattern: str
    base: str = ""
    mode: str = ""


@dataclass(frozen=True)
class _AliasGuard:
    alias: str
    physical: str
    target: str
    link: tuple
    parent_chain: tuple
    target_chain: tuple


@dataclass(frozen=True)
class _ExpandedSelection:
    """Physical read paths plus immutable guards; never additional authority."""
    policy: object
    paths: tuple
    aliases: tuple


def _enumerable(policy, path):
    return (not policy._excluded(path)
            and any((rule.mode == "tree" and _under(path, rule.base))
                    or (rule.mode == "children" and path == rule.base) for rule in policy._read))


def _alias_directory(info, uid):
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("files-symlink")
    if info.st_uid != uid:
        raise ValueError("files-owner")
    if info.st_mode & 0o022:
        raise ValueError("files-permission")


def _selected_directory(policy, path):
    """Resolve only a v2 selected root alias to a literal sibling directory.

    All ancestors and the target still use no-follow descriptor walks. Neither
    recursive child links nor a file/ancestor link receives this exception.
    """
    parent, name = path.rsplit("/", 1)
    try:
        with _open_directory(parent or "/", policy.uid) as (directory, recheck):
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                raise ValueError("files-missing") from None
            if not stat.S_ISLNK(info.st_mode):
                recheck()
                return path, None
            if policy._version != 2:
                raise ValueError("files-symlink")
            if not _enumerable(policy, path):
                raise ValueError("files-denied")
            _alias_directory(os.fstat(directory), policy.uid)
            if info.st_uid != policy.uid:
                raise ValueError("files-owner")
            if info.st_nlink != 1:
                raise ValueError("files-links")
            target = os.readlink(name, dir_fd=directory)
            if (type(target) is not str or not target or target in (".", "..")
                    or "/" in target or len(target.encode("utf-8")) > 255):
                raise ValueError("files-symlink")
            physical = _absolute((parent or "") + "/" + target)
            # No target metadata is touched until its own lexical authority is
            # checked. An allowed alias cannot grant its sibling's contents.
            if not _enumerable(policy, physical):
                raise ValueError("files-denied")
            try:
                target_info = os.stat(target, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                raise ValueError("files-missing") from None
            _alias_directory(target_info, policy.uid)
            with _open_directory(physical, policy.uid) as (target_fd, target_recheck):
                _alias_directory(os.fstat(target_fd), policy.uid)
                if _fingerprint(target_info) != _fingerprint(os.fstat(target_fd)):
                    raise ValueError("files-changed")
                guard = _AliasGuard(path, physical, target, _fingerprint(info),
                                    recheck.snapshot, target_recheck.snapshot)
                target_recheck()
            if (_fingerprint(os.stat(name, dir_fd=directory, follow_symlinks=False)) != guard.link
                    or os.readlink(name, dir_fd=directory) != target):
                raise ValueError("files-changed")
            recheck()
            return physical, guard
    except _MissingParent:
        raise ValueError("files-missing") from None
    except FileNotFoundError:
        raise ValueError("files-missing") from None
    except PermissionError:
        raise ValueError("files-permission") from None
    except OSError:
        raise ValueError("files-access") from None


MAX_ALIAS_HOPS = 2


def _resolve_alias_path(policy, root, path, *, directory_scope, exact):
    """Resolve same-parent directory aliases at the scope root and on every component below it.

    General and name-independent: any component that is a symlink must satisfy the one guarded alias rule of
    `_selected_directory` (v2 policy, owner-owned, single link, literal sibling directory, both spellings readable and not
    denied). The final component of an exact (file) selector never receives it, so file links stay refused. Returns the
    physical path and one immutable guard per alias, re-checked right before and after the real open.
    """
    guards = []
    current_logical = current_physical = root
    if directory_scope:
        current_physical, guard = _selected_directory(policy, root)
        if guard is not None:
            guards.append(guard)
    tail = path[len(root):].lstrip("/")
    names = tail.split("/") if tail else []
    for position, name in enumerate(names):
        logical = current_logical + "/" + name
        candidate = current_physical + "/" + name
        if exact and position == len(names) - 1:
            current_logical, current_physical = logical, candidate
            break
        physical, guard = _selected_directory(policy, candidate)
        if guard is not None:
            guards.append(guard)
            if len(guards) > MAX_ALIAS_HOPS:
                raise ValueError("files-symlink")
        current_logical, current_physical = logical, physical
    return current_physical, guards


def _recheck_alias(policy, guard):
    parent, name = guard.alias.rsplit("/", 1)
    try:
        with _open_directory(parent or "/", policy.uid) as (directory, recheck):
            if recheck.snapshot != guard.parent_chain:
                raise ValueError("files-changed")
            _alias_directory(os.fstat(directory), policy.uid)
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if _fingerprint(before) != guard.link or not stat.S_ISLNK(before.st_mode):
                raise ValueError("files-changed")
            if os.readlink(name, dir_fd=directory) != guard.target:
                raise ValueError("files-changed")
            with _open_directory(guard.physical, policy.uid) as (target_fd, target_recheck):
                if target_recheck.snapshot != guard.target_chain:
                    raise ValueError("files-changed")
                _alias_directory(os.fstat(target_fd), policy.uid)
                target_recheck()
            if _fingerprint(os.stat(name, dir_fd=directory, follow_symlinks=False)) != guard.link:
                raise ValueError("files-changed")
            recheck()
    except (OSError, _MissingParent):
        raise ValueError("files-changed") from None


def recheck_selection(policy, selection):
    """Recheck captured alias identities, then return already allowed paths."""
    if type(selection) is not _ExpandedSelection:
        return selection
    if type(policy) is not Policy:
        raise ValueError("policy-type")
    if (selection.policy is not policy or type(selection.paths) is not tuple
            or type(selection.aliases) is not tuple or not 1 <= len(selection.aliases) <= MAX_RULES
            or any(type(guard) is not _AliasGuard for guard in selection.aliases)):
        raise ValueError("files-selection")
    policy.select(selection.paths, limit=policy.limits().read_files)  # Physical authority remains independently required.
    for guard in selection.aliases:
        if (not _enumerable(policy, guard.alias) or not _enumerable(policy, guard.physical)):
            raise ValueError("files-denied")
        _recheck_alias(policy, guard)
    return selection.paths


@dataclass(frozen=True)
class _Rule:
    original: str
    base: str
    mode: str

    def matches(self, path):
        if self.mode == "exact":
            return path == self.base
        if not path.startswith(self.base + "/"):
            return False
        return self.mode == "tree" or "/" not in path[len(self.base) + 1:]

    def denies(self, path):
        """A deny rule protects everything below what it names, however it is spelled: `~/x`, `~/x/`, `~/x/*`, `~/x/**`."""
        if self.mode == "children":
            return path.startswith(self.base + "/")
        return _under(path, self.base)


def _rule(value, home, *, version=1):
    if type(value) is not str:
        raise ValueError("policy-rule")
    directory_alias = version == 2 and value.endswith("/")
    mode = "tree" if directory_alias or value.endswith("/**") else "children" if value.endswith("/*") else "exact"
    base = value[:-1] if directory_alias else value[:-3] if mode == "tree" else value[:-2] if mode == "children" else value
    base = home if base == "~" and mode != "exact" else _canonical(base, home)
    return _Rule(value, base, mode)


def _reserved(path):
    """Recovery belongs to the executor, never to configured model access."""
    return any(part == ".ai-desktop-tasks" or part.startswith(".ai-task-")
               for part in path.split("/"))


def _foreign_home(path, home):
    return ((_under(path, "/home") or _under(path, "/root")) and not _under(path, home))


def _blocked(path, home):
    pieces = path.split("/")
    name = pieces[-1].casefold()
    return (any(_under(path, root) for root in _BLOCKED_ROOTS)
            or any(p in _BLOCKED_COMPONENTS for p in pieces)
            or any(p.startswith(".ai-task-") for p in pieces)
            or any(_under(path, home + "/" + item) for item in _BLOCKED_HOME)
            or name in _BLOCKED_NAMES or name.startswith(".env.")
            or name.endswith((".key", ".pem", ".p12", ".pfx", ".kdbx"))
            or (_under(path, "/home") and not _under(path, home))
            or (_under(path, "/root") and not _under(path, home)))


def _ssh_safe(path, home, rules):
    if ".ssh" not in path.split("/"):
        return True
    parent, name = path.rsplit("/", 1)
    return (parent == home + "/.ssh" and (name in ("config", "known_hosts", "authorized_keys")
            or (name.endswith(".pub") and name != ".pub"))
            and any(r.mode == "exact" and r.base == path for r in rules))


def validate_write_path(path, home, *, managed=False):
    """Pure boundary, not a grant. Managed v2 names are controlled by deny."""
    path, home = _absolute(path), _absolute(home)
    if type(managed) is not bool:
        raise ValueError("files-write-denied")
    if managed:
        if ((path != home and _under(home, path)) or _foreign_home(path, home) or _reserved(path)):
            raise ValueError("files-write-denied")
        return path
    if (_under(home, path) or _blocked(path, home)
            or any(_under(path, root) for root in _WRITE_BLOCKED_ROOTS)
            or any(piece.casefold() in _WRITE_BLOCKED_COMPONENTS or piece.casefold() in _BLOCKED_NAMES
                   or piece.casefold().startswith((".env.", ".ai-task-"))
                   or piece.casefold().endswith((".key", ".pem", ".p12", ".pfx", ".kdbx"))
                   for piece in path.split("/"))):
        raise ValueError("files-write-denied")
    return path


@dataclass(frozen=True)
class Policy:
    uid: int
    home: str
    _raw: bytes
    _identity: tuple
    _read: tuple
    _deny: tuple
    _write: tuple = ()
    _claude: bool = False
    _weather: bool = False
    _systemd: tuple = ()
    _version: int = 1
    _web: bool = False
    _file_size: int = DEFAULT_FILE_SIZE
    _part_size: int = DEFAULT_PART_SIZE
    _codex: bool = False
    _limits: Limits = Limits()
    _commands: tuple = ()
    _changes: tuple = ()
    _share_processes: bool = False

    def provider_allowed(self, provider):
        if provider not in ("claude", "codex"):
            raise ValueError("provider")
        return self._claude if provider == "claude" else self._codex

    def limits(self):
        """Per-question resource budgets (owner's `[files]` keys or the defaults)."""
        return self._limits

    def part_size(self):
        """Size of one portion sent per request in a whole-file analysis (owner's `part_size`, default 240 KiB)."""
        return self._part_size

    def file_size(self):
        """Largest single file `material` may import (owner's `file_size`, default 5 MiB, ceiling 16 MiB)."""
        return self._file_size

    def sources(self):
        """Detached availability for the whole ask, without contacting sources."""
        value = {"claude": self._claude, "weather": self._weather, "systemd": list(self._systemd)}
        if self._web:
            value["web"] = True
        return value

    def access(self):
        """Bounded planner metadata; IDs are labels, never file access grants."""
        value = {"files": [{"id": "f" + str(i), "pattern": rule.original}
                           for i, rule in enumerate(self._read)],
                 "systemd": list(self._systemd), "weather": self._weather}
        if self._web:
            value["web"] = True
        if self._write:
            value["write"] = [{"id": "w" + str(i), "pattern": rule.original}
                              for i, rule in enumerate(self._write)]
        if self._commands and self._limits.command_rounds > 0:
            value["commands"] = [" ".join(template) for template in self._commands]
            if self._changes:
                value["change_commands"] = [" ".join(template) for template in self._changes]
        if len(json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")) > MAX_ACCESS_BYTES:
            raise ValueError("policy-access-size")
        return value

    def changes(self):
        """The owner's change templates: commands with side effects, each needing a strong consent plus test and undo."""
        return self._changes

    def commands(self):
        """The owner's allowed command templates (token tuples); empty means the assistant may run nothing."""
        return self._commands

    def share_processes(self):
        """Owner opt-in: read-only commands see the host's processes (and their command lines)."""
        return self._share_processes

    def scopes(self):
        """Configuration metadata only: does not stat or enumerate targets."""
        return tuple(Scope(i, rule.original, rule.base, rule.mode) for i, rule in enumerate(self._read))

    def write_scopes(self):
        """Immutable write metadata only; no enumeration or target access."""
        return tuple(Scope(i, rule.original, rule.base, rule.mode) for i, rule in enumerate(self._write))

    def can_write(self, path, *, index=None):
        """Lexical membership only; caller must separately recheck this policy."""
        path = _canonical(path, self.home)
        if index is not None and (type(index) is not int or not 0 <= index < len(self._write)):
            raise ValueError("files-scope")
        rules = self._write if index is None else (self._write[index],)
        try:
            validate_write_path(path, self.home, managed=self._version == 2)
        except ValueError:
            return False
        return any(rule.matches(path) for rule in rules) and not any(rule.denies(path) for rule in self._deny)

    def require_write(self, paths, *, index=None):
        """Revalidate authority without stat/read of existing or proposed targets.

        Existing edits also need select(existing_paths). The task snapshot,
        never this lexical method, proves absence before creating a new file.
        """
        self.recheck()
        if type(paths) not in (list, tuple) or not 1 <= len(paths) <= MAX_FILES:
            raise ValueError("files-selection")
        result = tuple(_canonical(path, self.home) for path in paths)
        if len(set(result)) != len(result) or any(not self.can_write(path, index=index) for path in result):
            raise ValueError("files-write-denied")
        self.recheck()
        return result

    def write_workspace(self, index):
        """Verify one configured existing root; never create its directories."""
        self.recheck()
        if type(index) is not int or not 0 <= index < len(self._write):
            raise ValueError("files-scope")
        rule = self._write[index]
        root = rule.base.rsplit("/", 1)[0] if rule.mode == "exact" else rule.base
        validate_write_path(root, self.home, managed=self._version == 2)
        if any(deny.denies(root) for deny in self._deny):
            raise ValueError("files-write-denied")
        with _open_directory(root, self.uid, secure=True) as (directory, recheck):
            info = os.fstat(directory)
            if info.st_uid != self.uid or info.st_mode & 0o7022:
                raise ValueError("files-directory")
            recheck()
            self.recheck()
        return root

    def recheck(self):
        if _identity() != (self.uid, self.home):
            raise ValueError("policy-changed")
        current = _read(self.home + "/" + _POLICY_RELATIVE, self.uid, MAX_POLICY_BYTES, policy=True)
        if current != (self._raw, self._identity):
            raise ValueError("policy-changed")

    def _excluded(self, path):
        return (_reserved(path) or (self._version == 1 and _blocked(path, self.home))
                or any(rule.denies(path) for rule in self._deny))

    def _allowed(self, path):
        return (not self._excluded(path)
                and (self._version == 2 or _ssh_safe(path, self.home, self._read))
                and any(rule.matches(path) for rule in self._read))

    def allows(self, path):
        """Lexical read membership only (no target access); used by the project scanner."""
        try:
            return self._allowed(_canonical(path, self.home))
        except ValueError:
            return False

    def excludes(self, path):
        """True when deny rules or reserved/blocked locations exclude this path (lexical)."""
        try:
            return self._excluded(_canonical(path, self.home))
        except ValueError:
            return True

    def select(self, paths, *, limit=MAX_FILES):
        """Validate exact selections without target metadata/content access."""
        if type(paths) not in (list, tuple) or not 1 <= len(paths) <= limit:
            raise ValueError("files-selection")
        result = tuple(_canonical(path, self.home) for path in paths)
        if len(set(result)) != len(result) or any(not self._allowed(path) for path in result):
            raise ValueError("files-denied")
        return result

    def expand(self, indices):
        """Expand complete configured scopes through the same bounded walker."""
        if (type(indices) not in (list, tuple) or not indices or len(indices) > MAX_RULES
                or any(type(i) is not int or not 0 <= i < len(self._read) for i in indices)
                or len(set(indices)) != len(indices)):
            raise ValueError("files-scope")
        return self.expand_selected(tuple((index, None) for index in indices))

    def expand_selected(self, selections):
        """Expand only selected subpaths; check all boundaries before target I/O.

        Each pair is (scope index, relative exact path or terminal /* or /**).
        None and the empty string select the complete configured scope.
        """
        if type(selections) not in (list, tuple) or not 1 <= len(selections) <= MAX_RULES:
            raise ValueError("files-scope")
        rules, seen, roots = [], set(), []
        for selection in selections:
            if type(selection) not in (list, tuple) or len(selection) != 2:
                raise ValueError("files-scope")
            index, relative = selection
            if type(index) is not int or not 0 <= index < len(self._read):
                raise ValueError("files-scope")
            rule = self._read[index]
            roots.append((rule.base, rule.mode != "exact"))
            if relative is not None and relative != "":
                if (type(relative) is not str or relative.startswith("/")
                        or len(relative.encode("utf-8", errors="surrogatepass")) > 512):
                    raise ValueError("files-selection")
                mode = "tree" if relative.endswith("/**") else "children" if relative.endswith("/*") else "exact"
                tail = relative[:-3] if mode == "tree" else relative[:-2] if mode == "children" else relative
                target = _absolute(rule.base + "/" + tail)
                if (rule.mode == "exact" or not rule.matches(target)
                        or (rule.mode == "children" and mode != "exact")):
                    raise ValueError("files-denied")
                rule = _Rule(relative, target, mode)
            elif relative is not None and type(relative) is not str:
                raise ValueError("files-selection")
            key = (index, rule.base, rule.mode)
            if key in seen:
                raise ValueError("files-scope")
            seen.add(key)
            # Exact choices must not be silently omitted. Validate all of
            # them before enumerating even the first otherwise allowed tree.
            if rule.mode == "exact" and not self._allowed(rule.base):
                raise ValueError("files-denied")
            rules.append(rule)
        self.recheck()
        selected, scanned, aliases = set(), 0, []

        def add(path, logical=None):
            if self._allowed(path) and self._allowed(logical if logical is not None else path):
                selected.add(path)
                if len(selected) > self._limits.read_files:
                    raise ValueError("files-count")

        def visit(path, recursive, depth, logical=None):
            nonlocal scanned
            logical = path if logical is None else logical
            # Deny includes a recursive rule's own enumeration boundary.
            if (self._excluded(path) or self._excluded(logical)
                    or (self._version == 1 and ".ssh" in path.split("/"))):
                return
            if logical != path and (not _enumerable(self, path) or not _enumerable(self, logical)):
                return
            if depth > MAX_DEPTH:
                raise ValueError("files-depth")
            with _open_directory(path, self.uid) as (directory, recheck):
                names = []
                with os.scandir(directory) as entries:
                    for entry in entries:
                        scanned += 1
                        names.append(entry.name)
                        if len(names) > MAX_DIRECTORY_ENTRIES or scanned > MAX_TOTAL_ENTRIES:
                            raise ValueError("files-entries")
                for name in sorted(names):
                    target = _absolute(path + "/" + name)
                    logical_target = _absolute(logical + "/" + name)
                    if (self._excluded(target) or self._excluded(logical_target)
                            or (self._version == 1 and name == ".ssh")):
                        continue
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        if recursive:
                            visit(target, True, depth + 1, logical_target)
                    elif stat.S_ISLNK(info.st_mode):
                        raise ValueError("files-symlink")
                    elif stat.S_ISREG(info.st_mode):
                        add(target, logical_target)
                    else:
                        raise ValueError("files-type")
                recheck()

        for rule, (root, directory_scope) in zip(rules, roots):
            if rule.mode != "exact" and self._excluded(rule.base):
                continue
            physical, guards = _resolve_alias_path(self, root, rule.base, directory_scope=directory_scope,
                                                   exact=rule.mode == "exact")
            aliases.extend(guards)
            if rule.mode == "exact":
                add(physical, rule.base)
            else:
                visit(physical, rule.mode == "tree", 0, rule.base)
        self.recheck()
        if not selected:
            raise ValueError("files-empty")
        paths = self.select(tuple(sorted(selected)), limit=self._limits.read_files)
        if aliases:
            selection = _ExpandedSelection(self, paths, tuple(aliases))
            recheck_selection(self, selection)
            return selection
        return paths


def load_policy(*, optional=False):
    """Read only the fixed private policy. HOME/XDG/environment are ignored."""
    if type(optional) is not bool:
        raise ValueError("policy-option")
    uid, home = _identity()
    path = home + "/" + _POLICY_RELATIVE
    result = _read(path, uid, MAX_POLICY_BYTES, policy=True, optional=optional)
    if result is None:
        return None
    raw, identity = result
    try:
        try:
            value = tomllib.loads(raw.decode("utf-8", errors="strict"))
        except (tomllib.TOMLDecodeError, UnicodeError):
            raise _Problem("Plik nie jest poprawnym TOML-em (błąd składni, np. brak cudzysłowu, przecinka albo nawiasu).") from None
        if type(value) is not dict:
            raise ValueError("policy-schema")
        missing = [key for key in ("version", "files") if key not in value]
        if missing:
            raise _Problem("Brak wymaganego klucza najwyższego poziomu: " + ", ".join(missing) + ".")
        unknown = sorted(set(value) - {"version", "files", "network", "system", "commands"})
        if unknown:
            raise _Problem("Nieznana sekcja: " + ", ".join(_shown(key) for key in unknown) + ".")
        if type(value["version"]) is not int or value["version"] not in (1, 2):
            raise _Problem("version musi mieć wartość 1 lub 2.")
        if type(value["files"]) is not dict:
            raise _Problem("files musi być sekcją [files].")
        absent = [key for key in ("read", "deny") if key not in value["files"]]
        if absent:
            raise _Problem("W [files] brakuje: " + ", ".join(absent) + ".")
        stray = sorted(set(value["files"]) - {"read", "deny", "write", "file_size", "part_size", *LIMIT_SPECS})
        if stray:
            raise _Problem("Nieznany klucz w [files]: " + ", ".join(_shown(key) for key in stray) + ".")
        rules = []
        for key in ("read", "deny", "write"):
            items = value["files"].get(key, [])
            if type(items) is not list:
                raise _Problem("files.%s musi być listą." % key)
            if len(items) > MAX_RULES:
                raise _Problem("files.%s ma za dużo reguł (%d, max %d)." % (key, len(items), MAX_RULES))
            parsed = []
            for item in items:
                try:
                    parsed.append(_rule(item, home, version=value["version"]))
                except ValueError:
                    raise _Problem("Niepoprawna reguła w files.%s: %s" % (key, _shown(item))) from None
            again = _repeated([(r.base, r.mode) for r in parsed])
            if again:
                names = [rule.original for rule in parsed if (rule.base, rule.mode) in again]
                raise _Problem(*("Powtórzona reguła w files.%s: %s" % (key, _shown(name)) for name in dict.fromkeys(names)))
            rules.append(tuple(parsed))
        for rule in rules[0]:
            if (_reserved(rule.base) or (value["version"] == 1
                    and (_blocked(rule.base, home) or not _ssh_safe(rule.base, home, (rule,))))):
                raise _Problem("Reguła files.read wskazuje miejsce, którego nie wolno czytać: " + _shown(rule.original))
        for rule in rules[2]:
            try:
                validate_write_path(rule.base, home, managed=value["version"] == 2)
            except ValueError:
                raise _Problem("Reguła files.write jest niedozwolona: " + _shown(rule.original)) from None
        network, system = value.get("network", {}), value.get("system", {})
        if type(network) is not dict or set(network) - {"claude", "codex", "weather", "web"}:
            raise _Problem("[network] zna tylko klucze claude, codex, weather, web.")
        if any(type(flag) is not bool for flag in network.values()):
            raise _Problem("Wartości w [network] muszą być true albo false.")
        if type(system) is not dict or set(system) - {"systemd"}:
            raise _Problem("[system] zna tylko klucz systemd.")
        systemd = system.get("systemd", [])
        if (type(systemd) is not list or len(systemd) > 2
                or any(type(scope) is not str or scope not in ("system", "user") for scope in systemd)
                or len(set(systemd)) != len(systemd)):
            raise _Problem('system.systemd to lista z "system" i/lub "user", bez powtórzeń.')
        try:
            file_size = parse_file_size(value["files"].get("file_size", DEFAULT_FILE_SIZE))
        except ValueError:
            raise _SettingError("file_size") from None
        try:
            part_size = parse_part_size(value["files"].get("part_size", DEFAULT_PART_SIZE))
        except ValueError:
            raise _SettingError("part_size") from None
        limits = parse_limits(value["files"])
        section = value.get("commands", {})
        if type(section) is not dict or set(section) - {"allow", "change", "share_processes"}:
            raise _Problem("[commands] zna tylko klucze allow, change, share_processes.")
        from .command_ops import MAX_TEMPLATES, parse_template, parse_templates
        parsed_lists = {}
        for key in ("allow", "change"):
            items = section.get(key, [])
            if type(items) is not list:
                raise _Problem("commands.%s musi być listą." % key)
            if len(items) > MAX_TEMPLATES:
                raise _Problem("commands.%s ma za dużo szablonów (%d, max %d)." % (key, len(items), MAX_TEMPLATES))
            for item in items:
                try:
                    parse_template(item, strict=key == "change")
                except ValueError:
                    raise _Problem("Niepoprawny szablon w commands.%s: %s" % (key, _shown(item))) from None
            again = _repeated(items)
            if again:
                raise _Problem(*("Powtórzony szablon w commands.%s: %s" % (key, _shown(item)) for item in again))
            parsed_lists[key] = parse_templates(items, strict=key == "change")
        commands, changes = parsed_lists["allow"], parsed_lists["change"]
        both = [" ".join(t) for t in commands if t in set(changes)]
        if both:
            raise _Problem(*("Szablon jest jednocześnie w allow i change: " + _shown(item) for item in both))
        share = section.get("share_processes", False)
        if type(share) is not bool:
            raise _Problem("commands.share_processes musi być true albo false.")
        policy = Policy(uid, home, raw, identity, *rules,
                        network.get("claude", False), network.get("weather", False), tuple(systemd), value["version"],
                        network.get("web", False), file_size, part_size, network.get("codex", False), limits, commands, changes, share)
        for template in (*commands, *changes):
            for token in template[1:]:
                if token[:1] == "/":        # templates carry absolute literal paths only (`~` is refused by the parser)
                    if policy._excluded(token):
                        raise _Problem("Szablon wskazuje ścieżkę objętą files.deny: " + _shown(" ".join(template)))
        try:
            policy.access()  # Reject oversized metadata before any planner transfer.
        except ValueError:
            raise _Problem("Dane dostępu są zbyt duże do wysłania do modelu (za dużo reguł albo szablonów).") from None
        return policy
    except _SettingError as exc:
        error = ValueError("policy-invalid")
        error.setting = exc.args[0]          # closed name of the bad option; never the value
        error.problem = ("Ustawienie files.%s ma niepoprawną wartość albo przekracza pułap." % exc.args[0],)
        raise error from None
    except _Problem as exc:
        error = ValueError("policy-invalid")
        error.problem = exc.lines
        raise error from None
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise ValueError("policy-invalid") from None


def read_selected(policy, paths):
    """Read exact approved files; caller must acquire fresh local consent first."""
    if type(policy) is not Policy:
        raise ValueError("policy-type")
    limits = policy.limits()
    selected = policy.select(recheck_selection(policy, paths), limit=limits.read_files)
    policy.recheck()
    recheck_selection(policy, paths)
    files, total = [], 0
    for path in selected:
        raw, _ = _read(path, policy.uid, limits.read_file_size, system=policy._version == 2)
        try:
            content = raw.decode("utf-8", errors="strict")
        except UnicodeError:
            raise ValueError("files-text") from None
        if policy._version == 1 and contains_private_key(content):
            raise ValueError("files-private-key")
        total += len(raw)
        if total > limits.read_total_size:
            raise ValueError("files-total")
        files.append({"path": path, "content": content})
    policy.recheck()
    recheck_selection(policy, paths)
    return validate_context({"kind": "files", "files": files})


def read_bounded(policy, path, limit, *, recheck=True):
    """Read ONE exact file allowed by the policy, up to `limit` bytes (caller supplies consent).

    Same ownership, link, type, mode and race checks as read_selected, but without
    its per-file budget so a deliberately attached material can be larger. Content
    validation (encoding, control characters) is the caller's job.
    """
    if type(policy) is not Policy or type(limit) is not int or not 1 <= limit <= 16 * 1024 * 1024:
        raise ValueError("policy-type")
    (selected,) = policy.select([path])
    if recheck:
        policy.recheck()
    raw, _ = _read(selected, policy.uid, limit, system=policy._version == 2)
    if recheck:
        policy.recheck()
    return selected, raw



# Narrow aliases for the read-only project scanner (same descriptor-relative, no-follow walk primitives).
open_directory = _open_directory
absolute_path = _absolute
