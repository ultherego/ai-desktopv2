"""Real terminal consent with synthetic collection and a recording transport."""
import contextlib
import io
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from ai_desktop import analysis_cli, analysis_admin, cli
from ai_desktop.analysis_contract import AnalysisConsent
from ai_desktop.terminal_consent import PROMPTS
from test_report_policy import report_fixture
from tests.profile_fixture import DEFAULT_PROFILE

ROOT = Path(__file__).resolve().parents[1]


class AnalysisCliTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("ai_desktop.agent_profiles.resolve", return_value=DEFAULT_PROFILE))

    def test_preview_collects_once_without_client_profile_or_transport(self):
        out = io.StringIO()
        with patch.object(analysis_cli.os, "geteuid", return_value=1000), \
                patch.object(analysis_cli.os, "getuid", return_value=1000), \
                patch.object(analysis_cli.diagnose, "collect", return_value=report_fixture()) as collect, \
                patch.object(analysis_cli, "send_once") as send, contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["analyze", "--preview", "--details"]), 0)
        collect.assert_called_once_with()
        send.assert_not_called()
        self.assertIn("is not a copy of all TLS traffic", out.getvalue())
        self.assertIn(AnalysisConsent(report_fixture()).sha256, out.getvalue())

    def test_root_and_pipe_refuse_before_collection_or_transport(self):
        with patch.object(analysis_cli.diagnose, "collect") as collect, \
                patch.object(analysis_cli, "send_once") as send, contextlib.redirect_stdout(io.StringIO()):
            for uid in (0, 1000):
                with patch.object(analysis_cli.os, "geteuid", return_value=uid), \
                        patch.object(analysis_cli.os, "getuid", return_value=uid), \
                        patch.object(analysis_cli.sys.stdin, "isatty", return_value=False):
                    self.assertEqual(analysis_cli.main(), 2)
            collect.assert_not_called()
            send.assert_not_called()

    def test_real_pty_accepts_yes_no_initials_and_reprompts_unknown_answers(self):
        script = """
import sys
from contextlib import nullcontext
sys.path.insert(0, 'tests')
from unittest.mock import patch
from test_report_policy import report_fixture
from ai_desktop import analysis_cli, cli, agent_profiles
default_profile = {'provider': 'claude', 'model': 'default', 'effort': 'default', 'instructions': '', 'profile': 'default', 'access': 'changes'}
def send(approval):
    print('SYNTHETIC_TRANSFER:' + approval.sha256, flush=True)
    return 'Synthetic result without changes'
with patch.object(agent_profiles, 'resolve', return_value=default_profile), patch.object(analysis_cli.os, 'getuid', return_value=1000), patch.object(analysis_cli.os, 'geteuid', return_value=1000), patch.object(analysis_cli.diagnose, 'collect', return_value=report_fixture()), patch.object(analysis_cli, 'installation_ready'), patch.object(analysis_cli, 'managed_session', return_value=nullcontext()), patch.object(analysis_cli, 'send_once', side_effect=send):
    raise SystemExit(cli.main(['analyze']))
"""
        for answers, approve in (((b"", b"?", b"tak", b"ANALIZUJ", b"N"), False),
                                 ((b"Y",), True), ((b"", b"Yes"), True)):
            master, slave = pty.openpty()
            process = subprocess.Popen([sys.executable, "-B", "-c", script], cwd=ROOT,
                                       stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            output = bytearray()
            try:
                deadline, answered = time.monotonic() + 10, 0
                while time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        try:
                            data = os.read(master, 8192)
                        except OSError:
                            break
                        if not data:
                            break
                        output.extend(data)
                    if output.count(PROMPTS["transfer"].encode()) > answered and answered < len(answers):
                        self.assertNotIn(b"SYNTHETIC_TRANSFER", output)
                        os.write(master, answers[answered] + b"\n")
                        answered += 1
                    if process.poll() is not None:
                        # Drain any final terminal data before asserting.
                        if not select.select([master], [], [], .1)[0]:
                            break
                process.wait(timeout=2)
                self.assertEqual(answered, len(answers), output.decode(errors="replace"))
                self.assertEqual(process.returncode, 0 if approve else 2)
                self.assertEqual(output.count(b"SYNTHETIC_TRANSFER:"), 1 if approve else 0)
                if approve:
                    self.assertIn(b"Synthetic result without changes", output)
                    self.assertIn(("SYNTHETIC_TRANSFER:" + AnalysisConsent(report_fixture()).sha256).encode(), output)
                self.assertNotIn(b"skopiuj", output)
            finally:
                process.kill() if process.poll() is None else None
                process.wait()
                os.close(master)


class ConciseAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("ai_desktop.agent_profiles.resolve", return_value=DEFAULT_PROFILE))
        self.output = io.StringIO()
        self.output.isatty = lambda: True
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.enterContext(patch.object(analysis_cli.os, "geteuid", return_value=1000))
        self.enterContext(patch.object(analysis_cli.os, "getuid", return_value=1000))
        self.enterContext(patch.object(analysis_cli.sys.stdin, "isatty", return_value=True))
        self.ready = self.enterContext(patch.object(analysis_cli, "installation_ready"))
        self.report = report_fixture()
        self.collect = self.enterContext(patch.object(analysis_cli.diagnose, "collect", return_value=self.report))
        self.events = []
        @contextlib.contextmanager
        def session(**kwargs):
            self.events.append("start")
            try:
                yield
            finally:
                self.events.append("cleanup")
        self.session = self.enterContext(patch.object(analysis_cli, "managed_session", side_effect=session))
        def send(approval):
            self.events.append("send")
            return "SYNTHETIC ANSWER"
        self.send = self.enterContext(patch.object(analysis_cli, "send_once", side_effect=send))

    def test_default_shows_complete_data_before_consent_and_answer_after_cleanup(self):
        import json
        question = "Które elementy wymagają sprawdzenia?"
        def reader(prompt):
            self.assertEqual(prompt, PROMPTS["transfer"])
            output = self.output.getvalue()
            self.assertEqual(json.loads(output.split("Report to analyze:\n", 1)[1].split("\nQuestion:\n", 1)[0]), self.report)
            self.assertIn(question, output)
            self.assertIn("Claude will receive the data above", output)
            self.session.assert_not_called()
            self.send.assert_not_called()
            return "yes"
        def render(value, **kwargs):
            self.assertEqual(self.events, ["start", "send", "cleanup"])
            return value
        with patch.object(analysis_cli, "render_answer", side_effect=render):
            self.assertEqual(analysis_cli.main(question=question, reader=reader), 0)
        self.session.assert_called_once_with(quiet=True)
        self.send.assert_called_once()
        payload = json.loads(self.send.call_args.args[0].payload)
        self.assertEqual(payload["report"], self.report)
        self.assertEqual(payload["question"], question)
        output = self.output.getvalue()
        self.assertIn("SYNTHETIC ANSWER", output)
        self.assertEqual(output.count(analysis_cli.ANSWER_SEPARATOR), 2)
        for technical in ("ANALYSIS:", "SESSION:", "SHA256", "APPLICATION INSTRUCTIONS",
                          "RULES AND RECIPIENT", analysis_cli.SYSTEM_PROMPT, analysis_cli.CLIENT_CONTEXT):
            self.assertNotIn(technical, output)

    def test_details_retains_full_diagnostic_preview_and_markers(self):
        self.assertEqual(analysis_cli.main(details=True, reader=lambda _: "y"), 0)
        self.session.assert_called_once_with()
        output = self.output.getvalue()
        for expected in ("=== REPORT TO ANALYZE ===", "=== YOUR QUESTION ===", analysis_cli.SYSTEM_PROMPT,
                         analysis_cli.CLIENT_CONTEXT, analysis_cli.CLIENT_VERSION, analysis_cli.POLICY,
                         "ANALYSIS: PASS sha256=" + AnalysisConsent(self.report).sha256, "SESSION: STOPPED"):
            self.assertIn(expected, output)
        self.assertNotIn(analysis_cli.ANSWER_SEPARATOR, output)

    def test_short_offline_preview_keeps_exact_data_without_runtime_or_markers(self):
        with patch.object(analysis_cli.sys.stdin, "isatty", return_value=False), \
                patch.object(analysis_cli, "confirm") as confirm:
            self.assertEqual(cli.main(["analyze", "--preview", "--question", "Dlaczego?"]), 0)
        self.collect.assert_called_once_with()
        self.ready.assert_not_called()
        self.session.assert_not_called()
        self.send.assert_not_called()
        confirm.assert_not_called()
        output = self.output.getvalue()
        self.assertEqual(json.loads(output.split("Report to analyze:\n", 1)[1].split("\nQuestion:\n", 1)[0]), self.report)
        self.assertIn("Dlaczego?", output)
        self.assertIn("Offline preview — nothing was sent.", output)
        self.assertNotIn("PASS", output)
        self.assertNotIn("SHA256", output)
        self.assertNotIn(analysis_cli.SYSTEM_PROMPT, output)

    def test_short_refusal_or_eof_never_start_session(self):
        for answer in ("no", EOFError(), KeyboardInterrupt(), OSError("PRIVATE")):
            def reader(_):
                if isinstance(answer, BaseException):
                    raise answer
                return answer
            self.output.seek(0)
            self.output.truncate(0)
            self.assertEqual(analysis_cli.main(reader=reader), 2)
            expected = "Could not prepare the analysis." if isinstance(answer, OSError) else "Cancelled."
            self.assertIn(expected, self.output.getvalue())
            self.assertNotIn("ANALYSIS:", self.output.getvalue())
            self.assertNotIn("PRIVATE", self.output.getvalue())
        self.session.assert_not_called()
        self.send.assert_not_called()

    def test_interactive_question_uses_fresh_bounded_input(self):
        import json
        with patch.object(analysis_cli, "fresh_input", return_value="Dlaczego?") as fresh, \
                patch.object(analysis_cli, "confirm", return_value=True):
            self.assertEqual(analysis_cli.main(ask=True), 0)
        fresh.assert_called_once_with("Question about the report (Enter = general analysis): ",
                                      max_bytes=analysis_cli.MAX_QUESTION_BYTES)
        self.assertEqual(json.loads(self.send.call_args.args[0].payload)["question"], "Dlaczego?")

    def test_cleanup_failure_withholds_answer_and_success(self):
        from ai_desktop.analysis_contract import AnalysisDenied
        @contextlib.contextmanager
        def failed(**kwargs):
            yield
            raise AnalysisDenied("cleanup")
        self.session.side_effect = failed
        with patch.object(analysis_cli, "render_answer") as render:
            self.assertEqual(analysis_cli.main(reader=lambda _: "y"), 2)
        render.assert_not_called()
        self.assertIn("ai-desktop stop", self.output.getvalue())
        self.assertNotIn("SYNTHETIC ANSWER", self.output.getvalue())
        self.assertNotIn("PASS", self.output.getvalue())

    def test_short_errors_and_interrupts_are_finite_and_never_retry(self):
        for failure in (ValueError("PRIVATE"), KeyboardInterrupt()):
            self.send.side_effect = failure
            self.output.seek(0)
            self.output.truncate(0)
            self.send.reset_mock()
            self.assertEqual(analysis_cli.main(reader=lambda _: "y"), 2)
            self.send.assert_called_once()
            self.assertNotIn("PRIVATE", self.output.getvalue())
            self.assertNotIn("ANALYSIS:", self.output.getvalue())
            if isinstance(failure, KeyboardInterrupt):
                self.assertIn("Data already sent cannot be taken back", self.output.getvalue())

    def test_short_flow_rechecks_expiry_after_session_setup(self):
        clock = [0]
        gate = AnalysisConsent(self.report, clock=lambda: clock[0])
        @contextlib.contextmanager
        def session(**kwargs):
            clock[0] = 181
            try:
                yield
            finally:
                self.events.append("cleanup")
        self.session.side_effect = session
        with patch.object(analysis_cli, "AnalysisConsent", return_value=gate):
            self.assertEqual(analysis_cli.main(reader=lambda _: "y"), 2)
        self.session.assert_called_once_with(quiet=True)
        self.send.assert_not_called()
        self.assertEqual(self.events, ["cleanup"])
