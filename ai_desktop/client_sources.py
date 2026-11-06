"""Read-only discovery of independently installed native client payloads.

User-owned inputs are never execution authority. This module only describes and
hashes a bounded native payload for a separate, explicitly approved root import.
It never executes a wrapper, package manager, installer or client, and never
selects an authentication/configuration directory as a payload.
"""
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat

from .e2_ipc import _unique_pairs
from .system_clients import _native_elf
from .client_limits import MAX_FILE_BYTES, MAX_TOTAL_BYTES, MAX_ENTRIES, MAX_DEPTH


PROVIDERS = ("claude", "codex")
MAX_PATH_BYTES = 4096
MAX_LINKS = 16
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class SourceError(ValueError):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail
        super().__init__(status + ":" + detail)


@dataclass(frozen=True)
class Source:
    provider: str
    status: str
    path: str | None = None
    native_path: str | None = None
    resolved_root: str | None = None
    layout: str | None = None
    files_count: int = 0
    total_bytes: int = 0
    digest: str | None = None
    detail: str = ""

    @property
    def bytes(self):
        return self.total_bytes


@dataclass(frozen=True)
class Entry:
    path: str
    kind: str
    mode: int
    size: int = 0
    digest: str | None = None
    target: str | None = None


def _provider(provider):
    if type(provider) is not str or provider not in PROVIDERS:
        raise ValueError("provider")
    return provider


def _uid(uid):
    uid = os.getuid() if uid is None else uid
    if type(uid) is not int or not 0 <= uid < 2**32 - 1:
        raise ValueError("uid")
    return uid


def _path(path):
    path = os.fspath(path)
    if (type(path) is not str or not path.startswith("/") or os.path.normpath(path) != path
            or len(os.fsencode(path)) > MAX_PATH_BYTES
            or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        raise SourceError("unsafe", "path")
    return path


def _signature(info, *, ancestor=False):
    if ancestor and stat.S_ISDIR(info.st_mode):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _trusted(info, uid):
    if info.st_uid not in (0, uid):
        raise SourceError("unsafe", "owner")
    if info.st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise SourceError("unsafe", "special-mode")
    if stat.S_ISLNK(info.st_mode):
        return
    if info.st_mode & 0o022:
        raise SourceError("unsafe", "permissions")
    # A privileged importer must not use its authority to read a root-private
    # source that the approving caller could not inspect. Known system clients
    # are publicly readable; caller-owned HOME trees may remain mode 0700.
    required = 0o500 if stat.S_ISDIR(info.st_mode) else 0o400
    if info.st_uid != uid:
        required >>= 6
    if info.st_mode & required != required:
        raise SourceError("unsafe", "inaccessible-to-caller")


def _link(parent, target):
    if target.startswith("/"):
        return _path(target)
    parts = target.split("/")
    ancestors = parent.split("/")[1:]
    while parts and parts[0] in (".", ".."):
        first = parts.pop(0)
        if first == "..":
            if not ancestors:
                raise SourceError("unsafe", "symlink-target")
            ancestors.pop()
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise SourceError("unsafe", "symlink-target")
    return _path("/" + "/".join(ancestors + parts))


def _capabilities(fd):
    try:
        value = os.getxattr(fd, "security.capability")
    except OSError as exc:
        if exc.errno in (errno.ENODATA, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)):
            return
        raise SourceError("unavailable", "capabilities") from None
    if value:
        raise SourceError("unsafe", "capabilities")


def _names(fd, limit):
    """Never materialize an attacker-controlled directory beyond the budget."""
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(names) >= limit:
                raise SourceError("unsupported", "entry-limit")
            names.append(entry.name)
    return sorted(names)


def _open(path, uid, *, kind="file"):
    """Resolve checked links with descriptors; return one held final handle."""
    path = _path(path)
    descriptors, nodes, edges = [], [], []
    keep = None
    try:
        root = os.open("/", _DIR)
        descriptors.append(root)
        info = os.fstat(root)
        _trusted(info, uid)
        nodes.append(("/", _signature(info, ancestor=True), None))
        current, parent = root, "/"
        pending = path.split("/")[1:]
        links = count = 0
        while pending:
            count += 1
            if count > 256:
                raise SourceError("unsafe", "components")
            name, *pending = pending
            resolved = os.path.join(parent, name)
            before = os.stat(name, dir_fd=current, follow_symlinks=False)
            _trusted(before, uid)
            edges.append((current, name, _signature(before, ancestor=bool(pending))))
            if stat.S_ISLNK(before.st_mode):
                links += 1
                if links > MAX_LINKS:
                    raise SourceError("unsafe", "symlink-limit")
                target = os.readlink(name, dir_fd=current)
                if _signature(os.stat(name, dir_fd=current, follow_symlinks=False)) != _signature(before):
                    raise SourceError("changed", "source-changed")
                nodes.append((resolved, _signature(before), target))
                destination = _link(parent, target)
                pending = destination.split("/")[1:] + pending
                current, parent = root, "/"
                continue
            directory = bool(pending) or kind == "directory"
            if not (stat.S_ISDIR(before.st_mode) if directory else stat.S_ISREG(before.st_mode)):
                raise SourceError("unsupported", "not-regular")
            if not directory and before.st_nlink != 1:
                raise SourceError("unsafe", "hardlink")
            descriptor = os.open(name, _DIR if directory else _FILE, dir_fd=current)
            descriptors.append(descriptor)
            if _signature(os.fstat(descriptor)) != _signature(before):
                raise SourceError("changed", "source-changed")
            nodes.append((resolved, _signature(before, ancestor=bool(pending)), None))
            if pending:
                current, parent = descriptor, resolved
                continue
            for edge_fd, entry, expected in edges:
                observed = os.stat(entry, dir_fd=edge_fd, follow_symlinks=False)
                if _signature(observed, ancestor=stat.S_ISDIR(observed.st_mode) and len(expected) == 5) != expected:
                    raise SourceError("changed", "source-changed")
            if not directory:
                _capabilities(descriptor)
            keep = descriptor
            return resolved, descriptor, tuple(nodes)
        raise SourceError("unsafe", "path-root")
    finally:
        for descriptor in reversed(descriptors):
            if descriptor != keep:
                os.close(descriptor)


class _Scan:
    def __init__(self, uid, writer):
        self.uid, self.writer = uid, writer
        self.observed, self.directories, self.entries, self.absent = [], [], [], []
        self.total_bytes = 0

    def open(self, path, *, kind="file"):
        resolved, fd, nodes = _open(path, self.uid, kind=kind)
        self.observed.append((path, kind, resolved, nodes))
        return resolved, fd

    def exists(self, path, *, kind="file"):
        try:
            _, fd = self.open(path, kind=kind)
        except FileNotFoundError:
            self.absent.append((path, kind))
            return False
        os.close(fd)
        return True

    def small_json(self, path):
        _, fd = self.open(path)
        try:
            if os.fstat(fd).st_size > 65536:
                raise SourceError("unsupported", "package-metadata")
            data = os.read(fd, 65537)
            value = json.loads(data, object_pairs_hook=_unique_pairs)
            if type(value) is not dict:
                raise ValueError()
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise SourceError("unsupported", "package-metadata") from None
        finally:
            os.close(fd)

    def add(self, entry, fd=None):
        if len(self.entries) >= MAX_ENTRIES:
            raise SourceError("unsupported", "entry-limit")
        self.entries.append(entry)
        if self.writer is not None:
            self.writer(entry, fd)

    def file(self, source, destination, *, native=False):
        _, fd = self.open(source)
        try:
            before = os.fstat(fd)
            if not 0 <= before.st_size <= MAX_FILE_BYTES:
                raise SourceError("unsupported", "file-limit")
            if native and (not before.st_mode & 0o111 or not _native_elf(os.pread(fd, 64, 0))):
                raise SourceError("unsupported", "native-elf")
            self.total_bytes += before.st_size
            if self.total_bytes > MAX_TOTAL_BYTES:
                raise SourceError("unsupported", "byte-limit")
            digest, offset = hashlib.sha256(), 0
            while offset < before.st_size:
                data = os.pread(fd, min(1024 * 1024, before.st_size - offset), offset)
                if not data:
                    raise SourceError("changed", "source-changed")
                digest.update(data)
                offset += len(data)
            mode = 0o555 if before.st_mode & 0o111 else 0o444
            self.add(Entry(destination, "file", mode, before.st_size, digest.hexdigest()), fd)
            if os.pread(fd, 1, offset) or _signature(os.fstat(fd)) != _signature(before):
                raise SourceError("changed", "source-changed")
        finally:
            os.close(fd)

    def tree(self, source, destination, *, root, depth=0, native_destination=None):
        if depth > MAX_DEPTH:
            raise SourceError("unsupported", "depth-limit")
        resolved, fd = self.open(source, kind="directory")
        try:
            before = os.fstat(fd)
            names = _names(fd, MAX_ENTRIES - len(self.entries))
            self.directories.append((source, _signature(before), tuple(names)))
            self.add(Entry(destination, "directory", 0o755))
            for name in names:
                if (name in ("", ".", "..") or "/" in name
                        or any(ord(char) < 32 or ord(char) == 127 for char in name)):
                    raise SourceError("unsupported", "entry-name")
                path, dest = resolved + "/" + name, destination + "/" + name
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                _trusted(info, self.uid)
                if stat.S_ISLNK(info.st_mode):
                    target = os.readlink(name, dir_fd=fd)
                    resolved_target = _link(resolved, target)
                    if not resolved_target.startswith(root + "/"):
                        raise SourceError("unsupported", "resource-link")
                    # Flatten only inside-root links to ordinary copied resources.
                    # The walk rejects loops and checks every link/ancestor.
                    actual, target_fd = self.open(path)
                    os.close(target_fd)
                    if not actual.startswith(root + "/"):
                        raise SourceError("unsupported", "resource-link")
                    self.file(path, dest, native=dest == native_destination)
                elif stat.S_ISDIR(info.st_mode):
                    self.tree(path, dest, root=root, depth=depth + 1, native_destination=native_destination)
                elif stat.S_ISREG(info.st_mode):
                    self.file(path, dest, native=dest == native_destination)
                else:
                    raise SourceError("unsupported", "resource-type")
                if _signature(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _signature(info):
                    raise SourceError("changed", "source-changed")
        finally:
            os.close(fd)

    def recheck(self):
        for path, kind in self.absent:
            try:
                _, fd, _ = _open(path, self.uid, kind=kind)
            except FileNotFoundError:
                continue
            except (OSError, SourceError):
                raise SourceError("changed", "source-changed") from None
            os.close(fd)
            raise SourceError("changed", "source-changed")
        for path, kind, resolved, nodes in self.observed:
            try:
                actual, fd, fresh = _open(path, self.uid, kind=kind)
                os.close(fd)
            except (OSError, SourceError):
                raise SourceError("changed", "source-changed") from None
            if actual != resolved or fresh != nodes:
                raise SourceError("changed", "source-changed")
        for path, identity, names in self.directories:
            _, fd, _ = _open(path, self.uid, kind="directory")
            try:
                if _signature(os.fstat(fd)) != identity or tuple(_names(fd, MAX_ENTRIES)) != names:
                    raise SourceError("changed", "source-changed")
            finally:
                os.close(fd)


def _native(scanner, path, provider):
    resolved, fd = scanner.open(path)
    try:
        info = os.fstat(fd)
        if _native_elf(os.pread(fd, 64, 0)) and info.st_mode & 0o111:
            return resolved
    finally:
        os.close(fd)
    # Fixed official npm package layouts only. Never interpret wrapper text.
    root = str(Path(resolved).parent.parent)
    if not scanner.exists(root + "/package.json"):
        raise SourceError("unsupported", "wrapper")
    package = scanner.small_json(root + "/package.json")
    expected = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex"}[provider]
    if package.get("name") != expected:
        raise SourceError("unsupported", "wrapper")
    if provider == "claude":
        candidates = [root + "/bin/claude.exe", root + "/claude"]
        for prefix in (root + "/node_modules/@anthropic-ai", str(Path(root).parent)):
            for name in ("claude-code-linux-x64", "claude-code-linux-x64-musl"):
                candidates += [prefix + "/" + name + "/claude", prefix + "/" + name + "/bin/claude"]
    else:
        roots = [root, root + "/node_modules/@openai/codex-linux-x64",
                 str(Path(root).parent / "codex-linux-x64")]
        candidates = [base + "/vendor/x86_64-unknown-linux-musl/" + tail
                      for base in roots for tail in ("bin/codex", "codex/codex")]
    for candidate in candidates:
        if candidate == resolved:
            continue
        try:
            native, descriptor = scanner.open(candidate)
        except FileNotFoundError:
            continue
        try:
            info = os.fstat(descriptor)
            if _native_elf(os.pread(descriptor, 64, 0)) and info.st_mode & 0o111:
                return native
            raise SourceError("unsupported", "native-elf")
        finally:
            os.close(descriptor)
    raise SourceError("unsupported", "native-package-missing")


def capture(provider, path, *, uid=None, writer=None):
    """Hash/copy only selected native bytes, then recheck all observed sources."""
    provider, uid, path = _provider(provider), _uid(uid), _path(path)
    scanner = _Scan(uid, writer)
    native = _native(scanner, path, provider)
    root, layout = str(Path(native).parent), "native-file"
    if provider == "codex":
        candidate = str(Path(native).parent.parent) if Path(native).parent.name in ("bin", "codex") else str(Path(native).parent)
        if scanner.exists(candidate + "/codex-package.json"):
            scanner.small_json(candidate + "/codex-package.json")
            if native != candidate + "/bin/codex":
                raise SourceError("unsupported", "bundle-layout")
            root, layout = candidate, "codex-bundle"
        elif (scanner.exists(candidate + "/codex-resources", kind="directory")
              or native.endswith("/vendor/x86_64-unknown-linux-musl/codex/codex")
              or Path(native).parent.name == "codex" and scanner.exists(candidate + "/path", kind="directory")):
            # Older releases infer resources from their original CODEX_HOME
            # release hierarchy. Relocating their ELF silently drops that
            # contract; upgrading through the original installer is required.
            raise SourceError("unsupported", "legacy-layout")
    complete_bin = provider == "codex" and layout != "native-file" and native == root + "/bin/codex"
    if complete_bin:
        scanner.tree(root + "/bin", "bin", root=root, native_destination="bin/codex")
    else:
        scanner.add(Entry("bin", "directory", 0o755))
        scanner.file(native, "bin/" + provider, native=True)
    if provider == "codex" and layout != "native-file":
        # Do not import siblings such as .codex/auth.json, npm's node_modules,
        # shell wrappers, user configuration or arbitrary package-manager data.
        for filename in ("codex-package.json",):
            if scanner.exists(root + "/" + filename):
                scanner.file(root + "/" + filename, filename)
        for dirname in ("codex-path", "codex-resources"):
            if scanner.exists(root + "/" + dirname, kind="directory"):
                scanner.tree(root + "/" + dirname, dirname, root=root)
    scanner.recheck()
    # Bind the reviewed bytes to the exact launcher chain and source objects,
    # including a replacement that happens to contain identical bytes. Directory
    # ancestors intentionally exclude mtimes so unrelated HOME activity does not
    # invalidate a review; complete resource directories retain their identity
    # and listing. The privileged importer independently repeats this capture.
    manifest = {"provider": provider, "source_uid": uid, "source": path,
                "native": native, "root": root, "layout": layout,
                "entries": [entry.__dict__ for entry in scanner.entries],
                "observed": scanner.observed, "directories": scanner.directories,
                "absent": scanner.absent}
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    return Source(provider, "present", path, native, root, layout,
                  sum(entry.kind == "file" for entry in scanner.entries), scanner.total_bytes, digest)


def discover(provider, explicit_path=None, *, uid=None, environ=None):
    """Resolve the caller's chosen command without running PATH entries."""
    provider, uid = _provider(provider), _uid(uid)
    path = None
    try:
        if explicit_path is not None:
            path = _path(explicit_path)
        else:
            environment = os.environ if environ is None else environ
            search = environment.get("PATH", "")
            if type(search) is not str or len(search) > 32768:
                raise SourceError("unsafe", "search-path")
            directories = search.split(os.pathsep)[:64]
            directories += [pwd.getpwuid(uid).pw_dir + "/.local/bin", "/usr/local/bin", "/usr/bin"]
            seen = set()
            for directory in directories:
                if not directory.startswith("/"):
                    continue
                candidate = _path(directory.rstrip("/") + "/" + provider)
                if candidate in seen:
                    continue
                seen.add(candidate)
                try:
                    os.lstat(candidate)
                except FileNotFoundError:
                    continue
                path = candidate
                break
            if path is None:
                raise SourceError("missing", "missing")
        return capture(provider, path, uid=uid)
    except SourceError as exc:
        return Source(provider, exc.status, path=path, detail=exc.detail)
    except FileNotFoundError:
        return Source(provider, "missing", path=path, detail="missing")
    except (OSError, ValueError, KeyError, TypeError):
        return Source(provider, "unavailable", path=path, detail="unavailable")
