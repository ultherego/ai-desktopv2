"""Real result packets reach the public ask controller; model prose stays private."""
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from ai_desktop import analysis_contract as contract, analysis_service as service
from ai_desktop import assistant_contract as planner, assistant_flow
import test_ask_cli as harness


ACCESS = {"files": [{"id": "f0", "pattern": "~/.config/**"}], "systemd": [], "weather": False}
FILES = {"kind": "files", "files": [{"path": "/home/example/.config/niri/config.kdl", "content": 'output "DP-1" {}\n'}]}
READ = {"action": "read", "files": [{"id": "f0", "path": "niri/config.kdl"}], "systemd": [], "weather": None}
QUESTION = "podaj mi mój config z niri/config.kdl o display"
PRIVATE = "PRIVATE_NATIVE_ERROR token=not-a-real-token path=/private/native"


class AskPreparationDiagnosticsTests(unittest.TestCase):
    # Reuse only the public CLI harness, not its test cases.
    run_ui = harness.AskCliTests.run_ui

    def run_native_plan(self, provider, raw, *, meta=None, verbose=False, collect_error=None):
        profile = {"provider": provider, "model": "default", "effort": "default", "instructions": "Diagnozuj.",
                   "profile": "diagnostic", "access": "read-only"}
        conn = MagicMock()
        conn.__enter__.return_value = conn
        observed, responses = [], []
        def native(approval):
            body = json.loads(approval.payload)
            mode = body["mode"]
            text = raw if mode == "assistant" else 'output "DP-1" {}'
            packet = contract.result_packet(approval.sha256, text=text, mode=mode, meta=meta)
            with patch.object(service, "receive_message", return_value=packet):
                result = service.send_once(approval)
            self.assertIs(type(result), str)
            self.assertEqual(service.send_once.last_meta, meta)
            observed.append(body)
            responses.append(result)
            return result
        with patch("test_ask_cli.DEFAULT_PROFILE", profile), \
                patch.object(service.socket, "socket", return_value=conn), \
                patch.object(service.e2_account, "account_ready", return_value=True), \
                patch.object(service, "manager_peer"), patch.object(service, "endpoint", return_value=(1, 2)), \
                patch.object(service, "packet", return_value=service.READY), \
                patch.object(service, "send_message") as sent:
            result = self.run_ui(["--v"] if verbose else [], answers=(QUESTION, "t"), send=native,
                                 access=ACCESS, context=[FILES], collect_source=collect_error)
        return result, observed, responses, sent

    def test_valid_native_read_plan_and_final_answer_work_for_both_providers(self):
        for provider in ("claude", "codex"):
            for with_meta in (False, True):
                meta = {"models": [{"model": "synthetic-model", "input": 10, "output": 20, "window": 1000}]} if with_meta else None
                with self.subTest(provider=provider, meta=with_meta):
                    result, requests, replies, sent = self.run_native_plan(provider, json.dumps(READ), meta=meta)
                    self.assertEqual(result.code, 0, result.output)
                    self.assertEqual([body["mode"] for body in requests], ["assistant", "question"])
                    self.assertEqual(requests[0]["access"], ACCESS)
                    self.assertEqual(requests[1]["context"], [FILES])
                    self.assertEqual({body["execution"]["provider"] for body in requests}, {provider})
                    self.assertEqual(sent.call_count, 2)
                    self.assertEqual(result.collect.call_count, 1)
                    self.assertIn('output "DP-1" {}', result.output)

    def test_invalid_model_plans_have_finite_reason_without_read_or_retry(self):
        cases = [
            # Plain prose is refused here (one-shot strictness is covered in test_ask_conversation); inside the interactive
            # conversation it is shown as an answer, so this case is not part of the interactive list.
            ("```json\n" + json.dumps(READ) + "\n```", "json"),
            ("[]", "shape"),
            (json.dumps({"action": "read", "files": ["f0"]}), "shape"),
            (json.dumps(dict(READ, files=[{"id": "f0", "path": "../private"}])), "files"),
            (json.dumps(dict(READ, files=["f63"])), "files"),
            (json.dumps({"action": PRIVATE}), "action"),
            ('{"action":"answer","text":"a","text":"' + PRIVATE + '"}', "json"),
            ('{"action":"answer","text":NaN}', "json"),
        ]
        for provider in ("claude", "codex"):
            for raw, check in cases:
                with self.subTest(provider=provider, check=check):
                    result, requests, replies, sent = self.run_native_plan(provider, raw)
                    self.assertEqual(result.code, 2)
                    self.assertIn("plan-" + check, result.output)
                    self.assertNotIn("Sprawdź treść", result.output)
                    self.assertNotIn(PRIVATE, result.output)
                    self.assertNotIn(raw, result.output)
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(sent.call_count, 1)
                    result.collect.assert_not_called()

    def test_verbose_model_plan_error_uses_closed_check_without_native_prose(self):
        result, _, _, _ = self.run_native_plan("claude", "{\"action\": " + PRIVATE, verbose=True)
        self.assertIn("ASK: REFUSED stage=plan check=json", result.output)
        self.assertNotIn(PRIVATE, result.output)

    def test_other_preparation_errors_report_only_closed_phase_and_class(self):
        for error, kind in ((ValueError(PRIVATE), "value"), (TypeError(PRIVATE), "type"),
                            (KeyError(PRIVATE), "key"), (OSError(PRIVATE), "os")):
            for verbose in (False, True):
                with self.subTest(kind=kind, verbose=verbose):
                    result, requests, _, sent = self.run_native_plan("claude", json.dumps(READ), verbose=verbose,
                        collect_error=Mock(side_effect=error))
                    self.assertEqual(result.code, 2)
                    self.assertIn("(prepare-collect)", result.output)
                    line = "ASK: REFUSED stage=collect check=prepare-" + kind
                    if verbose:
                        self.assertIn(line, result.output)
                    else:
                        self.assertNotIn("ASK: REFUSED", result.output)
                    self.assertNotIn(PRIVATE, result.output)
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(sent.call_count, 1)

    def test_forged_plan_error_attribute_cannot_be_printed(self):
        error = planner.PlanError(PRIVATE)
        self.assertEqual(error.check, "invalid")
        error.check = PRIVATE
        with patch.object(assistant_flow, "run", side_effect=error):
            result = self.run_ui()
        self.assertIn("plan-invalid", result.output)
        self.assertNotIn(PRIVATE, result.output)

    def test_phase_callback_never_exports_untrusted_values(self):
        def bad_flow(*args, phase, **kwargs):
            phase(PRIVATE)
            raise TypeError(PRIVATE)
        for verbose in (False, True):
            with self.subTest(verbose=verbose), patch.object(assistant_flow, "run", side_effect=bad_flow):
                result = self.run_ui(["--v"] if verbose else [])
            self.assertIn("(prepare-internal)", result.output)
            if verbose:
                self.assertIn("ASK: REFUSED stage=internal check=prepare-type", result.output)
            else:
                self.assertNotIn("ASK: REFUSED", result.output)
            self.assertNotIn(PRIVATE, result.output)
