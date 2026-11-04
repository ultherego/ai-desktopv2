"""Bounded, hash-confirmed removal of recognized user installations.

No recursive delete, arbitrary receipt paths or automatic modified-file cleanup.
Same-UID hostile rename races are outside this prototype's guarantees.
"""
import hashlib
import hmac
import json
import os
import re
import stat

from .install_policy import InstallDenied, _prefix, _wrapper

MANIFEST = "install-manifest.json"
INSTALL_JOURNAL = "install-journal.json"
REMOVE_JOURNAL = "remove-journal.json"
METADATA = frozenset({MANIFEST, INSTALL_JOURNAL, REMOVE_JOURNAL})
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
MAX_FILE = 128 * 1024
MAX_RECEIPT = 64 * 1024
MAX_FILES = 256


def _deny():
    raise InstallDenied("INSTALL: REFUSED")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _hex(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _source_path(value):
    return type(value) is str and re.fullmatch(
        r"(?:ai_desktop/[A-Za-z_][A-Za-z0-9_]*\.py|skills/[a-z0-9][a-z0-9-]*/SKILL\.md)", value) is not None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _deny()
        result[key] = value
    return result


def receipt(prefix, root_info, files, source_sha256):
    """Durable intent, published before creating any payload directories."""
    entries = [{"path": "lib/" + path, "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data), "mode": 0o600} for path, data in files]
    wrapper = _wrapper(prefix)
    entries.append({"path": "bin/ai-desktop", "sha256": hashlib.sha256(wrapper).hexdigest(),
                    "size": len(wrapper), "mode": 0o700})
    return {"schema": 2, "prefix": prefix, "root": [root_info.st_dev, root_info.st_ino],
            "sha256": source_sha256, "files": entries}


def _entries(data, prefix, root_info):
    if type(data) is not dict or type(data.get("schema")) is not int or not _hex(data.get("sha256")):
        _deny()
    schema = data["schema"]
    keys = {"schema", "files", "sha256"}
    if schema == 2:
        keys |= {"prefix", "root"}
        identity = data.get("root")
        if (data.get("prefix") != prefix or type(identity) is not list or len(identity) != 2 or
                any(type(x) is not int or x < 0 for x in identity) or
                identity != [root_info.st_dev, root_info.st_ino]):
            _deny()
    elif schema != 1:
        _deny()
    if set(data) != keys or type(data["files"]) is not list or not 1 <= len(data["files"]) <= MAX_FILES:
        _deny()
    entries, source = {}, []
    for entry in data["files"]:
        if type(entry) is not dict or set(entry) != ({"path", "sha256"} if schema == 1 else {"path", "sha256", "size", "mode"}):
            _deny()
        path = entry.get("path")
        if not _hex(entry.get("sha256")) or type(path) is not str:
            _deny()
        if schema == 1:
            if not _source_path(path):
                _deny()
            source.append(entry)
            path = "lib/" + path
            expected = {"sha256": entry["sha256"], "mode": 0o600, "size": None}
        else:
            if path == "bin/ai-desktop":
                wrappers = (_wrapper(prefix), _wrapper(prefix, isolated=False))
                if not any(entry == {"path": path, "sha256": hashlib.sha256(wrapper).hexdigest(),
                                     "size": len(wrapper), "mode": 0o700} for wrapper in wrappers):
                    _deny()
            elif path.startswith("lib/") and _source_path(path[4:]):
                source.append({"path": path[4:], "sha256": entry["sha256"]})
            else:
                _deny()
            if (type(entry["size"]) is not int or not 0 < entry["size"] <= MAX_FILE or
                    type(entry["mode"]) is not int or entry["mode"] != (0o700 if path == "bin/ai-desktop" else 0o600)):
                _deny()
            expected = {k: entry[k] for k in ("sha256", "size", "mode")}
        if path in entries:
            _deny()
        entries[path] = expected
    if (source != sorted(source, key=lambda e: e["path"]) or not source or
            not hmac.compare_digest(_digest(source), data["sha256"])):
        _deny()
    if schema == 1:
        wrapper = _wrapper(prefix, legacy=True)
        entries["bin/ai-desktop"] = {"sha256": hashlib.sha256(wrapper).hexdigest(),
                                    "mode": 0o700, "size": len(wrapper)}
    if "bin/ai-desktop" not in entries or "lib/ai_desktop/__init__.py" not in entries:
        _deny()
    return entries


def _fact(info):
    return {"dev": info.st_dev, "ino": info.st_ino, "uid": info.st_uid,
            "mode": stat.S_IMODE(info.st_mode), "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
            "nlink": info.st_nlink}


def _directory(fd):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        _deny()
    return _fact(info)


def _read(parent, name, *, metadata=False, links=1):
    fd = os.open(name, FILE_FLAGS, dir_fd=parent)
    try:
        before = os.fstat(fd)
        limit = MAX_RECEIPT if metadata else MAX_FILE
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or
                before.st_nlink != links or not 0 < before.st_size <= limit):
            _deny()
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, min(8192, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != before.st_size or _fact(before) != _fact(os.fstat(fd)):
            _deny()
        return {**_fact(before), "sha256": hashlib.sha256(data).hexdigest()}, bytes(data)
    finally:
        os.close(fd)


def _parent(root, relative):
    parts = relative.split("/")
    fd = os.dup(root)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
            _directory(fd)
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def _inspect(root, prefix, action):
    if action not in ("recover", "uninstall"):
        _deny()
    root_fact = _directory(root)
    metadata = set(os.listdir(root)) & METADATA
    if INSTALL_JOURNAL in metadata:
        if action != "recover" or REMOVE_JOURNAL in metadata:
            _deny()
        first = INSTALL_JOURNAL
    elif REMOVE_JOURNAL in metadata:
        if action != "uninstall":
            _deny()
        first = REMOVE_JOURNAL
    elif metadata == {MANIFEST} and action == "uninstall":
        first = MANIFEST
    else:
        _deny()
    meta_fact, raw = _read(root, first, metadata=True, links=len(metadata))
    if meta_fact["mode"] != 0o600:
        _deny()
    data = json.loads(raw, object_pairs_hook=_unique)
    entries = _entries(data, prefix, os.fstat(root))
    if INSTALL_JOURNAL in metadata and data["schema"] != 2:
        _deny()
    directories = set()
    for path in entries:
        parts = path.split("/")[:-1]
        while parts:
            directories.add("/".join(parts))
            parts.pop()
    files, dirs = [], []

    def walk(fd, relative=""):
        names = os.listdir(fd)
        if len(names) > MAX_FILES + 16:
            _deny()
        for name in sorted(names):
            path = relative + "/" + name if relative else name
            if path in directories:
                child = os.open(name, DIR_FLAGS, dir_fd=fd)
                try:
                    dirs.append({"path": path, **_directory(child)})
                    walk(child, path)
                finally:
                    os.close(child)
            elif path in entries or (not relative and name in metadata):
                is_meta = path in metadata
                fact, _ = _read(fd, name, metadata=is_meta, links=len(metadata) if is_meta else 1)
                if is_meta:
                    if fact != meta_fact:
                        _deny()  # Only the two known local hardlinks are allowed.
                else:
                    expected = entries[path]
                    if (fact["mode"] != expected["mode"] or fact["sha256"] != expected["sha256"] or
                            (expected["size"] is not None and fact["size"] != expected["size"])):
                        _deny()
                files.append({"path": path, **fact})
            else:
                _deny()  # Unknown file/directory: never recursively remove it.

    walk(root)
    if metadata == {MANIFEST} and ({f["path"] for f in files} - METADATA != set(entries) or
                                   {d["path"] for d in dirs} != directories):
        _deny()  # Missing payload is not proof of a previously approved uninstall.
    plan = {"schema": 1, "action": action, "prefix": prefix, "root": root_fact,
            "files": sorted(files, key=lambda e: e["path"]),
            "directories": sorted(dirs, key=lambda e: e["path"])}
    return {**plan, "sha256": _digest(plan)}


def preview_removal(prefix, action):
    root = None
    try:
        root = _prefix(prefix)
        return _inspect(root, prefix, action)
    except (OSError, ValueError, TypeError, RecursionError):
        _deny()
    finally:
        if root is not None:
            os.close(root)


def remove(prefix, confirmed_sha256, action):
    root = None
    try:
        if not _hex(confirmed_sha256):
            _deny()
        root = _prefix(prefix)
        plan = _inspect(root, prefix, action)
        if not hmac.compare_digest(plan["sha256"], confirmed_sha256):
            _deny()
        metadata = {e["path"]: e for e in plan["files"] if e["path"] in METADATA}
        if action == "uninstall" and REMOVE_JOURNAL not in metadata:
            # O_EXCL publication: no replacement of an unexpected file.
            os.link(MANIFEST, REMOVE_JOURNAL, src_dir_fd=root, dst_dir_fd=root, follow_symlinks=False)
            os.fsync(root)
            metadata[REMOVE_JOURNAL] = metadata[MANIFEST]
        for entry in plan["files"]:
            if entry["path"] in METADATA:
                continue
            parent, name = _parent(root, entry["path"])
            try:
                current, _ = _read(parent, name)
                if current != {k: v for k, v in entry.items() if k != "path"}:
                    _deny()
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (named.st_dev, named.st_ino) != (entry["dev"], entry["ino"]):
                    _deny()
                os.unlink(name, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(parent)
        for entry in sorted(plan["directories"], key=lambda e: (-e["path"].count("/"), e["path"]), reverse=False):
            parent, name = _parent(root, entry["path"])
            try:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (not stat.S_ISDIR(current.st_mode) or
                        (current.st_dev, current.st_ino) != (entry["dev"], entry["ino"])):
                    _deny()
                os.rmdir(name, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(parent)
        if set(os.listdir(root)) != set(metadata):
            _deny()
        # Payload and directories are gone; keep the durable journal until last.
        marker = INSTALL_JOURNAL if action == "recover" else REMOVE_JOURNAL
        order = sorted(set(metadata) - {marker}) + [marker]
        for name in order:
            current, _ = _read(root, name, metadata=True, links=len(metadata))
            expected = metadata[name]
            if any(current[k] != expected[k] for k in ("dev", "ino", "uid", "mode", "size", "sha256")):
                _deny()
            os.unlink(name, dir_fd=root)
            metadata.pop(name)
            os.fsync(root)
        return {"status": "RECOVERED" if action == "recover" else "UNINSTALLED",
                "removed_files": len(plan["files"]), "prefix_retained": True}
    except (OSError, ValueError, TypeError, RecursionError):
        _deny()
    finally:
        if root is not None:
            os.close(root)
