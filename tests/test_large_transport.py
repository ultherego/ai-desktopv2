"""Large selected input and isolated local-copy budgets, without a model call."""
import hashlib
import json
import unittest
from unittest.mock import patch

from ai_desktop import analysis_contract as contract, codex_protocol, execution, process_io
from ai_desktop.skill_runtime import using_knowledge


def knowledge(text):
    return {"resources": [{"name": "synthetic/reference", "status": "ok", "text": text,
                           "sha256": hashlib.sha256(text.encode()).hexdigest()}]}


class LargeTransportTests(unittest.TestCase):
    def test_two_mib_selected_file_survives_consent_worker_and_codex_reconstruction(self):
        content = 'zażółć "\\\n\t' * 150000
        self.assertGreater(len(content.encode()), 2 * 1024 * 1024)
        context = [{"kind": "files", "files": [{"path": "/synthetic/large.txt", "content": content}]}]
        selected = execution.validate({"provider": "codex", "model": "default", "effort": "default",
                                       "instructions": "", "profile": "default", "access": "read-only"})
        gate = contract.AnalysisConsent(None, question="Inspect selected text", context=context, execution=selected)
        approved = gate.authorize(None, question="Inspect selected text", context=context, execution=selected,
                                  sha256=gate.sha256, nonce=gate.nonce)
        data, digest = contract.parse_request(approved.payload.encode())
        self.assertEqual(digest, approved.sha256)
        self.assertEqual(json.loads(data)["context"], context)
        native = codex_protocol.Protocol(approved.payload.encode())
        self.assertIn(contract.canonical(context), native.prompt)
        self.assertGreater(native.limits.line_bytes, len(native.prompt.encode()))

    def test_knowledge_is_bound_and_worker_does_not_inherit_ambient_resources(self):
        with using_knowledge(knowledge("first procedure")):
            gate = contract.AnalysisConsent(None, question="inspect", context=[])
        with using_knowledge(knowledge("changed procedure")):
            with self.assertRaisesRegex(contract.AnalysisDenied, "reason=consent"):
                gate.authorize(None, question="inspect", context=[], sha256=gate.sha256, nonce=gate.nonce)
            data, _ = contract.parse_request(gate.payload.encode())
            self.assertEqual(json.loads(data)["knowledge"], knowledge("first procedure"))
        clean = contract.application_input(None, "inspect", context=[])
        with using_knowledge(knowledge("must remain local")):
            data, _ = contract.parse_request(clean.encode())
            self.assertNotIn("knowledge", json.loads(data))
            web = contract.application_input(None, "public technical question", web=True)
            self.assertNotIn("must remain local", web)
            data, _ = contract.parse_request(web.encode())
            self.assertNotIn("knowledge", json.loads(data))

    def test_large_local_copy_allowance_never_widens_model_or_private_pipes(self):
        from ai_desktop.copy_contract import MAX_PAYLOAD
        self.assertGreater(MAX_PAYLOAD, process_io.MAX_PRIVATE_INPUT_BYTES)
        with patch("ai_desktop.process_io.subprocess.Popen") as start:
            for options in ({}, {"private_input": True}, {"copy_input": 1},
                            {"copy_input": True, "private_input": True}):
                with self.subTest(options=options), self.assertRaisesRegex(contract.AnalysisDenied, "reason=input"):
                    process_io.bounded_process(["unused"], env={}, input_limit=MAX_PAYLOAD, **options)
            with self.assertRaisesRegex(contract.AnalysisDenied, "reason=cancelled"):
                process_io.bounded_process(["unused"], env={}, input_limit=MAX_PAYLOAD, copy_input=True,
                                           cancelled=lambda: True)
            start.assert_not_called()
