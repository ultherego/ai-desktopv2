"""Report UI follows the exact consented recipient, not a login or ambient label."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from contextlib import ExitStack, contextmanager, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import agent_profiles, analysis_child, analysis_cli, analysis_contract, cli
from ai_desktop import codex_child, codex_runtime, execution
from ai_desktop.terminal_consent import PROMPTS
from test_report_policy import report_fixture

QUESTION = "Porównaj wzmianki o Claude i Codex w raporcie."
ANSWER = "Odpowiedź zawiera nazwy Claude i Codex, bez zmiany ich treści."


class AnalysisRecipientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aid-report-recipient-", dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.store = agent_profiles.Store(self.temp.name)
        self.snapshot = Path(self.temp.name) / "Claude_i_Codex.json"
        self.report = report_fixture()
        self.snapshot.write_text(json.dumps(self.report), encoding="utf-8")
        self.events, self.sent, self.native = [], [], []
        self.output = io.StringIO()

    def select(self, provider):
        name = provider + "-report"
        profile = agent_profiles.Profile(provider=provider,
                                         model="sonnet" if provider == "claude" else "default",
                                         effort="high", access="read-only")
        self.store.mutate(lambda config: config.put(name, profile).select(name))
        return name

    @contextmanager
    def session(self, **kwargs):
        self.events.append("start")
        try:
            yield
        finally:
            self.events.append("cleanup")

    def send(self, approval):
        self.events.append("send")
        self.sent.append(approval)
        parsed, sha = analysis_contract.parse_request(approval.payload.encode("utf-8"))
        self.assertEqual(sha, approval.sha256)
        request = json.loads(parsed)
        self.assertEqual(request["report"], self.report)
        self.assertEqual(request["question"], QUESTION)
        selected = request.get("execution")
        if selected is not None and selected["provider"] == "codex":
            private = codex_runtime.private_packet("synthetic-report-access", parsed.encode("utf-8"))
            token, body, native_config = codex_child.private_input(private)
            self.assertEqual(token, "synthetic-report-access")
            self.assertEqual(json.loads(body)["report"], self.report)
        else:
            body, native_config = analysis_child.native_input("analyze", parsed.encode("utf-8"))
            self.assertEqual(json.loads(body)["report"], self.report)
        self.native.append(native_config)
        return ANSWER

    @contextmanager
    def environment(self):
        resolve = agent_profiles.resolve
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(self.output))
            # Resolve against an actual private config without reading the
            # desktop owner's real settings, account files or OAuth store.
            stack.enter_context(patch.object(agent_profiles, "resolve", side_effect=
                lambda profile=None, model=None, effort=None: resolve(profile, model, effort, store=self.store)))
            # Only the terminal command's entry guard simulates a desktop UID. File ownership
            # checks for these real temporary stores use the actual runner UID (including CI).
            identity = stack.enter_context(patch.object(analysis_cli, "os", wraps=analysis_cli.os))
            identity.getuid.return_value = identity.geteuid.return_value = 1000
            stack.enter_context(patch.object(analysis_cli.sys.stdin, "isatty", return_value=True))
            stack.enter_context(patch.object(analysis_cli.sys.stdout, "isatty", return_value=True))
            stack.enter_context(patch.object(analysis_cli, "installation_ready"))
            session = stack.enter_context(patch.object(analysis_cli, "managed_session", side_effect=self.session))
            send = stack.enter_context(patch.object(analysis_cli, "send_once", side_effect=self.send))
            stack.enter_context(patch.object(analysis_cli.diagnose, "collect", side_effect=AssertionError("snapshot only")))
            stack.enter_context(patch("subprocess.run", side_effect=AssertionError("no native process")))
            stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("no native process")))
            stack.enter_context(patch("socket.socket", side_effect=AssertionError("no network")))
            stack.enter_context(patch("ai_desktop.analysis_admin.main", side_effect=AssertionError("no Claude login")))
            stack.enter_context(patch("ai_desktop.codex_admin.main", side_effect=AssertionError("no Codex login")))
            yield session, send

    def invoke(self, *, details=False, preview=False, reader=None):
        args = ["analyze", "--report", str(self.snapshot), "--question", QUESTION, "--format", "plain"]
        if details:
            args.append("--v")
        if preview:
            args.append("--preview")
        if reader is None:
            reader = lambda _: "y"
        original = analysis_cli.main
        with patch.object(analysis_cli, "main", side_effect=lambda **kwargs: original(reader=reader, **kwargs)):
            return cli.main(args)

    def reset_run(self):
        self.events.clear()
        self.sent.clear()
        self.native.clear()
        self.output.seek(0)
        self.output.truncate(0)

    def test_real_profile_recipient_matches_preview_consent_wire_and_native_selection(self):
        for provider, brand in (("claude", "Claude"), ("codex", "Codex")):
            name = self.select(provider)
            before = self.store.path.read_bytes()
            for details in (False, True):
                with self.subTest(provider=provider, details=details):
                    self.reset_run()
                    prompts = []
                    def reader(prompt):
                        prompts.append(prompt)
                        self.assertIn(brand + " · profile: " + name, self.output.getvalue())
                        self.assertEqual(self.events, [])
                        return "y"
                    with self.environment():
                        self.assertEqual(self.invoke(details=details, reader=reader), 0)
                    self.assertEqual(prompts, [PROMPTS["transfer"].replace("Claude", brand)])
                    self.assertEqual(self.events, ["start", "send", "cleanup"])
                    self.assertEqual(len(self.sent), 1)
                    envelope = json.loads(self.sent[0].payload)
                    self.assertEqual(envelope["execution"]["provider"], provider)
                    self.assertEqual(self.native[0], envelope["execution"])
                    output = self.output.getvalue()
                    self.assertIn(QUESTION, output)
                    self.assertIn(ANSWER, output)
                    waiting = next(line for line in output.splitlines() if "Waiting for" in line)
                    self.assertIn(brand, waiting)
                    if details:
                        self.assertIn(envelope["client_context"], output)
                        self.assertIn("Client: " + envelope["client_version"], output)
                        self.assertIn("=== RESULT FROM " + brand.upper(), output)
                    else:
                        self.assertIn(brand + " will receive the data above", output)
                    self.assertEqual(self.store.path.read_bytes(), before)

    def test_offline_preview_keeps_the_selected_recipient_without_account_or_transport_calls(self):
        self.select("codex")
        with self.environment() as (session, send), \
                patch.object(analysis_cli, "confirm") as confirm:
            self.assertEqual(self.invoke(preview=True), 0)
        session.assert_not_called()
        send.assert_not_called()
        confirm.assert_not_called()
        self.assertIn("Codex · profile: codex-report", self.output.getvalue())
        self.assertIn("Codex will receive the data above", self.output.getvalue())
        self.assertIn(QUESTION, self.output.getvalue())

    def test_reprompt_is_bound_to_preview_and_changed_execution_still_refuses_transfer(self):
        self.select("codex")
        prompts = []
        claude = agent_profiles.resolve(profile="default", store=self.store)
        def reader(prompt):
            prompts.append(prompt)
            if len(prompts) == 1:
                execution.select(claude)
                return "?"
            return "y"
        with self.environment() as (session, send):
            self.assertEqual(self.invoke(reader=reader), 2)
        self.assertEqual(prompts, [PROMPTS["transfer"].replace("Claude", "Codex")] * 2)
        session.assert_not_called()
        send.assert_not_called()
        self.assertEqual(self.events, [])

    def test_failure_after_transfer_keeps_the_recipient_of_that_transfer(self):
        self.select("codex")
        claude = agent_profiles.resolve(profile="default", store=self.store)
        def failure(approval):
            self.send(approval)
            execution.select(claude)
            raise analysis_contract.AnalysisDenied("runtime")
        with self.environment() as (_, send):
            send.side_effect = failure
            self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.native[0]["provider"], "codex")
        self.assertIn("The isolated Codex runtime", self.output.getvalue())
        self.assertNotIn("The trusted Claude Code", self.output.getvalue())

    def test_absent_execution_in_preview_never_inherits_ambient_codex_label(self):
        self.select("codex")
        original = analysis_contract.AnalysisConsent
        def legacy(*args, **kwargs):
            return original(*args, **kwargs, execution=None)
        with self.environment() as (session, send), patch.object(analysis_cli, "AnalysisConsent", side_effect=legacy):
            self.assertEqual(self.invoke(preview=True), 0)
        self.assertIn("Claude · profile: default", self.output.getvalue())
        self.assertIn("Claude will receive the data above", self.output.getvalue())
        self.assertNotIn("Codex will receive the data above", self.output.getvalue())
        session.assert_not_called()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
