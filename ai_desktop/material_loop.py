"""Controller side of the model-driven read loop over ONE material (pure, no I/O).

The model proposes one action per request; this class executes it LOCALLY and
records an observation. The model never reads anything itself and its answer
cannot grant itself more: every read is bounded, every claimed source is checked
against what was really handed over, a repeated action ends the task, and an
over-large request is refused (not silently shortened).
"""
from . import material_step as step
from . import materials

SEARCH_SNIPPET = 200
REFUSAL_TEXT = {"NEEDLE_INVALID": "niepoprawna fraza", "RANGE_INVALID": "zakres wierszy poza plikiem",
                "TOO_LARGE": "zakres za duży na jedno żądanie", "PATH": "plik spoza indeksu projektu",
                "CHANGED": "plik zmienił się od poprzedniego odczytu", "LIMIT": "nie ma czego kontynuować",
                "FILE": "pliku nie da się odczytać"}


def count_lines(n):
    """Polish plural of "N wiersz": 1 wiersz, 2-4 wiersze, 5+ wierszy (12-14 wierszy)."""
    if n == 1:
        return "1 wiersz"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "%d wiersze" % n
    return "%d wierszy" % n


def span_text(spans):
    return ", ".join("%d–%d" % tuple(s) if s[0] != s[1] else str(s[0]) for s in spans)


def lines_label(spans):
    """"wiersz 5" for one single line, otherwise "wiersze 1–3, 7"."""
    spans = list(spans)
    single = len(spans) == 1 and spans[0][0] == spans[0][1]
    return ("wiersz " if single else "wiersze ") + span_text(spans)


ACTION_TEXT = {"search": "wyszukanie", "read": "odczyt", "list": "listę plików", "find": "wyszukanie nazw"}
WHOLE_BYTES = 200 * 1024         # smaller materials are handed over whole in the first request


def visible_lines(material):
    count = len(material.line_starts)
    return count - 1 if material.raw.endswith(b"\n") else count


def _cut(text, limit):
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore")


def history_turn(question, answer):
    """A finished turn for later requests: the answer is shortened visibly, never silently."""
    text = answer if len(answer.encode("utf-8")) <= step.HISTORY_TEXT else _cut(answer, step.HISTORY_TEXT) + " …[skrócono]"
    return {"question": question, "answer": text}


def read_observation(material, start, end):
    """Observation for one verified fragment of the material (also used by the whole-file mode)."""
    fragment = materials.read_range(material, material.sha256, start, end)
    first = material.line_of(start)[0]
    last = material.line_of(end - 1)[0]
    return {"kind": "read", "first": first, "last": last, "ref": fragment.ref.to_json(), "text": fragment.text}


class Loop:
    def __init__(self, material, max_steps=step.MAX_STEPS, history=()):
        self.history = [dict(turn) for turn in history][-step.MAX_HISTORY:]
        self.material = material
        self.lines = visible_lines(material)
        self.max_steps = max_steps
        self.observations = []
        self.executed = set()
        self.delivered = []          # (first, last) line spans really handed over, with duplicates
        self.hit_lines = set()       # lines whose WHOLE text arrived inside a search result
        self._whole_hits = set()     # locally prepared search evidence, not delivery
        self.read_bytes = 0
        self.format_note = False       # set when Claude answered in prose instead of the JSON action
        self.searches = []

    # --- request ---------------------------------------------------------------
    def request_input(self, step_number, bytes_left):
        self._compact()
        return {"material": {"id": self.material.material_id, "version": self.material.sha256,
                             "size": self.material.size, "lines": self.lines},
                "task": {"step": step_number, "max_steps": self.max_steps, "bytes_left": bytes_left},
                "observations": [dict(o) for o in self.observations],
                "history": [dict(t) for t in self.history]}

    def request_ranges(self):
        """Byte ranges of the fragments carried by the next request (for the ledger)."""
        return [(o["ref"]["start"], o["ref"]["end"]) for o in self.observations if o["kind"] == "read"]

    def data_bytes(self):
        return step.data_bytes(self.observations, self.history)

    def preload_range(self, first, last):
        """User-narrowed scope: hand over exactly these lines; False if they do not fit one request."""
        before = len(self.observations)
        self._read([(first, last)], clamp=False)       # a range typed by the user is never silently shortened
        return len(self.observations) > before and self.observations[-1]["kind"] == "read"

    def preload_whole(self):
        """Small material: hand the whole text over in the first request (nothing to search for)."""
        if self.material.size > WHOLE_BYTES:
            return False
        self._read([(1, self.lines)])
        return True

    def _compact(self):
        """Keep the next packet inside its budget by dropping the OLDEST read texts, visibly."""
        while len(self.observations) > step.MAX_OBSERVATIONS - 4 or self.data_bytes() > step.DATA_BYTES - 4096:
            if len(self.observations) > step.MAX_OBSERVATIONS - 4:
                # Too many entries: replacing a read does not shorten the list, so remove the oldest note of a dropped
                # read first; otherwise one surplus entry would wipe every read that is still useful.
                index = next((i for i, item in enumerate(self.observations) if item["kind"] == "dropped"), 0)
                del self.observations[index]
                continue
            for index, item in enumerate(self.observations):
                if item["kind"] == "read":
                    self.observations[index] = {"kind": "dropped", "first": item["first"], "last": item["last"]}
                    break
            else:
                del self.observations[0]

    # --- local operations ------------------------------------------------------
    def repeated(self, decision):
        key = self._key(decision)
        return key in self.executed

    @staticmethod
    def _key(decision):
        if decision["action"] == "search":
            return ("search", decision["needle"])
        return ("read", tuple(sorted(decision["ranges"])))

    def execute(self, decision):
        """Run a search/read proposal; returns a one-line announcement for the user."""
        action = decision["action"]
        if action == "search":
            return self._search(decision["needle"])
        return self._read(decision["ranges"])

    def _refuse(self, action, code, what=None, detail=""):
        """Record the closed refusal for the model; the returned line explains it to the user."""
        self.observations.append({"kind": "refused", "action": action, "code": code})
        return "%s — odmowa: %s%s" % (what or ACTION_TEXT[action], REFUSAL_TEXT[code], " (%s)" % detail if detail else "")

    def _search(self, needle):
        try:
            page = materials.search_text(self.material, needle)
        except materials.MaterialError:
            return self._refuse("search", "NEEDLE_INVALID")
        self.executed.add(("search", needle))
        hits = []
        for hit in page.hits[:step.MAX_HITS]:
            number = hit.line
            lo = self.material.line_starts[number - 1]
            hi = self.material.line_starts[number] if number < len(self.material.line_starts) else self.material.size
            text = self.material.raw[lo:min(hi, lo + SEARCH_SNIPPET * 4)].decode("utf-8", "ignore").rstrip("\r\n")
            hits.append({"line": number, "text": _cut(text, SEARCH_SNIPPET)})
            if hi - lo <= SEARCH_SNIPPET * 4 and len(text.encode("utf-8")) <= SEARCH_SNIPPET and hits[-1]["text"] == text:
                self._whole_hits.add((number, hits[-1]["text"]))
        self.observations.append({"kind": "search", "needle": needle, "complete": page.complete, "hits": hits})
        self.searches.append(needle)
        return "wyszukano „%s”: %d trafień%s" % (needle, len(hits), "" if page.complete else " (są kolejne)")

    def _read(self, spans, clamp=True):
        budget = step.DATA_BYTES - 4096 - self.data_bytes()
        planned = []
        what = ("odczyt wiersza " if len(spans) == 1 and spans[0][0] == spans[0][1] else "odczyt wierszy ") + span_text(spans)
        if any(first > self.lines or (not clamp and last > self.lines) for first, last in spans):
            return self._refuse("read", "RANGE_INVALID", what, "materiał ma %s" % count_lines(self.lines))
        spans = sorted({(first, min(last, self.lines)) for first, last in spans})   # an end past the last line is cut to it
        for first, last in spans:
            try:
                start, end = materials.line_range(self.material, first, last)
                parts = materials.split_range(self.material, start, end)
            except materials.MaterialError:
                return self._refuse("read", "RANGE_INVALID", what, "materiał ma %s" % count_lines(self.lines))
            planned.extend(parts)
        total = sum(b - a for a, b in planned)
        if total > max(0, budget):
            # A read the next packet cannot carry is refused whole; older texts are dropped first on retry.
            self._compact_for(total)
            if total > max(0, step.DATA_BYTES - 4096 - self.data_bytes()):
                return self._refuse("read", "TOO_LARGE", what)
        self.executed.add(("read", tuple(sorted(spans))))
        for start, end in planned:
            fragment = materials.read_range(self.material, self.material.sha256, start, end)
            first = self.material.line_of(start)[0]
            last = self.material.line_of(end - 1)[0]
            self.observations.append({"kind": "read", "first": first, "last": last,
                                      "ref": fragment.ref.to_json(), "text": fragment.text})
            self.read_bytes += end - start
        return "odczyt %s (%d B)" % ("wiersza " + span_text(spans) if len(spans) == 1 and spans[0][0] == spans[0][1] else "wierszy " + span_text(spans), total)

    def _compact_for(self, needed):
        limit = step.DATA_BYTES - 4096
        while self.data_bytes() + needed > limit:
            for index, item in enumerate(self.observations):
                if item["kind"] == "read":
                    self.observations[index] = {"kind": "dropped", "first": item["first"], "last": item["last"]}
                    break
            else:
                return

    # --- result ----------------------------------------------------------------
    def mark_handed_off(self, value):
        """Count only observations carried by the exact packet passed to the client."""
        for item in value["observations"]:
            if item["kind"] == "read":
                evidence = (item["first"], item["last"], item["ref"]["start"], item["ref"]["end"])
                if evidence not in self.delivered:
                    self.delivered.append(evidence)
            elif item["kind"] == "search":
                self.hit_lines.update(h["line"] for h in item["hits"] if (h["line"], h["text"]) in self._whole_hits)

    def confirm_sources(self, claimed):
        """Split claimed line spans into those covered by really delivered text and the rest."""
        have = set()
        for first, last, _, _ in self.delivered:
            have.update(range(first, last + 1))
        have |= self.hit_lines
        confirmed, rejected = [], []
        for first, last in claimed:
            (confirmed if all(n in have for n in range(first, last + 1)) else rejected).append((first, last))
        return confirmed, rejected

    def delivered_summary(self):
        spans = sorted({(f, l) for f, l, _, _ in self.delivered} | {(n, n) for n in self.hit_lines})
        merged = []
        for first, last in spans:
            if merged and first <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], last)
            else:
                merged.append([first, last])
        read = {(s, e) for _, _, s, e in self.delivered}
        hits = {(self.material.line_starts[n - 1],
                 self.material.line_starts[n] if n < len(self.material.line_starts) else self.material.size)
                for n in self.hit_lines}
        covered = sorted(read | {h for h in hits if not any(a <= h[0] and h[1] <= b for a, b in read)})
        return merged, sum(b - a for a, b in covered)
