"""The fixed whole-material plan carries knowledge loaded before its consent."""
import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import material_cli, materials, material_whole, skill_runtime
import test_shared_skill_loader as fixtures


class WholeMaterialKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedLoaderTests("test_catalog_and_selected_reference_do_not_send_unrelated_bodies")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_preloaded_knowledge_reaches_map_and_reduce_without_extra_handoffs(self):
        data = materials.import_bytes(b"synthetic content\n", "file")
        policy = SimpleNamespace(sources=lambda: {"claude": True})
        owner = material_cli.Session(data, "synthetic", policy,
            SimpleNamespace(details=False, format="plain"), lambda unused: "yes", clock=lambda: 0)
        plan = material_whole.WholePlan(data)
        sent = []
        responses = iter(['{"action":"notes","text":"verified synthetic finding"}',
                          '{"action":"answer","text":"complete","sources":[]}'])
        with skill_runtime.task_scope():
            skill_runtime.accept_skill_response('{"action":"skill","names":["procedure","procedure/manual"]}')
            prepared = skill_runtime.current_knowledge()
            with patch.object(owner, "_deliver", self.fixture._transfer(owner, sent, responses)), \
                    contextlib.redirect_stdout(io.StringIO()):
                answer = owner._whole_stage("task", plan, [("map", 0), ("reduce", 0)], 100)
        self.assertEqual(answer["text"], "complete")
        self.assertEqual([item["material"]["task"]["phase"] for item in sent], ["map", "reduce"])
        self.assertEqual(len(sent), 2)
        self.assertEqual(owner.tasks[-1]["session"].requests_handed_off(), 2)
        self.assertTrue(plan.complete())
        self.assertEqual(sent[0]["knowledge"], prepared)
        self.assertEqual(sent[1]["knowledge"], prepared)
        self.assertIn("REFERENCE-CONTENT", prepared["resources"][1]["text"])


if __name__ == "__main__":
    unittest.main()
