"""Fresh consent, cleanup and bounded planner rounds with synthetic transports."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stdout
import copy
import io
import json
import os
import re
from pathlib import Path
import pty
import select
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import ask_cli as ask, assistant_flow, cli, diagnose, file_context, question_consent
from ai_desktop.permissions import Limits, Scope
from ai_desktop.analysis_contract import AnalysisConsent, AnalysisDenied, ContextBudgetExceeded, InputBudgetExceeded
from ai_desktop.terminal_consent import PROMPTS
from test_weather_context import CONTEXT as WEATHER

ROOT = Path(__file__).resolve().parents[1]
EMPTY = {"files": [], "systemd": [], "weather": False}
ACCESS = {"files": [{"id": "f0", "pattern": "~/example/*"}], "systemd": ["system"], "weather": False}
FILES = {"kind": "files", "files": [{"path": "/tmp/example/config.txt", "content": "PRIVATE CONTENT"}]}
PLAN = {"action": "read", "files": ["f0"], "systemd": [], "weather": None}
WEB_ACCESS = {**ACCESS, "web": True}
WEB_SOURCES = {"claude": True, "systemd": [], "weather": False, "web": True}
WEB_PLAN = {"action": "web"}
DEFAULT_PROFILE = {"provider": "claude", "model": "default", "effort": "default", "instructions": "",
                   "profile": "default", "access": "changes"}



def _no_user_time_exemption():
    """Deadline-enforcement tests: neutralise the 'user time is not counted' pause so a plain clock jump still expires."""
    @contextmanager
    def waiting(self):
        yield
    return patch.object(question_consent.QuestionConsent, "waiting", waiting)


class AskCliTests(unittest.TestCase):
    def run_ui(self, argv=None, *, answers=("Co to jest DNS?", "yes"), gate=None,
               session=None, send=None, context=None, policy=None, access=None,
               sources=None, capture_error=None, uid=1000, tty=True, planner=None, collect_source=None,
               context_gate_error=None, stay=False):
        out = io.StringIO()
        out.isatty = lambda: tty
        if "--question" not in (argv or []) and "--preview" not in (argv or []) and not stay:
            answers = tuple(answers) + ("",)  # an empty line ends the interactive conversation
        reader, gates = Mock(side_effect=answers), []
        snapshot = assistant_flow.AccessSnapshot(policy if policy is not None else Mock(),
            sources or {"claude": True, "systemd": [], "weather": False},
            copy.deepcopy(EMPTY if access is None else access))
        snapshot.policy.select.side_effect = lambda paths, limit=None: tuple(paths)
        if not isinstance(snapshot.policy.limits.return_value, Limits):
            snapshot.policy.limits.return_value = Limits()
        def gate_factory(*args, **kwargs):
            if context_gate_error is not None and kwargs.get("context") is not None:
                raise context_gate_error
            value = gate if gate is not None else AnalysisConsent(*args, **kwargs)
            gates.append(value)
            return value
        def default_send(approval):
            mode = json.loads(approval.payload)["mode"]
            if mode == "assistant":
                return json.dumps(planner if planner is not None else
                                  {"action": "answer", "text": "**DNS** mapuje nazwy."})
            return "Wyjaśnienie danych."
        with ExitStack() as stack:
            # This transport harness owns its synthetic settings; never read
            # the runner's private profile or pwd entry for the mocked UID.
            stack.enter_context(patch("ai_desktop.agent_profiles.resolve", return_value=DEFAULT_PROFILE))
            stack.enter_context(patch.object(ask.os, "geteuid", return_value=uid))
            stack.enter_context(patch.object(ask.os, "getuid", return_value=uid))
            stack.enter_context(patch.object(ask.sys.stdin, "isatty", return_value=tty))
            ready = stack.enter_context(patch.object(ask, "installation_ready"))
            capture = stack.enter_context(patch.object(assistant_flow, "capture_access", return_value=snapshot,
                                                      side_effect=capture_error))
            collect = stack.enter_context(patch.object(assistant_flow, "collect", return_value=context or [],
                                                       side_effect=collect_source))
            report = stack.enter_context(patch.object(diagnose, "collect", side_effect=AssertionError("implicit report")))
            started = stack.enter_context(patch.object(ask, "managed_session", side_effect=session or (lambda **_: nullcontext())))
            sent = stack.enter_context(patch.object(ask, "send_once", side_effect=send or default_send))
            stack.enter_context(patch.object(ask, "AnalysisConsent", side_effect=gate_factory))
            stack.enter_context(redirect_stdout(out))
            code = ask.main(argv or [], reader=reader)
        if len(answers) > 0 and answers[-1] == "" and reader.call_args_list and reader.call_args_list[-1].args == (ask.QUESTION_PROMPT,) \
                and reader.call_count == len(answers) and not stay and "--question" not in (argv or []) and "--preview" not in (argv or []):
            # Hide the closing empty line of the interactive loop from legacy one-turn assertions.
            reader.call_args_list.pop()
            reader.call_count -= 1
        report.assert_not_called()
        return SimpleNamespace(code=code, output=out.getvalue(), reader=reader, ready=ready, capture=capture,
                               collect=collect, started=started, sent=sent, gates=gates, snapshot=snapshot)

    def test_general_answer_has_no_implicit_report_or_technical_noise(self):
        r = self.run_ui()
        self.assertEqual(r.code, 0, r.output)
        self.assertEqual([c.args[0] for c in r.reader.call_args_list], [ask.QUESTION_PROMPT, PROMPTS["ask-question"]])
        r.ready.assert_called_once_with()
        r.started.assert_called_once_with(quiet=True)
        r.collect.assert_not_called()
        data = json.loads(r.sent.call_args.args[0].payload)
        self.assertEqual((data["mode"], data["question"], data["access"]), ("assistant", "Co to jest DNS?", EMPTY))
        for key in ("report", "task", "context"):
            self.assertNotIn(key, data)
        self.assertIn("DNS", r.output)
        self.assertEqual(r.output.count(ask.ANSWER_SEPARATOR), 2)
        for technical in ("SHA256", "ASK: PASS", "SESSION:", "system_prompt", "client_context", '"action"', "Access:"):
            self.assertNotIn(technical, r.output)

    def test_details_shows_exact_payload_and_explicit_question_skips_prompt(self):
        for flag in ("--v", "--verbose", "-v", "--details"):
            with self.subTest(flag=flag):
                r = self.run_ui(["--question", "Co to jest DNS?", flag], answers=("Yes",))
                self.assertEqual(r.code, 0, r.output)
                self.assertTrue(r.output.startswith("Co to jest DNS?\n\n"))
                r.reader.assert_called_once_with(PROMPTS["ask-question"])
                r.started.assert_called_once_with()
                self.assertIn(r.sent.call_args.args[0].sha256, r.output)
                self.assertIn('"mode": "assistant"', r.output)
                self.assertIn("ASK: PASS", r.output)
                self.assertIn("SESSION: STOPPED", r.output)

    def test_preview_is_offline_without_terminal_and_reads_no_targets(self):
        r = self.run_ui(["--question", "Pytanie", "--preview"], answers=(), tty=False, access=ACCESS)
        self.assertEqual(r.code, 0, r.output)
        for mock in (r.reader, r.ready, r.collect, r.started, r.sent):
            mock.assert_not_called()
        r.capture.assert_called_once_with()
        self.assertIn("~/example/*", r.output)
        self.assertIn("Nothing was sent", r.output)

    def test_first_decline_prevents_model_and_sources(self):
        r = self.run_ui(answers=("Pytanie", "no"), access=ACCESS, planner=PLAN)
        self.assertEqual(r.code, 2)
        self.assertIn("Cancelled", r.output)
        for mock in (r.collect, r.started, r.sent):
            mock.assert_not_called()

    def test_file_read_refusal_stops_after_planner_with_one_local_hint_and_no_status_advice(self):
        for details in (False, True):
            for reason, expected in (("files-size", f"{Limits().read_file_size} bytes per file"),
                                     ("PRIVATE\n\x1b[31m", "Could not verify access")):
                with self.subTest(details=details, reason=reason):
                    policy = Mock()
                    policy.limits.return_value = Limits(plan_corrections=0)
                    policy.access.return_value = ACCESS
                    policy.scopes.return_value = (Scope(0, "~/example/*"),)
                    policy.expand.return_value = (FILES["files"][0]["path"],)
                    with patch.object(file_context.permissions, "read_selected", side_effect=ValueError(reason)) as read:
                        r = self.run_ui(["--details"] if details else [], access=ACCESS, planner=PLAN,
                                        policy=policy, collect_source=assistant_flow.collect,
                                        answers=("Sprawdź konfigurację", "yes"))
                    self.assertEqual(r.code, 2)
                    read.assert_called_once_with(policy, policy.expand.return_value)
                    r.sent.assert_called_once()
                    r.ready.assert_called_once()
                    r.started.assert_called_once()
                    self.assertEqual([call.args[0] for call in r.reader.call_args_list],
                                     [ask.QUESTION_PROMPT, PROMPTS["ask-question"]])
                    self.assertIn(expected, r.output)
                    self.assertEqual(r.output.count("File read refused:"), 1)
                    self.assertIn("The file contents were not sent to Claude.", r.output)
                    for unwanted in ("ai-desktop status", "ANALYSIS: REFUSED", "Could not get an answer",
                                     "PRIVATE", "\x1b"):
                        self.assertNotIn(unwanted, r.output)
                    self.assertNotIn("PRIVATE CONTENT", r.sent.call_args.args[0].payload)

    def test_context_budget_refusal_stops_before_second_transfer_or_session(self):
        context = [{"kind": "files", "files": [{"path": "/tmp/example/config.txt",
                                                "content": "PRIVATE BUDGET\n" + '"' * 13000}]}]
        for details in (False, True):
            with self.subTest(details=details):
                r = self.run_ui(["--details"] if details else [], access=ACCESS, planner=PLAN,
                                context=context, answers=("Sprawdź konfigurację", "yes"),
                                context_gate_error=ContextBudgetExceeded("PRIVATE BUDGET"))
                self.assertEqual(r.code, 2)
                self.assertIn(f"the JSON context is over the limit of {ask.MAX_CONTEXT_BYTES} bytes", r.output)
                self.assertIn("The selected data was not sent to Claude.", r.output)
                self.assertEqual([call.args[0] for call in r.reader.call_args_list],
                                 [ask.QUESTION_PROMPT, PROMPTS["ask-question"]])
                r.ready.assert_called_once()
                r.started.assert_called_once()
                r.sent.assert_called_once()
                self.assertEqual(len(r.gates), 1)
                for unwanted in ("PRIVATE BUDGET", "ai-desktop status", "context limit", "ASK: PASS"):
                    self.assertNotIn(unwanted, r.output)

    def test_only_typed_packet_budget_error_gets_size_hint_without_raw_exception(self):
        for details in (False, True):
            for error in (InputBudgetExceeded("PRIVATE RAW EXCEPTION"), ValueError("PRIVATE RAW EXCEPTION")):
                with self.subTest(details=details, error=type(error).__name__):
                    r = self.run_ui(["--details"] if details else [], access=ACCESS, planner=PLAN,
                                    context=[FILES], answers=("Sprawdź konfigurację", "yes"),
                                    context_gate_error=error)
                    self.assertEqual(r.code, 2)
                    self.assertEqual(f"the whole JSON request is over the limit of {ask.MAX_PACKET} bytes" in r.output,
                                     isinstance(error, InputBudgetExceeded))
                    self.assertEqual(r.reader.call_count, 2)
                    r.ready.assert_called_once()
                    r.started.assert_called_once()
                    r.sent.assert_called_once()
                    self.assertNotIn("PRIVATE RAW EXCEPTION", r.output)
                    self.assertNotIn("PRIVATE CONTENT", r.output)
                    self.assertNotIn("ai-desktop status", r.output)

    def test_large_escaped_context_is_transferred_whole_with_one_question_approval(self):
        context = [{"kind": "files", "files": [{"path": "/tmp/example/config.txt", "content": '"' * 13000}]}]
        r = self.run_ui(access=ACCESS, planner=PLAN, context=context, answers=("Pytanie", "yes"))
        self.assertEqual(r.code, 0, r.output)
        self.assertEqual(r.sent.call_count, 2)
        payload = json.loads(r.sent.call_args_list[1].args[0].payload)
        self.assertEqual(payload["context"], context)
        self.assertEqual(r.reader.call_count, 2)

    def test_blank_and_unknown_confirmation_reprompt_before_yes(self):
        r = self.run_ui(answers=("Pytanie", "", "?", "y"))
        self.assertEqual(r.code, 0, r.output)
        self.assertEqual(r.reader.call_count, 4)
        r.started.assert_called_once()
        r.sent.assert_called_once()

    def test_root_pipe_conflicting_flags_and_invalid_question_refuse_before_policy(self):
        cases = [([], {"uid": 0}), ([], {"tty": False}),
                 (["--question", "Pytanie", "--no-context", "--weather", "Warszawa"], {}),
                 (["--question", "Pytanie", "--no-context", "--systemd"], {}),
                 ([], {"answers": ("x\x1b",)}), ([], {"answers": ("x" * (128 * 1024 + 1),)})]
        for argv, kwargs in cases:
            with self.subTest(argv=argv, kwargs=kwargs):
                r = self.run_ui(argv, **kwargs)
                self.assertEqual(r.code, 2)
                for mock in (r.capture, r.ready, r.collect, r.started, r.sent):
                    mock.assert_not_called()

    def test_empty_question_reprompts_and_explicit_empty_refuses(self):
        r = self.run_ui(answers=("", " ", "Pytanie", "no"))
        self.assertEqual(r.code, 2)
        self.assertEqual([c.args[0] for c in r.reader.call_args_list],
                         [ask.QUESTION_PROMPT] * 3 + [PROMPTS["ask-question"]])
        r.capture.assert_called_once_with()
        r.sent.assert_not_called()
        r = self.run_ui(["--question", ""], answers=())
        self.assertEqual(r.code, 2)
        r.capture.assert_not_called()

    def test_question_or_first_approval_interrupt_does_not_send(self):
        for answers in ((EOFError(),), (KeyboardInterrupt(),), ("Pytanie", EOFError()), ("Pytanie", KeyboardInterrupt())):
            r = self.run_ui(answers=answers)
            self.assertEqual(r.code, 2)
            self.assertIn("Cancelled", r.output)
            r.started.assert_not_called()
            r.sent.assert_not_called()

    def test_expiry_during_session_startup_cleans_up_before_send(self):
        clock, events = [0], []
        gate = AnalysisConsent(None, question="Pytanie", access=EMPTY, clock=lambda: clock[0])
        @contextmanager
        def session(**kwargs):
            clock[0] = 181
            try:
                yield
            finally:
                events.append("cleanup")
        r = self.run_ui(answers=("Pytanie", "yes"), gate=gate, session=session)
        self.assertEqual(r.code, 2)
        self.assertEqual(events, ["cleanup"])
        r.sent.assert_not_called()

    def test_a_timeout_says_how_long_the_last_call_waited_and_what_the_limit_was(self):
        now = [1000.0]
        @contextmanager
        def session(**kwargs):
            yield
        def send(_):
            now[0] += 77                                   # the model call that ran out of time
            raise AnalysisDenied("timeout")
        with patch.object(ask.time, "monotonic", side_effect=lambda: now[0]):
            r = self.run_ui(session=session, send=send)
        self.assertEqual(r.code, 2, r.output)
        self.assertIn("The last call waited 77 s (limit 150 s).", r.output)

    def test_success_waits_for_cleanup_and_provider_failure_is_not_retried(self):
        for failure in (None, AnalysisDenied("client"), AnalysisDenied("auth-required")):
            events = []
            @contextmanager
            def session(**kwargs):
                events.append("start")
                try:
                    yield
                finally:
                    events.append("cleanup")
            def send(_):
                events.append("send")
                if failure:
                    raise failure
                return json.dumps({"action": "answer", "text": "Odpowiedź"})
            r = self.run_ui(session=session, send=send)
            self.assertEqual(r.code, 0 if failure is None else 2, r.output)
            self.assertEqual(events, ["start", "send", "cleanup"])
            self.assertEqual(ask.ANSWER_SEPARATOR in r.output, failure is None)
            r.sent.assert_called_once()

    def test_cleanup_failure_withholds_answer_and_private_error_details(self):
        @contextmanager
        def session(**kwargs):
            yield
            raise AnalysisDenied("cleanup")
        r = self.run_ui(session=session, send=lambda _: '{"action":"answer","text":"PRIVATE ANSWER"}')
        self.assertEqual(r.code, 2)
        self.assertNotIn("PRIVATE ANSWER", r.output)
        self.assertNotIn("REFUSED reason=", r.output)
        self.assertIn("ai-desktop stop", r.output)

    def test_two_rounds_have_distinct_nonces_and_exact_separate_payloads(self):
        for details in (False, True):
            with self.subTest(details=details):
                r = self.run_ui(["--details"] if details else [], access=ACCESS, planner=PLAN,
                                context=[FILES], answers=("Pytanie", "yes"))
                self.assertEqual(r.code, 0, r.output)
                self.assertEqual(r.sent.call_count, 2)
                self.assertEqual(r.started.call_count, 2)
                self.assertEqual(len(r.gates), 2)
                self.assertNotEqual(r.gates[0].nonce, r.gates[1].nonce)
                self.assertNotEqual(r.gates[0].sha256, r.gates[1].sha256)
                payloads = [json.loads(c.args[0].payload) for c in r.sent.call_args_list]
                self.assertEqual([p["mode"] for p in payloads], ["assistant", "question"])
                self.assertNotIn("PRIVATE CONTENT", r.sent.call_args_list[0].args[0].payload)
                self.assertEqual(payloads[1]["context"], [FILES])
                self.assertEqual("PRIVATE CONTENT" in r.output, details)
                self.assertEqual([c.args[0] for c in r.reader.call_args_list],
                                 [ask.QUESTION_PROMPT, PROMPTS["ask-question"]])
                self.assertEqual(r.output.count("Waiting for"), 1)

    def test_web_uses_one_question_consent_and_only_original_question_in_second_request(self):
        for details in (False, True):
            with self.subTest(details=details), \
                    patch.object(assistant_flow.ask_edit, "prepare", side_effect=AssertionError("local edit")) as edit:
                r = self.run_ui(["--v"] if details else [], access=WEB_ACCESS, sources=WEB_SOURCES,
                                planner=WEB_PLAN, answers=("Jakie są dzisiejsze wiadomości?", "y"))
                self.assertEqual(r.code, 0, r.output)
                self.assertEqual(r.sent.call_count, 2)
                self.assertEqual(r.started.call_count, 2)
                self.assertEqual([c.args[0] for c in r.reader.call_args_list],
                                 [ask.QUESTION_PROMPT, PROMPTS["ask-question"]])
                first, final = [json.loads(c.args[0].payload) for c in r.sent.call_args_list]
                self.assertEqual(first["mode"], "assistant")
                self.assertIs(first["access"]["web"], True)
                self.assertEqual(final["mode"], "web")
                self.assertEqual(final["question"], "Jakie są dzisiejsze wiadomości?")
                self.assertEqual(set(final), {"schema", "policy", "mode", "question", "system_prompt",
                                              "client_context", "client_version"})
                self.assertNotEqual(r.gates[0].nonce, r.gates[1].nonce)
                self.assertNotEqual(r.gates[0].sha256, r.gates[1].sha256)
                r.collect.assert_not_called()
                edit.assert_not_called()
                self.assertEqual(r.output.count("Waiting for"), 1)

    def test_web_cannot_mix_local_data_or_replace_the_original_question(self):
        for plan in (dict(WEB_PLAN, files=["f0"]), dict(WEB_PLAN, context=[FILES]),
                     dict(WEB_PLAN, task={}), dict(WEB_PLAN, question="inne pytanie"),
                     dict(WEB_PLAN, url="http://127.0.0.1/private")):
            with self.subTest(plan=plan):
                r = self.run_ui(access=WEB_ACCESS, sources=WEB_SOURCES, planner=plan)
                self.assertEqual(r.code, 2)
                r.sent.assert_called_once()
                r.collect.assert_not_called()
                self.assertEqual(r.started.call_count, 1)

    def test_web_needs_permission_in_both_source_policy_and_advertised_access(self):
        for access, sources in ((ACCESS, WEB_SOURCES), (WEB_ACCESS, {**WEB_SOURCES, "web": False}),
                                (WEB_ACCESS, {k: v for k, v in WEB_SOURCES.items() if k != "web"})):
            with self.subTest(access=access, sources=sources):
                r = self.run_ui(access=access, sources=sources, planner=WEB_PLAN)
                self.assertEqual(r.code, 2)
                r.sent.assert_called_once()
                r.collect.assert_not_called()
                self.assertEqual(r.started.call_count, 1)

    def test_web_revocation_after_planner_or_during_second_startup_blocks_web_transfer(self):
        for during_startup in (False, True):
            policy, sessions = Mock(), [0]
            @contextmanager
            def session(**kwargs):
                sessions[0] += 1
                if during_startup and sessions[0] == 2:
                    policy.recheck.side_effect = ValueError("PRIVATE POLICY")
                yield
            def send(_):
                if not during_startup:
                    policy.recheck.side_effect = ValueError("PRIVATE POLICY")
                return json.dumps(WEB_PLAN)
            with self.subTest(during_startup=during_startup):
                r = self.run_ui(access=WEB_ACCESS, sources=WEB_SOURCES, policy=policy,
                                session=session, send=send)
                self.assertEqual(r.code, 2)
                self.assertEqual(sessions[0], 2 if during_startup else 1)
                r.sent.assert_called_once()
                r.collect.assert_not_called()
                self.assertNotIn("PRIVATE POLICY", r.output)

    def test_web_reuses_original_deadline_through_second_startup(self):
        now, sessions, cleanup = [0], [0], []
        @contextmanager
        def session(**kwargs):
            sessions[0] += 1
            if sessions[0] == 2:
                now[0] = 181
            try:
                yield
            finally:
                cleanup.append(sessions[0])
        with _no_user_time_exemption(), patch.object(question_consent.time, "monotonic", side_effect=lambda: now[0]):
            r = self.run_ui(access=WEB_ACCESS, sources=WEB_SOURCES, planner=WEB_PLAN, session=session)
        self.assertEqual(r.code, 2)
        self.assertEqual(cleanup, [1, 2])
        self.assertEqual(r.started.call_count, 2)
        r.sent.assert_called_once()
        r.collect.assert_not_called()

    def test_web_policy_revoked_during_final_request_withholds_answer(self):
        policy = Mock()
        def send(auth):
            if json.loads(auth.payload)["mode"] == "assistant":
                return json.dumps(WEB_PLAN)
            policy.recheck.side_effect = ValueError("PRIVATE POLICY")
            return "PRIVATE WEB ANSWER"
        r = self.run_ui(access=WEB_ACCESS, sources=WEB_SOURCES, policy=policy, send=send)
        self.assertEqual(r.code, 2)
        self.assertEqual(r.sent.call_count, 2)
        self.assertNotIn("PRIVATE", r.output)
        self.assertNotIn(ask.ANSWER_SEPARATOR, r.output)
        r.collect.assert_not_called()

    def test_web_no_results_has_fixed_hint_and_no_retry_or_unverified_answer(self):
        cleanup, sessions = [], [0]
        @contextmanager
        def session(**kwargs):
            sessions[0] += 1
            try:
                yield
            finally:
                cleanup.append(sessions[0])
        def send(auth):
            if json.loads(auth.payload)["mode"] == "assistant":
                return json.dumps(WEB_PLAN)
            raise AnalysisDenied("web-no-results")
        r = self.run_ui(access=WEB_ACCESS, sources=WEB_SOURCES, send=send, session=session)
        self.assertEqual(r.code, 2)
        self.assertEqual(r.sent.call_count, 2)
        self.assertEqual(cleanup, [1, 2])
        self.assertIn("No confirmed search results came back", r.output)
        self.assertNotIn(ask.ANSWER_SEPARATOR, r.output)
        r.collect.assert_not_called()

    def test_no_context_disables_web_even_when_policy_allows_it(self):
        r = self.run_ui(["--question", "Wiadomości?", "--no-context"], answers=("y",),
                        access=WEB_ACCESS, sources=WEB_SOURCES, planner=WEB_PLAN)
        self.assertEqual(r.code, 0, r.output)
        r.sent.assert_called_once()
        r.collect.assert_not_called()
        payload = json.loads(r.sent.call_args.args[0].payload)
        self.assertEqual((payload["mode"], payload["context"]), ("question", []))
        self.assertNotIn("web", payload)

    def test_web_request_without_question_scoped_permission_is_rejected_before_startup(self):
        with patch.object(ask, "installation_ready") as ready, \
                patch.object(ask, "managed_session") as session, patch.object(ask, "send_once") as send:
            with self.assertRaises(AnalysisDenied):
                ask.request("Wiadomości?", SimpleNamespace(details=False), Mock(), web=True, check=Mock())
        ready.assert_not_called()
        session.assert_not_called()
        send.assert_not_called()

    def test_shared_deadline_after_planner_blocks_reads_and_second_send(self):
        now = [0]
        def send(_):
            now[0] = 181
            return json.dumps(PLAN)
        with patch.object(question_consent.time, "monotonic", side_effect=lambda: now[0]):
            r = self.run_ui(access=ACCESS, planner=PLAN, context=[FILES], answers=("Pytanie", "yes"), send=send)
            self.assertEqual(r.code, 2)
            r.sent.assert_called_once()
            r.collect.assert_not_called()
            self.assertNotIn("PRIVATE CONTENT", r.sent.call_args.args[0].payload)
            self.assertNotIn("PRIVATE CONTENT", r.output)
            self.assertEqual(r.reader.call_count, 2)

    def test_mutated_final_context_during_second_session_is_refused(self):
        context = [copy.deepcopy(FILES)]
        calls = [0]
        @contextmanager
        def session(**kwargs):
            calls[0] += 1
            if calls[0] == 2:
                context[0]["files"][0]["content"] = "changed"
            yield
        r = self.run_ui(access=ACCESS, planner=PLAN, context=context, session=session,
                        answers=("Pytanie", "yes"))
        self.assertEqual(r.code, 2)
        r.sent.assert_called_once()
        self.assertEqual(r.started.call_count, 2)

    def test_policy_revoked_during_second_sudo_wait_prevents_second_send(self):
        policy, sessions = Mock(), [0]
        @contextmanager
        def session(**kwargs):
            sessions[0] += 1
            if sessions[0] == 2:
                policy.recheck.side_effect = ValueError("PRIVATE POLICY")
            yield
        r = self.run_ui(access=ACCESS, planner=PLAN, context=[FILES], policy=policy,
                        session=session, answers=("Pytanie", "yes"))
        self.assertEqual(r.code, 2)
        self.assertEqual(sessions[0], 2)
        r.sent.assert_called_once()
        self.assertNotIn("PRIVATE POLICY", r.output)

    def test_time_spent_starting_the_session_for_the_sudo_password_is_not_counted(self):
        now, sessions = [0], [0]
        @contextmanager
        def session(**kwargs):
            sessions[0] += 1
            if sessions[0] == 2:
                now[0] = 900                      # the user took a long time typing the sudo password
            yield
        with patch.object(question_consent.time, "monotonic", side_effect=lambda: now[0]):
            r = self.run_ui(access=ACCESS, planner=PLAN, context=[FILES], session=session,
                            answers=("Pytanie", "yes"))
        self.assertEqual(r.code, 0)
        self.assertEqual(r.started.call_count, 2)

    def test_shared_deadline_expires_during_second_session_and_cleanup_still_runs(self):
        now, sessions, cleanup = [0], [0], []
        @contextmanager
        def session(**kwargs):
            sessions[0] += 1
            if sessions[0] == 2:
                now[0] = 181
            try:
                yield
            finally:
                cleanup.append(sessions[0])
        with _no_user_time_exemption(), patch.object(question_consent.time, "monotonic", side_effect=lambda: now[0]):
            r = self.run_ui(access=ACCESS, planner=PLAN, context=[FILES], session=session,
                            answers=("Pytanie", "yes"))
        self.assertEqual(r.code, 2)
        self.assertEqual(cleanup, [1, 2])
        self.assertEqual(r.started.call_count, 2)
        r.sent.assert_called_once()
        self.assertEqual(r.reader.call_count, 2)

    def test_explicit_weather_uses_same_single_upfront_approval(self):
        access = {"files": [], "systemd": [], "weather": True}
        sources = {"claude": True, "systemd": [], "weather": True}
        for answer in ("yes", "no"):
            with self.subTest(answer=answer):
                r = self.run_ui(["--question", "Pogoda?", "--weather", "Warszawa"],
                                access=access, sources=sources, context=[WEATHER], answers=(answer,))
                self.assertEqual(r.code, 0 if answer == "yes" else 2, r.output)
                r.reader.assert_called_once_with(PROMPTS["ask-question"])
                self.assertEqual(r.sent.call_count, 1 if answer == "yes" else 0)
                self.assertEqual(r.collect.call_count, 1 if answer == "yes" else 0)
                if answer == "yes":
                    self.assertEqual(json.loads(r.sent.call_args.args[0].payload)["context"], [WEATHER])

    def test_standalone_request_keeps_fresh_consent_and_boolean_cannot_skip_it(self):
        args = SimpleNamespace(details=False)
        reader, check = Mock(), Mock()
        with patch.object(ask, "confirm", return_value=False) as confirm, \
                patch.object(ask, "installation_ready") as ready, \
                patch.object(ask, "managed_session") as session, \
                patch.object(ask, "send_once") as send, redirect_stdout(io.StringIO()):
            with self.assertRaises(assistant_flow.Declined):
                ask.request("Pytanie", args, reader, context=[], check=check)
            confirm.assert_called_once_with("ask-transfer", reader=reader, config=None)
            ready.assert_called_once()
            session.assert_not_called()
            send.assert_not_called()
            confirm.reset_mock()
            ready.reset_mock()
            with self.assertRaises(AnalysisDenied):
                ask.request("Pytanie", args, reader, context=[], check=check, question_consent=True)
            confirm.assert_not_called()
            ready.assert_not_called()
            session.assert_not_called()
            send.assert_not_called()

    def test_no_context_and_explicit_flags_cannot_bypass_claude_denial_or_bad_config(self):
        for argv in (["--question", "Pytanie", "--no-context"],
                     ["--question", "Pytanie", "--systemd", "system"]):
            for kwargs in ({"sources": {"claude": False, "systemd": [], "weather": False}},
                           {"capture_error": ValueError("PRIVATE CONFIG")}):
                r = self.run_ui(argv, **kwargs)
                self.assertEqual(r.code, 2)
                for mock in (r.ready, r.collect, r.started, r.sent):
                    mock.assert_not_called()
                self.assertNotIn("PRIVATE CONFIG", r.output)

    def test_no_context_is_one_question_transfer_after_policy_check(self):
        r = self.run_ui(["--question", "Pytanie", "--no-context"], answers=("yes",))
        self.assertEqual(r.code, 0, r.output)
        r.capture.assert_called_once_with()
        r.collect.assert_not_called()
        data = json.loads(r.sent.call_args.args[0].payload)
        self.assertEqual((data["mode"], data["context"]), ("question", []))

    def test_invalid_policy_has_a_fixed_helpful_hint_without_raw_configuration(self):
        r = self.run_ui(capture_error=assistant_flow.AccessDenied("policy-invalid"))
        self.assertEqual(r.code, 2)
        self.assertIn("Invalid permissions.toml. Check version and the read/write/deny rules.", r.output)
        r.sent.assert_not_called()
        r = self.run_ui(capture_error=assistant_flow.AccessDenied("PRIVATE CONFIG CONTENT"))
        self.assertEqual(r.code, 2)
        self.assertNotIn("PRIVATE", r.output)
        self.assertNotIn("Invalid permissions.toml", r.output)

    def test_dispatcher_passes_ask_arguments_without_probing(self):
        with patch.object(ask, "main", return_value=0) as main:
            self.assertEqual(cli.main(["ask", "--question", "Pytanie"]), 0)
        main.assert_called_once_with(["--question", "Pytanie"])


class AskTerminalTests(unittest.TestCase):
    def test_question_and_approval_pasted_together_need_fresh_approval(self):
        script = """
from contextlib import nullcontext
from unittest.mock import Mock, patch
from ai_desktop import ask_cli, cli, assistant_flow, agent_profiles
default_profile = {'provider': 'claude', 'model': 'default', 'effort': 'default', 'instructions': '', 'profile': 'default', 'access': 'changes'}
def send(approval):
    print('SYNTHETIC_TRANSFER', flush=True)
    return 'wynik'
with patch.object(ask_cli, '_choose_thread', return_value=('new', None)), patch.object(agent_profiles, 'resolve', return_value=default_profile), patch.object(ask_cli.os, 'getuid', return_value=1000), patch.object(ask_cli.os, 'geteuid', return_value=1000), patch.object(assistant_flow, 'capture_access', return_value=assistant_flow.AccessSnapshot(Mock(), {'claude': True, 'systemd': [], 'weather': False}, {'files': [], 'systemd': [], 'weather': False})), patch.object(ask_cli, 'installation_ready'), patch.object(ask_cli, 'managed_session', side_effect=lambda **kwargs: nullcontext()), patch.object(ask_cli, 'send_once', side_effect=send):
    raise SystemExit(cli.main(['ask']))
"""
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, "-B", "-c", script], cwd=ROOT, env={**os.environ, "NO_COLOR": "1"},
                                   stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        output = bytearray()
        try:
            stage, deadline, confirmation_seen = 0, time.monotonic() + 10, None
            while time.monotonic() < deadline:
                if select.select([master], [], [], .02)[0]:
                    try:
                        raw = os.read(master, 8192)
                    except OSError:
                        break
                    if not raw:
                        break
                    output.extend(raw)
                if stage == 0 and ask.QUESTION_PROMPT.encode() in output.replace(b"\r\n", b"\n"):
                    # More than the ordinary 64-byte confirmation bound.
                    os.write(master, ("Wyjaśnij DNS. " * 12 + "\nyes\n").encode())
                    stage = 1
                if stage == 1 and PROMPTS["ask-question"].encode() in output:
                    confirmation_seen = time.monotonic()
                    stage = 2
                if stage == 2 and time.monotonic() - confirmation_seen >= .2:
                    self.assertNotIn(b"SYNTHETIC_TRANSFER", output)
                    self.assertIsNone(process.poll())
                    os.write(master, b"no\n")
                    stage = 3
                if (stage == 3 and "Cancelled".encode() in output
                        and b"> " in output[output.find("Cancelled".encode()):]):
                    os.write(master, b"\n")                 # the declined turn leaves the conversation open: end it
                    stage = 4
                if process.poll() is not None and not select.select([master], [], [], .02)[0]:
                    break
            self.assertEqual(process.wait(timeout=2), 2, output.decode(errors="replace"))
            self.assertEqual(stage, 4, output.decode(errors="replace"))
            plain = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", output.replace(b"\r\n", b"\n"))      # the prompt is coloured on a terminal
            self.assertIn(b"\n\n" + PROMPTS["ask-question"].encode(), plain)
            self.assertNotIn(b"SYNTHETIC_TRANSFER", output)
            self.assertIn("Cancelled".encode(), output)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            os.close(master)
