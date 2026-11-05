"""Full desktop entrance and policy revocation at the approved local transport boundary."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import pwd
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import agent_profiles, analysis_contract, analysis_service, analysis_session, ask_cli, cli
from ai_desktop import conversation_sessions, execution, permissions, terminal_consent
from test_local_provider import reply, response, server


class LocalHandoffTests(unittest.TestCase):
    def setUp(self):
        plan = json.dumps({"action": "answer", "text": "Local test answer"})
        self.port, self.requests = self.enterContext(server(response(reply(plan))))
        directory = self.enterContext(tempfile.TemporaryDirectory(dir=pwd.getpwuid(os.getuid()).pw_dir))
        home = Path(directory)
        self.config = home / ".config/ai-desktop/permissions.toml"
        self.config.parent.mkdir(parents=True, mode=0o700)
        self.policy()
        profiles = agent_profiles.Store(home)
        profiles.mutate(lambda config: config.with_provider("local"))
        self.enterContext(patch.object(agent_profiles, "Store", return_value=profiles))
        self.enterContext(patch.object(conversation_sessions, "Store", return_value=conversation_sessions.Store(home)))
        self.enterContext(patch.object(permissions, "_identity", return_value=(os.getuid(), directory)))
        self.output = self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(sys.stdin, "isatty", return_value=True))
        self.enterContext(patch.object(sys.stdout, "isatty", return_value=True))
        for module in (ask_cli, analysis_service, analysis_session):
            identity = self.enterContext(patch.object(module, "os", wraps=os))
            identity.getuid.return_value = identity.geteuid.return_value = 1000
            identity.isatty.return_value = True
        self.enterContext(patch.object(ask_cli, "Presence"))
        self.enterContext(patch.object(analysis_service, "endpoint", side_effect=AssertionError("cloud endpoint")))
        self.enterContext(patch.object(analysis_session, "authenticate", side_effect=AssertionError("sudo")))

    def policy(self, *, revoke=False):
        deny = '["~/example"]' if revoke else "[]"
        self.config.write_text(f'version=2\n[files]\nread=["~/example"]\ndeny={deny}\n'
                               f'[network]\nlocal=true\n[local]\nport={self.port}\n')
        self.config.chmod(0o600)

    def test_bare_terminal_command_goes_through_conversation_consent_to_the_configured_server(self):
        self.enterContext(patch.object(ask_cli, "_read_message", side_effect=["Question", "/exit"]))
        answers = iter(["yes", "no"])
        prompts = []
        def read(prompt):
            prompts.append(prompt)
            return next(answers)
        self.enterContext(patch.object(terminal_consent, "fresh_input", side_effect=read))
        self.assertEqual(cli.main([]), 0, self.output.getvalue())
        self.assertIn(f"127.0.0.1:{self.port}", prompts[0])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(json.loads(self.requests[0][2]["messages"][1]["content"])["question"], "Question")
        self.assertIn("Local test answer", self.output.getvalue())

    def test_new_policy_allowing_same_port_cannot_replace_original_file_permissions_before_post(self):
        marker = "Synthetic private file excerpt"
        with execution.using(agent_profiles.resolve()), execution.using_history([{"question": "Read example", "answer": marker}]):
            gate = analysis_contract.AnalysisConsent(None, question="Continue", context=[])
            approval = gate.authorize(None, question="Continue", context=[], nonce=gate.nonce, sha256=gate.sha256)
            self.assertIn(marker, approval.payload)
            self.policy(revoke=True)
            self.assertTrue(permissions.load_policy().provider_allowed("local"))
            with self.assertRaises(analysis_contract.AnalysisDenied):
                analysis_service.send_once(approval)
        self.assertEqual(self.requests, [])
