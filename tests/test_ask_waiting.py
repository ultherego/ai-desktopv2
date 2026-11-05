"""Request lifecycle integration with the existing offline ask harness."""
from contextlib import contextmanager
import json
import unittest
from unittest.mock import patch

from ai_desktop import ask_cli as ask
from ai_desktop.analysis_contract import AnalysisDenied
import test_ask_cli as existing


class AskWaitingTests(unittest.TestCase):
    def run_waiting(self, *, error=None, cleanup_error=None, web=False, decline=False, startup_error=None):
        events = []

        class Indicator:
            def __init__(self, **kwargs):
                events.append("idle")

            def __enter__(self):
                return self

            def phase(self, name):
                events.append(name)

            def __exit__(self, *_):
                events.append("stopped")

        @contextmanager
        def session(**kwargs):
            events.append("authorization")
            if startup_error:
                raise startup_error
            try:
                yield
            finally:
                events.append("cleanup-done")
                if cleanup_error:
                    raise cleanup_error

        def send(approval):
            mode = json.loads(approval.payload)["mode"]
            events.append("send-" + mode)
            if error:
                raise error
            if mode == "assistant":
                return json.dumps({"action": "web"} if web else {"action": "answer", "text": "SYNTHETIC_ANSWER"})
            return "SYNTHETIC_ANSWER"

        with patch.object(ask, "WaitingIndicator", Indicator):
            result = existing.AskCliTests().run_ui(session=session, send=send,
                answers=("Pytanie", "no" if decline else "yes"),
                access=existing.WEB_ACCESS if web else None,
                sources=existing.WEB_SOURCES if web else None)
        return result, events

    def test_animation_starts_after_authorization_and_stops_after_cleanup(self):
        result, events = self.run_waiting()
        self.assertEqual(result.code, 0)
        self.assertEqual(events, ["idle", "authorization", "assistant", "send-assistant", "cleanup", "cleanup-done", "stopped"])
        self.assertIn("SYNTHETIC_ANSWER", result.output)

    def test_each_request_has_own_indicator_and_no_spinner_during_second_sudo(self):
        result, events = self.run_waiting(web=True)
        self.assertEqual(result.code, 0)
        self.assertEqual(events, ["idle", "authorization", "assistant", "send-assistant", "cleanup", "cleanup-done", "stopped",
                                  "idle", "authorization", "web", "send-web", "cleanup", "cleanup-done", "stopped"])
        self.assertEqual(result.sent.call_count, 2)
        self.assertEqual(result.reader.call_count, 2)  # question and one approval

    def test_cancel_and_provider_refusal_cleanup_without_retry_or_answer(self):
        for error in (KeyboardInterrupt(), AnalysisDenied("rate-limit")):
            with self.subTest(error=type(error).__name__):
                result, events = self.run_waiting(error=error)
                self.assertEqual(result.code, 2)
                self.assertEqual(events[-3:], ["cleanup", "cleanup-done", "stopped"])
                result.sent.assert_called_once()
                self.assertNotIn("SYNTHETIC_ANSWER", result.output)

    def test_cleanup_failure_stops_animation_and_withholds_answer(self):
        result, events = self.run_waiting(cleanup_error=AnalysisDenied("cleanup"))
        self.assertEqual(result.code, 2)
        self.assertEqual(events[-1], "stopped")
        self.assertNotIn("SYNTHETIC_ANSWER", result.output)
        self.assertNotIn("REFUSED reason=", result.output)

    def test_refusal_before_transfer_never_animates_or_sends(self):
        result, events = self.run_waiting(decline=True)
        self.assertEqual(result.code, 2)
        self.assertEqual(events, [])
        result.sent.assert_not_called()

    def test_startup_failure_leaves_indicator_idle_and_never_sends(self):
        result, events = self.run_waiting(startup_error=AnalysisDenied("authorization"))
        self.assertEqual(result.code, 2)
        self.assertEqual(events, ["idle", "authorization", "stopped"])
        result.sent.assert_not_called()
