"""Pure controller integration; filesystem executor and model are never run."""
from contextlib import nullcontext, redirect_stdout
import copy
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import ask_cli, ask_edit, assistant_flow, task_cli
from ai_desktop.analysis_contract import AnalysisConsent, AnalysisDenied
from ai_desktop.permissions import Scope
from ai_desktop.question_consent import QuestionConsent
from ai_desktop.terminal_consent import PROMPTS
from tests.profile_fixture import DEFAULT_PROFILE


WORKSPACE = "/home/synthetic/project"
QUESTION = "Napraw dodawanie i utwórz wynik."
ACCESS = {"files": [{"id": "f0", "pattern": WORKSPACE + "/**"}],
          "systemd": [], "weather": False,
          "write": [{"id": "w0", "pattern": WORKSPACE + "/*"}]}
PLAN = {"action": "edit", "write": "w0", "files": ["f0"], "create": ["result.txt"], "check": "text"}
ORIGINAL = "def add(a, b):\n    return a - b\n" + "# synthetic padding\n" * 460
PROPOSAL = {"summary": "Poprawiono dodawanie i dodano wynik.", "files": [
    {"path": "main.py", "content": ORIGINAL.replace("a - b", "a + b")},
    {"path": "result.txt", "content": "SYNTHETIC RESULT\n"}]}


class AskEditTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("ai_desktop.agent_profiles.resolve", return_value=DEFAULT_PROFILE))
        self.output = io.StringIO()
        self.output.isatty = lambda: True
        self.enterContext(redirect_stdout(self.output))
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("unexpected process")))
        self.policy = Mock()
        self.snapshot = assistant_flow.AccessSnapshot(self.policy,
            {"claude": True, "systemd": [], "weather": False}, copy.deepcopy(ACCESS))
        self.policy.access.return_value = copy.deepcopy(ACCESS)
        self.policy.scopes.return_value = (Scope(0, WORKSPACE + "/**", WORKSPACE, "tree"),)
        self.policy.write_scopes.return_value = (Scope(0, WORKSPACE + "/*", WORKSPACE, "children"),)
        self.policy.write_workspace.return_value = WORKSPACE
        self.paths = (WORKSPACE + "/main.py", WORKSPACE + "/tests/info.txt")
        self.policy.expand.return_value = self.paths
        self.policy.select.side_effect = lambda paths, limit=None: tuple(paths)
        self.policy.can_write.side_effect = lambda path, index=None: index == 0 and path.rsplit("/", 1)[0] == WORKSPACE
        def require_write(paths, *, index=None):
            if not paths or any(not self.policy.can_write(path, index=index) for path in paths):
                raise ValueError("files-write-denied")
            return tuple(paths)
        self.policy.require_write.side_effect = require_write
        contents = (ORIGINAL, "SYNTHETIC READONLY\n")
        self.bundle = {"kind": "files", "files": [{"path": path, "content": content}
                                                     for path, content in zip(self.paths, contents)]}
        def record(content, inode):
            return {"inode": [1, inode], "mode": 0o600, "sha256": "a" * 64, "content": content}
        self.before = {"workspace": WORKSPACE, "root": [1, 2], "files": {
            "main.py": {"parent": [1, 2], "file": record(ORIGINAL, 3)},
            "tests/info.txt": {"parent": [1, 4], "file": record(contents[1], 5)},
            "result.txt": {"parent": [1, 2], "file": None}}}
        def snapshot(workspace, names, *, must_be_absent=()):
            self.assertEqual(workspace, WORKSPACE)
            if any(self.before["files"][name]["file"] is not None for name in must_be_absent):
                raise ValueError("creation target exists")
            return dict(self.before, files={name: copy.deepcopy(self.before["files"][name]) for name in names})
        self.capture = self.enterContext(patch.object(ask_edit.task_workspace, "snapshot", side_effect=snapshot))
        self.unchanged = self.enterContext(patch.object(ask_edit.task_workspace, "unchanged"))
        self.capacity = self.enterContext(patch.object(ask_edit.task_workspace, "_can_apply"))
        self.read = self.enterContext(patch.object(ask_edit.permissions, "read_selected", return_value=self.bundle))
        self.now = [0]
        self.permit = QuestionConsent(QUESTION, self.snapshot, clock=lambda: self.now[0])
        self.permit.approve()
        self.args = SimpleNamespace(details=False, format="plain")

    def prepare(self, plan=PLAN):
        return ask_edit.prepare(copy.deepcopy(plan), self.snapshot, self.args, Mock(), question_consent=self.permit)

    def planner(self):
        gate = AnalysisConsent(None, question=QUESTION, access=self.snapshot.access)
        self.permit.claim(gate, QUESTION, access=self.snapshot.access)
        self.permit.authorize(gate, QUESTION, access=self.snapshot.access)

    def test_read_and_create_are_bound_to_one_workspace_with_readonly_context(self):
        prepared = self.prepare()
        self.assertEqual(prepared.workspace, WORKSPACE)
        self.assertEqual(prepared.source["instruction"], QUESTION)
        self.assertEqual([(item["path"], item["writable"]) for item in prepared.source["files"]],
                         [("main.py", True), ("tests/info.txt", False), ("result.txt", True)])
        self.assertGreater(len(prepared.source["files"][0]["content"].encode()), 4096)
        self.assertEqual(prepared.source["files"][0]["content"], ORIGINAL)
        self.assertIsNone(prepared.source["files"][2]["content"])
        self.capture.assert_called_once_with(WORKSPACE, ["main.py", "tests/info.txt", "result.txt"],
                                             must_be_absent=["result.txt"])
        self.capacity.assert_called_once_with(WORKSPACE)

    def test_outside_workspace_read_scope_is_refused_before_content_read(self):
        self.policy.expand.return_value = ("/home/synthetic/other/info.txt",)
        with self.assertRaises(ask_edit.EditDenied):
            self.prepare()
        self.read.assert_not_called()
        self.capture.assert_not_called()

    def test_narrow_selection_uses_only_bound_subpath_not_entire_scope(self):
        self.policy.expand_selected.return_value = self.paths
        plan = dict(PLAN, files=[{"id": "f0", "path": "main.py"},
                                 {"id": "f0", "path": "tests/info.txt"}])
        prepared = self.prepare(plan)
        self.policy.expand.assert_not_called()
        self.policy.expand_selected.assert_called_once_with(((0, "main.py"), (0, "tests/info.txt")))
        self.read.assert_called_once_with(self.policy, self.paths)
        self.assertEqual(prepared.write_grant._read_paths, self.paths)
        prepared.write_grant.check()

    def test_narrow_selection_denial_prevents_read_snapshot_and_transfer(self):
        self.policy.expand_selected.side_effect = ValueError("files-denied")
        with self.assertRaises(ask_edit.EditDenied):
            self.prepare(dict(PLAN, files=[{"id": "f0", "path": "private/**"}]))
        self.policy.expand.assert_not_called()
        self.read.assert_not_called()
        self.capture.assert_not_called()

    def test_narrow_selected_path_outside_write_workspace_is_not_read(self):
        self.policy.expand_selected.return_value = ("/home/synthetic/other/secret.txt",)
        with self.assertRaises(ask_edit.EditDenied):
            self.prepare(dict(PLAN, files=[{"id": "f0", "path": "main.py"}]))
        self.read.assert_not_called()
        self.capture.assert_not_called()

    def test_create_only_does_not_open_read_scopes_and_requires_absence(self):
        prepared = self.prepare(dict(PLAN, files=[]))
        self.policy.expand.assert_not_called()
        self.read.assert_not_called()
        self.assertEqual(prepared.source["files"], [{"path": "result.txt", "content": None, "writable": True}])
        self.capture.assert_called_once_with(WORKSPACE, ["result.txt"], must_be_absent=["result.txt"])

    def test_existing_creation_target_is_refused_without_reading_its_content(self):
        self.before["files"]["result.txt"]["file"] = {"content": "PRIVATE PREEXISTING"}
        with self.assertRaises(ask_edit.EditDenied):
            self.prepare(dict(PLAN, files=[]))
        self.read.assert_not_called()
        self.assertNotIn("PRIVATE PREEXISTING", self.output.getvalue())

    def test_unadvertised_scope_and_new_path_outside_selected_scope_are_refused(self):
        with self.assertRaises(ask_edit.EditDenied):
            self.prepare(dict(PLAN, write="w1"))
        self.policy.write_workspace.assert_not_called()
        self.read.assert_not_called()
        with self.assertRaises(ask_edit.EditDenied):
            self.prepare(dict(PLAN, files=[], create=["tests/new.txt"]))
        self.capture.assert_not_called()

    def test_source_snapshot_and_captured_policy_mutations_revoke_write_grant(self):
        prepared = self.prepare()
        mutations = (
            lambda: prepared.source["files"][0].update(content="MUTATED"),
            lambda: prepared.before.update(root=[9, 9]),
            lambda: self.snapshot.access["write"][0].update(pattern="/other/*"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                original_source = copy.deepcopy(prepared.source)
                original_before = copy.deepcopy(prepared.before)
                original_access = copy.deepcopy(self.snapshot.access)
                mutate()
                with self.assertRaises(ask_edit.EditDenied):
                    prepared.write_grant.check()
                prepared.source.clear(); prepared.source.update(original_source)
                prepared.before.clear(); prepared.before.update(original_before)
                self.snapshot.access.clear(); self.snapshot.access.update(original_access)
        self.unchanged.side_effect = ValueError("PRIVATE CHANGED FILE")
        with self.assertRaises(ask_edit.EditDenied):
            prepared.write_grant.check()

    def test_completed_question_cannot_transfer_again_but_write_review_has_separate_ttl(self):
        prepared = self.prepare()
        self.now[0] = 181
        self.permit.close()
        prepared.write_grant.check()
        with self.assertRaises(AnalysisDenied):
            prepared.write_grant.require_transfer(self.permit, prepared.source)
        self.policy.recheck.side_effect = ValueError("PRIVATE REVOKED POLICY")
        with self.assertRaises(ask_edit.EditDenied):
            prepared.write_grant.check()

    def test_grant_freezes_scope_index_read_paths_and_write_targets(self):
        prepared = self.prepare()
        grant = prepared.write_grant
        for field, value in (("_index", 1), ("_read_paths", (WORKSPACE + "/other.txt",)),
                             ("_targets", (WORKSPACE + "/other.txt",))):
            with self.subTest(field=field):
                original = getattr(grant, field)
                setattr(grant, field, value)
                with self.assertRaises(ask_edit.EditDenied):
                    grant.check()
                setattr(grant, field, original)

    def test_finish_maps_executor_and_recursive_json_failures_without_raw_details(self):
        prepared = self.prepare()
        for error in (ask_edit.ExecutionDenied("PRIVATE EXECUTOR"), RecursionError("PRIVATE JSON")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(task_cli, "finish_task", side_effect=error), self.assertRaises(ask_edit.EditDenied) as refused:
                ask_edit.finish(prepared, "untrusted", self.args, Mock())
            self.assertNotIn("PRIVATE", str(refused.exception))

    def test_workspace_is_second_exact_payload_and_cannot_replay_or_change_instruction(self):
        self.planner()
        prepared = self.prepare()
        gate = AnalysisConsent(None, task=prepared.source)
        kwargs = {"task": prepared.source, "write_grant": prepared.write_grant}
        self.permit.claim(gate, QUESTION, **kwargs)
        approval = self.permit.authorize(gate, QUESTION, **kwargs)
        self.assertEqual(json.loads(approval.payload)["task"], prepared.source)
        with self.assertRaises(AnalysisDenied):
            self.permit.claim(AnalysisConsent(None, task=prepared.source), QUESTION, **kwargs)

    def run_ask(self, answer="tak", *, revoke_write=False, proposal=None):
        reader = Mock()
        values = iter((QUESTION, "tak", answer))
        asked = []
        def respond(prompt):
            if prompt == ask_cli.QUESTION_PROMPT and asked:
                return ""  # an empty line ends the interactive conversation
            asked.append(prompt) if prompt == ask_cli.QUESTION_PROMPT else None
            if prompt == PROMPTS["write"] and revoke_write:
                self.policy.recheck.side_effect = ValueError("PRIVATE REVOKED POLICY")
            return next(values)
        reader.side_effect = respond
        modes = []
        def send(approval):
            payload = json.loads(approval.payload)
            if payload["mode"] != "assistant":
                modes.append(payload["mode"])
                return json.dumps(proposal or PROPOSAL)
            # After an applied change the model is asked for the next step; here the task is done.
            return json.dumps({"action": "answer", "text": "Gotowe."} if modes else PLAN)
        with patch.object(ask_cli.os, "getuid", return_value=1000), \
                patch.object(ask_cli.os, "geteuid", return_value=1000), \
                patch.object(ask_cli.sys.stdin, "isatty", return_value=True), \
                patch.object(assistant_flow, "capture_access", return_value=self.snapshot), \
                patch.object(ask_cli, "installation_ready"), \
                patch.object(ask_cli, "managed_session", side_effect=lambda **_: nullcontext()), \
                patch.object(ask_cli, "send_once", side_effect=send) as sent, \
                patch.object(task_cli.task_sandbox, "run", return_value={"passed": True, "output": "SYNTHETIC", "check": "text"}) as test, \
                patch.object(task_cli.workspace, "apply", side_effect=lambda plan, auth, **kwargs: plan.consume(auth)) as apply:
            code = ask_cli.main([], reader=reader)
        return SimpleNamespace(code=code, reader=reader, sent=sent, test=test, apply=apply)

    def test_ask_edit_has_one_question_approval_two_requests_and_separate_write_approval(self):
        result = self.run_ask()
        self.assertEqual(result.code, 0, self.output.getvalue())
        self.assertEqual([call.args[0] for call in result.reader.call_args_list],
                         [ask_cli.QUESTION_PROMPT, PROMPTS["ask-question"], PROMPTS["write"], ask_cli.QUESTION_PROMPT])
        self.assertEqual(result.sent.call_count, 3)
        self.assertEqual([json.loads(call.args[0].payload)["mode"] for call in result.sent.call_args_list],
                         ["assistant", "workspace", "assistant"])
        result.test.assert_called_once()
        result.apply.assert_called_once()
        self.assertTrue(callable(result.apply.call_args.kwargs["write_guard"]))
        self.assertIn("Zapisano zmiany.", self.output.getvalue())
        self.assertIn("+    return a + b", self.output.getvalue())
        self.assertIn("+SYNTHETIC RESULT", self.output.getvalue())
        self.assertIn("Gotowe.", self.output.getvalue())                     # the model closes the task after the write

    def test_ask_proposal_identical_to_the_file_has_its_own_message_and_no_write(self):
        same = {"summary": "bez zmian", "files": [{"path": "main.py", "content": ORIGINAL}]}
        result = self.run_ask(proposal=same)
        self.assertEqual(result.code, 2)
        self.assertIn("Nie zaproponowano zmiany: plik ma już taką treść. Nic nie zapisano.", self.output.getvalue())
        self.assertNotIn(PROMPTS["write"], [call.args[0] for call in result.reader.call_args_list])
        result.test.assert_not_called()
        result.apply.assert_not_called()

    def test_write_decline_or_revocation_never_reaches_apply(self):
        for answer, revoked in (("nie", False), ("tak", True)):
            with self.subTest(answer=answer, revoked=revoked):
                result = self.run_ask(answer, revoke_write=revoked)
                self.assertEqual(result.code, 2)
                self.assertEqual(result.sent.call_count, 2)
                result.test.assert_called_once()
                result.apply.assert_not_called()
                self.assertNotIn("PRIVATE REVOKED POLICY", self.output.getvalue())
                self.policy.recheck.side_effect = None


if __name__ == "__main__":
    unittest.main()
