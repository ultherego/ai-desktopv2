"""Concise Codex failure hints and opt-in finite diagnostics; no native calls."""
from contextlib import nullcontext, redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from ai_desktop import ask_cli, assistant_flow, execution, run_controls
from ai_desktop.analysis_contract import AnalysisDenied

SECRET = "synthetic-private-error-prose-token"


def selected(provider="codex"):
    return {"provider": provider, "model": "default", "effort": "default", "instructions": "",
            "profile": "reviewer", "access": "read-only"}


class CodexDiagnosticUiTests(unittest.TestCase):
    def ask_failure(self, failure, *, provider="codex", verbose=False, change_context=False):
        sent, prompts = [], []
        def sender(approval):
            sent.append(json.loads(approval.payload))
            if change_context:
                execution.select(selected("claude"))
            raise failure
        arguments = ["--question", "Pytanie testowe.", "--no-context", "--format", "plain"]
        if verbose:
            arguments.append("--v")
        with execution.using(selected(provider)), redirect_stdout(io.StringIO()) as output, \
                patch.object(ask_cli.os, "geteuid", return_value=1000), \
                patch.object(ask_cli.os, "getuid", return_value=1000), \
                patch("ai_desktop.terminal_consent.sys.stdin.isatty", return_value=True), \
                patch("ai_desktop.terminal_consent.sys.stdout.isatty", return_value=True), \
                patch.object(assistant_flow.permissions, "load_policy", return_value=None), \
                patch("ai_desktop.conversation_sessions.load_session", return_value=None), \
                patch.object(ask_cli, "installation_ready"), \
                patch.object(ask_cli, "managed_session", return_value=nullcontext()), \
                patch.object(ask_cli, "send_once", side_effect=sender) as send, \
                patch("subprocess.run") as run, patch("subprocess.Popen") as spawn, \
                patch("socket.socket") as socket:
            result = ask_cli.main.__wrapped__(arguments, reader=lambda prompt: prompts.append(prompt) or "y")
        self.assertEqual(result, 2)
        send.assert_called_once()
        self.assertEqual(len(prompts), 1)
        run.assert_not_called()
        spawn.assert_not_called()
        socket.assert_not_called()
        return output.getvalue(), sent[0]

    def test_default_is_one_human_failure_line_and_verbose_adds_only_closed_code(self):
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                text, sent = self.ask_failure(AnalysisDenied("client", diagnostic="codex-rpc-thread"), verbose=verbose)
                self.assertEqual(sent["execution"]["provider"], "codex")
                self.assertIn("Codex did not start the conversation.", text)
                self.assertEqual("check=codex-rpc-thread" in text, verbose)
                self.assertEqual("ANALYSIS: REFUSED reason=client" in text, verbose)
                self.assertNotIn(SECRET, text)
                self.assertEqual(text.count("Codex did not start the conversation."), 1)

    def test_failure_hint_keeps_provider_of_exact_gate_after_ambient_change(self):
        text, sent = self.ask_failure(AnalysisDenied("client", diagnostic="codex-rpc-turn"), change_context=True)
        self.assertEqual(sent["execution"]["provider"], "codex")
        self.assertIn("Codex did not accept the question.", text)
        self.assertNotIn("check=", text)

    def test_raw_or_mismatched_diagnostics_never_get_a_specific_hint(self):
        fallback = "Fixed application message."
        for diagnostic, reason in ((SECRET, "client"), ({"message": SECRET}, "client"),
                                   ("codex-http-forbidden", "client"), ("codex-launch", "network")):
            with self.subTest(diagnostic=diagnostic), execution.using(selected()):
                hint = run_controls.failure_hint(reason, fallback, diagnostic=diagnostic)
            self.assertEqual(hint, fallback)
            self.assertNotIn(SECRET, hint)
        text, _ = self.ask_failure(AnalysisDenied("client", diagnostic=SECRET), verbose=True)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("check=", text)

    def test_non_codex_request_never_uses_codex_specific_hint(self):
        fallback = "Fixed Claude message."
        with execution.using(selected("claude")):
            self.assertEqual(run_controls.failure_hint("client", fallback, diagnostic="codex-launch"), fallback)
        text, sent = self.ask_failure(AnalysisDenied("client", diagnostic="codex-rpc-thread"), provider="claude")
        self.assertEqual(sent["execution"]["provider"], "claude")
        self.assertNotIn("Codex did not start the conversation.", text)
        self.assertNotIn("check=", text)

    def test_web_check_remains_visible_without_verbose(self):
        text, sent = self.ask_failure(AnalysisDenied("result", diagnostic="web-init-event"), provider="claude")
        self.assertEqual(sent["execution"]["provider"], "claude")
        self.assertIn("ANALYSIS: REFUSED reason=result check=web-init-event", text)

