"""Provider-owned UI follows the execution snapshot; user text stays literal."""
from contextlib import nullcontext, redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import assistant_flow, ask_cli, execution, material_cli, project_cli, task_cli
from ai_desktop.analysis_contract import AnalysisConsent, AnalysisDenied


QUESTION = "Wyjaśnij Claude i Codex w pliku Claude_Codex.txt."
ANSWER = "Nazwy Claude i Codex pozostają w odpowiedzi."
SOURCE = {"instruction": QUESTION, "files": [
    {"path": "Claude_Codex.txt", "content": "Claude oraz Codex", "writable": True}]}


def selection(provider):
    return {"provider": provider, "model": "default", "effort": "high", "instructions": "",
            "profile": "selected-profile", "access": "changes"}


class ProviderSurfaceTests(unittest.TestCase):
    def terminal(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("ai_desktop.terminal_consent.sys.stdin.isatty", return_value=True))
        stack.enter_context(patch("ai_desktop.terminal_consent.sys.stdout.isatty", return_value=True))
        return stack

    def test_plain_question_consent_and_wait_follow_selected_service_and_preserve_text(self):
        args = SimpleNamespace(no_context=True, preview=False, details=False,
                               systemd=None, systemd_state=None, weather=None)
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider), redirect_stdout(io.StringIO()) as output, \
                    execution.using(selection(provider)), self.terminal(), \
                    patch.object(assistant_flow.permissions, "load_policy", return_value=None), \
                    patch("subprocess.run") as run, patch("subprocess.Popen") as spawn, \
                    patch("socket.socket") as socket:
                prompts = []
                request = Mock(return_value=ANSWER)
                result = assistant_flow.run(QUESTION, args, lambda text: prompts.append(text) or "t", request)
            name = "Codex" if provider == "codex" else "Claude"
            self.assertEqual(result, ANSWER)
            self.assertEqual(request.call_args.args[0], QUESTION)
            self.assertEqual(len(prompts), 1)
            self.assertIn("żądanie do " + name + " [t/n]", prompts[0])
            self.assertIn("Czekam na odpowiedź od " + name + "…", output.getvalue())
            run.assert_not_called()
            spawn.assert_not_called()
            socket.assert_not_called()

    def test_standalone_ask_uses_same_provider_for_consent_wait_and_real_payload(self):
        args = SimpleNamespace(details=False, format="plain")
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider), redirect_stdout(io.StringIO()) as output, \
                    execution.using(selection(provider)), self.terminal(), \
                    patch.object(ask_cli, "installation_ready"), \
                    patch.object(ask_cli, "managed_session", return_value=nullcontext()), \
                    patch.object(ask_cli, "send_once", return_value=ANSWER) as send:
                prompts = []
                result = ask_cli.request(QUESTION, args, lambda prompt: prompts.append(prompt) or "t",
                                         context=[], check=lambda: None)
            name = "Codex" if provider == "codex" else "Claude"
            packet = json.loads(send.call_args.args[0].payload)
            self.assertEqual(packet["execution"]["provider"], provider)
            self.assertEqual(packet["question"], QUESTION)
            self.assertEqual(result, ANSWER)
            self.assertEqual(prompts, ["Wysłać do " + name + "? [t/n]: "])
            self.assertIn("Czekam na odpowiedź od " + name + "…", output.getvalue())

    def test_account_or_context_change_cannot_relabel_an_already_built_ask_gate(self):
        args = SimpleNamespace(details=False, format="plain")
        prompts = []
        # A deliberately stale controller context must refuse the transfer;
        # even this refusal must not relabel the already-built Codex consent.
        with execution.using(selection("codex")), self.terminal(), \
                patch.object(ask_cli, "installation_ready", side_effect=lambda: execution.select(selection("claude"))), \
                patch.object(ask_cli, "send_once") as send:
            with self.assertRaises(AnalysisDenied):
                ask_cli.request(QUESTION, args, lambda prompt: prompts.append(prompt) or "t",
                                context=[], check=lambda: None)
        self.assertEqual(prompts, ["Wysłać do Codex? [t/n]: "])
        send.assert_not_called()

    def test_task_preview_names_provider_without_rewriting_path_instruction_or_content(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider), execution.using(selection(provider)), \
                    patch.object(task_cli, "selection", return_value=(None, SOURCE)), \
                    patch.object(task_cli, "send_once") as send, redirect_stdout(io.StringIO()) as output:
                result = task_cli.run_task(SimpleNamespace(details=True, preview=True, test="text"), lambda _: "n")
            name = "Codex" if provider == "codex" else "Claude"
            self.assertEqual(result, 0)
            self.assertIn("=== WYBRANE DANE DO " + name.upper() + " ===", output.getvalue())
            self.assertIn(QUESTION, output.getvalue())
            self.assertIn("Claude_Codex.txt", output.getvalue())
            self.assertIn("Claude oraz Codex", output.getvalue())
            send.assert_not_called()

    def test_verbose_task_proposal_header_uses_provider_without_changing_summary(self):
        args = SimpleNamespace(details=True, format="plain", test="python")
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider), execution.using(selection(provider)), \
                    patch.object(task_cli.contract, "proposal", return_value={"summary": ANSWER, "files": []}), \
                    patch.object(task_cli.workspace, "unchanged"), \
                    patch.object(task_cli.task_sandbox, "run", return_value={"passed": False, "output": ""}), \
                    patch.object(task_cli.workspace, "apply") as apply, redirect_stdout(io.StringIO()) as output:
                result = task_cli.finish_task({}, SOURCE, "response", args, lambda _: "n")
            name = "Codex" if provider == "codex" else "Claude"
            self.assertEqual(result, 2)
            self.assertIn("=== PROPOZYCJA " + name.upper() + " ===", output.getvalue())
            self.assertIn(ANSWER, output.getvalue())
            apply.assert_not_called()

    def test_task_reply_and_failure_keep_the_provider_of_the_real_handoff(self):
        proposal = json.dumps({"summary": ANSWER, "files": [
            {"path": "Claude_Codex.txt", "content": "Nowa treść Claude i Codex."}]})
        argv = ["run", QUESTION, "--workspace", "/synthetic/project", "--file", "Claude_Codex.txt",
                "--test", "python-syntax", "--format", "plain", "--v"]
        for failure in (False, True):
            sent = []

            def sender(approval):
                sent.append(json.loads(approval.payload))
                execution.select(selection("claude"))
                if failure:
                    raise AnalysisDenied("runtime")
                return proposal

            with self.subTest(failure=failure), redirect_stdout(io.StringIO()) as output, \
                    execution.using(selection("codex")), self.terminal(), \
                    patch.object(task_cli.os, "geteuid", return_value=1000), \
                    patch.object(task_cli.os, "getuid", return_value=1000), \
                    patch.object(task_cli, "selection", return_value=({}, SOURCE)), \
                    patch.object(task_cli.workspace, "_can_apply", return_value=0), \
                    patch.object(task_cli.workspace, "unchanged"), \
                    patch.object(task_cli, "installation_ready"), \
                    patch.object(task_cli, "managed_session", return_value=nullcontext()), \
                    patch.object(task_cli, "send_once", side_effect=sender) as send, \
                    patch.object(task_cli.task_sandbox, "run", return_value={"passed": False, "output": ""}), \
                    patch.object(task_cli.workspace, "apply") as apply, \
                    patch("subprocess.run") as run, patch("subprocess.Popen") as spawn, \
                    patch("socket.socket") as socket:
                result = task_cli.main.__wrapped__(argv, reader=lambda _: "t")
            self.assertEqual(result, 2)
            send.assert_called_once()
            self.assertEqual(sent[0]["execution"]["provider"], "codex")
            self.assertEqual(sent[0]["task"], SOURCE)
            self.assertIn(QUESTION, output.getvalue())
            self.assertIn("Claude_Codex.txt", output.getvalue())
            if failure:
                self.assertIn("Brak zaufanego klienta Codex", output.getvalue())
                self.assertNotIn("Brak zaufanego klienta Claude", output.getvalue())
            else:
                self.assertIn("=== PROPOZYCJA CODEX ===", output.getvalue())
                self.assertNotIn("=== PROPOZYCJA CLAUDE ===", output.getvalue())
                self.assertIn(ANSWER, output.getvalue())
            apply.assert_not_called()
            run.assert_not_called()
            spawn.assert_not_called()
            socket.assert_not_called()

    def test_ask_failure_keeps_the_provider_of_the_real_handoff(self):
        sent = []

        def sender(approval):
            sent.append(json.loads(approval.payload))
            execution.select(selection("claude"))
            raise AnalysisDenied("runtime")

        with redirect_stdout(io.StringIO()) as output, execution.using(selection("codex")), self.terminal(), \
                patch.object(ask_cli.os, "geteuid", return_value=1000), \
                patch.object(ask_cli.os, "getuid", return_value=1000), \
                patch.object(assistant_flow.permissions, "load_policy", return_value=None), \
                patch("ai_desktop.conversation_sessions.load_session", return_value=None), \
                patch.object(ask_cli, "installation_ready"), \
                patch.object(ask_cli, "managed_session", return_value=nullcontext()), \
                patch.object(ask_cli, "send_once", side_effect=sender) as send, \
                patch("subprocess.run") as run, patch("subprocess.Popen") as spawn, \
                patch("socket.socket") as socket:
            result = ask_cli.main.__wrapped__(["--question", QUESTION, "--no-context", "--format", "plain"], reader=lambda _: "t")
        self.assertEqual(result, 2)
        send.assert_called_once()
        self.assertEqual(sent[0]["execution"]["provider"], "codex")
        self.assertEqual(sent[0]["question"], QUESTION)
        self.assertIn("Brak zaufanego klienta Codex", output.getvalue())
        self.assertNotIn("Brak zaufanego klienta Claude", output.getvalue())
        run.assert_not_called()
        spawn.assert_not_called()
        socket.assert_not_called()

    def test_material_failure_keeps_provider_after_the_real_handoff(self):
        from ai_desktop import material_loop, materials
        material = materials.import_bytes(b"Claude and Codex\n", "file")
        loop = material_loop.Loop(material)
        loop.preload_whole()
        payload = loop.request_input(1, material_loop.step.TOTAL_BYTES)
        controller = material_cli.Session.__new__(material_cli.Session)
        controller.args = SimpleNamespace(details=False, format="plain", max_requests=None)
        controller._handoffs = 0
        controller._whole_progress = None
        controller.pacer = Mock()
        controller.window = Mock(is_open=True, seconds_left=lambda: 100)
        controller._clock = lambda: 0
        ledger = Mock()
        sent = []

        def sender(approval):
            sent.append(json.loads(approval.payload))
            execution.select(selection("claude"))
            raise AnalysisDenied("runtime")

        controller._sender = sender
        with redirect_stdout(io.StringIO()) as output, execution.using(selection("codex")), \
                patch.object(material_cli, "installation_ready"), \
                patch("subprocess.run") as run, patch("subprocess.Popen") as spawn, \
                patch("socket.socket") as socket:
            gate = AnalysisConsent(None, question=QUESTION, material=payload)
            packet = SimpleNamespace(question=QUESTION, context=(), material=payload, payload=gate.payload)
            result = controller._deliver(ledger, packet)
        self.assertIsNone(result)
        self.assertEqual(controller._handoffs, 1)
        self.assertEqual(sent[0]["execution"]["provider"], "codex")
        self.assertEqual(sent[0]["question"], QUESTION)
        self.assertEqual(sent[0]["material"], payload)
        self.assertIn("Brak zaufanego klienta Codex", output.getvalue())
        self.assertNotIn("Brak zaufanego klienta Claude", output.getvalue())
        self.assertIn("Przekazanych danych nie można wycofać", output.getvalue())
        ledger.hand_off.assert_called_once_with(packet)
        ledger.record_unknown.assert_called_once_with(packet)
        ledger.cancel.assert_not_called()
        run.assert_not_called()
        spawn.assert_not_called()
        socket.assert_not_called()

    def test_material_and_project_local_help_follow_profile_without_network_or_model(self):
        for kind in (material_cli.Session, project_cli.ProjectSession):
            for provider in ("claude", "codex"):
                with self.subTest(kind=kind.__name__, provider=provider), execution.using(selection(provider)), \
                        patch("subprocess.run") as run, patch("subprocess.Popen") as spawn, \
                        patch("socket.socket") as socket, redirect_stdout(io.StringIO()) as output:
                    self.assertTrue(kind.__new__(kind)._dispatch("/pomoc"))
                name = "Codex" if provider == "codex" else "Claude"
                self.assertIn("przekazała do " + name, output.getvalue())
                if provider == "codex":
                    self.assertNotIn("Claude", output.getvalue())
                run.assert_not_called()
                spawn.assert_not_called()
                socket.assert_not_called()

    def test_command_help_is_neutral_when_no_task_provider_was_resolved(self):
        # Help intentionally bypasses profile resolution, so its instructions
        # must not assume that a Claude account is the selected destination.
        with execution.using(None):
            self.assertNotIn("Claude", ask_cli.build_parser().format_help())
            self.assertNotIn("Claude", material_cli.build_parser().format_help())
            with redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as done:
                    task_cli.build_parser().parse_args(["run", "--help"])
            self.assertEqual(done.exception.code, 0)
            self.assertNotIn("Claude", output.getvalue())
