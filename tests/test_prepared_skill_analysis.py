"""Prepared role instructions fit every whole-file hand-off; no client or OAuth."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import agent_profiles as profiles
from ai_desktop import analysis_child, analysis_contract as contract, execution
from ai_desktop import material_grant as grants, material_step, material_whole, materials, skills


class PreparedSkillWholeAnalysisTests(unittest.TestCase):
    def test_prepared_default_skill_completes_exact_four_mib_plan_with_both_part_sizes(self):
        with tempfile.TemporaryDirectory(prefix="aid-prepared-analysis-", dir=Path.home()) as temporary:
            home = Path(temporary)
            installed = home / "installed-skills"
            installed.mkdir(mode=0o755)
            for name in profiles.PRESET_SKILLS.values():
                directory = installed / name
                directory.mkdir(mode=0o755)
                path = directory / "SKILL.md"
                path.write_bytes((skills.ROOT / name / "SKILL.md").read_bytes())
                path.chmod(0o644)
            store = profiles.Store(home)
            with patch.object(skills, "ROOT", installed):
                store.mutate(profiles.prepared, expected=store.load())
                selected = profiles.resolve(store=store)
            self.assertIn("Skill concise-assistant:", selected["instructions"])
            self.assertEqual(store.load().config.profiles["default"].skills, ("concise-assistant",))

            line = b"synthetic whole-file material for budget verification".ljust(63, b".") + b"\n"
            raw = (line * ((4 * 1024 * 1024 + len(line) - 1) // len(line)))[:4 * 1024 * 1024]
            material = materials.import_bytes(raw, "file")
            question = "Przeanalizuj cały plik."
            for part_bytes in (240 * 1024, 512 * 1024):
                with self.subTest(part_bytes=part_bytes), execution.using(selected):
                    plan = material_whole.WholePlan(material, part_bytes=part_bytes)
                    stages = plan.stages()
                    self.assertGreater(len(stages), 1)
                    sent_requests = sent_dynamic_bytes = 0
                    delivered = []
                    for stage in stages:
                        grant = grants.MaterialGrant(material.material_id, material.sha256, question, 300,
                            max_requests=len(stage), max_bytes=plan.stage_budget(stage))
                        self.assertLessEqual(grant.max_requests, grants.MAX_REQUESTS)
                        self.assertLessEqual(grant.max_bytes, grants.MAX_TOTAL_BYTES)
                        session = grants.TransferSession(material, grant, clock=lambda: 0)
                        for number, (kind, index) in enumerate(stage, 1):
                            if kind == "map":
                                value = plan.map_input(index, number, session.bytes_left())
                                ranges = plan.parts[index].ranges()
                                delivered.extend(observation["text"].encode() for observation in value["observations"])
                            else:
                                value = plan.reduce_input(number, session.bytes_left())
                                ranges = ()
                            dynamic_bytes = sum(len(observation["text"].encode()) for observation in value["observations"])
                            self.assertEqual(dynamic_bytes, plan.item_bytes((kind, index)))
                            packet = session.prepare_step(question, value, dynamic_bytes, ranges)
                            gate = contract.AnalysisConsent(None, question=question, material=value)
                            approval = gate.authorize(None, question=question, material=value,
                                                      sha256=gate.sha256, nonce=gate.nonce)
                            self.assertEqual((packet.payload, packet.sha256), (approval.payload, approval.sha256))
                            self.assertEqual(json.loads(packet.payload)["execution"], selected)
                            # The complete serialized envelope includes fixed role
                            # instructions as well as the separately budgeted data.
                            ceiling = contract.MAX_PACKET_LARGE if kind == "map" else contract.MAX_PACKET
                            self.assertLessEqual(len(packet.payload.encode()), ceiling)
                            parsed, digest = contract.parse_request(packet.payload.encode())
                            self.assertEqual(digest, packet.sha256)
                            body, frozen = analysis_child.native_input("analyze", parsed.encode())
                            self.assertEqual(frozen, selected)
                            self.assertNotIn("execution", json.loads(body))
                            self.assertTrue(packet.verify())
                            session.hand_off(packet)
                            session.record_answer(packet)
                            if kind == "map":
                                plan.record_note(index, "n" * material_step.NOTE_BYTES)
                            sent_requests += 1
                            sent_dynamic_bytes += dynamic_bytes
                        self.assertEqual(session.requests_left(), 0)
                        self.assertEqual(session.bytes_left(), 0)
                        self.assertTrue(all(entry["state"] == "answered" for entry in session.ledger()))
                    self.assertEqual((sent_requests, sent_dynamic_bytes), plan.task_budget())
                    self.assertEqual(b"".join(delivered), raw)
                    self.assertTrue(plan.complete())
                    self.assertEqual(plan.covered_bytes(), 4 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
