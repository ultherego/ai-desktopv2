"""Large local materials keep bounded wire fragments and truthful coverage."""
import unittest
from unittest.mock import patch

from ai_desktop import analysis_contract, material_grant, material_step, material_whole, materials, permissions


class LargeMaterialLimitsTests(unittest.TestCase):
    def test_thirty_two_mib_material_ranges_and_whole_plan_fit_separate_budgets(self):
        ending = b"unique-last-line\n"
        raw = b"x" * (32 * 1024 * 1024 - len(ending)) + ending
        material = materials.import_bytes(raw, "file")
        self.assertEqual(material.size, permissions.DEFAULT_FILE_SIZE)
        piece = materials.read_range(material, material.sha256, material.size - len(ending), material.size)
        self.assertEqual(piece.text, "unique-last-line\n")
        plan = material_whole.WholePlan(material)
        self.assertGreater(len(plan.parts), 128)
        self.assertLessEqual(len(plan.parts), material_step.MAX_PARTS)
        self.assertEqual(sum(part.size for part in plan.parts), len(raw))
        self.assertFalse(plan.complete())
        plan.record_note(0, "Only this part was reviewed.")
        self.assertEqual(plan.covered_bytes(), plan.parts[0].size)
        self.assertFalse(plan.complete())
        self.assertEqual(len(plan.missing_parts()), len(plan.parts) - 1)
        for part in plan.parts:
            plan.record_note(part.index, "x" * material_step.NOTE_BYTES)
        self.assertTrue(plan.complete())
        final = plan.reduce_input(1, material_step.TOTAL_BYTES)
        data = material_step.data_bytes(final["observations"], final["history"])
        self.assertGreater(data, material_step.DATA_BYTES)
        grant = material_grant.MaterialGrant(material.material_id, material.sha256, "whole", 300,
                                              max_requests=1, max_bytes=data)
        transfer = material_grant.TransferSession(material, grant, clock=lambda: 0)
        packet = transfer.prepare_step("Summarize all checked parts", final, data)
        analysis_contract.parse_request(packet.payload.encode())
        self.assertEqual(transfer.bytes_left(), 0)
        self.assertEqual(len(packet.refs), 0)  # synthesis contains notes, no new source reads

    def test_newline_dense_material_uses_compact_readonly_index_and_interval_coverage(self):
        raw = b"\n" * (1024 * 1024)
        material = materials.import_bytes(raw, "file")
        self.assertTrue(material.line_starts.readonly)
        self.assertEqual(material.line_starts.nbytes, 4 * (len(raw) + 1))
        self.assertEqual(material.line_of(len(raw) - 1), (len(raw), 0))
        with self.assertRaises(TypeError):
            material.line_starts[0] = 1
        plan = material_whole.WholePlan(material)
        for part in plan.parts:
            plan.record_note(part.index, "checked")
        with patch.object(plan, "covered_lines", side_effect=AssertionError("unbounded per-line set")):
            self.assertEqual(plan.confirm_sources([(1, len(raw))]), ([(1, len(raw))], []))

    def test_a_larger_synthesis_cannot_be_used_to_smuggle_more_file_fragments(self):
        material = materials.import_bytes(b"data\n", "file")
        plan = material_whole.WholePlan(material)
        value = plan.map_input(0, 1, material_step.TOTAL_BYTES)
        value["task"]["phase"] = "reduce"
        with self.assertRaises(ValueError):
            material_step.validate_input(value)

    def test_explicit_part_ceiling_refuses_without_recording_false_coverage(self):
        material = materials.import_bytes(b"x" * (1024 * 1024), "file")
        with patch.object(material_whole, "MAX_PARTS", 2), self.assertRaises(material_whole.WholeError):
            material_whole.WholePlan(material)


if __name__ == "__main__":
    unittest.main()
