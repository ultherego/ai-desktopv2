"""The optional 512 KiB part profile: the larger limits exist only for a whole-file "map" request, nothing else widens."""
import copy
import json
import unittest

from ai_desktop import analysis_contract as ac
from ai_desktop import material_grant as g
from ai_desktop import material_step as ms
from ai_desktop import material_whole as w
from ai_desktop import materials as m

KIB = 1024


def material(lines=40000, width=70):
    text = "\n".join(("row %06d " % i).ljust(width, "x") for i in range(1, lines + 1)) + "\n"
    return m.import_bytes(text.encode(), "file")


def map_request(mat, part_bytes):
    plan = w.WholePlan(mat, part_bytes=part_bytes)
    big = max(plan.parts, key=lambda p: p.size)
    return plan, plan.map_input(big.index, 1, ms.TOTAL_BYTES)


class SchemaTests(unittest.TestCase):
    def test_a_map_request_may_carry_up_to_512_kib_but_a_read_request_stays_at_256(self):
        mat = material()
        plan, request = map_request(mat, 512 * KIB)
        data = ms.data_bytes(request["observations"], request["history"])
        self.assertGreater(data, ms.DATA_BYTES)                       # more than the old ceiling ...
        self.assertLessEqual(data, ms.PART_DATA_BYTES)
        ms.validate_input(request)                                    # ... and accepted in the map phase
        other = copy.deepcopy(request)
        other["task"].pop("phase")
        other["task"].pop("part")
        with self.assertRaises(ValueError):                           # the same data without the phase: refused
            ms.validate_input(other)
        reduce = copy.deepcopy(request)
        reduce["task"]["phase"] = "reduce"
        with self.assertRaises(ValueError):
            ms.validate_input(reduce)

    def test_one_byte_over_the_part_ceiling_is_refused(self):
        self.assertEqual(ms.data_limit({"phase": "map"}), ms.PART_DATA_BYTES)
        self.assertEqual(ms.data_limit({"phase": "reduce"}), ms.DATA_BYTES)
        self.assertEqual(ms.data_limit({}), ms.DATA_BYTES)
        self.assertEqual(ms.data_limit(None), ms.DATA_BYTES)


class PacketTests(unittest.TestCase):
    def test_only_a_map_request_may_exceed_the_512_kib_packet(self):
        mat = material()
        _, request = map_request(mat, 512 * KIB)
        payload = ac.application_input(None, "Pytanie?", material=request)
        size = len(payload.encode())
        self.assertTrue(ac.MAX_PACKET < size <= ac.MAX_PACKET_LARGE, size)
        ac.parse_request(payload.encode())                            # the transport and the parser accept it
        reduce_like = copy.deepcopy(request)
        reduce_like["task"]["phase"] = "reduce"
        with self.assertRaises(ValueError):                           # but not under another phase / mode
            ac.application_input(None, "Pytanie?", material=reduce_like)

    def test_the_transport_ceiling_does_not_widen_other_modes(self):
        big = "x" * 600 * KIB
        with self.assertRaises(ValueError):
            ac.application_input(None, "Pytanie?", context=[{"kind": "file", "text": big}])
        with self.assertRaises(ac.AnalysisDenied):                    # parse_request re-derives the strict limit per mode
            ac.parse_request(json.dumps({"mode": "question", "question": "Pytanie?", "context": [], "pad": big}).encode())
        with self.assertRaises(ac.AnalysisDenied):
            ac.parse_request(b"x" * (ac.MAX_PACKET_LARGE + 1))


class GrantTests(unittest.TestCase):
    def session(self, max_bytes=g.MAX_TOTAL_BYTES):
        mat = material()
        grant = g.MaterialGrant(mat.material_id, mat.sha256, "cel", 300.0, max_bytes=max_bytes)
        return mat, g.TransferSession(mat, grant, clock=lambda: 0.0)

    def test_map_data_up_to_512_kib_is_allowed_only_for_the_map_phase(self):
        mat, session = self.session()
        plan, request = map_request(mat, 512 * KIB)
        big = max(plan.parts, key=lambda p: p.size)
        data = sum(len(o["text"].encode()) for o in request["observations"])
        session.prepare_step("Pytanie?", request, data, big.ranges())
        mat2, session2 = self.session()
        loop_like = copy.deepcopy(request)
        loop_like["task"].pop("phase")
        loop_like["task"].pop("part")
        with self.assertRaises(g.GrantDenied) as raised:
            session2.prepare_step("Pytanie?", loop_like, data, big.ranges())
        self.assertEqual(raised.exception.code, "PACKET_LIMIT")
        with self.assertRaises(g.GrantDenied):                        # project requests never get the map ceiling
            session2.prepare_step("Pytanie?", request, data, big.ranges(), project=True)

    def test_the_stage_budget_is_enforced_by_the_grant(self):
        mat, session = self.session(max_bytes=300 * KIB)
        plan, request = map_request(mat, 512 * KIB)
        big = max(plan.parts, key=lambda p: p.size)
        with self.assertRaises(g.GrantDenied) as raised:
            session.prepare_step("Pytanie?", request, big.size, big.ranges())
        self.assertEqual(raised.exception.code, "BUDGET_EXHAUSTED")


def exact(size, width=63):
    """A material of exactly `size` bytes made of fixed-width rows (width + newline)."""
    rows = size // (width + 1)
    return m.import_bytes(("\n".join(("r%07d " % i).ljust(width, "x") for i in range(rows)) + "\n").encode(), "file")


class StageGrantTests(unittest.TestCase):
    """Every stage grant is built from the exact stage budget; small budgets are valid and the upper limits stay."""

    def grants(self, plan):
        mat = plan.material
        return [g.TransferSession(mat, g.MaterialGrant(mat.material_id, mat.sha256, "cel", 300.0, max_requests=len(stage),
                                                       max_bytes=plan.stage_budget(stage)), clock=lambda: 0.0)
                for stage in plan.stages()]

    def run_stage(self, plan, session, stage):
        for number, (kind, index) in enumerate(stage, 1):
            if kind == "map":
                request = plan.map_input(index, number, session.bytes_left())
                data, ranges = plan.parts[index].size, plan.parts[index].ranges()
            else:
                request = plan.reduce_input(number, session.bytes_left())
                data, ranges = plan.item_bytes(("reduce", None)), []
            packet = session.prepare_step("Pytanie?", request, data, ranges)
            session.hand_off(packet)
            session.record_answer(packet)

    def test_a_stage_with_only_the_synthesis_gets_a_valid_small_grant(self):
        plan = w.WholePlan(exact(2 * 1024 * KIB), part_bytes=512 * KIB)
        stages = plan.stages()
        self.assertEqual(stages[-1], [("reduce", None)])                    # the case found on 4592f22
        self.assertLess(plan.stage_budget(stages[-1]), g.PACKET_BYTES)
        sessions = self.grants(plan)                                       # all constructible, none refused
        for index in range(len(plan.parts)):
            plan.record_note(index, "n" * 100)
        self.run_stage(plan, sessions[-1], stages[-1])

    def test_a_small_tail_runs_together_with_the_synthesis(self):
        plan = w.WholePlan(exact(2 * 1024 * KIB - 600 * KIB, 63), part_bytes=512 * KIB)
        stages = plan.stages()
        self.assertEqual(stages[-1][-1], ("reduce", None))
        self.assertTrue(any(kind == "map" for kind, _ in stages[-1]))
        sessions = self.grants(plan)
        for stage, session in zip(stages, sessions):
            for kind, index in stage:
                if kind == "map":
                    plan.record_note(index, "n" * 100)
            self.run_stage(plan, session, stage)

    def test_a_whole_small_plan_below_256_kib_is_one_valid_stage(self):
        plan = w.WholePlan(exact(100 * KIB), part_bytes=240 * KIB)
        stages = plan.stages()
        self.assertEqual(len(stages), 1)
        self.assertLess(plan.stage_budget(stages[0]), g.PACKET_BYTES)
        session = self.grants(plan)[0]
        plan.record_note(0, "n" * 100)
        self.run_stage(plan, session, stages[0])

    def test_the_upper_limits_stay(self):
        plan = w.WholePlan(exact(100 * KIB), part_bytes=240 * KIB)
        mat = plan.material
        for overrides in ({"max_bytes": 0}, {"max_bytes": g.MAX_TOTAL_BYTES + 1}, {"max_requests": g.MAX_REQUESTS + 1}):
            with self.subTest(overrides=overrides):
                fields = dict(max_requests=1, max_bytes=1024, **{})
                fields.update(overrides)
                with self.assertRaises(g.GrantDenied) as raised:
                    g.TransferSession(mat, g.MaterialGrant(mat.material_id, mat.sha256, "cel", 300.0, **fields), clock=lambda: 0.0)
                self.assertEqual(raised.exception.code, "GRANT_INVALID")


class PlanTests(unittest.TestCase):
    def test_512_kib_parts_cover_every_byte_and_every_request_fits(self):
        mat = material()
        plan = w.WholePlan(mat, part_bytes=512 * KIB)
        self.assertEqual(b"".join(mat.raw[p.start:p.end] for p in plan.parts), mat.raw)
        self.assertLessEqual(max(p.size for p in plan.parts), 512 * KIB)
        self.assertGreater(max(p.size for p in plan.parts), 240 * KIB)
        self.assertTrue(plan._fits_one_packet())
        for part in plan.parts:
            self.assertLessEqual(len(part.chunks), ms.MAX_OBSERVATIONS)

    def test_short_lines_that_make_many_small_chunks_shrink_the_part_instead_of_failing_later(self):
        # 20 KiB lines give ~20 KiB chunks: 512 KiB would need more than 24 observations, so the plan must shrink on its own
        lines = ["%06d%s" % (i, "y" * (20 * KIB - 7)) for i in range(1, 160)]
        mat = m.import_bytes(("\n".join(lines) + "\n").encode(), "file")
        plan = w.WholePlan(mat, part_bytes=512 * KIB)
        for part in plan.parts:
            self.assertLessEqual(len(part.chunks), ms.MAX_OBSERVATIONS)
        self.assertEqual(b"".join(mat.raw[p.start:p.end] for p in plan.parts), mat.raw)
        _, request = map_request(mat, 512 * KIB)
        ms.validate_input(request)

    def test_stages_respect_both_the_request_count_and_the_data_budget(self):
        plan = w.WholePlan(material(), part_bytes=512 * KIB)
        stages = plan.stages()
        flat = [item for stage in stages for item in stage]
        self.assertEqual(flat[-1], ("reduce", None))
        self.assertEqual([i for kind, i in flat if kind == "map"], list(range(len(plan.parts))))
        for stage in stages:
            self.assertLessEqual(len(stage), w.STAGE_REQUESTS)
            self.assertLessEqual(plan.stage_budget(stage), ms.TOTAL_BYTES)
        self.assertLess(max(len(s) for s in stages[:-1]), w.STAGE_REQUESTS)      # big parts: fewer requests per stage
        self.assertEqual(ms.TOTAL_BYTES, 2 * 1024 * KIB)                         # the budget was NOT doubled

    def test_the_task_budget_includes_the_synthesis_and_the_history(self):
        history = [{"question": "q" * 100, "answer": "a" * 200}]
        plan = w.WholePlan(material(), history=history)
        requests, budget = plan.task_budget()
        self.assertEqual(requests, len(plan.parts) + 1)
        synthesis = len(plan.parts) * ms.NOTE_BYTES + 300
        self.assertEqual(budget, plan.material.size + synthesis)
        self.assertGreater(budget, plan.material.size)
        self.assertEqual(budget, sum(plan.stage_budget(s) for s in plan.stages()))

    def test_the_synthesis_data_never_exceeds_its_budgeted_worst_case(self):
        plan = w.WholePlan(material(), part_bytes=512 * KIB)
        for part in plan.parts:
            plan.record_note(part.index, "n" * ms.NOTE_BYTES)
        request = plan.reduce_input(1, ms.TOTAL_BYTES)
        data = sum(len(o["text"].encode()) for o in request["observations"])
        self.assertLessEqual(data, plan.item_bytes(("reduce", None)))


if __name__ == "__main__":
    unittest.main()
