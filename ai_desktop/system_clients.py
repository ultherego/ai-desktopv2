"""Read-only metadata for system-installed native clients, never a launcher.

Finding a root-owned ELF does not authenticate its vendor, establish protocol
compatibility or prove that its runtime dependencies are available. No client,
shell, package manager, credential store or network operation is invoked here.
The launcher separately snapshots the selected installation before execution.
"""
from dataclasses import dataclass, field
import errno
import os
import re
import stat
from types import MappingProxyType


_CANDIDATES = MappingProxyType({"claude": "/usr/bin/claude", "codex": "/usr/bin/codex"})
_NATIVE_ALTERNATES = MappingProxyType({"claude": "/opt/claude-code/bin/claude"})
_REGISTERED_ROOTS = MappingProxyType({provider: "/var/lib/ai-desktop-clients/" + provider
                                    for provider in ("claude", "codex")})
_PACKAGE_ROOTS = MappingProxyType({"claude": ("/opt/claude-code", _REGISTERED_ROOTS["claude"] + "/payload"),
                                 "codex": ("/usr/lib/openai-codex", _REGISTERED_ROOTS["codex"] + "/payload")})
_TRUSTED_ROOTS = ("/usr", "/opt", "/var/lib/ai-desktop-clients")
_BASELINES = MappingProxyType({"claude": (2, 1, 312), "codex": (0, 166, 0)})
MAX_LINKS = 16
MAX_COMPONENTS = 256
MAX_PATH_BYTES = 4096
MAX_VERSION_BYTES = 128
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_VERSION = r"(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})"
_VERSION_PATTERNS = {
    "claude": re.compile(_VERSION + r" \(Claude Code\)(?:\r?\n)?\Z", re.ASCII),
    "codex": re.compile(r"codex-cli " + _VERSION + r"(?:\r?\n)?\Z", re.ASCII),
}


@dataclass(frozen=True)
class Fingerprint:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _Node:
    path: str
    identity: Fingerprint
    mode: int
    uid: int
    gid: int
    links: int
    target: str | None = None


@dataclass(frozen=True)
class Inspection:
    provider: str
    status: str
    path: str | None
    detail: str
    identity: Fingerprint | None = None
    _nodes: tuple[_Node, ...] = field(default=(), repr=False)
    source: str = "system"

    def recheck(self):
        """Whether the fixed candidate still matches this complete snapshot.

        This is metadata evidence, not an execution authorization or an open
        executable handle: a future launcher must also bind the file it uses.
        """
        if self.status != "present" or self.identity is None or not self._nodes:
            return False
        fresh = inspect(self.provider)
        return (fresh.status == "present" and fresh.path == self.path
                and fresh.identity == self.identity and fresh._nodes == self._nodes
                and fresh.source == self.source)


@dataclass(frozen=True)
class Compatibility:
    provider: str
    status: str
    version: str | None
    detail: str


class _Denied(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail


def _provider(provider):
    if type(provider) is not str or provider not in _CANDIDATES:
        raise ValueError("provider")
    return provider


def _identity(info):
    return Fingerprint(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _node(path, info, target=None):
    directory = stat.S_ISDIR(info.st_mode)
    # Unrelated children can change directory timestamps/size/link count. Its
    # inode, owner and permissions, and the parent/name binding, are what matter.
    identity = Fingerprint(info.st_dev, info.st_ino, 0, 0, 0) if directory else _identity(info)
    return _Node(path, identity, info.st_mode, info.st_uid, info.st_gid,
                 0 if directory else info.st_nlink, target)


def _trusted(info):
    if info.st_uid != 0:
        raise _Denied("unsafe", "owner")
    # Symlink permission bits have no access-control meaning on Linux. Their
    # owner and every containing directory are checked instead.
    if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022:
        raise _Denied("unsafe", "permissions")
    if info.st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise _Denied("unsafe", "special-mode")


def _target(path):
    if (type(path) is not str or not path.startswith("/")
            or len(os.fsencode(path)) > MAX_PATH_BYTES
            or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        raise _Denied("unsafe", "symlink-target")
    if os.path.normpath(path) != path:
        raise _Denied("unsafe", "symlink-target")
    if not any(path == root or path.startswith(root + "/") for root in _TRUSTED_ROOTS):
        raise _Denied("unsafe", "symlink-target")
    return path


def _link_target(parent, target):
    # Leading ../ refers to already inspected ancestors. Never lexically
    # collapse a/../b: a could itself be a symlink and change what .. means.
    # Such noncanonical layouts are refused rather than resolved incorrectly.
    if target.startswith("/"):
        return _target(target)
    parts = target.split("/")
    ancestors = parent.split("/")[1:]
    while parts and parts[0] in (".", ".."):
        head = parts.pop(0)
        if head == "..":
            if not ancestors:
                raise _Denied("unsafe", "symlink-target")
            ancestors.pop()
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise _Denied("unsafe", "symlink-target")
    return _target("/" + "/".join(ancestors + parts))


def _native_elf(header):
    """Bounded ELF header/layout check for this package's x86_64 target."""
    return (len(header) == 64 and header[:7] == b"\x7fELF\x02\x01\x01"
            and int.from_bytes(header[16:18], "little") in (2, 3)
            and int.from_bytes(header[18:20], "little") == 62
            and int.from_bytes(header[20:24], "little") == 1
            and int.from_bytes(header[52:54], "little") == 64)


def _capabilities(descriptor):
    try:
        os.getxattr(descriptor, "security.capability")
    except OSError as exc:
        if exc.errno in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
            return
        raise
    raise _Denied("unsafe", "file-capabilities")


def _walk(candidate, *, kind="native", capture=False):
    """Descriptor-relative inspection; symlinks are read and checked manually."""
    candidate = _target(candidate)
    descriptors = []
    nodes = []
    edges = []
    try:
        root = os.open("/", _DIR)
        descriptors.append(root)
        root_info = os.fstat(root)
        _trusted(root_info)
        nodes.append(_node("/", root_info))
        current, parent = root, "/"
        pending = candidate.split("/")[1:]
        links, components = 0, 0
        while pending:
            components += 1
            if components > MAX_COMPONENTS:
                raise _Denied("unsafe", "symlink-limit")
            name, *pending = pending
            path = os.path.join(parent, name)
            before = os.stat(name, dir_fd=current, follow_symlinks=False)
            _trusted(before)
            edges.append((current, name, _node(path, before)))
            if stat.S_ISLNK(before.st_mode):
                links += 1
                if links > MAX_LINKS:
                    raise _Denied("unsafe", "symlink-limit")
                target = os.readlink(name, dir_fd=current)
                if _node(path, before) != _node(path, os.stat(name, dir_fd=current, follow_symlinks=False)):
                    raise _Denied("changed", "metadata-changed")
                nodes.append(_node(path, before, target))
                destination = _link_target(parent, target)
                pending = destination.split("/")[1:] + pending
                if len(os.fsencode("/" + "/".join(pending))) > MAX_PATH_BYTES:
                    raise _Denied("unsafe", "symlink-target")
                current, parent = root, "/"
                continue
            if pending:
                if not stat.S_ISDIR(before.st_mode):
                    raise _Denied("unsupported", "not-regular")
                descriptor = os.open(name, _DIR, dir_fd=current)
            else:
                if kind == "directory":
                    if not stat.S_ISDIR(before.st_mode):
                        raise _Denied("unsupported", "not-directory")
                    descriptor = os.open(name, _DIR, dir_fd=current)
                elif not stat.S_ISREG(before.st_mode):
                    raise _Denied("unsupported", "not-regular")
                else:
                    if before.st_nlink != 1:
                        raise _Denied("unsafe", "hardlink")
                    if kind == "native" and not before.st_mode & 0o111:
                        raise _Denied("unsupported", "not-executable")
                    descriptor = os.open(name, _FILE, dir_fd=current)
            descriptors.append(descriptor)
            opened = os.fstat(descriptor)
            if _node(path, opened) != _node(path, before):
                raise _Denied("changed", "metadata-changed")
            nodes.append(_node(path, before))
            if pending:
                current, parent = descriptor, path
                continue
            if kind != "directory":
                _capabilities(descriptor)
            header = os.read(descriptor, 64) if kind == "native" else b""
            after = os.fstat(descriptor)
            linked = os.stat(name, dir_fd=current, follow_symlinks=False)
            if _node(path, before) != _node(path, after) or _node(path, before) != _node(path, linked):
                raise _Denied("changed", "metadata-changed")
            if kind == "native" and not _native_elf(header):
                raise _Denied("unsupported", "not-native-elf")
            if nodes[0] != _node("/", os.fstat(root)):
                raise _Denied("changed", "metadata-changed")
            # A directory descriptor can remain readable after its path was
            # replaced. Recheck every parent/name edge before accepting bytes
            # read through it, including edges traversed before symlink resets.
            for parent_fd, entry, expected in edges:
                try:
                    observed = os.stat(entry, dir_fd=parent_fd, follow_symlinks=False)
                except OSError:
                    raise _Denied("changed", "metadata-changed") from None
                if _node(expected.path, observed) != expected:
                    raise _Denied("changed", "metadata-changed")
            result = (path, _identity(after), tuple(nodes))
            if capture:
                os.lseek(descriptor, 0, os.SEEK_SET)
                return (*result, os.dup(descriptor))
            return result
        raise _Denied("unsupported", "not-regular")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _registration_manifest(path, provider):
    # Reuse the installer's closed schema, but never retain source metadata in
    # the runtime result or expose the manifest to the model namespace.
    from .client_registration import RegistrationError, validate_manifest
    resolved, identity, nodes, descriptor = _walk(path, kind="raw", capture=True)
    try:
        if identity.size > 16384:
            raise _Denied("unsafe", "registration-manifest")
        data = os.read(descriptor, 16385)
        if len(data) != identity.size or _identity(os.fstat(descriptor)) != identity:
            raise _Denied("changed", "metadata-changed")
        try:
            validate_manifest(data, provider)
        except RegistrationError:
            raise _Denied("unsafe", "registration-manifest") from None
        return resolved, identity, nodes
    finally:
        os.close(descriptor)


def inspect(provider):
    """Inspect the registered payload or system native, independent of PATH/HOME.

    Safe root-owned symlinks may resolve within /usr or /opt. Every traversed
    component is checked, and a second independent traversal detects replaced
    ancestors/links. File capabilities are refused. No version, vendor, complete
    ELF loadability, library closure, authentication or protocol claim is made.
    """
    provider = _provider(provider)
    candidate = _CANDIDATES[provider]
    source = "system"
    try:
        registered = False
        manifest_snapshot = None
        registration = _REGISTERED_ROOTS[provider]
        try:
            resolved, _, _ = _walk(registration, kind="directory")
        except FileNotFoundError:
            pass
        except (OSError, _Denied):
            source = "registered"
            raise
        else:
            source = "registered"
            # Presence of an explicit registration selects it exclusively.
            # A damaged or unsafe registration never falls back to another
            # client, including an otherwise valid system installation.
            if resolved != registration:
                raise _Denied("unsafe", "registration-layout")
            registered = True
            candidate = registration + "/payload/bin/" + provider
            manifest_path = registration + "/manifest.json"
            try:
                manifest_snapshot = _registration_manifest(manifest_path, provider)
            except FileNotFoundError:
                raise _Denied("unsupported", "registration-incomplete") from None
        chosen = candidate
        try:
            first = _walk(chosen)
        except (FileNotFoundError, _Denied) as exc:
            if registered:
                if isinstance(exc, FileNotFoundError):
                    raise _Denied("unsupported", "registration-incomplete") from None
                raise
            # Some system packages put a root-owned launcher at /usr/bin and
            # the native ELF at this compiled location. Never execute or parse
            # that launcher; an unsafe primary cannot trigger an alternate.
            if (provider not in _NATIVE_ALTERNATES or isinstance(exc, _Denied)
                    and (exc.status, exc.detail) != ("unsupported", "not-native-elf")):
                raise
            chosen = _NATIVE_ALTERNATES[provider]
            try:
                first = _walk(chosen)
            except FileNotFoundError:
                raise exc
        if _walk(chosen) != first:
            raise _Denied("changed", "metadata-changed")
        path, identity, nodes = first
        if manifest_snapshot is not None:
            if _walk(manifest_path, kind="raw") != manifest_snapshot:
                raise _Denied("changed", "metadata-changed")
            nodes = manifest_snapshot[2] + nodes
        return Inspection(provider, "present", path, "vendor-unverified", identity, nodes, source)
    except _Denied as exc:
        return Inspection(provider, exc.status, candidate, exc.detail, source=source)
    except FileNotFoundError:
        return Inspection(provider, "missing", candidate, "missing", source=source)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return Inspection(provider, "changed", candidate, "metadata-changed", source=source)
        if exc.errno == errno.ENOTDIR:
            return Inspection(provider, "unsupported", candidate, "not-regular", source=source)
        return Inspection(provider, "unavailable", candidate, "metadata-unavailable", source=source)


def compatibility(provider, output):
    """Classify already obtained native version text; never run the client.

    The exact tested baseline is supported by existing evidence. Older or
    malformed output is unsupported; any newer version remains explicitly
    untested, not silently admitted by this parser.
    """
    provider = _provider(provider)
    try:
        if type(output) not in (bytes, str) or len(output) > MAX_VERSION_BYTES:
            raise ValueError()
        if type(output) is bytes:
            text = output.decode("ascii")
        else:
            text = output
        text.encode("ascii")
        match = _VERSION_PATTERNS[provider].fullmatch(text)
        if match is None:
            raise ValueError()
    except (ValueError, UnicodeError):
        return Compatibility(provider, "unsupported", None, "invalid-version-output")
    version = tuple(int(part) for part in match.groups())
    displayed = ".".join(str(part) for part in version)
    baseline = _BASELINES[provider]
    if version == baseline:
        return Compatibility(provider, "tested", displayed, "tested-baseline")
    if version < baseline:
        return Compatibility(provider, "unsupported", displayed, "older-than-baseline")
    return Compatibility(provider, "untested", displayed, "newer-unverified")
