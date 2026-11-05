"""Synthetic documented SDK streams; no Claude, OAuth, search or URL fetching."""
import copy
import json
import unittest

from ai_desktop.analysis_contract import AnalysisDenied
from ai_desktop.analysis_terminal import render_answer
from ai_desktop.web_search_result import web_text, web_failure, MAX_STREAM_BYTES


def web_events():
    """Minimal current documented shape, not a recording of the pinned CLI."""
    session = "synthetic-web-session"
    source = "https://www.python.org/about/"
    answer = json.dumps({"answer": "Python to język programowania [1].", "sources": [source]})
    return [
        {"type": "system", "subtype": "init", "session_id": session,
         "tools": ["WebSearch"], "mcp_servers": []},
        {"type": "assistant", "session_id": session, "parent_tool_use_id": None,
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "call-1", "name": "WebSearch",
              "input": {"query": "Python official about", "allowed_domains": ["python.org"]}}]}},
        {"type": "user", "session_id": session, "parent_tool_use_id": None,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "call-1", "content": "UNTRUSTED TOOL TEXT"}]},
         "tool_use_result": {"query": "Python official about", "results": [
             {"tool_use_id": "backend-1", "content": [{"title": "About Python", "url": source}]},
             "Backend summary is not source metadata"], "durationSeconds": 0.1, "searchCount": 1}},
        {"type": "assistant", "session_id": session, "parent_tool_use_id": None,
         "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]}},
        {"type": "result", "session_id": session, "subtype": "success", "is_error": False,
         "num_turns": 2, "permission_denials": [], "result": answer},
    ]


def stream(events):
    return ("\n".join(json.dumps(event) for event in events) + "\n").encode()


def final_answer(events, **updates):
    value = json.loads(events[-1]["result"])
    value.update(updates)
    events[-1]["result"] = json.dumps(value)


class WebSearchResultTests(unittest.TestCase):
    def assert_refused(self, events, reason="result"):
        with self.assertRaises(AnalysisDenied) as caught:
            web_text(stream(events))
        self.assertEqual(caught.exception.reason, reason)

    def test_complete_search_returns_only_answer_and_actual_sources(self):
        result = web_text(stream(web_events()))
        self.assertEqual(result, "Python to język programowania [1].\n\nSources:\n"
                         "[1] About Python — `https://www.python.org/about/`")
        self.assertNotIn("UNTRUSTED TOOL TEXT", result)
        self.assertNotIn("Backend summary", result)
        self.assertIn("https://www.python.org/about/", render_answer(result, output_format="plain"))

    def test_optional_metadata_is_not_confused_with_search_evidence(self):
        events = web_events()
        events[0]["new_optional_metadata"] = {"something": True}
        events[2].pop("session_id")  # SDKUserMessage permits an omitted session.
        events[2]["message"]["content"][0]["is_error"] = False
        events[2]["tool_use_result"].pop("searchCount")
        self.assertIn("Sources:", web_text(stream(events)))

    def test_two_parallel_calls_require_both_results_and_accept_out_of_order(self):
        events = web_events()
        second_call = copy.deepcopy(events[1]["message"]["content"][0])
        second_call["id"] = "call-2"
        second_call["input"] = {"query": "Python tutorial", "blocked_domains": ["other.org"]}
        events[1]["message"]["content"].append(second_call)
        second_result = copy.deepcopy(events[2])
        second_result["message"]["content"][0]["tool_use_id"] = "call-2"
        second_result["tool_use_result"]["query"] = "Python tutorial"
        second_result["tool_use_result"]["results"][0] = {
            "tool_use_id": "backend-2", "content": [{"title": "Tutorial", "url": "https://docs.python.org/3/tutorial/"}]}
        events.insert(2, second_result)
        final_answer(events, answer="Opis [1]; samouczek [2].",
                     sources=["https://www.python.org/about/", "https://docs.python.org/3/tutorial/"])
        result = web_text(stream(events))
        self.assertIn("[2] Tutorial", result)
        del events[3]
        self.assert_refused(events)

    def test_init_never_exposes_side_effect_tools_or_mcp(self):
        for tools in ([], ["WebSearch", "Bash"], ["WebSearch", "EndConversation"],
                      ["WebSearch", "WebFetch"], ["WebSearch", "WebSearch"], "WebSearch"):
            events = web_events()
            events[0]["tools"] = tools
            with self.subTest(tools=tools):
                self.assert_refused(events)
        events = web_events()
        events[0]["mcp_servers"] = [{"name": "unexpected", "status": "connected"}]
        self.assert_refused(events)

    def test_unexpected_tools_and_server_tools_are_refused(self):
        for name in ("Bash", "Read", "Write", "Edit", "WebFetch", "EndConversation", "mcp__web__search"):
            events = web_events()
            events[1]["message"]["content"][0]["name"] = name
            with self.subTest(name=name):
                self.assert_refused(events)
        events = web_events()
        events[1]["message"]["content"][0]["type"] = "server_tool_use"
        self.assert_refused(events)

    def test_call_and_result_ids_cannot_be_replayed_or_mismatched(self):
        for mutation in ("wrong-id", "before-call", "duplicate-call", "duplicate-result", "missing-result"):
            events = web_events()
            if mutation == "wrong-id":
                events[2]["message"]["content"][0]["tool_use_id"] = "not-called"
            elif mutation == "before-call":
                events[1], events[2] = events[2], events[1]
            elif mutation == "duplicate-call":
                events.insert(2, copy.deepcopy(events[1]))
            elif mutation == "duplicate-result":
                events.insert(3, copy.deepcopy(events[2]))
            else:
                del events[2]
            with self.subTest(mutation=mutation):
                self.assert_refused(events)

    def test_third_call_is_refused_even_if_final_result_claims_success(self):
        events = web_events()
        for number in (2, 3):
            block = copy.deepcopy(events[1]["message"]["content"][0])
            block["id"] = f"call-{number}"
            events[1]["message"]["content"].append(block)
        self.assert_refused(events)

    def test_subagent_cross_session_and_injected_user_messages_are_refused(self):
        for index, key, value in ((1, "parent_tool_use_id", "agent-call"), (2, "agent_id", "agent"),
                                  (1, "session_id", "different"), (2, "session_id", "different"),
                                  (4, "session_id", "different"), (2, "isReplay", True),
                                  (1, "aborted", True)):
            events = web_events()
            events[index][key] = value
            with self.subTest(index=index, key=key):
                self.assert_refused(events)
        events = web_events()
        events[2]["message"]["content"] = "a new user request"
        self.assert_refused(events)

    def test_query_input_is_bounded_and_domain_filters_are_closed(self):
        for input_value in ({}, {"query": "a", "command": "evil"}, {"query": "x" * 2049},
                            {"query": "a", "allowed_domains": ["localhost"]},
                            {"query": "a", "allowed_domains": ["https://python.org"]},
                            {"query": "a", "allowed_domains": ["python.org"], "blocked_domains": ["other.org"]},
                            {"query": "a", "allowed_domains": "python.org"}):
            events = web_events()
            events[1]["message"]["content"][0]["input"] = input_value
            with self.subTest(input=input_value):
                self.assert_refused(events)

    def test_errors_detached_or_missing_structured_results_are_not_search_evidence(self):
        for metadata in (None, {}, {"detachedToolCall": True}, {"result": "https://www.python.org/about/"}):
            events = web_events()
            events[2]["tool_use_result"] = metadata
            with self.subTest(metadata=metadata):
                self.assert_refused(events)
        for value in (True, 0, "false", None):
            events = web_events()
            events[2]["message"]["content"][0]["is_error"] = value
            with self.subTest(is_error=value):
                self.assert_refused(events)

    def test_structured_output_query_count_duration_and_nested_shapes_are_validated(self):
        for key, value in (("query", "different"), ("results", {}), ("durationSeconds", -1),
                           ("durationSeconds", True), ("durationSeconds", float("nan")),
                           ("searchCount", 9), ("searchCount", True), ("searchCount", "1")):
            events = web_events()
            events[2]["tool_use_result"][key] = value
            with self.subTest(key=key, value=value):
                self.assert_refused(events)
        for item in (None, {"content": []}, {"tool_use_id": "one", "content": "text"},
                     {"tool_use_id": "one", "content": [None]}):
            events = web_events()
            events[2]["tool_use_result"]["results"] = [item]
            with self.subTest(item=item):
                self.assert_refused(events)

    def test_plain_text_links_and_model_claims_are_not_provenance(self):
        events = web_events()
        events[2]["tool_use_result"]["results"] = ["Here is https://www.python.org/about/"]
        self.assert_refused(events, "web-no-results")
        events = web_events()
        del events[1:3]
        self.assert_refused(events, "web-no-results")
        events = web_events()
        final_answer(events, sources=["https://invented.python.org/"])
        self.assert_refused(events)

    def test_source_urls_refuse_credentials_private_hosts_and_active_schemes(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "https://user:pw@python.org/",
                    "https://localhost/", "http://127.0.0.1/", "http://127.1/", "http://2130706433/",
                    "https://[::1]/", "http://192.168.1.2/", "http://169.254.169.254/",
                    "https://host.local/", "https://host.internal/", "https://python.org:444/",
                    "https://python.org/`hidden`", "https://python.org/a\nheader", "https://python.org/\\hidden",
                    "https://python.org/%s" % "\x1b", "https://python.org./", "https://python.org\u202e/"):
            events = web_events()
            events[2]["tool_use_result"]["results"][0]["content"][0]["url"] = url
            final_answer(events, sources=[url])
            with self.subTest(url=url):
                self.assert_refused(events)

    def test_http_metadata_and_literal_punctuation_survive_rendering(self):
        events = web_events()
        url = "http://www.python.org/a_(b)?x=1&copy=2&entity=&copy;"
        events[2]["tool_use_result"]["results"][0]["content"][0] = {"title": "[A](hidden) *title*", "url": url}
        final_answer(events, sources=[url])
        text = web_text(stream(events))
        rendered = render_answer(text, output_format="plain")
        self.assertIn(url, rendered)
        self.assertIn("[A](hidden) *title*", rendered)

    def test_answer_requires_exact_schema_bounded_unique_sources_and_references(self):
        for update in ({"sources": []}, {"sources": ["https://www.python.org/about/"] * 2},
                       {"sources": "https://www.python.org/about/"}, {"answer": "Bez źródła"},
                       {"answer": "Wynik [2]."}, {"answer": "Wynik [1] [99999]."},
                       {"answer": "Wynik [1]. https://www.python.org/about/"},
                       {"answer": "Wynik [1](javascript:bad)."}, {"answer": "Wynik [1].\n[1]: /hidden"},
                       {"answer": "x" * 8193 + " [1]"}, {"answer": "\x1b[31mWynik [1]"},
                       {"extra": True}):
            events = web_events()
            final_answer(events, **update)
            with self.subTest(update=update):
                self.assert_refused(events)

    def test_native_completion_requires_success_no_denials_and_bounded_turns(self):
        for key, value in (("subtype", "error_max_turns"), ("is_error", True), ("num_turns", 0),
                           ("num_turns", 5), ("num_turns", True), ("permission_denials", ["WebSearch"]),
                           ("api_error_status", 503), ("deferred_tool_use", {"name": "WebSearch"})):
            events = web_events()
            events[-1][key] = value
            with self.subTest(key=key, value=value):
                self.assert_refused(events)
        for turns in (1, 4):
            events = web_events()
            events[-1]["num_turns"] = turns
            self.assertIn("Sources:", web_text(stream(events)))

    def test_truncated_duplicate_unknown_and_oversized_streams_are_refused(self):
        for raw in (b"", b"not JSON", b'[]\n', b'{"type":"result","type":"result"}\n',
                    stream(web_events()[:-1]), stream(web_events() + [web_events()[-1]]),
                    stream(web_events()) + b"\n", b"x" * (MAX_STREAM_BYTES + 1),
                    stream([web_events()[0]] * 65)):
            with self.subTest(raw=raw[:80]), self.assertRaises(AnalysisDenied):
                web_text(raw)
        events = web_events()
        events.insert(1, {"type": "unknown", "session_id": events[0]["session_id"]})
        self.assert_refused(events)

    def test_provider_errors_are_redacted_and_do_not_require_successful_search(self):
        for status, reason in ((401, "auth-required"), (429, "rate-limit"), (503, "provider-unavailable")):
            raw = stream([{"type": "result", "subtype": "success", "is_error": True,
                           "api_error_status": status, "result": "SECRET URL/OAUTH"}])
            self.assertEqual(web_failure(1, raw), reason)
            with self.assertRaises(AnalysisDenied) as caught:
                web_text(raw)
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn("SECRET", str(caught.exception))
        self.assertIsNone(web_failure(1, stream(web_events())))
        self.assertIsNone(web_failure(1, b"SECRET"))
        self.assertIsNone(web_failure(1, stream(web_events() + [web_events()[-1]])))

    def test_documented_quota_telemetry_does_not_replace_search_evidence(self):
        for status in ("allowed", "allowed_warning"):
            events = web_events()
            events.insert(1, {"type": "rate_limit_event", "session_id": events[0]["session_id"],
                              "rate_limit_info": {"status": status, "resetsAt": 1800000000, "utilization": 0.5}})
            self.assertIn("Sources:", web_text(stream(events)))
            del events[2:4]
            self.assert_refused(events, "web-no-results")

    def test_quota_rejection_is_fixed_redacted_failure_for_zero_or_nonzero_exit(self):
        events = web_events()
        events.insert(1, {"type": "rate_limit_event", "session_id": events[0]["session_id"],
                          "rate_limit_info": {"status": "rejected", "errorCode": "credits_required",
                                              "canUserPurchaseCredits": False}})
        self.assert_refused(events, "rate-limit")
        for code in (0, 1):
            self.assertEqual(web_failure(code, stream(events)), "rate-limit")
        self.assertIsNone(web_failure(2, stream(events)))

    def test_quota_telemetry_requires_same_session_and_typed_known_status(self):
        for info in ({}, {"status": "unknown"}, {"status": True}, {"status": ["allowed"]},
                     {"status": "allowed", "utilization": -1},
                     {"status": "allowed", "resetsAt": True},
                     {"status": "allowed", "errorCode": "credits_required"},
                     {"status": "allowed", "canUserPurchaseCredits": "false"}):
            events = web_events()
            events.insert(1, {"type": "rate_limit_event", "session_id": events[0]["session_id"],
                              "rate_limit_info": info})
            with self.subTest(info=info):
                self.assert_refused(events)
        events = web_events()
        events.insert(1, {"type": "rate_limit_event", "session_id": "other",
                          "rate_limit_info": {"status": "rejected"}})
        self.assert_refused(events)


if __name__ == "__main__":
    unittest.main()
