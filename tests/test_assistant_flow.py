"""Generic planning obeys one captured policy and never loops through tools."""
import contextlib
import copy
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import assistant_flow as flow
from ai_desktop.analysis_contract import AnalysisDenied
from test_weather_context import CONTEXT as WEATHER


ACCESS = {"files": [{"id": "f0", "pattern": "~/project/*"}],
          "systemd": ["system", "user"], "weather": True}
SOURCES = {"claude": True, "systemd": ["system", "user"], "weather": True}
FILES = {"kind": "files", "files": [{"path": "/tmp/project/info.txt", "content": "PRIVATE DATA"}]}
SYSTEMD = {"kind": "systemd", "source": "systemctl", "scope": "system", "view": "enabled",
           "listing": "unit-files", "observed_at": "2026-10-30T12:00:00Z", "status": "ok",
           "truncated": False, "total": 1,
           "items": [{"name": "synthetic.service", "state": "enabled", "substate": None}]}
READ = {"action": "read", "files": ["f0"], "systemd": [{"scope": "system", "view": "enabled"}],
        "weather": "Warszawa"}


def options(**changes):
    return SimpleNamespace(**{"no_context": False, "preview": False, "details": False,
                             "systemd": None, "systemd_state": None, "weather": None, **changes})


class AssistantFlowTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.policy = Mock()
        self.policy.sources.return_value = copy.deepcopy(SOURCES)
        self.policy.access.return_value = copy.deepcopy(ACCESS)
        self.load = self.enterContext(patch.object(flow.permissions, "load_policy", return_value=self.policy))
        self.files = self.enterContext(patch.object(flow.file_context, "collect", return_value=FILES))
        self.systemd = self.enterContext(patch.object(flow.ask_context, "_systemd", return_value=[SYSTEMD]))
        self.weather = self.enterContext(patch.object(flow.ask_context, "_weather", return_value=WEATHER))
        self.confirm = self.enterContext(patch.object(flow, "confirm", return_value=True))
        self.reader = Mock(side_effect=AssertionError("unexpected input"))
        self.request = Mock(return_value='{"action":"answer","text":"Odpowiedź"}')

    def assert_no_sources(self):
        self.files.assert_not_called()
        self.systemd.assert_not_called()
        self.weather.assert_not_called()

    def test_general_question_sends_only_metadata_and_returns_answer_without_read(self):
        result = flow.run("Wyjaśnij dowolny temat", options(), self.reader, self.request)
        self.assertEqual(result, "Odpowiedź")
        self.request.assert_called_once()
        kwargs = self.request.call_args.kwargs
        self.assertEqual(kwargs["access"], ACCESS)
        self.assertNotIn("context", kwargs)
        self.assertIs(kwargs["question_consent"]._snapshot.policy, self.policy)
        self.assertIs(kwargs["check"].__self__, kwargs["question_consent"])
        self.confirm.assert_called_once_with("ask-question", reader=self.reader, config=None)
        with self.assertRaises(AnalysisDenied):
            kwargs["question_consent"].check()
        self.load.assert_called_once_with(optional=True)
        self.assert_no_sources()
        self.reader.assert_not_called()

    def test_one_plan_collects_selected_sources_then_one_final_answer_without_loop(self):
        # A JSON-shaped final answer is returned as text, never treated as a
        # third read plan, even if a model ignores the final-answer instruction.
        final = json.dumps(READ)
        self.request.side_effect = [json.dumps(READ), final]
        result = flow.run("Dowolne pytanie wymagające danych", options(), self.reader, self.request)
        self.assertEqual(result, final)
        self.assertEqual(self.request.call_count, 2)
        first, second = self.request.call_args_list
        self.assertEqual(first.kwargs["access"], ACCESS)
        self.assertEqual(second.kwargs["context"], [FILES, SYSTEMD, WEATHER])
        self.assertNotIn("access", second.kwargs)
        snapshot = first.kwargs["check"].__self__
        self.assertIs(second.kwargs["check"].__self__, snapshot)
        self.assertIs(self.files.call_args.args[0], self.policy)
        self.assertEqual(self.files.call_args.args[1], ["f0"])
        self.assertEqual(self.systemd.call_args.args[:2], ("system", "enabled"))
        self.assertIs(self.systemd.call_args.kwargs["check"].__self__, snapshot)
        self.assertEqual(self.weather.call_args.args[0], "Warszawa")
        self.assertIs(self.weather.call_args.kwargs["check"].__self__, snapshot)
        self.load.assert_called_once_with(optional=True)
        self.assertNotIn("PRIVATE DATA", self.output.getvalue())
        self.confirm.assert_called_once_with("ask-question", reader=self.reader, config=None)
        self.assertEqual(self.output.getvalue().count("Czekam na odpowiedź"), 1)
        self.assertNotIn("Dostępy:", self.output.getvalue())

    def test_out_of_scope_model_plan_refuses_before_any_collector_or_second_request(self):
        cases = [dict(READ, files=["f2"]), dict(READ, systemd=[{"scope": "remote", "view": "all"}]),
                 dict(READ, command="cat /etc/shadow"),
                 dict(READ, files=[], systemd=[], weather=None)]
        for plan in cases:
            self.request.reset_mock()
            self.request.return_value = json.dumps(plan)
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                flow.run("Pytanie", options(), self.reader, self.request)
            self.request.assert_called_once()
            self.assert_no_sources()

    def test_declined_question_never_sends_or_reads_any_source(self):
        self.confirm.return_value = False
        with self.assertRaises(flow.Declined):
            flow.run("Pytanie", options(), self.reader, self.request)
        self.request.assert_not_called()
        self.assert_no_sources()

    def test_claude_denial_or_bad_config_stops_before_request_for_all_modes(self):
        for args in (options(), options(no_context=True), options(systemd="system"), options(weather="Warszawa")):
            self.policy.sources.return_value["claude"] = False
            with self.subTest(args=args), self.assertRaises(flow.AccessDenied):
                flow.run("Pytanie", args, self.reader, self.request)
        self.load.side_effect = ValueError("invalid configuration")
        with self.assertRaises(ValueError):
            flow.run("Pytanie", options(no_context=True), self.reader, self.request)
        self.request.assert_not_called()
        self.assert_no_sources()

    def test_explicit_sources_cannot_bypass_their_policy_denials(self):
        self.policy.access.return_value = {"files": [], "systemd": [], "weather": False}
        for args in (options(systemd="system"), options(systemd="both"), options(weather="Warszawa")):
            with self.subTest(args=args), self.assertRaises(flow.AccessDenied):
                flow.run("Pytanie", args, self.reader, self.request)
        self.request.assert_not_called()
        self.assert_no_sources()

    def test_no_context_is_direct_question_but_still_checks_policy(self):
        self.request.return_value = "Odpowiedź"
        result = flow.run("Pytanie", options(no_context=True), self.reader, self.request)
        self.assertEqual(result, "Odpowiedź")
        self.load.assert_called_once_with(optional=True)
        self.assertEqual(self.request.call_args.kwargs["context"], [])
        self.assertNotIn("access", self.request.call_args.kwargs)
        self.assertGreater(self.policy.recheck.call_count, 0)
        self.assert_no_sources()

    def test_changed_policy_after_planner_reply_prevents_all_source_reads(self):
        def request(*args, **kwargs):
            self.policy.recheck.side_effect = ValueError("policy changed")
            return json.dumps(READ)
        self.request.side_effect = request
        with self.assertRaises(AnalysisDenied):
            flow.run("Pytanie", options(), self.reader, self.request)
        self.request.assert_called_once()
        self.assert_no_sources()

    def test_changed_policy_during_read_prevents_other_sources_and_second_send(self):
        self.request.return_value = json.dumps(READ)
        def files(*args, **kwargs):
            self.policy.recheck.side_effect = ValueError("policy changed")
            return FILES
        self.files.side_effect = files
        with self.assertRaises(AnalysisDenied):
            flow.run("Pytanie", options(), self.reader, self.request)
        self.request.assert_called_once()
        self.files.assert_called_once()
        self.systemd.assert_not_called()
        self.weather.assert_not_called()

    def test_failed_or_declined_source_never_transfers_partial_context(self):
        self.request.return_value = json.dumps(READ)
        for target, error in ((self.files, AnalysisDenied("consent")),
                              (self.systemd, AnalysisDenied("input")),
                              (self.weather, flow.ask_context.weather_context.WeatherError("network"))):
            self.request.reset_mock()
            target.side_effect = error
            with self.subTest(target=target), self.assertRaises((AnalysisDenied, ValueError)):
                flow.run("Pytanie", options(), self.reader, self.request)
            self.request.assert_called_once()
            target.side_effect = None

    def test_second_transfer_uses_original_snapshot_when_config_changes(self):
        checks = []
        def request(*args, **kwargs):
            checks.append(kwargs["check"])
            if "context" in kwargs:
                self.policy.recheck.side_effect = ValueError("policy changed while approving")
                kwargs["check"]()
                self.fail("changed policy allowed final transfer")
            return json.dumps(READ)
        self.request.side_effect = request
        with self.assertRaises(AnalysisDenied):
            flow.run("Pytanie", options(), self.reader, self.request)
        self.assertEqual(len(checks), 2)
        self.assertIs(checks[0].__self__, checks[1].__self__)
        self.assertIs(checks[1].__self__._snapshot.policy, self.policy)
        self.load.assert_called_once_with(optional=True)

    def test_preview_is_policy_metadata_only_without_model_or_scope_enumeration(self):
        result = flow.run("Pytanie", options(preview=True, details=True), self.reader, self.request)
        self.assertIsNone(result)
        self.request.assert_not_called()
        self.assert_no_sources()
        self.policy.expand.assert_not_called()
        self.reader.assert_not_called()
        self.confirm.assert_not_called()
        self.assertIn("~/project/*", self.output.getvalue())
        self.assertNotIn("PRIVATE DATA", self.output.getvalue())

    def test_web_preview_reports_permission_without_reading_or_searching(self):
        self.policy.sources.return_value["web"] = True
        self.policy.access.return_value["web"] = True
        result = flow.run("Wiadomości?", options(preview=True), self.reader, self.request)
        self.assertIsNone(result)
        self.assertIn("Internet: dozwolony", self.output.getvalue())
        self.assert_no_sources()
        self.policy.expand.assert_not_called()
        self.confirm.assert_not_called()
        self.request.assert_not_called()

    def test_absent_policy_allows_plain_question_without_any_source_access(self):
        self.load.return_value = None
        snapshot = flow.capture_access()
        self.assertIsNone(snapshot.policy)
        self.assertEqual(snapshot.access, {"files": [], "systemd": [], "weather": False})
        self.assertEqual(snapshot.sources, {"claude": True, "systemd": [], "weather": False})
        self.assertEqual(flow.run("Pytanie", options(), self.reader, self.request), "Odpowiedź")
        self.request.assert_called_once()
        self.request.return_value = json.dumps(READ)
        with self.assertRaises(ValueError):
            flow.run("Pytanie", options(), self.reader, self.request)
        self.assert_no_sources()
        self.load.return_value = self.policy
        with self.assertRaises(flow.AccessDenied):
            snapshot.require_claude()

    def test_policy_created_during_unconfigured_question_revokes_initial_decision(self):
        self.load.return_value = None
        def approve(*args, **kwargs):
            self.load.return_value = self.policy
            return True
        self.confirm.side_effect = approve
        with self.assertRaises(AnalysisDenied):
            flow.run("Pytanie", options(), self.reader, self.request)
        self.request.assert_not_called()
        self.assert_no_sources()


if __name__ == "__main__":
    unittest.main()
