"""Synthetic SDK telemetry regression; no native client, OAuth or network.

The fixtures model the documented message shapes, not a host recording.
"""
import copy
import json
import unittest

from ai_desktop.analysis_contract import AnalysisDenied
from ai_desktop.web_search_result import MAX_EVENTS, web_text


def events():
    session = "synthetic-web-telemetry"
    source = "https://www.python.org/"
    answer = json.dumps({"answer": "Python [1].", "sources": [source]})
    return [
        {"type": "system", "subtype": "init", "session_id": session,
         "tools": ["WebSearch"], "mcp_servers": []},
        {"type": "assistant", "session_id": session, "parent_tool_use_id": None,
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "call-1", "name": "WebSearch",
              "input": {"query": "Python official"}}]}},
        {"type": "user", "session_id": session, "parent_tool_use_id": None,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "call-1", "content": "UNTRUSTED"}]},
         "tool_use_result": {"query": "Python official", "results": [
             {"tool_use_id": "backend-1", "content": [{"title": "Python", "url": source}]}],
             "durationSeconds": 1}},
        {"type": "assistant", "session_id": session, "parent_tool_use_id": None,
         "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]}},
        {"type": "result", "session_id": session, "subtype": "success", "is_error": False,
         "num_turns": 2, "permission_denials": [], "result": answer},
    ]


def thinking(**updates):
    value = {"type": "system", "subtype": "thinking_tokens", "session_id": "synthetic-web-telemetry",
             "uuid": "thinking-1", "estimated_tokens": 100, "estimated_tokens_delta": 20}
    value.update(updates)
    return value


def progress(**updates):
    value = {"type": "tool_progress", "session_id": "synthetic-web-telemetry",
             "uuid": "progress-1", "tool_use_id": "call-1", "tool_name": "WebSearch",
             "parent_tool_use_id": None, "elapsed_time_seconds": 30, "heartbeat": True}
    value.update(updates)
    return value


def parse(value):
    return web_text(("\n".join(json.dumps(event) for event in value) + "\n").encode("utf-8"))


class WebStreamTelemetryTests(unittest.TestCase):
    def refused(self, value, reason="result"):
        with self.assertRaises(AnalysisDenied) as caught:
            parse(value)
        self.assertEqual(caught.exception.reason, reason)
        self.assertNotIn("UNTRUSTED", str(caught.exception))

    def test_thinking_before_and_after_tool_preserves_exact_answer(self):
        value = events()
        expected = parse(value)
        value.insert(1, thinking())
        value.insert(-1, thinking(uuid="thinking-2", user_message_uuid="user-1"))
        self.assertEqual(parse(value), expected)

    def test_progress_and_heartbeat_preserve_exact_answer(self):
        for heartbeat in (None, False, True):
            value = events()
            notice = progress()
            if heartbeat is None:
                notice.pop("heartbeat")
            else:
                notice["heartbeat"] = heartbeat
            value.insert(2, notice)
            with self.subTest(heartbeat=heartbeat):
                self.assertEqual(parse(value), parse(events()))

    def test_telemetry_never_supplies_a_missing_tool_result(self):
        value = events()
        value[2] = progress()
        value.insert(1, thinking())
        self.refused(value)

    def test_telemetry_cannot_create_search_provenance(self):
        value = events()
        del value[1:3]
        value.insert(1, thinking())
        self.refused(value, "web-no-results")
        value = events()
        value.insert(2, progress())
        value[3]["tool_use_result"]["results"] = []
        self.refused(value, "web-no-results")

    def test_progress_requires_pending_same_session_websearch(self):
        changes = ({"session_id": "other"}, {"tool_use_id": "not-called"},
                   {"tool_use_id": []}, {"tool_name": "Bash"}, {"tool_name": "WebFetch"},
                   {"parent_tool_use_id": "parent"}, {"task_id": "background"},
                   {"agent_id": "subagent"}, {"subagent_type": "general-purpose"},
                   {"subagent_retry": {}}, {"isReplay": True}, {"heartbeat": "true"},
                   {"heartbeat": 1}, {"uuid": "\x1b[31m"})
        for update in changes:
            value = events()
            value.insert(2, progress(**update))
            with self.subTest(update=update):
                self.refused(value)
        for position in (1, 3, 4, 5):
            value = events()
            value.insert(position, progress())
            with self.subTest(position=position):
                self.refused(value)
        value = events()
        notice = progress()
        del notice["parent_tool_use_id"]
        value.insert(2, notice)
        self.refused(value)

    def test_thinking_rejects_other_session_subagents_and_unknown_payload(self):
        for update in ({"session_id": "other"}, {"subtype": "hook_progress"},
                       {"parent_tool_use_id": "parent"}, {"agent_id": "subagent"},
                       {"user_message_uuid": "\x1b[31m"}, {"isReplay": True},
                       {"tool_use_result": {"url": "https://invented.example/"}}):
            value = events()
            value.insert(1, thinking(**update))
            with self.subTest(update=update):
                self.refused(value)

    def test_numeric_telemetry_rejects_bool_missing_negative_and_nonfinite(self):
        for factory, position, keys in ((thinking, 1, ("estimated_tokens", "estimated_tokens_delta")),
                                        (progress, 2, ("elapsed_time_seconds",))):
            for key in keys:
                for invalid in (None, True, "1", -1, float("nan"), float("inf"), [], {}):
                    value = events()
                    value.insert(position, factory(**{key: invalid}))
                    with self.subTest(key=key, invalid=repr(invalid)):
                        self.refused(value)
                value = events()
                notice = factory()
                notice.pop(key)
                value.insert(position, notice)
                self.refused(value)

    def test_telemetry_does_not_relax_source_or_tool_validation(self):
        value = events()
        value.insert(2, progress())
        value.insert(1, thinking())
        for mutation in ("source", "tool", "denial", "incomplete", "extra-call"):
            changed = copy.deepcopy(value)
            if mutation == "source":
                changed[-1]["result"] = json.dumps({"answer": "Wynik [1].", "sources": ["https://invented.example/"]})
            elif mutation == "tool":
                changed[2]["message"]["content"][0]["name"] = "Read"
            elif mutation == "denial":
                changed[-1]["permission_denials"] = ["WebSearch"]
            elif mutation == "incomplete":
                changed.pop()
            else:
                block = changed[2]["message"]["content"][0]
                changed[2]["message"]["content"] += [dict(block, id="call-2"), dict(block, id="call-3")]
            with self.subTest(mutation=mutation):
                self.refused(changed)

    def test_existing_event_budget_and_final_message_order_remain_enforced(self):
        value = events()
        value[1:1] = [thinking(uuid="thinking-" + str(i)) for i in range(MAX_EVENTS - len(value))]
        self.assertEqual(parse(value), parse(events()))
        value.insert(1, thinking())
        self.refused(value)
        self.refused(events() + [thinking()])

    def test_rate_rejection_is_not_erased_by_telemetry(self):
        value = events()
        value.insert(1, thinking())
        value.insert(2, {"type": "rate_limit_event", "session_id": "synthetic-web-telemetry",
                         "rate_limit_info": {"status": "rejected"}})
        self.refused(value, "rate-limit")


if __name__ == "__main__":
    unittest.main()
