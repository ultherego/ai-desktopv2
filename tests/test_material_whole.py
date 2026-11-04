"""Whole-material plan: exact cover, stages, controller-only coverage, phase schema."""
import copy
import unittest

from ai_desktop import analysis_contract as ac
from ai_desktop import material_step as ms
from ai_desktop import material_whole as w
from ai_desktop import materials as m


def material(lines=50000):
    text = "\n".join("row %06d filler text to make this log long enough to need many parts" % i
                     for i in range(1, lines + 1)) + "\n"
    return m.import_bytes(text.encode(), "file")


class PlanTests(unittest.TestCase):
    def test_parts_cover_every_byte_once_in_order_and_fit_one_request(self):
        mat = material()
        plan = w.WholePlan(mat)
        self.assertEqual(plan.parts[0].start, 0)
        self.assertEqual(plan.parts[-1].end, mat.size)
        for before, after in zip(plan.parts, plan.parts[1:]):
            self.assertEqual(before.end, after.start)
        self.assertEqual(b"".join(mat.raw[p.start:p.end] for p in plan.parts), mat.raw)
        for part in plan.parts:
            self.assertLessEqual(part.size, w.PART_BYTES)
            self.assertLessEqual(len(part.chunks), 8)
            self.assertLessEqual(part.first, part.last)
        self.assertEqual(plan.parts[0].first, 1)
        self.assertEqual(plan.parts[-1].last, plan.lines)

    def test_a_smaller_part_size_gives_more_parts_that_still_cover_every_byte(self):
        mat = material(lines=20000)
        default, small = w.WholePlan(mat), w.WholePlan(mat, part_bytes=64 * 1024)
        self.assertGreater(len(small.parts), len(default.parts))
        for part in small.parts:
            self.assertLessEqual(part.size, 64 * 1024)
        self.assertEqual(b"".join(mat.raw[p.start:p.end] for p in small.parts), mat.raw)

    def test_stages_hold_at_most_eight_requests_and_the_synthesis_comes_last(self):
        plan = w.WholePlan(material())
        stages = plan.stages()
        self.assertEqual([len(s) for s in stages], [8, 8])
        flat = [item for stage in stages for item in stage]
        self.assertEqual(flat[-1], ("reduce", None))
        self.assertEqual([i for kind, i in flat if kind == "map"], list(range(len(plan.parts))))
        self.assertLessEqual(plan.stage_bytes(stages[0]), ms.TOTAL_BYTES)      # a stage fits one grant

    def test_coverage_changes_only_through_a_recorded_note(self):
        plan = w.WholePlan(material())
        self.assertFalse(plan.complete())
        self.assertEqual((plan.covered_parts(), plan.covered_bytes(), plan.covered_lines()), ([], 0, set()))
        plan.record_note(0, "notatka")
        self.assertEqual(plan.covered_parts(), [0])
        self.assertEqual(plan.covered_bytes(), plan.parts[0].size)
        self.assertEqual(plan.covered_lines(), set(range(plan.parts[0].first, plan.parts[0].last + 1)))
        for index in range(1, len(plan.parts)):
            plan.record_note(index, "n")
        self.assertTrue(plan.complete())
        self.assertEqual(plan.covered_bytes(), plan.material.size)

    def test_missing_parts_and_spans_are_reported_from_the_controller_state(self):
        plan = w.WholePlan(material())
        for index in (0, 1, 4, 5, 6):
            plan.record_note(index, "n")
        missing = plan.missing_parts()
        self.assertEqual([p.index for p in missing], [i for i in range(len(plan.parts)) if i not in (0, 1, 4, 5, 6)])
        spans = plan.spans(missing)
        self.assertEqual(spans[0][0], plan.parts[2].first)
        self.assertEqual(spans[0][1], plan.parts[3].last)

    def test_sources_are_confirmed_only_for_covered_parts(self):
        plan = w.WholePlan(material())
        plan.record_note(0, "n")
        inside = (plan.parts[0].first, plan.parts[0].first + 1)
        outside = (plan.parts[1].first, plan.parts[1].first)
        straddle = (plan.parts[0].last, plan.parts[1].first)
        self.assertEqual(plan.confirm_sources([inside, outside, straddle]), ([inside], [outside, straddle]))

    def test_request_inputs_validate_and_are_accepted_by_the_transport(self):
        plan = w.WholePlan(material(), history=[{"question": "q", "answer": "a"}])
        first = plan.map_input(0, 1, 700000)
        self.assertEqual({o["kind"] for o in first["observations"]}, {"read"})
        self.assertEqual(first["history"], [])                         # map requests carry no conversation
        ms.validate_input(first)
        ac.application_input(None, "Pytanie?", material=first)
        plan.record_note(0, "wiersze 1-3 zawierają błąd")
        reduce = plan.reduce_input(2, 600000)
        ms.validate_input(reduce)
        self.assertEqual({o["kind"] for o in reduce["observations"]}, {"notes"})
        self.assertEqual(reduce["task"]["part"], {"index": 1, "total": len(plan.parts)})
        self.assertEqual(reduce["history"], [{"question": "q", "answer": "a"}])
        raw = ac.application_input(None, "Pytanie?", material=reduce).encode("utf-8")
        ac.parse_request(raw)

    def test_escape_heavy_text_shrinks_the_parts_and_every_part_serializes(self):
        raw = ('"\\' * 100 + "\n") * 5000                       # ~1 MB of quotes and backslashes: JSON doubles it
        mat = m.import_bytes(raw.encode(), "file")
        plan = w.WholePlan(mat)
        self.assertLess(max(p.size for p in plan.parts), w.PART_BYTES)          # shrunk below the default
        self.assertTrue(plan._fits_one_packet())
        self.assertEqual(b"".join(mat.raw[p.start:p.end] for p in plan.parts), mat.raw)

    def test_more_parts_than_the_synthesis_can_take_are_refused_up_front(self):
        big = material(lines=70000)                              # ~5 MiB: 22 parts of 240 KiB fit the synthesis
        self.assertLessEqual(len(w.WholePlan(big).parts), ms.MAX_OBSERVATIONS)
        with self.assertRaises(w.WholeError):                    # 128 KiB parts would be 40: a note per part cannot fit
            w.WholePlan(big, part_bytes=128 * 1024)

    def test_a_file_that_would_need_too_many_parts_is_refused(self):
        original = w.MAX_PARTS
        w.MAX_PARTS = 3
        try:
            with self.assertRaises(w.WholeError):
                w.WholePlan(material())
        finally:
            w.MAX_PARTS = original


class PhaseSchemaTests(unittest.TestCase):
    def good(self):
        plan = w.WholePlan(material())
        return plan, plan.map_input(0, 1, 700000)

    def test_phase_part_and_notes_are_strictly_validated(self):
        plan, good = self.good()
        plan.record_note(0, "ok")
        reduce = plan.reduce_input(2, 1000)
        mutations = [
            (good, lambda v: v["task"].update(phase="other")),
            (good, lambda v: v["task"].pop("part")),
            (good, lambda v: v["task"]["part"].update(index=99)),
            (good, lambda v: v["task"]["part"].update(total=0)),
            (good, lambda v: v["task"]["part"].update(extra=1)),
            (good, lambda v: v["task"].update(phase=True)),
            (reduce, lambda v: v["observations"][0].update(text="")),
            (reduce, lambda v: v["observations"][0].update(text="x" * 3200)),
            (reduce, lambda v: v["observations"][0].update(first=9, last=2)),
            (reduce, lambda v: v["observations"][0].update(extra=1)),
            (reduce, lambda v: v["observations"][0].update(first=10 ** 7, last=10 ** 7)),
        ]
        for base, mutate in mutations:
            bad = copy.deepcopy(base)
            mutate(bad)
            with self.subTest(str(mutate.__code__.co_consts[-1])[:40]):
                with self.assertRaises(ValueError):
                    ms.validate_input(bad)

    def test_the_notes_decision_is_closed_and_bounded(self):
        self.assertEqual(ms.parse_decision('{"action":"notes","text":"wiersz 4: błąd"}', 100), {"action": "notes", "text": "wiersz 4: błąd"})
        for bad in ('{"action":"notes"}', '{"action":"notes","text":""}', '{"action":"notes","text":"x","extra":1}',
                    '{"action":"notes","text":"%s"}' % ("a" * 3200), '{"action":"notes","text":"a\\u001b"}'):
            with self.subTest(bad[:40]):
                with self.assertRaises(ValueError):
                    ms.parse_decision(bad, 100)


if __name__ == "__main__":
    unittest.main()


class FiveMiBTests(unittest.TestCase):
    def test_a_file_at_the_new_limit_is_planned_into_a_bounded_number_of_parts_and_stages(self):
        from ai_desktop import materials as m
        line = b"2026-10-31T12:00:00 INFO heartbeat ok row %07d\n"
        raw = b"".join(line % i for i in range(1, 200000))[:m.MAX_MATERIAL_BYTES]
        raw = raw[:raw.rfind(b"\n") + 1]
        material = m.import_bytes(raw, "file")
        self.assertGreater(material.size, 5 * 1024 * 1024 - 100)
        plan = w.WholePlan(material)
        self.assertLessEqual(len(plan.parts), 64)
        stages = plan.stages()
        self.assertTrue(all(len(stage) <= 8 for stage in stages))
        self.assertEqual(sum(len(stage) for stage in stages), len(plan.parts) + 1)      # one request per part plus the synthesis
        self.assertEqual(sum(p.size for p in plan.parts), material.size)               # every byte is in exactly one part
        with self.assertRaises(m.MaterialError):
            m.import_bytes(raw + b"x" * (m.MAX_MATERIAL_BYTES - len(raw) + 1), "file")
