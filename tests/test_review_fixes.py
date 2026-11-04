"""Regressions found by the repository review: long tasks, service failures and bounded observations."""
import unittest
from unittest.mock import patch

from ai_desktop import analysis_contract, codex_protocol, material_loop, material_step, project_loop, project_step


class TimeoutTests(unittest.TestCase):
    def test_codex_accepts_the_time_budget_it_is_given_for_slow_reasoning(self):
        self.assertLessEqual(analysis_contract.request_seconds("xhigh"), analysis_contract.REQUEST_SECONDS_LONG)
        self.assertEqual(codex_protocol.REQUEST_SECONDS_LONG, analysis_contract.REQUEST_SECONDS_LONG)


class ObservationTests(unittest.TestCase):
    def fill(self, loop, module, make):
        loop.observations = [make(index) for index in range(module.MAX_OBSERVATIONS + 2)]
        with patch.object(type(loop), "data_bytes", return_value=0):      # only the entry count is over its limit here
            loop._compact()

    def test_material_one_surplus_entry_does_not_wipe_every_read(self):
        loop = material_loop.Loop.__new__(material_loop.Loop)
        make = lambda i: {"kind": "read", "first": i + 1, "last": i + 1, "text": "x"}
        self.fill(loop, material_step, make)
        self.assertLessEqual(len(loop.observations), material_step.MAX_OBSERVATIONS - 4)
        self.assertTrue(all(item["kind"] == "read" for item in loop.observations))

    def test_project_one_surplus_entry_does_not_wipe_every_read(self):
        loop = project_loop.Loop.__new__(project_loop.Loop)
        make = lambda i: {"kind": "read", "path": "a.py", "first": i + 1, "last": i + 1, "text": "x"}
        self.fill(loop, project_step, make)
        self.assertLessEqual(len(loop.observations), project_step.MAX_OBSERVATIONS - 4)
        self.assertTrue(any(item["kind"] == "read" for item in loop.observations))


if __name__ == "__main__":
    unittest.main()
