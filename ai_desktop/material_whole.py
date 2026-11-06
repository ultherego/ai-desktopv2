"""Whole-material mode (pure, no I/O): a plan of parts, controller-owned coverage, notes.

Used when a question needs the ENTIRE large file. The material is cut into parts of at
most ~240 KiB (8 chunks of 30 KiB; shrunk automatically if one request would not fit the 512 KiB packet); each part gets its own request whose answer is a short
note; a final request combines the notes. Coverage is filled only by the controller after
a part's note arrived in a valid form, so the final text can never claim more than was
really analysed. Requests are grouped into stages of at most 8 (one grant, one consent each).
"""
from . import material_loop as ml
from . import material_step as step
from . import materials

CHUNK = 30 * 1024
PART_BYTES = 240 * 1024
STAGE_REQUESTS = 8
MAX_PARTS = step.MAX_PARTS  # dedicated bounded notes-only synthesis, independent of fragment observations


class WholeError(ValueError):
    """The material cannot be planned into a bounded number of parts."""


class Part:
    def __init__(self, index, chunks, material):
        self.index, self.chunks = index, tuple(chunks)
        self.start, self.end = chunks[0].start, chunks[-1].end
        self.size = self.end - self.start
        self.first = material.line_of(self.start)[0]
        self.last = material.line_of(self.end - 1)[0]

    def ranges(self):
        return [(c.start, c.end) for c in self.chunks]


class WholePlan:
    def __init__(self, material, history=(), part_bytes=PART_BYTES):
        self.material = material
        self.history = [dict(t) for t in history][-step.MAX_HISTORY:]
        self.lines = ml.visible_lines(material)
        chunks = materials.chunk_plan(material, chunk_bytes=CHUNK)
        while True:
            groups, current, size = [], [], 0
            for chunk in chunks:
                length = chunk.end - chunk.start
                if current and size + length > part_bytes:
                    groups.append(current)
                    current, size = [], 0
                current.append(chunk)
                size += length
            if current:
                groups.append(current)
            self.parts = tuple(Part(i, g, material) for i, g in enumerate(groups))
            if len(groups) > MAX_PARTS:
                raise WholeError("too many parts")
            if self._fits_one_packet():
                break
            if part_bytes <= CHUNK:
                raise WholeError("a part does not fit one request")
            part_bytes = max(CHUNK, part_bytes * 3 // 4)           # too many chunks, or text that escapes badly in JSON: smaller parts
        if len(groups) > MAX_PARTS:
            raise WholeError("too many parts")
        self.coverage = materials.Coverage(chunks)
        self.notes = {}
        if not self._synthesis_fits():
            raise WholeError("synthesis does not fit")

    def _fits_one_packet(self):
        """EVERY part, as really serialized with the longest allowed question, must stay below the packet limit
        (a smaller part full of quotes or backslashes can serialize larger than the biggest plain one)."""
        from . import analysis_contract as ac
        question = "x" * ac.MAX_QUESTION_BYTES
        try:
            for part in self.parts:
                ac.application_input(None, question, material=self.map_input(part.index, 1, step.TOTAL_BYTES))
        except (ValueError, ac.AnalysisDenied):
            return False
        return True

    def _synthesis_fits(self):
        """The worst allowed synthesis (every part with a maximal note) must validate and serialize now, not after the work."""
        from . import analysis_contract as ac
        self.notes = {part.index: "x" * step.NOTE_BYTES for part in self.parts}
        try:
            ac.application_input(None, "x" * ac.MAX_QUESTION_BYTES,
                                 material=self.reduce_input(STAGE_REQUESTS, step.TOTAL_BYTES))
            return True
        except (ValueError, ac.AnalysisDenied):
            return False
        finally:
            self.notes = {}

    # --- plan --------------------------------------------------------------------
    def item_bytes(self, item):
        """Worst-case dynamic data of one request: a part's text, or the synthesis (a maximal note per part + the history)."""
        kind, index = item
        if kind == "map":
            return self.parts[index].size
        return len(self.parts) * step.NOTE_BYTES + sum(
            len(t["question"].encode("utf-8")) + len(t["answer"].encode("utf-8")) for t in self.history)

    def stages(self):
        """Requests ('map', part index) ... ('reduce', None). A stage holds at most 8 requests AND at most TOTAL_BYTES of
        data (the grant budget), so big parts give stages with fewer requests; the synthesis comes last."""
        items = [("map", p.index) for p in self.parts] + [("reduce", None)]
        stages, current, size = [], [], 0
        for item in items:
            length = self.item_bytes(item)
            if current and (len(current) >= STAGE_REQUESTS or size + length > step.TOTAL_BYTES):
                stages.append(current)
                current, size = [], 0
            current.append(item)
            size += length
        stages.append(current)
        return stages

    def stage_bytes(self, stage):
        return sum(self.parts[i].size for kind, i in stage if kind == "map")

    def stage_budget(self, stage):
        """The hard data budget of a stage grant (worst case, including the synthesis)."""
        return sum(self.item_bytes(item) for item in stage)

    def task_budget(self):
        """The whole task, separate from the material size: (requests, worst-case data bytes sent in total)."""
        stages = self.stages()
        return sum(len(s) for s in stages), sum(self.stage_budget(s) for s in stages)

    # --- requests ----------------------------------------------------------------
    def _meta(self):
        return {"id": self.material.material_id, "version": self.material.sha256,
                "size": self.material.size, "lines": self.lines}

    def map_input(self, index, number, bytes_left):
        part = self.parts[index]
        observations = [ml.read_observation(self.material, a, b) for a, b in part.ranges()]
        return {"material": self._meta(),
                "task": {"step": number, "max_steps": STAGE_REQUESTS, "bytes_left": bytes_left,
                         "phase": "map", "part": {"index": index + 1, "total": len(self.parts)}},
                "observations": observations, "history": []}

    def reduce_input(self, number, bytes_left):
        observations = [{"kind": "notes", "first": self.parts[i].first, "last": self.parts[i].last, "text": text}
                        for i, text in sorted(self.notes.items())]
        return {"material": self._meta(),
                "task": {"step": number, "max_steps": STAGE_REQUESTS, "bytes_left": bytes_left,
                         "phase": "reduce", "part": {"index": len(self.notes), "total": len(self.parts)}},
                "observations": observations, "history": [dict(t) for t in self.history]}

    # --- coverage (controller only) --------------------------------------------------
    def record_note(self, index, text):
        self.notes[index] = text
        for chunk in self.parts[index].chunks:
            self.coverage.mark(chunk.index)

    def covered_parts(self):
        return sorted(self.notes)

    def missing_parts(self):
        return [p for p in self.parts if p.index not in self.notes]

    def covered_bytes(self):
        return self.coverage.covered_bytes()

    def complete(self):
        return self.coverage.complete

    def covered_lines(self):
        lines = set()
        for index in self.notes:
            lines.update(range(self.parts[index].first, self.parts[index].last + 1))
        return lines

    def confirm_sources(self, claimed):
        # Source claims are intervals; never allocate a set of every line in a large log.
        have = self.spans([self.parts[index] for index in sorted(self.notes)])
        confirmed, rejected = [], []
        for first, last in claimed:
            (confirmed if any(lo <= first <= last <= hi for lo, hi in have) else rejected).append((first, last))
        return confirmed, rejected

    @staticmethod
    def spans(parts):
        spans = []
        for p in parts:
            if spans and p.first <= spans[-1][1] + 1:
                spans[-1][1] = max(spans[-1][1], p.last)
            else:
                spans.append([p.first, p.last])
        return spans
