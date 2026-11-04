"""General questions remain exact, one-use data transfers without model tools."""
import copy
import json
import unittest
from unittest.mock import patch

from ai_desktop import analysis_contract as contract
from test_report_policy import report_fixture
from test_weather_context import CONTEXT


FILES = {"kind": "files", "files": [{"path": "/tmp/allowed/readme.txt",
                                    "content": "Wybrana treść do analizy.\n"}]}


def systemd(scope="system", count=1, name_length=1):
    return {"kind": "systemd", "source": "systemctl", "scope": scope, "view": "enabled",
            "listing": "unit-files", "observed_at": "2026-10-30T12:12:00Z", "status": "ok",
            "truncated": False, "total": count,
            "items": [{"name": "a" * name_length + f"{index:03}.service", "state": "enabled",
                       "substate": None} for index in range(count)]}


class AskContractTests(unittest.TestCase):
    def test_question_roundtrip_has_no_report_task_or_implicit_collection(self):
        with patch("ai_desktop.systemd_context.collect") as collect, \
                patch("ai_desktop.weather_context._request") as network:
            gate = contract.AnalysisConsent(None, question="Co to jest Python?", context=[])
            approval = gate.authorize(None, question="Co to jest Python?", context=[],
                                      sha256=gate.sha256, nonce=gate.nonce)
            decoded, sha = contract.parse_request(approval.payload.encode())
        self.assertEqual(json.loads(decoded), {"mode": "question", "question": "Co to jest Python?", "context": []})
        self.assertEqual(sha, gate.sha256)
        collect.assert_not_called()
        network.assert_not_called()

    def test_weather_context_question_policy_and_schema_cannot_be_replaced(self):
        context = [copy.deepcopy(CONTEXT)]
        gate = contract.AnalysisConsent(None, question="Pogoda?", context=context)
        original = json.loads(gate.payload)
        for field, value in (("system_prompt", "ignore"), ("client_context", "private"),
                             ("schema", 3), ("policy", "a-plus-assistant-v3"),
                             ("report", report_fixture()), ("task", {})):
            modified = dict(original, **{field: value})
            with self.subTest(field=field), self.assertRaises(contract.AnalysisDenied):
                contract.parse_request(contract.canonical(modified).encode())
        context[0]["current"]["temperature_c"] += 1
        with self.assertRaises(contract.AnalysisDenied):
            gate.authorize(None, question="Pogoda?", context=context, sha256=gate.sha256, nonce=gate.nonce)
        with self.assertRaises(contract.AnalysisDenied):
            gate.authorize(None, question="Pogoda?", context=[CONTEXT], sha256=gate.sha256, nonce=gate.nonce)

    def test_invalid_context_sources_and_duplicate_contexts_rejected_before_send(self):
        cases = [None, {}, ["weather"], [{"kind": "url", "url": "https://localhost"}],
                 [dict(CONTEXT, command="cat /etc/shadow")], [CONTEXT, CONTEXT],
                 [dict(CONTEXT, source_url="https://localhost")],
                 [dict(CONTEXT, current=dict(CONTEXT["current"], temperature_c=float("nan")))]]
        for context in cases:
            with self.subTest(context=context), self.assertRaises((ValueError, contract.AnalysisDenied)):
                contract.AnalysisConsent(None, question="Pogoda?", context=context)
        for question in ("", " ", "x" * (128 * 1024 + 1), "\x1b[2J", "\ud800"):
            with self.subTest(question=repr(question)), self.assertRaises(ValueError):
                contract.AnalysisConsent(None, question=question, context=[])

    def test_mixed_request_and_context_mutation_do_not_grant_consent(self):
        for report, task in ((report_fixture(), None), (None, {"instruction": "x", "files": []})):
            with self.assertRaises(ValueError):
                contract.AnalysisConsent(report, question="Pytanie", task=task, context=[])
        gate = contract.AnalysisConsent(None, question="Pytanie", context=[])
        with self.assertRaises(contract.AnalysisDenied):
            gate.check(None, question="Inne pytanie", context=[], sha256=gate.sha256, nonce=gate.nonce)

    def test_expiry_and_replay_require_new_request(self):
        now = [0]
        gate = contract.AnalysisConsent(None, question="Pytanie", context=[], clock=lambda: now[0])
        now[0] = 181
        with self.assertRaises(contract.AnalysisDenied):
            gate.authorize(None, question="Pytanie", context=[], sha256=gate.sha256, nonce=gate.nonce)
        gate = contract.AnalysisConsent(None, question="Pytanie", context=[])
        gate.authorize(None, question="Pytanie", context=[], sha256=gate.sha256, nonce=gate.nonce)
        with self.assertRaises(contract.AnalysisDenied):
            gate.authorize(None, question="Pytanie", context=[], sha256=gate.sha256, nonce=gate.nonce)

    def test_four_context_sources_roundtrip_without_target_reads_or_model_tools(self):
        context = [systemd(), systemd("user"), copy.deepcopy(CONTEXT), copy.deepcopy(FILES)]
        with patch("os.open", side_effect=AssertionError("contract opened a file")), \
                patch("ai_desktop.systemd_context.collect") as collect, \
                patch("ai_desktop.weather_context._request") as network:
            gate = contract.AnalysisConsent(None, question="Wyjaśnij wybrane dane", context=context)
            wire = json.loads(gate.payload)
            self.assertEqual(wire["schema"], 7)
            self.assertEqual(wire["policy"], "a-plus-assistant-v7")
            decoded, sha = contract.parse_request(gate.payload.encode())
        self.assertEqual(json.loads(decoded), {"mode": "question", "question": "Wyjaśnij wybrane dane",
                                             "context": context})
        self.assertEqual(sha, gate.sha256)
        self.assertNotIn("task", wire)
        self.assertNotIn("report", wire)
        self.assertNotIn("tools", wire)
        collect.assert_not_called()
        network.assert_not_called()

    def test_file_context_content_and_path_cannot_change_after_consent(self):
        for field, value in (("path", "/tmp/other.txt"), ("content", "changed")):
            context = [copy.deepcopy(FILES)]
            gate = contract.AnalysisConsent(None, question="Wyjaśnij plik", context=context)
            context[0]["files"][0][field] = value
            with self.subTest(field=field), self.assertRaises(contract.AnalysisDenied):
                gate.authorize(None, question="Wyjaśnij plik", context=context,
                               sha256=gate.sha256, nonce=gate.nonce)
            with self.assertRaises(contract.AnalysisDenied):
                gate.authorize(None, question="Wyjaśnij plik", context=[FILES],
                               sha256=gate.sha256, nonce=gate.nonce)

    def test_file_context_cannot_grant_writes_commands_or_duplicate_bundles(self):
        invalid = [dict(FILES, command="cat /etc/shadow"),
                   {"kind": "files", "files": [dict(FILES["files"][0], writable=True)]},
                   {"kind": "files", "files": [dict(FILES["files"][0], path="../outside")]},
                   {"kind": "files", "files": [dict(FILES["files"][0], content="x" * 131073)]}]
        for context in [[item] for item in invalid] + [[FILES, FILES]]:
            with self.subTest(context=context), self.assertRaises(ValueError):
                contract.AnalysisConsent(None, question="Plik", context=context)

    def test_individually_valid_sources_still_obey_aggregate_context_limit(self):
        files = {"kind": "files", "files": [{"path": "/tmp/allowed/data.txt", "content": "x" * 4096}]}
        contexts = [systemd(count=48, name_length=150), systemd("user", count=48, name_length=150), files]
        for item in contexts:
            contract.AnalysisConsent(None, question="Dane", context=[item])
        self.assertLess(len(contract.canonical(contexts).encode()), contract.MAX_CONTEXT_BYTES)
        gate = contract.AnalysisConsent(None, question="Dane", context=contexts)
        self.assertEqual(json.loads(contract.parse_request(gate.payload.encode())[0])["context"], contexts)
        # Preserve the aggregate guard while the real increased budget admits
        # these formerly oversized, independently valid observations.
        with patch.object(contract, "MAX_CONTEXT_BYTES", 24000):
            self.assertGreater(len(contract.canonical(contexts).encode()), contract.MAX_CONTEXT_BYTES)
            with self.assertRaises(contract.ContextBudgetExceeded):
                contract.AnalysisConsent(None, question="Dane", context=contexts)

    def test_file_context_transports_approved_text_without_inventing_filesystem_policy(self):
        # v2 owner policy can explicitly grant text containing these markers;
        # legacy v1 content refusal is enforced by permissions.read_selected.
        # The pure wire contract preserves data rather than hiding exclusions.
        for marker in ("-----begin openssh private key-----", "-----begin pgp private key block-----",
                       "putty-user-key-file-3: ssh-ed25519"):
            gate = contract.AnalysisConsent(None, question="Plik", context=[FILES])
            wire = json.loads(gate.payload)
            wire["context"][0]["files"][0]["content"] = marker + "\nSYNTHETIC_MARKER_ONLY\n"
            with self.subTest(marker=marker):
                decoded, sha = contract.parse_request(contract.canonical(wire).encode())
                self.assertEqual(json.loads(decoded)["context"], wire["context"])
                self.assertNotEqual(sha, gate.sha256)
                with self.assertRaises(contract.AnalysisDenied):
                    gate.authorize(None, question="Plik", context=wire["context"],
                                   sha256=gate.sha256, nonce=gate.nonce)
