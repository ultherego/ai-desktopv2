"""Per-task immutable native client payloads for the existing bwrap boundary.

Only compiled installation layouts are supported. Root-owned source metadata
is not vendor authentication. The ordinary read-only /usr mount remains the OS
runtime baseline; private client resources are snapshotted in their entirety.
The memfds contain no credentials, and only bwrap receives their descriptors.
Bwrap copies them into linked files on private tmpfs mounts made read-only
before execution. This preserves a live canonical executable path for clients
that discover their resources through /proc/self/exe.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import os
import stat

from .analysis_contract import AnalysisDenied
from . import system_clients as clients


MAX_FILE_BYTES = 384 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_DEPTH = 24
INNER_ROOT = "/run/ai-desktop-native"
# Linux UAPI constants are absent in some otherwise supported Python builds.
_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
_SEALS = 0x0f  # WRITE | GROW | SHRINK | SEAL


def binary(provider):
    return INNER_ROOT + "/" + clients._provider(provider)


def reason(status):
    return {"missing": "client-missing", "unsafe": "client-unsafe",
            "unsupported": "client-layout", "changed": "client-changed",
            "unavailable": "client-unavailable"}.get(status, "client-unavailable")


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class _Entry:
    path: str
    kind: str
    identity: tuple
    target: str | None = None


@dataclass(frozen=True)
class _Tree:
    root: str
    nodes: tuple
    entries: tuple[_Entry, ...]


class _Capture:
    def __init__(self, *, copy):
        self.copy = copy
        self.bytes = self.entries = 0
        self.files = {}

    def account(self, info):
        self.entries += 1
        if self.entries > MAX_ENTRIES:
            raise AnalysisDenied("client-layout")
        if stat.S_ISREG(info.st_mode):
            if not 0 <= info.st_size <= MAX_FILE_BYTES:
                raise AnalysisDenied("client-layout")
            self.bytes += info.st_size
            if self.bytes > MAX_TOTAL_BYTES:
                raise AnalysisDenied("client-layout")

    def file(self, source, path, before):
        clients._capabilities(source)
        if not self.copy:
            return
        flags = os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
        try:
            descriptor = os.memfd_create("ai-desktop-native", flags | getattr(os, "MFD_EXEC", 0x10))
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise
            # Before Linux 6.3 memfds were executable by default. Do not retry
            # a permission refusal from a host that prohibits executable memfds.
            descriptor = os.memfd_create("ai-desktop-native", flags)
        try:
            remaining = before.st_size
            while remaining:
                block = os.read(source, min(1024 * 1024, remaining))
                if not block:
                    raise AnalysisDenied("client-changed")
                remaining -= len(block)
                written = 0
                while written < len(block):
                    count = os.write(descriptor, block[written:])
                    if count <= 0:
                        raise AnalysisDenied("client-unavailable")
                    written += count
            if os.read(source, 1) or _signature(os.fstat(source)) != _signature(before):
                raise AnalysisDenied("client-changed")
            mode = 0o555 if before.st_mode & 0o111 else 0o444
            os.fchmod(descriptor, mode)
            fcntl.fcntl(descriptor, _ADD_SEALS, _SEALS)
            if fcntl.fcntl(descriptor, _GET_SEALS) != _SEALS:
                raise AnalysisDenied("client-unavailable")
            os.lseek(descriptor, 0, os.SEEK_SET)
            self.files[path] = (descriptor, mode)
        except BaseException:
            os.close(descriptor)
            raise

    def close(self):
        for descriptor, _ in self.files.values():
            os.close(descriptor)
        self.files.clear()


def _inside(root, path):
    return path == root or path.startswith(root + "/")


def _names(descriptor, maximum, *, changed=False):
    names = []
    with os.scandir(descriptor) as entries:
        for entry in entries:
            if len(names) >= maximum:
                raise AnalysisDenied("client-changed" if changed else "client-layout")
            names.append(entry.name)
    return sorted(names)


def _tree(root, capture):
    try:
        resolved, identity, nodes, descriptor = clients._walk(root, kind="directory", capture=True)
    except FileNotFoundError:
        return _Tree(root, (), ())
    # A private layout root itself must be a directory, not an alias to an
    # unrelated package. Symlinks inside it may only refer inside the root.
    if resolved != root:
        os.close(descriptor)
        raise AnalysisDenied("client-layout")
    entries = []

    def visit(fd, path, depth):
        if depth > MAX_DEPTH:
            raise AnalysisDenied("client-layout")
        before = os.fstat(fd)
        clients._trusted(before)
        capture.account(before)
        entries.append(_Entry(path, "directory", _signature(before)))
        names = _names(fd, MAX_ENTRIES - capture.entries)
        for name in names:
            if (name in ("", ".", "..") or "/" in name
                    or any(ord(char) < 32 or ord(char) == 127 for char in name)):
                raise AnalysisDenied("client-layout")
            child = path + "/" + name
            if len(os.fsencode(child)) > clients.MAX_PATH_BYTES:
                raise AnalysisDenied("client-layout")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            clients._trusted(info)
            if stat.S_ISLNK(info.st_mode):
                capture.account(info)
                target = os.readlink(name, dir_fd=fd)
                destination = clients._link_target(path, target)
                if not _inside(root, destination):
                    raise AnalysisDenied("client-layout")
                entries.append(_Entry(child, "symlink", _signature(info), target))
            elif stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise AnalysisDenied("client-unsafe")
                opened = os.open(name, clients._DIR if stat.S_ISDIR(info.st_mode) else clients._FILE,
                                 dir_fd=fd)
                try:
                    actual = os.fstat(opened)
                    if _signature(actual) != _signature(info):
                        raise AnalysisDenied("client-changed")
                    if stat.S_ISDIR(info.st_mode):
                        visit(opened, child, depth + 1)
                    else:
                        capture.account(info)
                        capture.file(opened, child, info)
                        entries.append(_Entry(child, "file", _signature(info)))
                    if _signature(os.fstat(opened)) != _signature(info):
                        raise AnalysisDenied("client-changed")
                finally:
                    os.close(opened)
            else:
                raise AnalysisDenied("client-layout")
            if _signature(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _signature(info):
                raise AnalysisDenied("client-changed")
        if _names(fd, len(names), changed=True) != names or _signature(os.fstat(fd)) != _signature(before):
            raise AnalysisDenied("client-changed")

    try:
        if clients._identity(os.fstat(descriptor)) != identity:
            raise AnalysisDenied("client-changed")
        visit(descriptor, root, 0)
    finally:
        os.close(descriptor)
    # Resolve symlinks against the captured inventory, not against a changing
    # host path. No dangling link, loop or escape is admitted to the snapshot.
    lookup = {entry.path: entry for entry in entries}
    for entry in entries:
        if entry.kind != "symlink":
            continue
        pending = entry.path[len(root):].lstrip("/").split("/")
        current, links = root, 0
        while pending:
            name, *pending = pending
            current += "/" + name
            node = lookup.get(current)
            if node is None:
                raise AnalysisDenied("client-layout")
            if node.kind == "symlink":
                links += 1
                if links > clients.MAX_LINKS:
                    raise AnalysisDenied("client-layout")
                target = clients._link_target(os.path.dirname(current), node.target)
                pending = target[len(root):].lstrip("/").split("/") + pending if target != root else pending
                current = root
            elif pending and node.kind != "directory":
                raise AnalysisDenied("client-layout")
    return _Tree(root, nodes, tuple(entries))


class BoundClient:
    """One sequential task's sealed payload. Not a persisted execution grant."""
    def __init__(self, provider, inspection, trees, capture, executable):
        self.provider = provider
        self.binary = binary(provider)
        self._inspection, self._trees = inspection, trees
        self._capture, self._executable = capture, executable
        self._closed = False

    @property
    def pass_fds(self):
        if self._closed:
            raise AnalysisDenied("client-changed")
        return tuple(value[0] for value in self._capture.files.values())

    def recheck(self):
        if self._closed:
            raise AnalysisDenied("client-changed")
        try:
            if not self._inspection.recheck():
                raise AnalysisDenied("client-changed")
            fresh = _Capture(copy=False)
            if tuple(_tree(tree.root, fresh) for tree in self._trees) != self._trees:
                raise AnalysisDenied("client-changed")
        except (AnalysisDenied, clients._Denied, OSError, ValueError):
            raise AnalysisDenied("client-changed") from None

    def mount_args(self):
        self.recheck()
        # --ro-bind resolves source paths and cannot resolve an unlinked memfd;
        # --ro-bind-data unlinks its temporary backing file. Keep --file copies
        # linked inside our tmpfs so current_exe().canonicalize() still works.
        args = ["--tmpfs", INNER_ROOT, "--dir", INNER_ROOT + "/payload"]
        mounted = set()
        for tree in self._trees:
            if not tree.entries:
                continue
            args += ["--tmpfs", tree.root]
            for entry in tree.entries:
                if entry.kind == "directory":
                    if entry.path != tree.root:
                        args += ["--dir", entry.path]
                elif entry.kind == "symlink":
                    args += ["--symlink", entry.target, entry.path]
                else:
                    descriptor, mode = self._capture.files[entry.path]
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    args += ["--perms", format(mode, "o"), "--file", str(descriptor), entry.path]
                    mounted.add(entry.path)
            args += ["--remount-ro", tree.root]
        if self._inspection.path not in mounted:
            descriptor, mode = self._capture.files[self._inspection.path]
            os.lseek(descriptor, 0, os.SEEK_SET)
            args += ["--perms", format(mode, "o"), "--file", str(descriptor), self._executable]
        args += ["--symlink", self._executable, self.binary, "--remount-ro", INNER_ROOT]
        return args

    def close(self):
        if not self._closed:
            self._closed = True
            self._capture.close()


@contextmanager
def bind(provider):
    """Snapshot exactly one compiled installation before any auth or network."""
    provider = clients._provider(provider)
    capture = _Capture(copy=True)
    bound = None
    try:
        try:
            inspection = clients.inspect(provider)
            if inspection.status != "present":
                raise AnalysisDenied(reason(inspection.status))
            roots = clients._PACKAGE_ROOTS[provider]
            matching = tuple(root for root in roots if _inside(root, inspection.path))
            bundled = bool(matching)
            if not bundled and inspection.path != clients._CANDIDATES[provider]:
                raise AnalysisDenied("client-layout")
            roots = matching if matching else tuple(root for root in roots
                if not _inside(clients._REGISTERED_ROOTS[provider], root))
            trees = tuple(_tree(root, capture) for root in roots)
            executable = inspection.path if bundled else INNER_ROOT + "/payload/" + provider
            if inspection.path not in capture.files:
                path, identity, nodes, descriptor = clients._walk(inspection.path, capture=True)
                try:
                    if (path != inspection.path or identity != inspection.identity):
                        raise AnalysisDenied("client-changed")
                    info = os.fstat(descriptor)
                    capture.account(info)
                    capture.file(descriptor, inspection.path, info)
                finally:
                    os.close(descriptor)
            bound = BoundClient(provider, inspection, trees, capture, executable)
            bound.recheck()
        except clients._Denied as exc:
            raise AnalysisDenied(reason(exc.status)) from None
        except (OSError, ValueError):
            raise AnalysisDenied("client-unavailable") from None
        yield bound
    finally:
        if bound is None:
            capture.close()
        else:
            bound.close()
