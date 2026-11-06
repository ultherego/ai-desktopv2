"""Real request/consent/dispatch paths with synthetic Skills and no model or host operations."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import agent_profiles, analysis_contract as contract, execution, material_cli, material_grant
from ai_desktop import ask_cli
from ai_desktop import material_loop, materials, project_cli, project_step, skills, skill_runtime as runtime, task_cli


class SharedLoaderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(skills, "ROOT", self.root))
        self.enterContext(patch.object(skills, "user_root", return_value=self.root / "missing"))
        for name in ("procedure", "unrelated"):
            directory = self.root / name
            directory.mkdir()
            (directory / "SKILL.md").write_text("---\nname: " + name + "\ndescription: " + name + " task\n---\nBODY-" + name)
        refs = self.root / "procedure/references"
        refs.mkdir()
        (refs / "manual.md").write_text(
            "---\nname: manual\ndescription: Tool flags\ntools: pacman\nsource: https://wiki.archlinux.org/title/Pacman\n"
            "section: Usage\nchecked: 2026-11-06\nversion: rolling\n---\nREFERENCE-CONTENT")
        self.profile = agent_profiles.Profile(skills=("procedure", "unrelated"))
        self.config = {"provider": "claude", "model": "default", "effort": "default", "access": "changes",
                       "profile": "default", "instructions": agent_profiles.catalog_for(self.profile)}
        self.store = SimpleNamespace(load=lambda: SimpleNamespace(config=agent_profiles.defaults().put("default", self.profile)))
        self.enterContext(patch.object(agent_profiles, "Store", return_value=self.store))
        self.enterContext(execution.using(self.config))
        self.enterContext(runtime.using_knowledge(None))

    def test_catalog_and_selected_reference_do_not_send_unrelated_bodies(self):
        self.assertNotIn("BODY-", self.config["instructions"])
        self.assertIn("procedure/manual", self.config["instructions"])
        with runtime.task_scope():
            self.assertTrue(runtime.accept_skill_response('{"action":"skill","names":["procedure"]}'))
            self.assertTrue(runtime.accept_skill_response('{"action":"skill","names":["procedure/manual"]}'))
            wire = contract.application_input(None, question="synthetic", context=[])
            parsed, _ = contract.parse_request(wire.encode())
            text = json.loads(parsed)["knowledge"]["resources"]
            self.assertEqual([row["name"] for row in text], ["procedure", "procedure/manual"])
            self.assertIn("REFERENCE-CONTENT", text[1]["text"])
            self.assertNotIn("BODY-unrelated", wire)
        self.assertIsNone(runtime.current_knowledge())

    def test_unassigned_path_and_duplicate_requests_cannot_load_data(self):
        with runtime.task_scope():
            runtime.accept_skill_response('{"action":"skill","names":["absent/manual"]}')
            self.assertEqual(runtime.current_knowledge()["resources"][0]["status"], "missing")
            for names in (["../secret"], ["procedure", "procedure"], ["absent/manual"]):
                with self.subTest(names=names), self.assertRaises(ValueError):
                    runtime.accept_skill_response(json.dumps({"action": "skill", "names": names}))

    def test_changed_reference_or_removed_assignment_invalidates_transfer(self):
        for change in ("reference", "assignment"):
            with self.subTest(change=change), runtime.task_scope():
                check = runtime.recheck_with(lambda: None)
                if change == "reference":
                    path = self.root / "procedure/references/manual.md"
                    before = path.read_text()
                    path.write_text(before + "\nchanged")
                else:
                    original = self.profile
                    self.profile = agent_profiles.Profile(skills=())
                try:
                    with self.assertRaises(ValueError):
                        check()
                finally:
                    if change == "reference":
                        path.write_text(before)
                    else:
                        self.profile = original

    def test_reference_symlink_hardlink_and_unsafe_mode_are_rejected(self):
        path = self.root / "procedure/references/manual.md"
        source = path.read_bytes()
        outside = self.root / "outside.md"
        outside.write_bytes(source)
        for kind in ("symlink", "hardlink", "mode"):
            path.unlink()
            if kind == "symlink":
                path.symlink_to(outside)
            elif kind == "hardlink":
                os.link(outside, path)
            else:
                path.write_bytes(source)
                path.chmod(0o666)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                skills.bundle("procedure")

    def test_knowledge_tampering_and_budget_are_refused(self):
        with runtime.task_scope():
            runtime.accept_skill_response('{"action":"skill","names":["procedure"]}')
            original = runtime.current_knowledge()
            for replacement in ("changed", "x" * (skills.MAX_SKILL_BYTES + 1)):
                changed = json.loads(json.dumps(original))
                changed["resources"][0]["text"] = replacement
                with self.assertRaises(ValueError):
                    runtime.validate_knowledge(changed)

    def test_project_edit_handoff_rechecks_loaded_knowledge_before_transport(self):
        with runtime.task_scope():
            runtime.accept_skill_response('{"action":"skill","names":["procedure/manual"]}')
            path = self.root / "procedure/references/manual.md"
            path.write_text(path.read_text() + "\nchanged after analysis")
            with patch.object(ask_cli, "send_once") as sender, self.assertRaises(ValueError):
                ask_cli.request("fix", SimpleNamespace(details=False), lambda unused: "yes", context=[], check=lambda: None)
            sender.assert_not_called()

    def _transfer(self, owner, sent, responses):
        def deliver(session, packet, **kwargs):
            gate = contract.AnalysisConsent(None, source_check=runtime.recheck_with(lambda: None), **owner._gate_kwargs(packet))
            self.assertEqual(gate.payload, packet.payload)
            approval = gate.authorize(None, sha256=gate.sha256, nonce=gate.nonce, **owner._gate_kwargs(packet))
            approval.recheck_sources()
            contract.parse_request(approval.payload.encode())
            session.hand_off(packet)
            sent.append(json.loads(approval.payload))
            session.record_answer(packet)
            return material_cli.Delivery(next(responses), False, 0, 0, None)
        return deliver

    def test_material_loop_intercepts_skills_and_reference_before_answer(self):
        data = materials.import_bytes(b"synthetic\n", "file")
        args = SimpleNamespace(details=False, format="plain", only_whole=False)
        policy = SimpleNamespace(sources=lambda: {"claude": True})
        owner = material_cli.Session(data, "synthetic", policy, args, lambda unused: "yes")
        loop = material_loop.Loop(data)
        loop.preload_whole()
        session = material_grant.TransferSession(data, material_grant.MaterialGrant(data.material_id, data.sha256, "task", 10), clock=lambda: 0)
        replies = iter(['{"action":"skill","names":["procedure"]}',
                        '{"action":"skill","names":["procedure/manual"]}',
                        '{"action":"answer","text":"done","sources":[]}'])
        sent = []
        with runtime.task_scope(), patch.object(owner, "_deliver", self._transfer(owner, sent, replies)), \
                patch.object(owner, "_answer", return_value="done") as answered, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(owner._run("task", loop, session, "all"), "done")
        self.assertEqual(len(sent), 3)
        self.assertNotIn("knowledge", sent[0])
        self.assertEqual(len(sent[2]["knowledge"]["resources"]), 2)
        self.assertEqual(session.requests_handed_off(), 3)
        answered.assert_called_once()

    def test_range_consent_does_not_expand_after_loading_skills(self):
        data = materials.import_bytes(b"selected\nnot-approved\n", "file")
        owner = material_cli.Session(data, "synthetic", SimpleNamespace(sources=lambda: {"claude": True}),
                                     SimpleNamespace(details=False, format="plain", only_whole=False), lambda unused: "yes")
        loop = material_loop.Loop(data)
        loop.preload_range(1, 1)
        session = material_grant.TransferSession(data, material_grant.MaterialGrant(data.material_id, data.sha256, "task", 10), clock=lambda: 0)
        replies = iter(['{"action":"skill","names":["procedure"]}', '{"action":"read","ranges":[{"first":2,"last":2}]}'])
        sent = []
        with runtime.task_scope(), patch.object(owner, "_deliver", self._transfer(owner, sent, replies)), \
                patch.object(owner, "_stopped", return_value="stopped"), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(owner._run("only first line", loop, session, "range"), "stopped")
        self.assertIn("Additional material reads were refused", output.getvalue())
        self.assertEqual(len(sent), 2)
        self.assertNotIn("not-approved", json.dumps(sent))

    def test_project_loop_intercepts_skills_with_the_same_counted_grant(self):
        owner = object.__new__(project_cli.ProjectSession)
        owner.args = SimpleNamespace(details=False)
        binding = project_cli.Binding("a" * 16, "b" * 64)
        shape = {"project": {"id": "a" * 16, "files": 0, "complete": True, "progress": "", "stage": 1,
                              "visited": [], "skipped": {k: 0 for k in project_step.SKIPPED_KEYS}},
                 "task": {"step": 1, "max_steps": 8, "bytes_left": 10000}, "observations": [], "history": []}
        loop = SimpleNamespace(max_steps=8, request_input=lambda number, left: {**shape, "task": {"step": number, "max_steps": 8, "bytes_left": left}},
                               data_bytes=lambda: 0, mark_handed_off=lambda value: None)
        session = material_grant.TransferSession(binding, material_grant.MaterialGrant(binding.material_id, binding.sha256, "task", 10), clock=lambda: 0)
        sent = []
        replies = iter(['{"action":"skill","names":["procedure/manual"]}', '{"action":"answer","text":"done","sources":[]}'])
        with runtime.task_scope(), patch.object(owner, "_deliver", self._transfer(owner, sent, replies)), \
                patch.object(owner, "_answer", return_value="done") as answered:
            self.assertEqual(owner._run("task", loop, session), "done")
        self.assertEqual(len(sent), 2)
        self.assertIn("REFERENCE-CONTENT", sent[1]["knowledge"]["resources"][0]["text"])
        self.assertEqual(session.requests_handed_off(), 2)
        answered.assert_called_once()

    def test_standalone_task_loads_on_demand_under_one_transfer_consent(self):
        source = {"instruction": "fix", "files": [{"path": "test.txt", "content": "old", "writable": True}]}
        args = SimpleNamespace(preview=False, workspace="/synthetic", details=False, format="plain")
        sent = []
        responses = iter(['{"action":"skill","names":["procedure"]}', '{"summary":"ok","files":[{"path":"test.txt","content":"new"}]}'])
        def send(approval):
            approval.recheck_sources()
            contract.parse_request(approval.payload.encode())
            sent.append(json.loads(approval.payload))
            return next(responses)
        with patch.object(task_cli, "selection", return_value=({}, source)), patch.object(task_cli.workspace, "_can_apply", return_value=None), \
                patch.object(task_cli.workspace, "unchanged"), patch.object(task_cli, "installation_ready"), \
                patch.object(task_cli, "managed_session", return_value=contextlib.nullcontext()), \
                patch.object(task_cli, "confirm", return_value=True) as confirm, patch.object(task_cli, "send_once", side_effect=send), \
                patch.object(task_cli, "finish_task", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(task_cli.run_task(args, lambda unused: "yes"), 0)
        confirm.assert_called_once()
        self.assertEqual(len(sent), 2)
        self.assertNotIn("knowledge", sent[0])
        self.assertEqual(sent[1]["knowledge"]["resources"][0]["text"], skills.bundle("procedure")["text"])
        self.assertNotIn("BODY-unrelated", json.dumps(sent))


if __name__ == "__main__":
    unittest.main()
