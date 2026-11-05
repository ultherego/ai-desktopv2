"""LC4 (first slice): a read-only project scope. No model, no network, no writes, no code execution.

A project is one explicit root directory that lies inside the owner's `files.read` policy.
The scan walks it with descriptor-relative, no-follow opens and builds an index of regular
files the policy allows. Everything else is skipped and only COUNTED (links, special files,
hard links, `.git`, denied entries): names of denied entries are never stored or shown.
Reads go through `permissions.read_bounded` (ownership, link, type, mode and race checks),
and every file a model may see is pinned by the SHA-256 of its bytes. Limits make the scan
end with `complete=False` and a reason instead of silently pretending to be total.
"""
from dataclasses import dataclass
import os
import secrets
import stat

from . import materials
from . import permissions

MAX_FILES = 20000
MAX_SCAN_ENTRIES = 100000
MAX_DEPTH = 16
MAX_DIR_ENTRIES = 5000
FILE_READ_BYTES = 16 * 1024 * 1024        # largest single project file that can be read
SEARCH_FILE_BYTES = FILE_READ_BYTES        # larger files are listed but skipped by text search
SEARCH_BUDGET_BYTES = 64 * 1024 * 1024    # bytes read per search call before returning a cursor
MAX_HITS = 20
PAGE = 100
MAX_PATH_BYTES = 512
SNIPPET = 200

CODES = frozenset({"PROJECT_ROOT", "PROJECT_PATH", "PROJECT_FILE", "PROJECT_CHANGED",
                   "PROJECT_NEEDLE", "PROJECT_POLICY", "TEST_SIZE", "TEST_CHANGED", "TEST_FORMAT"})


class ProjectError(ValueError):
    """Closed refusal code; never carries a path or file text."""

    def __init__(self, code):
        if code not in CODES:
            raise AssertionError("unknown code")
        super().__init__(code)
        self.code = code


class TestScopeRefused(ProjectError):
    """Local-only diagnostics derived from authorized metadata; never includes a filename."""

    def __init__(self, *details):
        super().__init__("TEST_SIZE")
        self.details = details


@dataclass(frozen=True)
class Entry:
    path: str          # relative to the root, "/" separated
    size: int
    ident: tuple       # stable format for existing saved project-session manifests
    identity: tuple    # full fd fingerprint, including mode and ctime, for a test copy


@dataclass(frozen=True)
class ProjectFile:
    entry: Entry
    material: object   # materials.Material (version = material.sha256)
    changed: bool      # True when size/identity differ from the scan


@dataclass(frozen=True)
class SearchHit:
    path: str
    line: int
    text: str
    whole: bool        # the text is the complete line (not cut at SNIPPET bytes)


@dataclass(frozen=True)
class SearchResult:
    hits: tuple
    next_cursor: object      # (file index, byte offset) or None when the scan reached the end
    complete: bool
    skipped: dict


def _valid_relative(path):
    if (type(path) is not str or not path or path.startswith("/") or path.endswith("/")
            or len(path.encode("utf-8", errors="surrogatepass")) > MAX_PATH_BYTES):
        return False
    return all(part not in ("", ".", "..") and "\x00" not in part for part in path.split("/"))


OVERVIEW_TOP = 40
OVERVIEW_EXTENSIONS = 15
OVERVIEW_KEY_FILES = 20
_KEY_NAMES = frozenset({"makefile", "dockerfile", "go.mod", "package.json", "pyproject.toml", "setup.py", "cargo.toml",
                        "pom.xml", "build.gradle", "composer.json", "gemfile", "requirements.txt", "docker-compose.yml",
                        "docker-compose.yaml", "project.md", "architecture.md", "changelog.md", "security.md"})


def _is_key_file(path):
    name = path.rsplit("/", 1)[-1].lower()
    return name in _KEY_NAMES or name.startswith(("readme", "license", "contributing")) and path.count("/") <= 2


class Project:
    def __init__(self, policy, root, *, scan_limit=None):
        if type(policy) is not permissions.Policy:
            raise ProjectError("PROJECT_POLICY")
        try:
            canonical = permissions.absolute_path(root)
        except ValueError:
            raise ProjectError("PROJECT_ROOT") from None
        if canonical == "/" or not policy.allows(canonical + "/_") or policy.excludes(canonical + "/_"):
            raise ProjectError("PROJECT_ROOT")
        self.policy, self.root = policy, canonical
        self.scan_limit = scan_limit
        self.project_id = secrets.token_hex(8)
        self.files = ()
        self._by_path = {}
        self.stats = {}
        self.complete = False
        self.reason = None
        self._scan_detail = None

    # --- scan -------------------------------------------------------------------
    def scan(self):
        """(Re)build the index. Never raises for hostile content, only for a changed policy/root."""
        policy, uid = self.policy, self.policy.uid
        found, counts = [], {"denied": 0, "links": 0, "special": 0, "unsafe": 0, "vcs": 0, "other": 0}
        state = {"scanned": 0, "reason": None, "detail": None, "stop": False}
        entry_limit = min(MAX_SCAN_ENTRIES, self.scan_limit or MAX_SCAN_ENTRIES)

        def mark(reason, detail, *, stop=False):
            if state["reason"] is None:
                state["reason"], state["detail"] = reason, detail
            state["stop"] = state["stop"] or stop

        def visit(absolute, relative, depth):
            if state["stop"]:
                return
            if depth > MAX_DEPTH:
                mark("depth", f"a directory exceeded the maximum depth of {MAX_DEPTH}")
                return
            try:
                with permissions.open_directory(absolute, uid) as (directory, recheck):
                    with os.scandir(directory) as entries:
                        names = []
                        for entry in entries:
                            names.append(entry.name)
                            state["scanned"] += 1
                            if state["scanned"] > entry_limit:
                                mark("entries", f"the scan exceeded its budget of {entry_limit} directory entries", stop=True)
                                return
                            if len(names) > MAX_DIR_ENTRIES:
                                mark("entries", f"a single directory exceeded {MAX_DIR_ENTRIES} entries")
                                return
                    for name in sorted(names):
                        if state["stop"]:
                            return
                        try:
                            target = permissions.absolute_path(absolute + "/" + name)
                        except ValueError:
                            counts["other"] += 1
                            continue
                        if name == ".git":
                            counts["vcs"] += 1
                            continue
                        if policy.excludes(target):
                            counts["denied"] += 1
                            continue
                        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                        child = name if not relative else relative + "/" + name
                        if stat.S_ISDIR(info.st_mode):
                            visit(target, child, depth + 1)
                        elif stat.S_ISLNK(info.st_mode):
                            counts["links"] += 1
                        elif stat.S_ISREG(info.st_mode):
                            if info.st_uid != uid or info.st_nlink != 1 or info.st_mode & 0o6000:
                                counts["unsafe"] += 1
                            elif not policy.allows(target) or not _valid_relative(child):
                                counts["denied"] += 1
                            elif len(found) >= MAX_FILES:
                                mark("files", f"the index exceeded its limit of {MAX_FILES} files", stop=True)
                                return
                            else:
                                found.append(Entry(child, info.st_size,
                                                   (info.st_dev, info.st_ino, info.st_mtime_ns), permissions._fingerprint(info)))
                        else:
                            counts["special"] += 1
                    recheck()
            except (ValueError, OSError):
                counts["other"] += 1
                mark("unreadable", "a directory could not be read or changed during the scan")

        policy.recheck()
        try:
            probe = permissions.absolute_path(self.root)
            info = os.lstat(probe)
        except (ValueError, OSError):
            raise ProjectError("PROJECT_ROOT") from None
        if not stat.S_ISDIR(info.st_mode):
            raise ProjectError("PROJECT_ROOT")
        visit(self.root, "", 0)
        policy.recheck()
        self.files = tuple(sorted(found, key=lambda e: e.path))
        self._by_path = {e.path: e for e in self.files}
        self.stats = dict(counts, indexed=len(self.files), scanned=state["scanned"])
        self.reason = state["reason"]
        self._scan_detail = state["detail"]
        self.complete = state["reason"] is None
        return self

    def manifest_sha256(self):
        """Digest of the scanned index (paths, sizes, identities): pins the project version a grant binds to."""
        import hashlib
        digest = hashlib.sha256()
        for entry in self.files:
            digest.update(("%s\0%d\0%d\0%d\0%d\n" % ((entry.path, entry.size) + entry.ident)).encode("utf-8", "surrogatepass"))
        return digest.hexdigest()

    def test_selection(self):
        """All policy-visible entries or a refusal. Never silently narrow the suite to a passing subset."""
        from . import task_contract
        limits = self.policy.limits()
        if not self.complete:
            raise TestScopeRefused(f"Incomplete index: {self._scan_detail or 'scan not completed'}. "
                                   f"{len(self.files)} permitted file(s) indexed so far; full totals are unknown.")
        if not self.files:
            raise TestScopeRefused("The complete index contains 0 permitted regular files.")
        totals = (("Indexed files", len(self.files), "read_files", task_contract.MAX_FILES),
                  ("Largest indexed file (bytes)", max(e.size for e in self.files), "read_file_size", task_contract.MAX_FILE),
                  ("Total indexed bytes", sum(e.size for e in self.files), "read_total_size", task_contract.MAX_TOTAL))
        exceeded = []
        for label, actual, key, ceiling in totals:
            configured = getattr(limits, key)
            if actual > min(configured, ceiling):
                exceeded.append(f"{label}: {actual} > {min(configured, ceiling)} "
                                f"(permissions.toml [files] {key} = {configured}; fixed copy ceiling = {ceiling}).")
        if exceeded:
            raise TestScopeRefused(*exceeded)
        for entry in self.files:
            try:
                task_contract.path(entry.path)
            except ValueError:
                raise ProjectError("TEST_FORMAT") from None
        return self.files

    def test_copy(self, approved):
        from . import task_contract
        if self.test_selection() != approved:
            raise ProjectError("TEST_CHANGED")
        files = {}
        for entry in approved:
            # Compare the *opened* file against the approved object, even if an ancestor is swapped and restored.
            _, raw = permissions.read_bounded(self.policy, self.root + "/" + entry.path,
                                              max(1, entry.size), expected_identity=entry.identity)
            if len(raw) != entry.size:
                raise ProjectError("TEST_CHANGED")
            try:
                text = task_contract.file_text(raw.decode("utf-8"))
                if permissions.contains_private_key(text):
                    raise ValueError("private key")
            except ValueError:
                raise ProjectError("TEST_FORMAT") from None
            files[entry.path] = text
        self.scan()
        if self.test_selection() != approved:
            raise ProjectError("TEST_CHANGED")
        return files

    # --- map --------------------------------------------------------------------
    def listing(self, prefix="", cursor=0, limit=PAGE):
        """Paged names under a directory prefix (all depths), sorted. `next` is None at the end."""
        if type(prefix) is not str or (prefix and not _valid_relative(prefix.rstrip("/"))):
            raise ProjectError("PROJECT_PATH")
        if type(cursor) is not int or isinstance(cursor, bool) or cursor < 0 or type(limit) is not int or not 1 <= limit <= PAGE:
            raise ProjectError("PROJECT_PATH")
        head = prefix.rstrip("/") + "/" if prefix else ""
        matching = [e for e in self.files if e.path.startswith(head)]
        page = matching[cursor:cursor + limit]
        following = cursor + limit
        return tuple(page), (following if following < len(matching) else None), len(matching)

    def children(self, prefix="", cursor=0, limit=PAGE):
        """One directory level: sub-directories (aggregated: file count and bytes) first, then files."""
        if type(prefix) is not str or (prefix and not _valid_relative(prefix.rstrip("/"))):
            raise ProjectError("PROJECT_PATH")
        if type(cursor) is not int or isinstance(cursor, bool) or cursor < 0 or type(limit) is not int or not 1 <= limit <= PAGE:
            raise ProjectError("PROJECT_PATH")
        head = prefix.rstrip("/") + "/" if prefix else ""
        grouped = {}
        for entry in self.files:
            if not entry.path.startswith(head):
                continue
            name, separator, _ = entry.path[len(head):].partition("/")
            kind, count, size = grouped.get(name, (bool(separator), 0, 0))
            grouped[name] = (kind, count + 1, size + entry.size)
        ordered = sorted(grouped.items(), key=lambda item: (not item[1][0], item[0]))
        page = [{"name": name, "dir": kind, "files": count, "size": size} for name, (kind, count, size) in ordered[cursor:cursor + limit]]
        following = cursor + limit
        return page, (following if following < len(ordered) else None), len(ordered)

    def overview(self):
        """A compact map of the whole index: top level, file types, and the files that usually explain a project."""
        top, _, _ = self.children("", 0, OVERVIEW_TOP)
        extensions = {}
        for entry in self.files:
            name = entry.path.rsplit("/", 1)[-1]
            suffix = os.path.splitext(name)[1].lower()[:16] or "(none)"
            extensions[suffix] = extensions.get(suffix, 0) + 1
        common = sorted(extensions.items(), key=lambda item: (-item[1], item[0]))[:OVERVIEW_EXTENSIONS]
        key = sorted((e.path for e in self.files if _is_key_file(e.path)), key=lambda p: (p.count("/"), p))[:OVERVIEW_KEY_FILES]
        return {"files": len(self.files), "size": sum(e.size for e in self.files), "top": top,
                "extensions": [{"ext": ext, "files": count} for ext, count in common], "key_files": key}

    def find_names(self, fragment, cursor=0, limit=PAGE):
        if type(fragment) is not str or not fragment or len(fragment.encode("utf-8", errors="ignore")) > materials.MAX_NEEDLE_BYTES:
            raise ProjectError("PROJECT_NEEDLE")
        matching = [e for e in self.files if fragment in e.path]
        page = matching[cursor:cursor + limit]
        following = cursor + limit
        return tuple(page), (following if following < len(matching) else None), len(matching)

    # --- reads ------------------------------------------------------------------
    def _recheck(self):
        try:
            self.policy.recheck()
        except ValueError:
            raise ProjectError("PROJECT_POLICY") from None

    def _load(self, entry, limit, *, recheck=True):
        absolute = self.root + "/" + entry.path
        try:
            _, raw = permissions.read_bounded(self.policy, absolute, limit, recheck=recheck)
        except ValueError as exc:
            code = str(exc)
            raise ProjectError("PROJECT_POLICY" if code.startswith("policy-") else "PROJECT_FILE") from None
        return raw

    def read_file(self, path):
        """The whole file as a pinned Material, with a flag telling whether it changed since the scan."""
        if not _valid_relative(path) or path not in self._by_path:
            raise ProjectError("PROJECT_PATH")
        entry = self._by_path[path]
        raw = self._load(entry, FILE_READ_BYTES)
        try:
            material = materials.import_bytes(raw, "file", limit=FILE_READ_BYTES)
        except materials.MaterialError:
            raise ProjectError("PROJECT_FILE") from None
        try:
            now = os.lstat(self.root + "/" + entry.path)
            moved = (now.st_dev, now.st_ino, now.st_mtime_ns) != entry.ident
        except OSError:
            moved = True
        return ProjectFile(entry, material, len(raw) != entry.size or moved)

    def search_text(self, needle, cursor=(0, 0)):
        """Literal search across indexed text files from `cursor`; resumable; skipped files are counted."""
        if (type(cursor) is not tuple or len(cursor) != 2 or any(type(v) is not int or v < 0 for v in cursor)
                or cursor[0] > len(self.files)):
            raise ProjectError("PROJECT_PATH")
        skipped = {"large": 0, "binary": 0, "unreadable": 0}
        hits, spent = [], 0
        self._recheck()                        # once per call; per-file reads below skip the repeated policy re-read
        index, offset = cursor
        while index < len(self.files) and len(hits) < MAX_HITS:
            entry = self.files[index]
            if entry.size == 0 or entry.size > SEARCH_FILE_BYTES:
                skipped["large" if entry.size else "binary"] += 1
                index, offset = index + 1, 0
                continue
            if spent + entry.size > SEARCH_BUDGET_BYTES and spent:
                return SearchResult(tuple(hits), (index, offset), False, skipped)
            try:
                raw = self._load(entry, SEARCH_FILE_BYTES, recheck=False)
            except ProjectError as exc:
                if exc.code == "PROJECT_POLICY":
                    raise
                skipped["unreadable"] += 1
                index, offset = index + 1, 0
                continue
            spent += len(raw)
            try:
                material = materials.import_bytes(raw, "file", limit=SEARCH_FILE_BYTES)
                page = materials.search_text(material, needle, cursor=offset, max_hits=MAX_HITS - len(hits))
            except materials.MaterialError as exc:
                if exc.code == "NEEDLE_INVALID":
                    raise ProjectError("PROJECT_NEEDLE") from None
                skipped["binary"] += 1
                index, offset = index + 1, 0
                continue
            for hit in page.hits:
                lo = material.line_starts[hit.line - 1]
                hi = material.line_starts[hit.line] if hit.line < len(material.line_starts) else material.size
                text = material.raw[lo:min(hi, lo + SNIPPET * 4)].decode("utf-8", "ignore").rstrip("\r\n")
                cut = text.encode("utf-8")[:SNIPPET].decode("utf-8", "ignore")
                hits.append(SearchHit(entry.path, hit.line, cut, cut == text and hi - lo <= SNIPPET * 4))
            if page.complete:
                index, offset = index + 1, 0
            else:
                offset = page.next_cursor
        self._recheck()
        done = index >= len(self.files)
        return SearchResult(tuple(hits), None if done else (index, offset), done, skipped)

    def unchanged(self, path, version):
        """Re-read the file and compare with a pinned version (SHA-256); False when it differs or vanished."""
        try:
            return self.read_file(path).material.sha256 == version
        except ProjectError:
            return False
