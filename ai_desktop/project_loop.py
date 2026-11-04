"""Controller side of the model-driven read loop over ONE read-only project (LC4).

The model proposes one action per request (list, find, search, read, answer, ask_user); this
class executes it LOCALLY through `project_scope` and records an observation. The model never
reads anything itself, never names a path outside the scan's index, and its answer cannot
grant itself more: reads are bounded, claimed sources are checked against what was really
handed over, a repeated action ends the task, an over-large read is refused, and a file that
changed under the conversation is reported and re-pinned (never silently mixed).
"""
from . import material_step as base
from . import materials
from . import project_scope as scope
from . import project_step as step
from .material_loop import ACTION_TEXT, REFUSAL_TEXT, count_lines, lines_label, _cut, history_turn, visible_lines     # noqa: F401  (history_turn re-exported)

SEARCH_RESERVE = 4096


class Loop:
    def __init__(self, project, max_steps=step.MAX_STEPS, history=(), progress="", pasted=(), visited=(), stage=1):
        self.project = project
        self.progress = progress
        self.stage = stage
        self.visited = list(visited)       # files read in earlier stages of the same deep task (oldest first)
        self.history = [dict(turn) for turn in history][-step.MAX_HISTORY:]
        self.max_steps = max_steps
        self.observations = []
        self.executed = set()
        self.delivered = {}          # path -> [(first, last, start, end)] really handed over
        self.delivered_versions = {} # path -> hash of the version actually handed over
        self.hit_lines = {}          # path -> {line} whose WHOLE text arrived inside a search result
        self._whole_hits = set()
        self.versions = {}           # path -> sha256 pinned at first read
        self.file_lines = {}         # path -> number of lines of the version we read
        self.changed = []            # paths that differed from the scan or from an earlier read
        self.cursors = {}            # needle -> cursor of an unfinished search
        self.searches = []           # (needle, complete)
        self.read_bytes = 0
        self._overview()                  # the first request already carries the map of the whole project
        self.executed.add(("overview",))
        self.format_note = False          # set when Claude answered in prose instead of the JSON action
        for number, text in pasted:       # what the user pasted: their own material, never dropped by compaction
            self.observations.append({"kind": "pasted", "id": number, "lines": text.count("\n") + 1, "text": text})

    # --- request ---------------------------------------------------------------
    def request_input(self, step_number, bytes_left):
        self._compact()
        stats = self.project.stats
        return {"project": {"id": self.project.project_id, "files": stats["indexed"],
                            "complete": self.project.complete, "progress": self.progress, "stage": self.stage,
                            "visited": self.visited[-step.MAX_VISITED:],
                            "skipped": {key: stats[key] for key in step.SKIPPED_KEYS}},
                "task": {"step": step_number, "max_steps": self.max_steps, "bytes_left": bytes_left},
                "observations": [dict(o) for o in self.observations],
                "history": [dict(t) for t in self.history]}

    def data_bytes(self):
        return (step.data_bytes(self.observations, self.history) + len(self.progress.encode("utf-8"))
                + sum(len(p.encode("utf-8")) for p in self.visited[-step.MAX_VISITED:]))

    def _compact(self):
        """Keep the next packet inside its budget by dropping the OLDEST texts, visibly."""
        while len(self.observations) > step.MAX_OBSERVATIONS - 4 or self.data_bytes() > step.DATA_BYTES - SEARCH_RESERVE:
            if len(self.observations) > step.MAX_OBSERVATIONS - 4:
                # Same rule as material: a surplus entry removes an old "dropped" note, not every useful read.
                index = next((i for i, item in enumerate(self.observations) if item["kind"] == "dropped"), None)
                if index is not None:
                    del self.observations[index]
                    continue
            if not self._drop_one():
                break

    def _drop_one(self):
        for index, item in enumerate(self.observations):
            if item["kind"] == "read":
                self.observations[index] = {"kind": "dropped", "path": item["path"],
                                            "first": item["first"], "last": item["last"]}
                return True
        for index, item in enumerate(self.observations):
            if item["kind"] != "pasted":
                del self.observations[index]
                return True
        return False

    def _compact_for(self, needed):
        limit = step.DATA_BYTES - SEARCH_RESERVE
        while self.data_bytes() + needed > limit:
            if not self._drop_one():
                return

    # --- local operations ------------------------------------------------------
    def _key(self, decision):
        action = decision["action"]
        if action == "overview":
            return ("overview",)
        if action == "list":
            return ("list", decision["prefix"].rstrip("/"), decision["cursor"])
        if action == "find":
            return ("find", decision["fragment"], decision["cursor"])
        if action == "search":
            # A continuation is a different request for every page: the key carries the stored cursor.
            return ("search", decision["needle"], self.cursors.get(decision["needle"]) if decision["continue"] else None)
        return ("read", decision["path"], tuple(sorted(decision["ranges"])))

    def repeated(self, decision):
        return self._key(decision) in self.executed

    def execute(self, decision):
        """Run one proposal locally; returns a one-line announcement for the user."""
        action = decision["action"]
        key = self._key(decision)
        self.executed.add(key)
        if action == "overview":
            return self._overview()
        if action == "list":
            return self._tree(decision["prefix"], decision["cursor"])
        if action == "find":
            return self._names("", decision["fragment"], decision["cursor"])
        if action == "search":
            return self._search(decision["needle"], decision["continue"])
        note = self._read(decision["path"], decision["ranges"])
        if self.observations and self.observations[-1] == {"kind": "refused", "action": "read", "code": "CHANGED"}:
            self.executed.discard(key)        # the invited retry reads the re-pinned version; it is not a repeat
        return note

    def _refuse(self, action, code, what=None, detail=""):
        """Record the closed refusal for the model; the returned line explains it to the user."""
        self.observations.append({"kind": "refused", "action": action, "code": code})
        return "%s — odmowa: %s%s" % (what or ACTION_TEXT[action], REFUSAL_TEXT[code], " (%s)" % detail if detail else "")

    def _overview(self):
        view = self.project.overview()
        self.observations.append(dict(kind="overview", **view))
        return "przegląd projektu: %d plików, %d pozycji na najwyższym poziomie" % (view["files"], len(view["top"]))

    def _tree(self, prefix, cursor):
        try:
            items, following, total = self.project.children(prefix, cursor)
        except scope.ProjectError:
            return self._refuse("list", "PATH")
        self.observations.append({"kind": "tree", "prefix": prefix.rstrip("/"), "items": items, "total": total, "next": following})
        where = " %s" % prefix.rstrip("/") if prefix else " projektu"
        return "zawartość katalogu%s: %d z %d pozycji%s" % (where, len(items), total, " (są kolejne)" if following else "")

    def _names(self, prefix, query, cursor):
        action = "find" if query else "list"
        try:
            if query:
                items, following, total = self.project.find_names(query, cursor)
            else:
                items, following, total = self.project.listing(prefix, cursor)
        except scope.ProjectError as exc:
            return self._refuse(action, "NEEDLE_INVALID" if exc.code == "PROJECT_NEEDLE" else "PATH")
        self.observations.append({"kind": "names", "prefix": prefix, "query": query,
                                  "items": [{"path": e.path, "size": e.size} for e in items],
                                  "total": total, "next": following})
        what = "wyszukanie nazw „%s”" % query if query else "listę plików%s" % (" w %s" % prefix if prefix else "")
        return "%s: %d z %d%s" % (what, len(items), total, " (są kolejne)" if following else "")

    def _search(self, needle, resume):
        cursor = (0, 0)
        if resume:
            cursor = self.cursors.get(needle)
            if cursor is None:
                return self._refuse("search", "LIMIT")
        try:
            result = self.project.search_text(needle, cursor)
        except scope.ProjectError as exc:
            if exc.code == "PROJECT_POLICY":
                raise
            return self._refuse("search", "NEEDLE_INVALID" if exc.code == "PROJECT_NEEDLE" else "PATH")
        more = result.next_cursor is not None
        if more:
            self.cursors[needle] = result.next_cursor
        else:
            self.cursors.pop(needle, None)
        hits = []
        for hit in result.hits:
            hits.append({"path": hit.path, "line": hit.line, "text": hit.text})
            if hit.whole:
                self._whole_hits.add((hit.path, hit.line, hit.text))
        self.observations.append({"kind": "search", "needle": needle, "complete": result.complete, "more": more,
                                  "hits": hits, "skipped": dict(result.skipped)})
        self.searches.append((needle, result.complete))
        return "wyszukano „%s” w projekcie: %d trafień%s" % (needle, len(hits), " (są kolejne)" if more else "")

    def _read(self, path, spans):
        try:
            loaded = self.project.read_file(path)
        except scope.ProjectError as exc:
            if exc.code == "PROJECT_POLICY":
                raise
            return self._refuse("read", "PATH" if exc.code == "PROJECT_PATH" else "FILE")
        material = loaded.material
        if loaded.changed and path not in self.changed:
            self.changed.append(path)
        pinned = self.versions.get(path)
        if pinned is not None and pinned != material.sha256:
            # The file changed since an earlier read in this task: forget what we held about it, tell the model.
            self.versions[path] = material.sha256
            self.delivered.pop(path, None)
            self.hit_lines.pop(path, None)
            for index, item in enumerate(self.observations):
                if item["kind"] == "read" and item["path"] == path:
                    self.observations[index] = {"kind": "dropped", "path": path,
                                                "first": item["first"], "last": item["last"]}
            if path not in self.changed:
                self.changed.append(path)
            return self._refuse("read", "CHANGED")
        self.versions[path] = material.sha256
        lines = visible_lines(material)
        self.file_lines[path] = lines
        planned = []
        what = "odczyt %s, %s" % (path, lines_label(spans))
        if any(first > lines for first, _ in spans):
            return self._refuse("read", "RANGE_INVALID", what, "plik ma %s" % count_lines(lines))
        cut = any(last > lines for _, last in spans)
        spans = sorted({(first, min(last, lines)) for first, last in spans})       # an end past the last line is cut to it
        for first, last in spans:
            try:
                start, end = materials.line_range(material, first, last)
                planned.extend(materials.split_range(material, start, end))
            except materials.MaterialError:
                return self._refuse("read", "RANGE_INVALID", what, "plik ma %s" % count_lines(lines))
        total = sum(b - a for a, b in planned)
        if total > max(0, step.DATA_BYTES - SEARCH_RESERVE - self.data_bytes()):
            self._compact_for(total)
            if total > max(0, step.DATA_BYTES - SEARCH_RESERVE - self.data_bytes()):
                return self._refuse("read", "TOO_LARGE", what)
        for start, end in planned:
            fragment = materials.read_range(material, material.sha256, start, end)
            first = material.line_of(start)[0]
            last = material.line_of(end - 1)[0]
            self.observations.append({"kind": "read", "path": path, "first": first, "last": last, "lines": lines,
                                      "ref": fragment.ref.to_json(), "text": fragment.text})
            self.read_bytes += end - start
        return "odczyt %s, %s (%d B)%s" % (path, lines_label(spans), total, " — plik ma %s" % count_lines(lines) if cut else "")

    # --- evidence ---------------------------------------------------------------
    def mark_handed_off(self, value):
        """Update coverage at the transport boundary, never at local preparation."""
        for item in value["observations"]:
            if item["kind"] == "read":
                path, version = item["path"], item["ref"]["version"]
                if self.delivered_versions.get(path) != version:
                    self.delivered[path] = []
                    self.hit_lines.pop(path, None)
                self.delivered_versions[path] = version
                evidence = (item["first"], item["last"], item["ref"]["start"], item["ref"]["end"])
                if evidence not in self.delivered[path]:
                    self.delivered[path].append(evidence)
                if path not in self.visited:
                    self.visited.append(path)
            elif item["kind"] == "search":
                for hit in item["hits"]:
                    if (hit["path"], hit["line"], hit["text"]) in self._whole_hits:
                        self.hit_lines.setdefault(hit["path"], set()).add(hit["line"])

    def confirm_sources(self, claimed):
        """Split claimed (path, first, last) into those covered by really delivered text and the rest."""
        confirmed, rejected = [], []
        for path, first, last in claimed:
            have = set(self.hit_lines.get(path, ()))
            for a, b, _, _ in self.delivered.get(path, ()):
                have.update(range(a, b + 1))
            (confirmed if all(n in have for n in range(first, last + 1)) else rejected).append((path, first, last))
        return confirmed, rejected

    def fully_read(self, path):
        """True when Claude has already received this whole file (in this task), unchanged since."""
        lines = self.file_lines.get(path)
        spans = self.delivered_summary()[0].get(path)
        return bool(lines and spans == [[1, lines]]
                    and self.delivered_versions.get(path) == self.versions.get(path))

    def delivered_summary(self):
        """({path: [[first, last], ...]}, bytes) of the text really handed to the model."""
        result, size = {}, 0
        for path in sorted(set(self.delivered) | set(self.hit_lines)):
            spans = sorted({(f, l) for f, l, _, _ in self.delivered.get(path, ())}
                           | {(n, n) for n in self.hit_lines.get(path, ())})
            merged = []
            for first, last in spans:
                if merged and first <= merged[-1][1] + 1:
                    merged[-1][1] = max(merged[-1][1], last)
                else:
                    merged.append([first, last])
            result[path] = merged
            size += sum(b - a for a, b in {(s, e) for _, _, s, e in self.delivered.get(path, ())})
        return result, size
