"""Synthetic regression for the five-field pre-init notice observed on CachyOS.

The field layout/session relation came from the offline 2.1.312 probe. Values
below are synthetic, not a recording, and no native client or network is used.
"""
import copy
import json
import unittest

from ai_desktop.analysis_contract import AnalysisDenied
from ai_desktop.web_search_result import MAX_EVENTS, MAX_STREAM_BYTES, web_failure, web_text

SESSION = "synthetic-ui-session"
PRIVATE = "PRIVATE_UI_VALUE_NOT_FOR_OUTPUT"
SOURCE = "https://www.python.org/"


def notice(**updates):
    event = {"type": "system", "subtype": "ui_invalidate", "event": "ui.render",
             "session_id": SESSION, "uuid": "synthetic-notice"}
    event.update(updates)
    return event


def events():
    answer = json.dumps({"answer": "Python [1].", "sources": [SOURCE]})
    return [
        {"type": "system", "subtype": "init", "session_id": SESSION,
         "tools": ["WebSearch"], "mcp_servers": []},
        {"type": "assistant", "session_id": SESSION, "parent_tool_use_id": None,
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "WebSearch", "id": "call-1",
              "input": {"query": "Python official"}}]}},
        {"type": "user", "session_id": SESSION, "parent_tool_use_id": None,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "call-1", "content": "UNTRUSTED"}]},
         "tool_use_result": {"query": "Python official", "durationSeconds": 1,
             "results": [{"tool_use_id": "backend-1", "content": [{"title": "Python", "url": SOURCE}]}]}},
        {"type": "assistant", "session_id": SESSION, "parent_tool_use_id": None,
         "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]}},
        {"type": "result", "subtype": "success", "session_id": SESSION,
         "is_error": False, "num_turns": 2, "permission_denials": [], "result": answer},
    ]


def wire(value):
    return ("\n".join(json.dumps(item) for item in value) + "\n").encode("utf-8")


class WebUiInvalidationTests(unittest.TestCase):
    def refused(self, value, reason="result", diagnostic=None):
        with self.assertRaises(AnalysisDenied) as caught:
            web_text(wire(value))
        self.assertEqual(caught.exception.reason, reason)
        if diagnostic is not None:
            self.assertEqual(caught.exception.diagnostic, diagnostic)
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_preinit_notice_preserves_exact_verified_answer(self):
        expected = web_text(wire(events()))
        self.assertEqual(web_text(wire([notice()] + events())), expected)
        self.assertEqual(expected, "Python [1].\n\nSources:\n[1] Python — `https://www.python.org/`")

    def test_notice_during_session_before_final_is_inert(self):
        expected = web_text(wire(events()))
        for index in range(1, len(events())):
            value = events()
            value.insert(index, notice())
            with self.subTest(index=index):
                self.assertEqual(web_text(wire(value)), expected)

    def test_multiple_notices_never_enter_answer_or_supply_authority(self):
        value = [notice(event=PRIVATE, uuid=PRIVATE) for _ in range(3)] + events()
        value.insert(-1, notice(event=SOURCE))
        answer = web_text(wire(value))
        self.assertEqual(answer, web_text(wire(events())))
        self.assertNotIn(PRIVATE, answer)

    def test_unknown_or_active_prefix_is_not_scanned_past(self):
        for prefix in ({"type": "system", "subtype": "unknown", "value": PRIVATE},
                       {"type": "system", "subtype": "thinking_tokens"},
                       {"type": "assistant"}, {"type": "user"},
                       {"type": "tool_progress"}, {"type": "control_request"}, {}):
            for leading in ([], [notice()]):
                with self.subTest(prefix=prefix, leading=bool(leading)):
                    self.refused(leading + [prefix] + events(), diagnostic="web-init-event")

    def test_notice_without_init_never_establishes_a_session(self):
        for value in ([notice()], [notice(), notice()], [notice()] + events()[1:]):
            with self.subTest(length=len(value)):
                self.refused(value, diagnostic="web-init-event")

    def test_duplicate_init_still_refuses(self):
        for position in range(1, len(events()) + 1):
            value = events()
            value.insert(position, copy.deepcopy(value[0]))
            with self.subTest(position=position):
                self.refused([notice()] + value)

    def test_notice_must_share_the_validated_init_session(self):
        for index in (0, 1, 3):
            value = events()
            value.insert(index, notice(session_id=PRIVATE))
            with self.subTest(index=index):
                self.refused(value, diagnostic="web-session")
        value = [notice(), notice(session_id="another-session")] + events()
        self.refused(value, diagnostic="web-session")

    def test_notice_requires_exact_five_field_shape(self):
        for key in notice():
            changed = notice()
            del changed[key]
            for position in (0, 1):
                value = events()
                value.insert(position, changed)
                with self.subTest(missing=key, position=position):
                    self.refused(value)
        for key in ("tools", "mcp_servers", "parent_tool_use_id", "agent_id", "task_id",
                    "isReplay", "tool_use_result", "instances", "result", "sources"):
            for position in (0, 2):
                value = events()
                value.insert(position, notice(**{key: PRIVATE}))
                with self.subTest(extra=key, position=position):
                    self.refused(value)

    def test_notice_strings_are_bounded_nonempty_and_control_free(self):
        for key in ("event", "uuid", "session_id"):
            for invalid in (None, True, 1, [], {}, "", " ", "x" * 257, "a\nb", "a\tb",
                            "a\rb", "\x1b[31m", "\u202e", "\ud800", "ż" * 129):
                for position in (0, 1):
                    value = events()
                    value.insert(position, notice(**{key: invalid}))
                    with self.subTest(key=key, invalid=repr(invalid), position=position):
                        self.refused(value)
        self.assertEqual(web_text(wire([notice(event="ż" * 128, uuid="x" * 256)] + events())),
                         web_text(wire(events())))

    def test_notice_cannot_replace_call_result_or_source_metadata(self):
        value = events()
        value[2] = notice()
        self.refused([notice()] + value)
        value = events()
        del value[1:3]
        self.refused([notice(event=SOURCE)] + value, reason="web-no-results")
        value = events()
        value[2]["tool_use_result"]["results"] = []
        self.refused([notice(event=SOURCE)] + value, reason="web-no-results")

    def test_notice_does_not_allow_forged_sources_or_side_effect_tools(self):
        for mutation in ("source", "tool", "mcp", "init-tools", "turns", "denials"):
            value = events()
            if mutation == "source":
                value[-1]["result"] = json.dumps({"answer": "Python [1].", "sources": ["https://invented.example/"]})
            elif mutation == "tool":
                value[1]["message"]["content"][0]["name"] = "Bash"
            elif mutation == "mcp":
                value[0]["mcp_servers"] = [{"name": "unexpected"}]
            elif mutation == "init-tools":
                value[0]["tools"] = ["WebSearch", "Read"]
            elif mutation == "turns":
                value[-1]["num_turns"] = 5
            else:
                value[-1]["permission_denials"] = ["WebSearch"]
            with self.subTest(mutation=mutation):
                self.refused([notice()] + value)

    def test_call_budget_is_unchanged_with_ui_prefix(self):
        value = events()
        original = value[1]["message"]["content"][0]
        value[1]["message"]["content"] += [dict(original, id="call-2"), dict(original, id="call-3")]
        self.refused([notice()] + value, diagnostic="web-tool-call")

    def test_final_order_and_completeness_still_mandatory(self):
        self.refused([notice()] + events()[:-1], diagnostic="web-missing-result")
        self.refused([notice()] + events() + [notice()], diagnostic="web-final-order")
        self.refused([notice()] + events() + [events()[-1]], diagnostic="web-final-order")

    def test_prefix_remains_inside_original_event_and_byte_budgets(self):
        value = [notice() for _ in range(MAX_EVENTS - len(events()))] + events()
        self.assertEqual(web_text(wire(value)), web_text(wire(events())))
        self.refused([notice()] + value, diagnostic="web-stream")
        oversized = wire([notice()] + events())
        oversized += b" " * (MAX_STREAM_BYTES + 1 - len(oversized))
        with self.assertRaises(AnalysisDenied) as caught:
            web_text(oversized)
        self.assertEqual(caught.exception.diagnostic, "web-stream")

    def test_duplicate_json_keys_in_notice_are_refused(self):
        raw = wire([notice()]).replace(b'"event": "ui.render"', b'"event":"ui.render","event":"extra"')
        with self.assertRaises(AnalysisDenied) as caught:
            web_text(raw + wire(events()))
        self.assertEqual(caught.exception.diagnostic, "web-stream")

    def test_rate_rejection_survives_ui_prelude_for_both_exit_codes(self):
        value = [notice(), events()[0], {"type": "rate_limit_event", "session_id": SESSION,
                 "rate_limit_info": {"status": "rejected"}}]
        for code in (0, 1):
            with self.subTest(code=code):
                self.assertEqual(web_failure(code, wire(value)), "rate-limit")
        self.refused(value, reason="rate-limit")
        for invalid_code in (True, 2, "0", None):
            self.assertIsNone(web_failure(invalid_code, wire(value)))

    def test_rate_rejection_cannot_be_taken_from_other_session_or_bad_prefix(self):
        value = [notice(), events()[0], {"type": "rate_limit_event", "session_id": SESSION,
                 "rate_limit_info": {"status": "rejected"}}]
        for index in (0, 2):
            changed = copy.deepcopy(value)
            changed[index]["session_id"] = PRIVATE
            self.assertIsNone(web_failure(0, wire(changed)))
            self.refused(changed, diagnostic="web-session")
        changed = copy.deepcopy(value)
        changed[0]["unexpected"] = True
        self.assertIsNone(web_failure(1, wire(changed)))
        self.refused(changed, diagnostic="web-init-event")
        self.assertIsNone(web_failure(0, wire(value + [events()[-1], events()[-1]])))

    def test_structured_provider_errors_keep_existing_classification(self):
        for status, reason in ((401, "auth-required"), (429, "rate-limit"), (503, "provider-unavailable")):
            value = [notice(), {"type": "result", "subtype": "success", "is_error": True,
                               "api_error_status": status, "result": PRIVATE}]
            with self.subTest(status=status):
                self.assertEqual(web_failure(1, wire(value)), reason)
                self.refused(value, reason=reason)


if __name__ == "__main__":
    unittest.main()
