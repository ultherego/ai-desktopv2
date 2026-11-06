"""Bounded metadata classification; no real client, OAuth or provider traffic."""
import contextlib
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_desktop import analysis_sandbox as sandbox, provider_errors as errors
from ai_desktop.analysis_contract import AnalysisDenied, CLIENT_VERSION
from system_client_fixtures import FakeBinding
from claude_client_fixtures import compatibility_results


def failure(status, **changes):
    value = {"type": "result", "subtype": "success", "is_error": True,
             "api_error_status": status, "result": "SECRET result", "errors": ["SECRET error"]}
    value.update(changes)
    return json.dumps(value).encode()


class ProviderErrorTests(unittest.TestCase):
    def test_only_documented_failure_metadata_maps_to_redacted_reasons(self):
        for code in (0, 1):
            for status, reason in errors.HTTP_REASONS.items():
                with self.subTest(code=code, status=status):
                    self.assertEqual(errors.classify_failure(code, failure(status)), reason)
                    with self.assertRaises(AnalysisDenied) as caught:
                        sandbox.model_text(code, failure(status))
                    self.assertEqual(caught.exception.reason, reason)
                    self.assertNotIn("SECRET", str(caught.exception))

    def test_model_prose_and_unknown_or_wrongly_typed_fields_cannot_classify(self):
        examples = [failure(429, is_error=False), failure(429, subtype="error_max_turns"),
                    failure(429, type="assistant"), failure("429"), failure(True),
                    failure(None), failure(403), failure(400), failure(418),
                    b'{"type":"result","subtype":"success","is_error":true,"result":"429 rate_limit authentication_error"}']
        for raw in examples:
            with self.subTest(raw=raw):
                self.assertIsNone(errors.classify_failure(1, raw))
        for code in (-9, 2, 127, True, "1"):
            self.assertIsNone(errors.classify_failure(code, failure(429)))

    def test_invalid_oversized_duplicate_or_nonfinite_json_is_not_classified(self):
        for raw in (b"", b"SECRET", b"\xff", b"[]", b"null", b"{" * 2000,
                    b"[" * 2000 + b"]" * 2000,
                    b'{"type":"result","subtype":"success","is_error":true,"api_error_status":401,"api_error_status":429}',
                    failure(401)[:-1] + b',"metadata":{"x":1,"x":2}}',
                    failure(401)[:-1] + b',"metadata":NaN}',
                    failure(401) + b" " * errors.MAX_FAILURE_BYTES,
                    None, "SECRET"):
            with self.subTest(raw_type=type(raw).__name__):
                self.assertIsNone(errors.classify_failure(1, raw))

    def test_success_with_error_metadata_is_refused_and_error_prose_is_not_parsed(self):
        success = {"type": "result", "subtype": "success", "is_error": False,
                   "num_turns": 1, "permission_denials": [], "result": "429 authentication_error"}
        self.assertEqual(sandbox.model_text(0, json.dumps(success).encode()), success["result"])
        with self.assertRaisesRegex(AnalysisDenied, "reason=result$"):
            sandbox.model_text(0, json.dumps(dict(success, api_error_status=429)).encode())
        with self.assertRaisesRegex(AnalysisDenied, "reason=client$"):
            sandbox.model_text(1, json.dumps(success).encode())

    def test_boundary_signal_precedes_provider_and_only_observed_dial_means_network(self):
        marker = b"CLIENT: REFUSED stage=boundary reason=sandbox\nSECRET"
        result = sandbox.analysis_failure(1, failure(401), marker, True)
        self.assertEqual((result.stage, result.reason), ("boundary", "sandbox"))
        self.assertEqual(sandbox.analysis_failure(1, failure(429), b"SECRET", True).reason, "rate-limit")
        self.assertEqual(sandbox.analysis_failure(1, b"SECRET", b"network SECRET", False).reason, "client")
        self.assertEqual(sandbox.analysis_failure(1, b"SECRET", b"SECRET", True).reason, "network")
        self.assertEqual(sandbox.analysis_failure(-9, b"SECRET", b"SECRET", True).reason, "client")

    def test_signed_out_status_is_more_specific_than_same_phase_connection_failure(self):
        for output, reason in ((b'{"loggedIn":false,"email":"SECRET"}', "auth-required"),
                               (b'{"loggedIn":true}', "network"), (b"SECRET", "network")):
            with self.subTest(reason=reason), self.assertRaises(AnalysisDenied) as caught:
                sandbox.check_auth(1, output, connection_failed=True)
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn("SECRET", str(caught.exception))


class ProxyObservationTests(unittest.TestCase):
    def make_proxy(self, dial):
        def initialize(instance, path, *, dial, stream_seconds, stream_limit):
            instance.dial = dial
        with patch.object(sandbox.ConnectProxy, "__init__", initialize):
            return sandbox._Proxy("unused", dial=dial)

    def test_previous_phase_connection_failure_cannot_contaminate_new_phase(self):
        dial = Mock(side_effect=OSError("SECRET address"))
        proxy = self.make_proxy(dial)
        previous = proxy.begin_phase()
        old_request, new_request = object(), object()
        with patch.object(sandbox.ConnectProxy, "verify_request", return_value=True):
            self.assertTrue(proxy.verify_request(old_request, ""))
            current = proxy.begin_phase()
            self.assertTrue(proxy.verify_request(new_request, ""))
        def request_thread(instance, request, address):
            with self.assertRaises(OSError):
                instance.dial("api.anthropic.com")
        with patch.object(sandbox.ConnectProxy, "process_request_thread", request_thread):
            proxy.process_request_thread(old_request, "")
            self.assertTrue(previous.is_set())
            self.assertFalse(current.is_set())
            proxy.process_request_thread(new_request, "")
            self.assertTrue(current.is_set())
        self.assertEqual(dial.call_count, 2)
        self.assertEqual(proxy._request_failures, {})
        self.assertFalse(hasattr(proxy._request_local, "failure"))

    def test_success_and_unaccepted_connections_do_not_report_network_failure(self):
        proxy = self.make_proxy(Mock(return_value="synthetic socket"))
        event = proxy.begin_phase()
        request = object()
        with patch.object(sandbox.ConnectProxy, "verify_request", return_value=False):
            self.assertFalse(proxy.verify_request(request, ""))
        self.assertEqual(proxy._request_failures, {})
        self.assertEqual(proxy.dial("api.anthropic.com"), "synthetic socket")
        self.assertFalse(event.is_set())


class NativeSupervisorFailureTests(unittest.TestCase):
    def invoke(self, final, *, failed_phase=None):
        events = []
        def begin_phase():
            event = threading.Event()
            if len(events) == failed_phase:
                event.set()
            events.append(event)
            return event
        proxy = SimpleNamespace(begin_phase=begin_phase, serve_forever=lambda: None,
                                shutdown=Mock())
        results = compatibility_results() + [(0, json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                                   "configDirectory": sandbox.INNER_PROFILE}).encode(), b""), final]
        with patch.object(sandbox, "supervisor_ready"), \
                patch.object(sandbox, "profile_lock", return_value=contextlib.nullcontext()), \
                patch.object(sandbox.client_binding, "bind", side_effect=lambda _: FakeBinding("claude")), \
                patch.object(sandbox, "namespace_command", return_value=["unused"]), \
                patch.object(sandbox, "_Proxy", return_value=contextlib.nullcontext(proxy)), \
                patch.object(sandbox, "bounded_process", side_effect=results) as process:
            with self.assertRaises(AnalysisDenied) as caught:
                sandbox.run_client("analyze", b"synthetic request")
        proxy.shutdown.assert_called_once()
        self.assertEqual(process.call_count, 7)
        self.assertNotIn("SECRET", str(caught.exception))
        return caught.exception.reason

    def test_failure_cleanup_once_without_retry_or_native_output(self):
        self.assertEqual(self.invoke((1, failure(401), b"SECRET")), "auth-required")
        self.assertEqual(self.invoke((1, failure(429), b"SECRET")), "rate-limit")
        self.assertEqual(self.invoke((0, failure(503), b"SECRET")), "provider-unavailable")
        self.assertEqual(self.invoke((1, b"SECRET", b"SECRET"), failed_phase=1), "network")
        self.assertEqual(self.invoke((1, b"SECRET", b"SECRET"), failed_phase=0), "client")
