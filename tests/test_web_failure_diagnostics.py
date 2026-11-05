"""Closed failure codes; synthetic streams only, no native client/OAuth/network."""
import contextlib
import copy
import io
import json
import unittest
from tests.profile_fixture import DEFAULT_PROFILE
from unittest.mock import MagicMock, patch

from ai_desktop.analysis_contract import AnalysisDenied, read_result, result_packet
from ai_desktop.analysis_diagnostics import WEB_DIAGNOSTICS, web_diagnostic
from ai_desktop.web_search_result import web_text

HASH = "a" * 64
SECRET = "PRIVATE-SENTINEL-do-not-print"


def events():
    answer = json.dumps({"answer": "Python [1].", "sources": ["https://www.python.org/"]})
    return [
        {"type": "system", "subtype": "init", "session_id": SECRET,
         "tools": ["WebSearch"], "mcp_servers": []},
        {"type": "assistant", "session_id": SECRET, "parent_tool_use_id": None,
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "call-1", "name": "WebSearch", "input": {"query": SECRET}}]}},
        {"type": "user", "session_id": SECRET, "parent_tool_use_id": None,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "call-1", "content": SECRET}]},
         "tool_use_result": {"query": SECRET, "results": [
             {"tool_use_id": "backend-1", "content": [{"title": "Python", "url": "https://www.python.org/"}]}],
             "durationSeconds": 0.1}},
        {"type": "result", "session_id": SECRET, "subtype": "success", "is_error": False,
         "num_turns": 2, "permission_denials": [], "result": answer},
    ]


def stream(value):
    return ("\n".join(json.dumps(event) for event in value) + "\n").encode()


class WebDiagnosticContractTests(unittest.TestCase):
    def refused(self, value, check, reason="result"):
        with self.assertRaises(AnalysisDenied) as caught:
            web_text(stream(value))
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(caught.exception.diagnostic, check)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn("https://", str(caught.exception))
        return caught.exception

    def test_valid_answer_stays_identical(self):
        self.assertEqual(web_text(stream(events())),
                         "Python [1].\n\nSources:\n[1] Python — `https://www.python.org/`")

    def test_init_checks_distinguish_event_tools_and_mcp(self):
        for key, value, check in (("subtype", SECRET, "web-init-event"),
                                  ("tools", ["WebSearch", SECRET], "web-init-tools"),
                                  ("mcp_servers", [SECRET], "web-init-mcp"),
                                  ("session_id", None, "web-session")):
            value_events = events()
            value_events[0][key] = value
            with self.subTest(check=check):
                self.refused(value_events, check)

    def test_session_and_message_refusals_reveal_no_values(self):
        for index, key, value, check in ((1, "session_id", "other", "web-session"),
                                        (1, "parent_tool_use_id", SECRET, "web-assistant-message"),
                                        (2, "agent_id", SECRET, "web-tool-result-message")):
            items = events()
            items[index][key] = value
            with self.subTest(check=check):
                self.refused(items, check)

    def test_tool_and_query_checks_remain_strict(self):
        for key, value, check in (("name", SECRET, "web-assistant-tool"),
                                  ("id", None, "web-tool-call"),
                                  ("input", {"query": SECRET, "shell": SECRET}, "web-query")):
            items = events()
            items[1]["message"]["content"][0][key] = value
            with self.subTest(check=check):
                self.refused(items, check)
        items = events()
        items[2]["message"]["content"][0]["tool_use_id"] = SECRET
        self.refused(items, "web-tool-result-pair")

    def test_search_metadata_distinguishes_shape_query_results_duration_count(self):
        for key, value, check in ((None, None, "web-search-shape"),
                                  ("query", "different", "web-search-query"),
                                  ("results", {}, "web-search-results"),
                                  ("durationSeconds", None, "web-search-duration"),
                                  ("searchCount", True, "web-search-count")):
            items = events()
            if key is None:
                items[2]["tool_use_result"] = value
            else:
                items[2]["tool_use_result"][key] = value
            with self.subTest(check=check):
                self.refused(items, check)
        items = events()
        items[2]["tool_use_result"]["results"][0]["content"][0]["url"] = "file:///" + SECRET
        self.refused(items, "web-search-source")

    def test_final_checks_never_release_partial_or_denied_answers(self):
        for key, value, check in (("subtype", "error_max_turns", "web-final-status"),
                                  ("num_turns", 5, "web-final-turns"),
                                  ("permission_denials", [SECRET], "web-final-permissions")):
            items = events()
            items[-1][key] = value
            with self.subTest(check=check):
                self.refused(items, check)
        items = events()
        del items[2]
        self.refused(items, "web-final-pending")
        self.refused(events() + [copy.deepcopy(events()[-1])], "web-final-order")
        self.refused(events()[:-1], "web-missing-result")

    def test_answer_checks_distinguish_json_shape_text_sources_and_citations(self):
        for value, check in ((SECRET, "web-answer-json"),
                             (json.dumps({"answer": SECRET}), "web-answer-shape"),
                             (json.dumps({"answer": "\x1b" + SECRET, "sources": []}), "web-answer-text"),
                             (json.dumps({"answer": "Text [1]", "sources": []}), "web-answer-sources"),
                             (json.dumps({"answer": "Text [1]", "sources": ["https://unobserved.example/"]}),
                              "web-answer-provenance"),
                             (json.dumps({"answer": "Text [99]", "sources": ["https://www.python.org/"]}),
                              "web-answer-references")):
            items = events()
            items[-1]["result"] = value
            with self.subTest(check=check):
                self.refused(items, check)

    def test_unknown_event_and_invalid_telemetry_do_not_leak_names(self):
        items = events()
        items.insert(1, {"type": SECRET, "session_id": SECRET})
        self.refused(items, "web-event-type")
        items = events()
        items.insert(2, {"type": "tool_progress", "session_id": SECRET, "tool_name": SECRET})
        self.refused(items, "web-telemetry")

    def test_stream_shape_failures_are_redacted_and_bounded(self):
        for raw in (SECRET.encode(), b"[]\n", b"x" * 131073, stream(events()) + b"\n"):
            with self.subTest(raw=raw[:20]), self.assertRaises(AnalysisDenied) as caught:
                web_text(raw)
            self.assertEqual(caught.exception.diagnostic, "web-stream")
            self.assertNotIn(SECRET, str(caught.exception))

    def test_provider_error_and_no_results_keep_existing_meaning(self):
        for status, reason in ((401, "auth-required"), (429, "rate-limit"), (503, "provider-unavailable")):
            items = [{"type": "result", "subtype": "success", "is_error": True,
                      "api_error_status": status, "result": SECRET}]
            self.refused(items, None, reason)
        items = events()
        items[2]["tool_use_result"]["results"] = [SECRET]
        self.refused(items, None, "web-no-results")

    def test_all_codes_survive_worker_packet_and_controller_validation(self):
        for check in WEB_DIAGNOSTICS:
            self.assertRegex(check, r"\Aweb-[a-z-]+\Z")
            packet = result_packet(HASH, reason="result", mode="web", diagnostic=check)
            self.assertEqual(set(json.loads(packet)),
                             {"schema", "request_sha256", "status", "reason", "text", "diagnostic"})
            with self.subTest(check=check), self.assertRaises(AnalysisDenied) as caught:
                read_result(packet, HASH, mode="web")
            self.assertEqual((caught.exception.reason, caught.exception.diagnostic), ("result", check))

    def test_exception_never_stringifies_unknown_diagnostic(self):
        class Dangerous:
            def __str__(self):
                raise AssertionError("must not stringify")
        for value in (SECRET, "web-" + SECRET, [], {}, True, 12, Dangerous(), None):
            self.assertIsNone(web_diagnostic(value))
            failure = AnalysisDenied("result", diagnostic=value)
            self.assertIsNone(failure.diagnostic)
            self.assertEqual(str(failure), "ANALYSIS: REFUSED reason=result")
        self.assertIsNone(AnalysisDenied("auth-required", diagnostic="web-init-tools").diagnostic)

    def test_packet_builder_rejects_unknown_success_or_nonweb_diagnostic(self):
        for kwargs in ({"reason": "result", "mode": "web", "diagnostic": SECRET},
                       {"reason": "result", "mode": "web", "diagnostic": []},
                       {"reason": "result", "mode": "assistant", "diagnostic": "web-init-tools"},
                       {"reason": "auth-required", "mode": "web", "diagnostic": "web-init-tools"},
                       {"text": "answer", "mode": "web", "diagnostic": "web-init-tools"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(AnalysisDenied) as caught:
                result_packet(HASH, **kwargs)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_controller_rejects_injected_diagnostic_in_success_wrong_mode_or_reason(self):
        base = json.loads(result_packet(HASH, reason="result", mode="web", diagnostic="web-init-tools"))
        for updates, mode in (({"status": "ok", "reason": None, "text": SECRET}, "web"),
                              ({"reason": "auth-required"}, "web"), ({}, "question"),
                              ({"diagnostic": None}, "web"), ({"diagnostic": SECRET}, "web"),
                              ({"diagnostic": []}, "web"), ({"extra": SECRET}, "web"),
                              ({"request_sha256": "b" * 64}, "web"), ({"text": SECRET}, "web")):
            with self.subTest(updates=updates, mode=mode), self.assertRaises(AnalysisDenied) as caught:
                read_result(json.dumps(dict(base, **updates)).encode(), HASH, mode=mode)
            self.assertEqual(caught.exception.reason, "result")
            self.assertIsNone(caught.exception.diagnostic)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_duplicate_diagnostic_is_refused(self):
        raw = result_packet(HASH, reason="result", mode="web", diagnostic="web-init-tools")
        raw = raw[:-1] + b',"diagnostic":"web-init-tools"}'
        with self.assertRaises(AnalysisDenied) as caught:
            read_result(raw, HASH, mode="web")
        self.assertIsNone(caught.exception.diagnostic)

    def test_legacy_packets_and_success_shape_stay_unchanged(self):
        for mode in ("analysis", "assistant", "question", "workspace", "web"):
            packet = result_packet(HASH, text="answer", mode=mode)
            self.assertNotIn(b"diagnostic", packet)
            self.assertEqual(read_result(packet, HASH, mode=mode), "answer")
            refused = result_packet(HASH, reason="result", mode=mode)
            self.assertNotIn(b"diagnostic", refused)
            with self.assertRaises(AnalysisDenied) as caught:
                read_result(refused, HASH, mode=mode)
            self.assertIsNone(caught.exception.diagnostic)


class WebDiagnosticIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("ai_desktop.agent_profiles.resolve", return_value=DEFAULT_PROFILE))

    def test_supervisor_does_not_erase_parser_check(self):
        from ai_desktop.analysis_sandbox import failure_stage
        with self.assertRaises(AnalysisDenied) as caught:
            with failure_stage("analysis"):
                raise AnalysisDenied("result", diagnostic="web-init-tools")
        self.assertEqual(caught.exception.stage, "analysis")
        self.assertEqual(caught.exception.diagnostic, "web-init-tools")

    def test_worker_transports_only_closed_check_on_web_refusal(self):
        from ai_desktop import analysis_service as service
        conn = MagicMock()
        conn.__enter__.return_value = conn
        listener = MagicMock()
        listener.__enter__.return_value = listener
        listener.accept.return_value = (conn, None)
        with patch.object(service, "activation_listener", return_value=listener), \
                patch.object(service.select, "select", side_effect=[([listener], [], []), ([], [], [])]), \
                patch.object(service.e2_ipc, "_peer_uid"), patch.object(service, "supervisor_ready"), \
                patch.object(service.os, "geteuid", return_value=2000), \
                patch.object(service, "send_packet"), patch.object(service, "receive_message"), \
                patch.object(service, "parse_request", return_value=(json.dumps({"mode": "web"}), HASH)), \
                patch.object(service, "run_client", side_effect=AnalysisDenied("result", diagnostic="web-init-tools")), \
                patch.object(service, "send_message") as send:
            service.receive_once(1000)
        packet = send.call_args.args[1]
        with self.assertRaises(AnalysisDenied) as caught:
            read_result(packet, HASH, mode="web")
        self.assertEqual(caught.exception.diagnostic, "web-init-tools")

    def test_normal_ask_displays_fixed_reason_and_check_without_verbose_payload(self):
        from ai_desktop import ask_cli
        class TTY(io.StringIO):
            def isatty(self):
                return True
        output = TTY()
        with patch.object(ask_cli.os, "getuid", return_value=1000), \
                patch.object(ask_cli.os, "geteuid", return_value=1000), \
                patch.object(ask_cli.sys, "stdin", TTY()), patch.object(ask_cli.sys, "stdout", output), \
                patch.object(ask_cli.assistant_flow, "run", side_effect=AnalysisDenied("result", diagnostic="web-init-tools")):
            code = ask_cli.main(["--question", "Public synthetic question"])
        self.assertEqual(code, 2)
        self.assertIn("ANALYSIS: REFUSED reason=result check=web-init-tools", output.getvalue())
        self.assertNotIn("EXACT DATA", output.getvalue())
        self.assertNotIn(SECRET, output.getvalue())


    def test_nonweb_failures_preserve_quiet_and_verbose_presentation(self):
        from ai_desktop import ask_cli
        class TTY(io.StringIO):
            def isatty(self):
                return True
        for reason in ("cleanup", "client", "consent", "result"):
            for verbose in (False, True):
                output = TTY()
                args = ["--question", "Public synthetic question"]
                if verbose:
                    args.append("--v")
                with self.subTest(reason=reason, verbose=verbose), \
                        patch.object(ask_cli.os, "getuid", return_value=1000), \
                        patch.object(ask_cli.os, "geteuid", return_value=1000), \
                        patch.object(ask_cli.sys, "stdin", TTY()), \
                        patch.object(ask_cli.sys, "stdout", output), \
                        patch.object(ask_cli.assistant_flow, "run", side_effect=AnalysisDenied(reason)):
                    code = ask_cli.main(args)
                self.assertEqual(code, 2)
                self.assertEqual("ANALYSIS: REFUSED reason=" in output.getvalue(), verbose)
                self.assertNotIn("check=", output.getvalue())
                self.assertNotIn("EXACT DATA", output.getvalue())
                self.assertNotIn(SECRET, output.getvalue())
