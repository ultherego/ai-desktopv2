"""LC0 materials: limits, exact bytes, deterministic chunks, ranges, search, coverage, refs."""
import json
import unittest

from ai_desktop import materials as m

MARK = "ERROR-MARKER"


def log_1mib(marker_at=(0, 0.5, 1.0)):
    """A1: a ~1 MiB log with error markers at the start, around the middle and at the very end."""
    line = "2026-10-31T12:00:00 INFO service heartbeat ok\n"
    body = bytearray()
    while len(body) < m.MAX_MATERIAL_BYTES - 200:
        body += line.encode()
    positions = {int(len(body) * p) for p in marker_at}
    out = bytearray(body)
    for pos in sorted(positions, reverse=True):
        pos = out.rfind(b"\n", 0, pos) + 1  # keep markers on line starts
        out[pos:pos] = (MARK + " code=%d\n" % pos).encode()
    out = out[:m.MAX_MATERIAL_BYTES - 40] + ("\n" + MARK + " code=END\n").encode()
    assert len(out) <= m.MAX_MATERIAL_BYTES
    return bytes(out)


class ImportTests(unittest.TestCase):
    def test_exact_limit_is_accepted_and_one_more_byte_is_refused_without_truncation(self):
        raw = b"a" * m.MAX_MATERIAL_BYTES
        self.assertEqual(m.import_bytes(raw, "file").raw, raw)
        with self.assertRaises(m.MaterialError) as raised:
            m.import_bytes(raw + b"a", "file")
        self.assertEqual(raised.exception.code, "IMPORT_LIMIT")

    def test_bytes_are_preserved_and_hashed_exactly(self):
        raw = log_1mib()
        material = m.import_bytes(raw, "file")
        self.assertEqual((material.raw, material.size, material.sha256), (raw, len(raw), m.sha256(raw)))
        self.assertRegex(material.material_id, r"^[0-9a-f]{16}$")

    def test_refusals_carry_closed_codes(self):
        cases = [(b"", "IMPORT_EMPTY"), (b"\xff\xfe", "IMPORT_ENCODING"), (b"\xc0\x80", "IMPORT_ENCODING"),
                 (b"\xed\xa0\x80", "IMPORT_ENCODING"), (b"abc\x00def", "IMPORT_CONTROL"),
                 (b"\x1b[31mred\x1b[0m\n", "IMPORT_CONTROL"), (b"\x7f", "IMPORT_CONTROL"),
                 ("﻿bom".encode(), "IMPORT_CONTROL"), ("a‮b".encode(), "IMPORT_CONTROL"),
                 ("a​b".encode(), "IMPORT_CONTROL"), ("a\x85b".encode(), "IMPORT_CONTROL"),
                 ("ab".encode(), "IMPORT_CONTROL")]
        for raw, code in cases:
            with self.subTest(raw=raw[:8]):
                with self.assertRaises(m.MaterialError) as raised:
                    m.import_bytes(raw, "file")
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("abc", str(raised.exception))
        with self.assertRaises(m.MaterialError):
            m.import_bytes("text", "file")  # str is not bytes
        with self.assertRaises(m.MaterialError):
            m.import_bytes(b"text", "network")

    def test_tabs_crlf_and_multibyte_text_are_accepted(self):
        for raw in (b"a\tb\r\nc\r\n", "zażółć gęślą jaźń 日本語 🙂\n".encode(), b"no final newline"):
            self.assertEqual(m.import_bytes(raw, "paste").raw, raw)


class ChunkTests(unittest.TestCase):
    def assert_exact_cover(self, material, plan):
        self.assertEqual(plan[0].start, 0)
        self.assertEqual(plan[-1].end, material.size)
        for before, after in zip(plan, plan[1:]):
            self.assertEqual(before.end, after.start)  # no gap, no overlap
        for chunk in plan:
            self.assertLessEqual(chunk.end - chunk.start, m.CHUNK_BYTES)
            piece = material.raw[chunk.start:chunk.end]
            piece.decode("utf-8")  # every chunk is valid UTF-8 on its own
            self.assertEqual(chunk.sha256, m.sha256(piece))
        self.assertEqual(b"".join(material.raw[c.start:c.end] for c in plan), material.raw)

    def test_log_chunks_cover_all_bytes_prefer_line_ends_and_are_deterministic(self):
        material = m.import_bytes(log_1mib(), "file")
        plan = m.chunk_plan(material)
        self.assert_exact_cover(material, plan)
        self.assertEqual(plan, m.chunk_plan(material))
        for chunk in plan[:-1]:
            self.assertEqual(material.raw[chunk.end - 1:chunk.end], b"\n")
        self.assertGreater(len(plan), 30)

    def test_single_one_mib_line_is_split_on_character_boundaries(self):
        for unit in ("a", "ą", "日", "🙂"):
            with self.subTest(unit=unit):
                raw = (unit * (m.MAX_MATERIAL_BYTES // len(unit.encode())))[:m.MAX_MATERIAL_BYTES // len(unit.encode())]
                material = m.import_bytes(raw.encode(), "file")
                self.assertEqual(len(material.line_starts), 1)
                self.assert_exact_cover(material, m.chunk_plan(material))

    def test_crlf_without_final_newline_keeps_exact_bytes_and_line_mapping(self):
        raw = b"first\r\nsecond\r\nlast without newline"
        material = m.import_bytes(raw, "file")
        plan = m.chunk_plan(material, chunk_bytes=8)
        self.assertEqual(b"".join(raw[c.start:c.end] for c in plan), raw)
        self.assertEqual(material.line_of(0), (1, 0))
        self.assertEqual(material.line_of(raw.index(b"second")), (2, 0))
        self.assertEqual(material.line_of(raw.index(b"last")), (3, 0))
        self.assertEqual(material.line_of(len(raw)), (3, len(b"last without newline")))

    def test_edge_sizes_never_lose_the_tail(self):
        c = m.CHUNK_BYTES
        samples = [b"a", b"a" * (c - 1), b"a" * c, b"a" * (c + 1), b"a" * (2 * c + 1),
                   b"a" * (c - 1) + b"\n", b"a" * c + b"\n", b"a" * c + b"\n" + b"b",
                   ("a" * (c - 1) + "ą").encode(), ("a" * (c - 2) + "ą" + "b").encode(), b"\n" * (c + 1)]
        for raw in samples:
            with self.subTest(size=len(raw), tail=raw[-2:]):
                material = m.import_bytes(raw, "file")
                plan = m.chunk_plan(material)
                self.assert_exact_cover(material, plan)
                coverage = m.Coverage(plan)
                for chunk in plan:
                    coverage.mark(chunk.index)
                self.assertEqual((coverage.complete, coverage.covered_bytes()), (True, len(raw)))

    def test_tiny_chunk_size_still_progresses_over_wide_characters(self):
        material = m.import_bytes(("🙂" * 50).encode(), "file")
        plan = m.chunk_plan(material, chunk_bytes=5)
        self.assertEqual(b"".join(material.raw[c.start:c.end] for c in plan), material.raw)
        for chunk in plan:
            material.raw[chunk.start:chunk.end].decode("utf-8")


class RangeTests(unittest.TestCase):
    def setUp(self):
        self.material = m.import_bytes("zażółć\nlinia druga\n".encode(), "file")

    def test_exact_fragment_with_verified_reference(self):
        fragment = m.read_range(self.material, self.material.sha256, 0, 10)  # "zażółć" is 10 bytes
        self.assertEqual(fragment.text, "zażółć")
        self.assertEqual(fragment.ref.fragment_sha256, m.sha256(self.material.raw[0:10]))
        self.assertEqual(fragment.ref.label()[9:], "0-10")

    def test_stale_version_split_character_and_bad_bounds_are_refused(self):
        v = self.material.sha256
        with self.assertRaises(m.MaterialError) as stale:
            m.read_range(self.material, "0" * 64, 0, 3)
        self.assertEqual(stale.exception.code, "SOURCE_CHANGED")
        for start, end in ((3, 4), (2, 3), (0, 0), (5, 2), (-1, 3), (0, self.material.size + 1), (0, True)):
            with self.subTest(start=start, end=end):
                with self.assertRaises(m.MaterialError) as bad:
                    m.read_range(self.material, v, start, end)
                self.assertEqual(bad.exception.code, "RANGE_INVALID")
        with self.assertRaises(m.MaterialError):
            m.read_range(self.material, v, 0, self.material.size, max_bytes=4)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.raw = log_1mib()
        self.material = m.import_bytes(self.raw, "file")

    def naive(self, needle):
        pattern, found, position = needle.encode(), [], 0
        while (index := self.raw.find(pattern, position)) != -1:
            found.append(index)
            position = index + len(pattern)
        return found

    def all_pages(self, needle):
        hits, cursor, pages = [], 0, 0
        while True:
            page = m.search_text(self.material, needle, cursor)
            hits += page.hits
            pages += 1
            if page.complete:
                return hits, pages
            cursor = page.next_cursor

    def test_markers_at_start_middle_and_end_are_all_found_with_correct_lines(self):
        hits, _ = self.all_pages(MARK)
        self.assertEqual([h.start for h in hits], self.naive(MARK))
        self.assertGreaterEqual(len(hits), 3)
        self.assertEqual(hits[0].start, 0)
        self.assertEqual(self.raw[hits[-1].start:hits[-1].end], MARK.encode())
        self.assertEqual(hits[-1].end + len(" code=END\n"), self.material.size)
        for hit in hits:
            line, column = self.material.line_of(hit.start)
            self.assertEqual((hit.line, hit.column), (line, column))
            self.assertEqual(column, 0)

    def test_pagination_is_lossless_and_complete_only_at_the_end(self):
        hits, pages = self.all_pages("heartbeat")
        self.assertEqual([h.start for h in hits], self.naive("heartbeat"))
        self.assertGreater(pages, 100)
        first = m.search_text(self.material, "heartbeat", 0)
        self.assertFalse(first.complete)  # "no more hits" is never claimed from a partial scan
        self.assertEqual(len(first.hits), m.MAX_HITS_PER_PAGE)

    def test_hit_across_a_chunk_border_is_found_and_context_is_readable(self):
        plan = m.chunk_plan(self.material)
        border = plan[3].start
        raw = bytearray(b"x" * m.MAX_MATERIAL_BYTES)
        raw[border - 3:border + 3] = b"BORDER"
        material = m.import_bytes(bytes(raw), "file")
        page = m.search_text(material, "BORDER")
        self.assertEqual([h.start for h in page.hits], [border - 3])
        lo, hi = page.hits[0].context
        self.assertIn("BORDER", m.read_range(material, material.sha256, lo, hi).text)

    def test_multibyte_context_never_splits_a_character(self):
        material = m.import_bytes(("ą" * 600 + "NEEDLE" + "日" * 600).encode(), "file")
        hit = m.search_text(material, "NEEDLE").hits[0]
        text = m.read_range(material, material.sha256, *hit.context).text
        self.assertIn("NEEDLE", text)

    def test_unknown_text_is_not_found_in_a_complete_scan(self):
        page = m.search_text(self.material, "NO-SUCH-TEXT")
        self.assertEqual((page.hits, page.complete), ((), True))

    def test_invalid_needles_and_cursors_are_refused(self):
        for needle in ("", "x" * 300, "bad\x00", "\x1b[0m", "\ud800", 5):
            with self.subTest(needle=needle):
                with self.assertRaises(m.MaterialError) as raised:
                    m.search_text(self.material, needle)
                self.assertEqual(raised.exception.code, "NEEDLE_INVALID")
        for cursor in (-1, self.material.size + 1, True, "0"):
            with self.assertRaises(m.MaterialError):
                m.search_text(self.material, "ok", cursor)


class CoverageAndRefTests(unittest.TestCase):
    def setUp(self):
        self.material = m.import_bytes(log_1mib(), "file")
        self.plan = m.chunk_plan(self.material)

    def test_coverage_is_complete_only_when_every_planned_chunk_was_analysed(self):
        coverage = m.Coverage(self.plan)
        for chunk in self.plan[:-1]:
            coverage.mark(chunk.index)
        self.assertFalse(coverage.complete)  # the last chunk was skipped
        self.assertEqual(coverage.missing(), ((self.plan[-1].start, self.plan[-1].end),))
        coverage.mark(self.plan[-1].index)
        coverage.mark(self.plan[-1].index)   # idempotent
        self.assertTrue(coverage.complete)
        self.assertEqual(coverage.covered_bytes(), self.material.size)
        for bad in (-1, len(self.plan), "0", True):
            with self.assertRaises(m.MaterialError):
                coverage.mark(bad)

    def test_reference_is_valid_only_when_the_controller_delivered_exactly_it(self):
        fragment = m.read_range(self.material, self.material.sha256, 0, 100)
        delivered = {fragment.ref}
        self.assertEqual(m.check_ref(self.material, fragment.ref, delivered), fragment)
        forged = m.parse_ref(dict(fragment.ref.to_json(), start=10, end=90))
        wrong_hash = m.parse_ref(dict(fragment.ref.to_json(), fragment="0" * 64))
        other = m.import_bytes(b"different material", "file")
        for ref in (forged, wrong_hash):
            with self.assertRaises(m.MaterialError) as raised:
                m.check_ref(self.material, ref, delivered)
            self.assertEqual(raised.exception.code, "REF_INVALID")
        with self.assertRaises(m.MaterialError):
            m.check_ref(other, fragment.ref, delivered)

    def test_parse_ref_is_strict(self):
        good = m.read_range(self.material, self.material.sha256, 0, 10).ref.to_json()
        self.assertEqual(m.parse_ref(good).start, 0)
        bad = [dict(good, extra=1), {k: v for k, v in good.items() if k != "end"}, dict(good, start=-1),
               dict(good, start=5, end=5), dict(good, start="0"), dict(good, start=True),
               dict(good, version="A" * 64), dict(good, material="zz"), "ref", None, [good]]
        for value in bad:
            with self.subTest(value=str(value)[:40]):
                with self.assertRaises(m.MaterialError):
                    m.parse_ref(value)
        self.assertEqual(json.loads(json.dumps(good)), good)


if __name__ == "__main__":
    unittest.main()
