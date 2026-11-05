"""Public controller integration without native clients, OAuth or host writes."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from contextlib import redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import agent_profiles, assistant_flow, execution, material_cli, project_cli, run_controls, task_cli, terminal_consent
from ai_desktop.analysis_contract import AnalysisConsent
from ai_desktop.materials import import_bytes
from ai_desktop.question_input import PromptSession


def reviewer():
    return {"provider": "claude", "model": "opus", "effort": "high", "instructions": "Przejrzyj kod.",
            "profile": "reviewer", "access": "read-only"}


class ManagementIntegrationTests(unittest.TestCase):
    def test_selected_controls_are_frozen_in_real_consent_and_reset_after_invocation(self):
        selected = reviewer()
        @run_controls.configured
        def entry(argv):
            self.assertEqual(argv, ["--question", "Sprawdź plik"])
            # Modifying the source dictionary cannot mutate the running task.
            selected["model"] = "haiku"
            gate = AnalysisConsent(None, question="Sprawdź plik", context=[])
            expected = json.loads(gate.payload)
            self.assertEqual(expected["execution"]["model"], "opus")
            self.assertEqual(expected["execution"]["access"], "read-only")
            gate.check(None, question="Sprawdź plik", context=[], sha256=gate.sha256, nonce=gate.nonce)
            return 0
        output = io.StringIO()
        with patch.object(agent_profiles, "resolve", return_value=selected) as resolve, redirect_stdout(output):
            code = entry(["--agent", "reviewer", "--model", "opus", "--effort", "high",
                          "--question", "Sprawdź plik"])
        self.assertEqual(code, 0)
        resolve.assert_called_once_with("reviewer", "opus", "high")
        self.assertIsNone(execution.current())
        self.assertIn("reviewer", output.getvalue())

    def test_default_settings_preserve_accepted_wire_without_execution_extension(self):
        selected = {"provider": "claude", "model": "default", "effort": "default", "instructions": "",
                    "profile": "default", "access": "changes"}
        @run_controls.configured
        def entry(argv):
            self.assertIsNone(execution.current())
            self.assertNotIn("execution", json.loads(AnalysisConsent(None, question="Pytanie", context=[]).payload))
            return 0
        with patch.object(agent_profiles, "resolve", return_value=selected):
            self.assertEqual(entry([]), 0)

    def test_local_recovery_status_remains_available_when_agent_configuration_is_broken(self):
        with patch.object(agent_profiles, "resolve", side_effect=agent_profiles.ProfileError("config")), \
                patch.object(task_cli.os, "geteuid", return_value=1000), \
                patch.object(task_cli.os, "getuid", return_value=1000), \
                patch.object(task_cli.workspace, "recovery_records", return_value=[]) as records, \
                patch("ai_desktop.workspace_trash.recovery_records", return_value=[]) as trash, \
                redirect_stdout(io.StringIO()):
            code = task_cli.main(["status", "--workspace", "/selected/project"])
        self.assertEqual(code, 0)
        records.assert_called_once_with("/selected/project")
        trash.assert_called_once_with("/selected/project")

    def test_readonly_project_rejects_model_edit_and_returns_to_prompt_without_execution(self):
        session = project_cli.ProjectSession.__new__(project_cli.ProjectSession)
        session.can_send = True
        session.args = SimpleNamespace(details=False)
        session.prompt_session = PromptSession()
        session._refresh = Mock(return_value=True)
        session._scope = Mock()
        session._open_section = Mock()
        session._close_section = Mock()
        session._timing = Mock()
        session.history, session.tasks = [], []
        session.progress = ""
        session.name = "project"
        session.project = Mock()
        session.binding = project_cli.Binding("a" * 32, "b" * 64)
        session._clock = lambda: 0
        session.reader = lambda _: "y"
        session.conversation = None
        model_answer = json.dumps({"action": "edit", "files": ["a.py"], "create": [], "check": "text"})
        session._deliver = Mock(return_value=SimpleNamespace(answer=model_answer))
        loop = Mock()
        loop.max_steps = 1
        loop.request_input.return_value = {}
        transfer = Mock()
        with execution.using(reviewer()), \
                patch.object(project_cli, "Loop", return_value=loop), \
                patch.object(project_cli, "TransferSession", return_value=transfer), \
                patch.object(project_cli, "confirm", return_value=True) as confirm, \
                patch.object(project_cli, "build_edit_plan") as edit_plan, \
                patch.object(project_cli.workspace_trash, "prepare") as delete_plan, \
                redirect_stdout(io.StringIO()):
            session.question("Zmień a.py")
        edit_plan.assert_not_called()
        delete_plan.assert_not_called()
        self.assertEqual(confirm.call_count, 1)  # Only read-task approval; no write approval is requested.
        session._close_section.assert_called()

    def test_saved_conversation_rejects_idle_configuration_change(self):
        output = io.StringIO()
        with execution.using(reviewer()), redirect_stdout(output):
            before = execution.current()
            self.assertTrue(run_controls.conversation_command("/model haiku", saved=True))
            self.assertEqual(execution.current(), before)
        self.assertIn("tied to its agent configuration", output.getvalue())

    def test_default_claude_conversation_cannot_change_destination_to_codex(self):
        codex = {**reviewer(), "provider": "codex", "model": "default", "profile": "codex-review"}
        with execution.using(None), patch.object(agent_profiles, "resolve", return_value=codex), \
                redirect_stdout(io.StringIO()):
            self.assertTrue(run_controls.conversation_command("/agent codex-review"))
            self.assertIsNone(execution.current())

    def test_readonly_task_refuses_before_workspace_snapshot_or_model_transport(self):
        arguments = SimpleNamespace(preview=True)
        with execution.using(reviewer()), patch.object(task_cli, "selection") as selection:
            with self.assertRaises(Exception) as caught:
                task_cli.run_task(arguments, lambda _: "y")
        self.assertEqual(type(caught.exception).__name__, "AnalysisDenied")
        selection.assert_not_called()

    def test_readonly_ask_has_valid_access_without_write_capability(self):
        with execution.using(reviewer()), patch.object(assistant_flow.permissions, "load_policy", return_value=None):
            snapshot = assistant_flow.capture_access()
        self.assertNotIn("write", snapshot.access)
        self.assertEqual(snapshot.access["files"], [])

    def test_readonly_ask_narrows_existing_write_policy_without_mutating_it(self):
        access = {"files": [{"id": "f0", "pattern": "~/project/**"}], "systemd": [], "weather": False,
                  "write": [{"id": "w0", "pattern": "~/project/**"}]}
        policy = Mock()
        policy.access.return_value = access
        policy.write_scopes.return_value = (SimpleNamespace(index=0, base="/", mode="tree"),)
        policy.sources.return_value = {"claude": True, "weather": False, "systemd": []}
        with execution.using(reviewer()), patch.object(assistant_flow.permissions, "load_policy", return_value=policy):
            snapshot = assistant_flow.capture_access()
        self.assertNotIn("write", snapshot.access)
        self.assertEqual(snapshot.access["files"], access["files"])
        self.assertEqual(len(access["write"]), 1)


class RecipientPresentationTests(unittest.TestCase):
    def codex(self):
        return {**reviewer(), "provider": "codex", "model": "default", "profile": "codex-review"}

    def test_transfer_prompt_names_actual_destination_and_default_is_unchanged(self):
        for selected, expected in ((None, terminal_consent.PROMPTS["project-edit"]),
                                   (self.codex(), "Send these files to Codex and prepare a diff? [y/n]: ")):
            prompts = []
            with execution.using(selected), \
                    patch.object(terminal_consent.sys.stdin, "isatty", return_value=True), \
                    patch.object(terminal_consent.sys.stdout, "isatty", return_value=True):
                self.assertTrue(terminal_consent.confirm("project-edit", reader=lambda prompt: prompts.append(prompt) or "y"))
            self.assertEqual(prompts, [expected])

    def test_material_preview_names_codex_and_preserves_filename_containing_claude(self):
        session = material_cli.Session.__new__(material_cli.Session)
        session.args = SimpleNamespace(details=False)
        session.name = "Claude_notes.txt"
        session.material = SimpleNamespace(size=1024)
        loop = SimpleNamespace(history=[], lines=12)
        output = io.StringIO()
        with execution.using(self.codex()), redirect_stdout(output):
            session._scope("Co robi Claude?", "loop", loop)
        text = output.getvalue()
        self.assertIn("To Codex:", text)
        self.assertIn("the parts Codex asks for", text)
        self.assertIn("Claude_notes.txt", text)
        self.assertNotIn("Codex_notes.txt", text)

    def test_whole_file_preview_names_codex_before_declined_transfer(self):
        session = material_cli.Session.__new__(material_cli.Session)
        session.material = import_bytes(b"example line\n" * 100, "file")
        session.args = SimpleNamespace(details=False)
        session.policy = SimpleNamespace(part_size=lambda: 240 * 1024)
        session.history = []
        session.reader = lambda _: "n"
        session._close_section = Mock()
        session._grants_possible = Mock(return_value=True)
        session._whole_result = Mock()
        output = io.StringIO()
        with execution.using(self.codex()), patch.object(material_cli, "confirm", return_value=False), redirect_stdout(output):
            session._whole("Przeanalizuj całość")
        self.assertIn("will be sent to Codex", output.getvalue())
        self.assertNotIn("will be sent to Claude", output.getvalue())

    def test_task_preview_preserves_user_instruction_and_path(self):
        source = {"instruction": "Popraw opis Claude w pliku.",
                  "files": [{"path": "Claude_notes.txt", "content": "Claude", "writable": True}]}
        selected = {**self.codex(), "access": "changes"}
        output = io.StringIO()
        with execution.using(selected), patch.object(task_cli, "selection", return_value=(None, source)), redirect_stdout(output):
            code = task_cli.run_task(SimpleNamespace(preview=True, details=False), lambda _: "n")
        self.assertEqual(code, 0)
        self.assertIn("To Codex: the instruction", output.getvalue())
        self.assertIn("Task: Popraw opis Claude w pliku.", output.getvalue())
        self.assertIn("Claude_notes.txt", output.getvalue())
        self.assertNotIn("Codex_notes.txt", output.getvalue())

    def test_model_answer_is_never_relabelled(self):
        session = material_cli.Session.__new__(material_cli.Session)
        session.args = SimpleNamespace(details=False, format="plain")
        session._close_section = Mock()
        session._save_answer = Mock()
        session.remember = Mock()
        loop = SimpleNamespace(confirm_sources=lambda _: ([], []), format_note=False)
        answer = "W pliku występuje nazwa Claude oraz nazwa Codex."
        output = io.StringIO()
        with execution.using(self.codex()), redirect_stdout(output):
            session._answer("Wymień nazwy", loop, {"sources": [], "text": answer})
        self.assertIn(answer, output.getvalue())
        session._save_answer.assert_called_once_with(answer)
        session.remember.assert_called_once_with("Wymień nazwy", answer)

    def test_verbose_task_uses_the_exact_consented_codex_recipient(self):
        source = {"instruction": "Popraw opis.",
                  "files": [{"path": "notes.txt", "content": "tekst", "writable": True}]}
        output = io.StringIO()
        with execution.using({**self.codex(), "access": "changes"}), \
                patch.object(task_cli, "selection", return_value=(None, source)), redirect_stdout(output):
            code = task_cli.run_task(SimpleNamespace(preview=True, details=True, test="text"), lambda _: "n")
        self.assertEqual(code, 0)
        self.assertIn("Recipient: OpenAI through the official Codex", output.getvalue())
        self.assertNotIn("Recipient: Anthropic", output.getvalue())


if __name__ == "__main__":
    unittest.main()
