"""No OAuth or network: consent mutation, bounded results and terminal safety."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from ai_desktop import analysis_contract as contract
from ai_desktop import task_contract
from ai_desktop import file_context_contract
from test_report_policy import report_fixture


def sized_file_context(encoded_bytes):
    """Pure synthetic context with an exact JSON size and legal raw content."""
    context = [{"kind": "files", "files": [{"path": "/synthetic/config.txt", "content": ""}]}]
    remaining = encoded_bytes - len(contract.canonical(context).encode("utf-8"))
    units, plain = divmod(remaining, 2)
    escaped = '"\\\n\t'
    context[0]["files"][0]["content"] = escaped * (units // len(escaped)) + escaped[:units % len(escaped)] + "x" * plain
    return context


class AnalysisContractTests(unittest.TestCase):
    def test_full_application_input_is_bound_and_strictly_decoded(self):
        report = report_fixture()
        gate = contract.AnalysisConsent(report)
        auth = gate.authorize(report, sha256=gate.sha256, nonce=gate.nonce)
        data, sha = contract.parse_request(auth.payload.encode())
        self.assertEqual(json.loads(data), {"mode": "analysis", "report": report, "question": ""})
        self.assertEqual(sha, auth.sha256)
        for key in ("client_context", "system_prompt", "client_version", "policy", "schema"):
            changed = json.loads(auth.payload)
            changed[key] = "changed"
            with self.subTest(key=key), self.assertRaises(contract.AnalysisDenied):
                contract.parse_request(contract.canonical(changed).encode())
        for raw in (b'[]', b'{}', b'{"report":1,"report":2}', b'null', b'x' * 32769):
            with self.assertRaises(contract.AnalysisDenied):
                contract.parse_request(raw)

    def test_web_wire_contains_only_original_question_and_separate_bound_prompt(self):
        question = "Sprawdź bieżące wiadomości i podaj źródła."
        gate = contract.AnalysisConsent(None, question=question, web=True)
        self.assertEqual(json.loads(gate.payload)["system_prompt"], contract.WEB_SYSTEM_PROMPT)
        self.assertNotEqual(gate.sha256, contract.AnalysisConsent(None, question=question, context=[]).sha256)
        approval = gate.authorize(None, question=question, web=True, sha256=gate.sha256, nonce=gate.nonce)
        model_input, sha = contract.parse_request(approval.payload.encode())
        self.assertEqual(json.loads(model_input), {"mode": "web", "question": question})
        self.assertEqual(sha, approval.sha256)
        for extra in ({"context": []}, {"access": {}}, {"task": {}}, {"report": {}}):
            altered = dict(json.loads(approval.payload), **extra)
            with self.subTest(extra=extra), self.assertRaises(contract.AnalysisDenied):
                contract.parse_request(contract.canonical(altered).encode())
        for args in (dict(report={}), dict(context=[]), dict(task={}), dict(access={}),
                     dict(web=1), dict(web="true"), dict(question="")):
            values = dict(report=None, question=question, web=True)
            values.update(args)
            with self.subTest(args=args), self.assertRaises(ValueError):
                contract.application_input(**values)
        for key, value in (("mode", "question"), ("system_prompt", contract.SYSTEM_PROMPT), ("schema", 6)):
            altered = dict(json.loads(approval.payload), **{key: value})
            with self.subTest(key=key), self.assertRaises(contract.AnalysisDenied):
                contract.parse_request(contract.canonical(altered).encode())
        with self.assertRaises(contract.AnalysisDenied):
            gate.authorize(None, question=question, web=True, sha256=gate.sha256, nonce=gate.nonce)

    def test_web_cannot_reuse_plain_question_consent_or_bypass_response_limits(self):
        gate = contract.AnalysisConsent(None, question="Pytanie", context=[])
        with self.assertRaises(contract.AnalysisDenied):
            gate.authorize(None, question="Pytanie", web=True, sha256=gate.sha256, nonce=gate.nonce)
        raw = contract.result_packet("a" * 64, text="Źródła: https://example.test", mode="web")
        self.assertEqual(contract.read_result(raw, "a" * 64, mode="web"), "Źródła: https://example.test")
        with self.assertRaises(contract.AnalysisDenied):
            contract.result_packet("a" * 64, text="x" * (contract.MAX_TEXT + 1), mode="web")
        with self.assertRaisesRegex(contract.AnalysisDenied, "reason=web-no-results$"):
            contract.read_result(contract.result_packet("a" * 64, reason="web-no-results", mode="web"),
                                 "a" * 64, mode="web")

    def test_read_only_five_file_sizes_round_trip_without_truncation(self):
        sizes = (9222, 1096, 1090, 579, 188)
        files = []
        for index, size in enumerate(sizes):
            prefix = f'SYNTHETIC {index}: zażółć "\\\n\t'
            content = prefix + "x" * (size - len(prefix.encode("utf-8")))
            files.append({"path": f"/synthetic/project/file{index}.txt", "content": content})
        context = [{"kind": "files", "files": files}]
        self.assertEqual(tuple(len(item["content"].encode("utf-8")) for item in files), sizes)
        self.assertEqual(sum(sizes), 12175)
        gate = contract.AnalysisConsent(None, question="Sprawdź syntetyczne pliki", context=context)
        approval = gate.authorize(None, question="Sprawdź syntetyczne pliki", context=context,
                                  sha256=gate.sha256, nonce=gate.nonce)
        data, sha = contract.parse_request(approval.payload.encode("utf-8"))
        self.assertEqual(sha, approval.sha256)
        self.assertEqual(json.loads(data), {"mode": "question", "question": "Sprawdź syntetyczne pliki",
                                           "context": context})
        for actual, expected in zip(json.loads(data)["context"][0]["files"], files):
            self.assertEqual(actual["content"].encode("utf-8"), expected["content"].encode("utf-8"))

    @patch.object(contract, "MAX_CONTEXT_BYTES", 24000)
    def test_serialized_context_budget_has_an_exact_boundary_and_redacted_wire_refusal(self):
        # The production ceiling has spare room above all legal sources. Lower
        # only the aggregate ceiling to exercise its guard without widening any
        # content/path/file grant or forging an invalid source contract.
        context = sized_file_context(24000)
        self.assertEqual(len(contract.canonical(context).encode("utf-8")), 24000)
        self.assertLessEqual(len(context[0]["files"][0]["content"].encode("utf-8")), 16384)
        payload = contract.application_input(None, "Pytanie", context=context)
        self.assertEqual(json.loads(contract.parse_request(payload.encode("utf-8"))[0])["context"], context)
        oversized = sized_file_context(24001)
        self.assertEqual(len(contract.canonical(oversized).encode("utf-8")), 24001)
        with self.assertRaises(contract.ContextBudgetExceeded):
            contract.application_input(None, "Pytanie", context=oversized)
        tampered = json.loads(payload)
        tampered["context"] = oversized
        raw = contract.canonical(tampered).encode("utf-8")
        self.assertLessEqual(len(raw), contract.MAX_PACKET)
        with self.assertRaises(contract.AnalysisDenied) as refusal:
            contract.parse_request(raw)
        self.assertEqual(refusal.exception.reason, "input")
        self.assertNotIn("synthetic", str(refusal.exception))

    def test_maximum_serialized_question_context_fits_packet_and_native_stdin(self):
        question = '"' * 2048
        context = [{"kind": "files", "files": [
            {"path": "/" + f"{index:02d}" + '"' * 509, "content": '"\\\n\t' * 512}
            for index in range(64)]}]
        self.assertEqual(sum(len(item["content"].encode()) for item in context[0]["files"]), 131072)
        self.assertTrue(all(len(item["path"].encode()) == 512 for item in context[0]["files"]))
        file_context_contract.validate_context(context[0])
        self.assertLessEqual(len(contract.canonical(context).encode()), contract.MAX_CONTEXT_BYTES)
        payload = contract.application_input(None, question, context=context)
        self.assertGreater(len(payload.encode("utf-8")), 32768)
        self.assertLessEqual(len(payload.encode("utf-8")), contract.MAX_PACKET)
        model_input, _ = contract.parse_request(payload.encode("utf-8"))
        self.assertLessEqual(len(model_input.encode("utf-8")), contract.MAX_PACKET)
        self.assertEqual(json.loads(model_input), {"mode": "question", "question": question, "context": context})

    def test_request_budget_checks_the_complete_envelope_at_the_exact_boundary(self):
        task = {"instruction": '"' * 2048,
                "files": [{"path": "d" * 64 + "/" + "e" * 64 + "/" + f"{index:02d}" + "x" * 28,
                           "content": '"' * 2048, "writable": True} for index in range(64)]}
        self.assertEqual(task_contract.request(task), task)
        payload = contract.application_input(None, task=task)
        raw = payload.encode()
        self.assertGreater(len(raw), 32768)
        self.assertLessEqual(len(raw), contract.MAX_PACKET)
        self.assertEqual(json.loads(contract.parse_request(raw)[0])["task"], task)
        # Isolate the envelope guard; source contracts stay fully enforced.
        with patch.object(contract, "MAX_PACKET", len(raw)):
            self.assertEqual(contract.application_input(None, task=task), payload)
        with patch.object(contract, "MAX_PACKET", len(raw) - 1):
            with self.assertRaises(contract.InputBudgetExceeded):
                contract.application_input(None, task=task)
            with self.assertRaisesRegex(contract.AnalysisDenied, "reason=input"):
                contract.parse_request(raw)

    def test_concise_style_is_in_every_application_mode_and_bound_by_consent(self):
        sources = [dict(report=report_fixture()),
                   dict(report=None, question="Pytanie", context=[]),
                   dict(report=None, question="Pytanie", web=True),
                   dict(report=None, question="Pytanie", access={"files": [], "weather": False, "systemd": []}),
                   dict(report=None, task={"instruction": "Zmien", "files": [
                       {"path": "x.txt", "content": "x", "writable": True}]})]
        for source in sources:
            with self.subTest(mode=json.loads(contract.application_input(**source))["mode"]):
                payload = contract.application_input(**source)
                gate = contract.AnalysisConsent(**source)
                self.assertIn(contract.RESPONSE_STYLE, json.loads(payload)["system_prompt"])
                self.assertEqual(gate.sha256, contract.digest(payload))
                changed = json.loads(payload)
                changed["system_prompt"] = changed["system_prompt"].replace(contract.RESPONSE_STYLE, "")
                with self.assertRaises(contract.AnalysisDenied):
                    contract.parse_request(contract.canonical(changed).encode())
                prompt_name = "WEB_SYSTEM_PROMPT" if source.get("web") else "SYSTEM_PROMPT"
                with patch.object(contract, prompt_name, getattr(contract, prompt_name) + " Other style."):
                    self.assertNotEqual(contract.digest(contract.application_input(**source)), gate.sha256)
                    with self.assertRaises(contract.AnalysisDenied):
                        gate.authorize(**source, sha256=gate.sha256, nonce=gate.nonce)
        for rule in ("1-3 sentences", "no introduction", "Do not truncate commands, code or diffs", "security"):
            self.assertIn(rule, contract.RESPONSE_STYLE)

    def test_expired_changed_wrong_nonce_and_replayed_consent_are_closed(self):
        for variant in ("expiry", "mutation", "nonce", "sha", "replay"):
            now = [0]
            report = report_fixture()
            gate = contract.AnalysisConsent(report, clock=lambda: now[0])
            args = dict(sha256=gate.sha256, nonce=gate.nonce)
            if variant == "expiry":
                now[0] = 181
            elif variant == "mutation":
                report["kernel"] = "changed"
            elif variant == "nonce":
                args["nonce"] = "bad"
            elif variant == "sha":
                args["sha256"] = "bad"
            else:
                gate.authorize(report, **args)
            with self.subTest(variant=variant), self.assertRaises(contract.AnalysisDenied):
                gate.authorize(report, **args)
            # A failed attempt cannot be corrected and reused either.
            with self.assertRaises(contract.AnalysisDenied):
                gate.authorize(report_fixture(), sha256=gate.sha256, nonce=gate.nonce)

    def test_output_is_bounded_hash_bound_and_controls_cannot_reach_terminal(self):
        raw = contract.result_packet("a" * 64, text="tekst\n\t\x1b]52;clipboard\a\u202e")
        text = contract.read_result(raw, "a" * 64)
        self.assertIn("tekst\n\t", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\u202e", text)
        with self.assertRaises(contract.AnalysisDenied):
            contract.read_result(raw, "b" * 64)
        for text in ("x" * 16385, "\x00" * 16000):
            with self.assertRaises(contract.AnalysisDenied):
                contract.result_packet("a" * 64, text=text)

    def test_invalid_results_and_refusal_do_not_leak_error_data(self):
        base = json.loads(contract.result_packet("a" * 64, text="ok"))
        for key, value in (("schema", True), ("status", "failure"), ("reason", []),
                           ("text", {}), ("text", ""), ("extra", "SECRET")):
            changed = dict(base, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(contract.AnalysisDenied):
                contract.read_result(json.dumps(changed).encode(), "a" * 64)
        for raw in (b'null', b'{"schema":1,"schema":1}', b'\xff', b'[]'):
            with self.assertRaises(contract.AnalysisDenied):
                contract.read_result(raw, "a" * 64)
        with self.assertRaisesRegex(contract.AnalysisDenied, "reason=auth-required$"):
            contract.read_result(contract.result_packet("a" * 64, reason="auth-required"), "a" * 64)
        self.assertEqual(contract.AnalysisDenied([]).reason, "internal")


class AnswerLanguageTests(unittest.TestCase):
    """The interface is English, but the model answers in the language of the user's own message."""

    def test_every_prompt_follows_the_language_of_the_users_message(self):
        for prompt in (contract.SYSTEM_PROMPT, contract.WEB_SYSTEM_PROMPT):
            with self.subTest(prompt=prompt[:30]):
                self.assertIn("in the language of the user's own message", prompt)
                self.assertNotIn("po polsku", prompt)
                self.assertNotRegex(prompt, r"(?i)\b(answer|reply|respond|write)\b[^.]* in (polish|english)\b")

    def test_a_message_from_the_application_does_not_set_the_language(self):
        from ai_desktop.ask_cli import CONTINUE_PREFIX
        self.assertTrue(CONTINUE_PREFIX.startswith("Continue the task"))
        self.assertIn("starting with Continue the task, does not set the language", contract.SYSTEM_PROMPT)
        self.assertIn("A question starting with Continue the task comes from the application", contract.SYSTEM_PROMPT)

