"""Pinned quota metadata is validated then discarded, never used as authority."""
import copy
import unittest

from ai_desktop import analysis_contract as contract, codex_protocol as codex
from tests.test_codex_notifications import initialized, thread_started, completed, notification, record

SECRET = "SYNTHETIC-PRIVATE-BILLING"


def snapshot():
    # Pinned v2/account.rs RateLimitSnapshot, CreditsSnapshot and spend window.
    return {"limitId": SECRET, "limitName": SECRET, "normalModelSlug": SECRET,
            "primary": {"usedPercent": 101, "windowDurationMins": 300, "resetsAt": 1},
            "secondary": None, "credits": {"hasCredits": True, "unlimited": False, "balance": SECRET},
            "individualLimit": {"limit": SECRET, "used": SECRET, "remainingPercent": -1, "resetsAt": 1},
            "spendControlReached": False, "planType": "prolite", "rateLimitReachedType": None}


def rate_message(value):
    return notification("account/rateLimits/updated", {"rateLimits": value})


def turn_ready(*, limits=None):
    protocol = initialized(limits=limits)
    thread_started(protocol)
    protocol.feed(record({"id": 2, "result": {"turn": {
        "id": "turn_a", "status": "inProgress", "items": [], "error": None}}}))
    return protocol


def state(protocol):
    return {key: copy.deepcopy(value) for key, value in vars(protocol).items() if key not in ("bytes", "messages")}


class CodexRateLimitTests(unittest.TestCase):
    def refused(self, value, *, protocol=None, reason="result"):
        protocol = turn_ready() if protocol is None else protocol
        with self.assertRaises(contract.AnalysisDenied) as caught:
            protocol.feed(record(value))
        self.assertEqual(caught.exception.reason, reason)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_native_shaped_metadata_during_and_after_turn_preserves_answer_and_model_meta(self):
        protocol = turn_ready()
        before = state(protocol)
        self.assertEqual(protocol.feed(record(rate_message(snapshot()))), [])
        self.assertEqual(state(protocol), before)
        item = {"id": "item_a", "type": "agentMessage", "text": "Odpowiedź.", "phase": "final_answer"}
        for method, stamp in (("item/started", "startedAtMs"), ("item/completed", "completedAtMs")):
            protocol.feed(record(notification(method,
                {"threadId": "thr_a", "turnId": "turn_a", "item": item, stamp: 123})))
        protocol.feed(record(notification("turn/completed", {"threadId": "thr_a", "turn": {
            "id": "turn_a", "status": "completed", "items": [], "error": None}})))
        answer = protocol.finish()
        before = state(protocol)
        protocol.feed(record(rate_message(snapshot())))
        self.assertEqual(state(protocol), before)
        self.assertEqual(protocol.finish(), answer)
        self.assertEqual(str(answer), "Odpowiedź.")
        self.assertNotIn(SECRET, str(answer))
        self.assertNotIn(SECRET, contract.canonical(answer.meta))

    def test_known_optional_fields_allow_omission_or_null_without_token_usage_event(self):
        for value in ({}, {key: None for key in snapshot()}, {"primary": {"usedPercent": 0}},
                      {"credits": {"hasCredits": False, "unlimited": True}}, {"planType": "unknown"}):
            protocol = turn_ready()
            before = state(protocol)
            self.assertEqual(protocol.feed(record(rate_message(value))), [])
            self.assertEqual(state(protocol), before)

    def test_rust_signed_ranges_are_accepted_without_invented_percent_or_money_semantics(self):
        for percent in (-2**31, -1, 101, 2**31 - 1):
            for timestamp in (-2**63, 2**63 - 1):
                value = snapshot()
                value["primary"] = {"usedPercent": percent, "windowDurationMins": timestamp, "resetsAt": timestamp}
                value["individualLimit"] = {"limit": "", "used": "1e9999", "remainingPercent": percent,
                                             "resetsAt": timestamp}
                self.assertEqual(turn_ready().feed(record(rate_message(value))), [])

    def test_unknown_fields_and_wrong_scalar_types_are_refused_without_printing_values(self):
        cases = [{"extra": SECRET}, {"limitId": 1}, {"limitName": []}, {"normalModelSlug": {}},
                 {"spendControlReached": 1}, {"planType": SECRET}, {"rateLimitReachedType": SECRET},
                 {"limitId": "x" * 4097}, {"limitName": "x\n"}, {"normalModelSlug": "\u202e"}]
        for change in cases:
            with self.subTest(change=change):
                self.refused(rate_message({**snapshot(), **change}))
        self.refused({**rate_message(snapshot()), "unknown": SECRET})
        self.refused(notification("account/rateLimits/updated", {"rateLimits": snapshot(), "threadId": SECRET}))
        self.refused(notification("account/rateLimits/updated", {}))

    def test_nested_windows_credits_and_spend_objects_are_closed_and_exactly_typed(self):
        values = [
            ("primary", {}), ("primary", {"usedPercent": True}), ("primary", {"usedPercent": 1.0}),
            ("primary", {"usedPercent": 2**31}), ("primary", {"usedPercent": -2**31 - 1}),
            ("primary", {"usedPercent": 0, "resetsAt": 2**63}),
            ("secondary", {"usedPercent": 0, "windowDurationMins": -2**63 - 1}),
            ("secondary", {"usedPercent": 0, "unexpected": SECRET}),
            ("credits", {"hasCredits": 1, "unlimited": False}),
            ("credits", {"hasCredits": False}),
            ("credits", {"hasCredits": False, "unlimited": False, "balance": 1.5}),
            ("individualLimit", {"limit": SECRET, "used": SECRET, "remainingPercent": 0}),
            ("individualLimit", {"limit": SECRET, "used": SECRET, "remainingPercent": True, "resetsAt": 0}),
            ("individualLimit", {"limit": SECRET, "used": SECRET, "remainingPercent": 0, "resetsAt": None}),
        ]
        for key, value in values:
            with self.subTest(key=key, value=value):
                self.refused(rate_message({**snapshot(), key: value}))

    def test_nonfinite_json_is_rejected_before_metadata_validation(self):
        for number in (b"NaN", b"Infinity", b"-Infinity"):
            raw = (b'{"method":"account/rateLimits/updated","params":{"rateLimits":'
                   b'{"primary":{"usedPercent":' + number + b'}}}}')
            with self.subTest(number=number), self.assertRaises(contract.AnalysisDenied) as caught:
                turn_ready().feed(raw)
            self.assertEqual(caught.exception.reason, "result")

    def test_rate_metadata_is_refused_before_turn_but_remains_valid_after_completion(self):
        for protocol in (codex.Protocol(contract.application_input(None, "Pytanie.", context=[]).encode()), initialized()):
            self.refused(rate_message(snapshot()), protocol=protocol)
        self.assertEqual(completed().feed(record(rate_message(snapshot()))), [])

    def test_metadata_floods_still_hit_existing_message_and_wire_budgets(self):
        protocol = turn_ready(limits=codex.Limits(messages=4))
        self.assertEqual(protocol.feed(record(rate_message(snapshot()))), [])
        self.refused(rate_message(snapshot()), protocol=protocol, reason="output-limit")
        protocol = turn_ready()
        protocol.bytes = protocol.limits.wire_bytes
        self.refused(rate_message({}), protocol=protocol, reason="output-limit")
