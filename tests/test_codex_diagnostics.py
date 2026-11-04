"""Finite Codex diagnostics across private child and approved response channels."""
from contextlib import redirect_stderr
import io
import json
import sys
import unittest
from unittest.mock import Mock, patch

from ai_desktop import analysis_contract as contract, analysis_service as service
from ai_desktop import analysis_sandbox as sandbox, codex_child, codex_protocol as protocol
from ai_desktop import execution
from ai_desktop.analysis_diagnostics import CODEX_DIAGNOSTIC_REASONS, codex_diagnostic

SECRET = "synthetic-private-token-DO-NOT-PRINT"
SHA = "a" * 64


def selected(provider="codex"):
    return {"provider": provider, "model": "default", "effort": "default", "instructions": "",
            "profile": "reviewer", "access": "read-only"}


def application():
    return contract.application_input(None, "Pytanie.", context=[], execution=selected()).encode()


def encode(value):
    return contract.canonical(value).encode()


def stage_protocol(stage):
    item = protocol.Protocol(application())
    item.start()
    if stage in ("thread", "turn"):
        item.feed(encode({"id": 0, "result": {}}))
    if stage == "turn":
        item.feed(encode({"id": 1, "result": {
            "thread": {"id": "thr_a", "ephemeral": True}, "model": "synthetic-model",
            "modelProvider": protocol.PROVIDER, "approvalPolicy": "never", "cwd": "/work",
            "sandbox": {"type": "readOnly", "networkAccess": False}, "instructionSources": []}}))
    return item


class CodexDiagnosticTests(unittest.TestCase):
    def assert_failure(self, callable_, reason, check):
        with self.assertRaises(contract.AnalysisDenied) as caught:
            callable_()
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(caught.exception.diagnostic, check)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn("https://", str(caught.exception))
        return caught.exception

    def test_closed_codes_cross_child_marker_and_result_without_native_values(self):
        for check, reasons in CODEX_DIAGNOSTIC_REASONS.items():
            for reason in reasons:
                with self.subTest(check=check, reason=reason):
                    marker = ("CLIENT: REFUSED stage=native-exec reason=" + reason + " check=" + check).encode()
                    failure = sandbox.child_failure(marker + b"\n" + SECRET.encode(), "client", provider="codex")
                    self.assertEqual((failure.stage, failure.reason, failure.diagnostic), ("native-exec", reason, check))
                    raw = contract.result_packet(SHA, reason=failure.reason, diagnostic=failure.diagnostic,
                                                 provider="codex", mode="question")
                    self.assertNotIn(SECRET.encode(), raw)
                    self.assert_failure(lambda: contract.read_result(raw, SHA, provider="codex", mode="question"),
                                        reason, check)
                    self.assert_failure(lambda: contract.read_result(raw, SHA, provider="claude", mode="question"),
                                        "result", None)

    def test_child_failure_code_requires_exact_provider_code_and_reason(self):
        prefix = b"CLIENT: REFUSED stage=native-exec reason=client check="
        for check in (SECRET, "codex-launch " + SECRET,
                      "web-init-event", "codex-http-forbidden"):
            with self.subTest(check=check):
                failure = sandbox.child_failure(prefix + check.encode(), "client", provider="codex")
                self.assertNotIn(SECRET, str(failure))
                self.assertIsNone(failure.diagnostic)
        failure = sandbox.child_failure(prefix + b"codex-launch", "client", provider="claude")
        self.assertIsNone(failure.diagnostic)

    def test_packet_build_and_parse_reject_wrong_provider_mode_reason_and_success(self):
        for options in ({"provider": "claude"}, {"provider": "codex", "mode": "web"},
                        {"provider": "codex", "reason": "network"}, {"provider": SECRET},
                        {"provider": "codex", "diagnostic": SECRET},
                        {"provider": "codex", "diagnostic": {"message": SECRET}}):
            values = {"provider": "codex", "reason": "client", "diagnostic": "codex-launch", **options}
            with self.subTest(options=options), self.assertRaises(contract.AnalysisDenied):
                contract.result_packet(SHA, **values)
        for field, value in (("diagnostic", SECRET), ("reason", "network"), ("status", "ok"),
                             ("text", SECRET)):
            raw = json.loads(contract.result_packet(SHA, provider="codex", reason="client", diagnostic="codex-launch"))
            raw[field] = value
            with self.subTest(field=field):
                self.assert_failure(lambda: contract.read_result(encode(raw), SHA, provider="codex"), "result", None)
        with self.assertRaises(contract.AnalysisDenied):
            contract.result_packet(SHA, provider="codex", text="Answer", diagnostic="codex-launch")

    def test_no_diagnostic_success_keeps_model_text_and_metadata(self):
        meta = {"models": [{"model": "synthetic-model", "input": 1, "output": 2, "window": 3}]}
        raw = contract.result_packet(SHA, provider="codex", text="Odpowiedź.", meta=meta)
        value = contract.read_result(raw, SHA, provider="codex")
        self.assertIs(type(value), contract.ModelText)
        self.assertEqual((str(value), value.meta), ("Odpowiedź.", meta))
        self.assertNotIn("diagnostic", json.loads(raw))

    def test_rpc_error_stage_comes_from_outgoing_state_not_native_message(self):
        for stage, ident in (("initialize", 0), ("thread", 1), ("turn", 2)):
            item = stage_protocol(stage)
            with self.subTest(stage=stage):
                self.assert_failure(lambda: item.feed(encode({"id": ident,
                    "error": {"code": -32602, "message": SECRET, "data": {"url": "https://" + SECRET}}})),
                    "client", "codex-rpc-" + stage)

    def test_pinned_camelcase_error_classes_and_typed_http_status_only(self):
        cases = [("usageLimitExceeded", "rate-limit", "codex-usage-limit"),
                 ("rateLimitExceeded", "rate-limit", "codex-rate-limit"),
                 ("unauthorized", "auth-required", "codex-unauthorized"),
                 ("modelNotFound", "client", "codex-model-unavailable"),
                 ("serverOverloaded", "provider-unavailable", "codex-server-overloaded"),
                 ({"httpConnectionFailed": {"httpStatusCode": 401}}, "auth-required", "codex-http-unauthorized"),
                 ({"httpConnectionFailed": {"httpStatusCode": 403}}, "authorization", "codex-http-forbidden"),
                 ({"responseStreamConnectionFailed": {"httpStatusCode": 429}}, "rate-limit", "codex-http-rate-limit"),
                 ({"responseStreamDisconnected": {"httpStatusCode": 503}}, "provider-unavailable", "codex-http-unavailable"),
                 ({"responseTooManyFailedAttempts": {"httpStatusCode": None}}, "network", "codex-stream-attempts"),
                 ({"httpConnectionFailed": {"httpStatusCode": 599}}, "network", "codex-http-connection")]
        for info, reason, check in cases:
            item = stage_protocol("turn")
            with self.subTest(info=info):
                self.assert_failure(lambda: item.feed(encode({"method": "error", "params": {
                    "error": {"codexErrorInfo": info, "message": SECRET}}})), reason, check)

    def test_error_after_accepted_turn_is_stream_failure_not_question_rejection(self):
        item = stage_protocol("turn")
        item.feed(encode({"id": 2, "result": {"turn": {
            "id": "turn_a", "status": "inProgress", "items": [], "error": None}}}))
        self.assert_failure(lambda: item.feed(encode({"id": 2, "error": {"message": SECRET}})),
                            "client", "codex-stream")

    def test_unknown_native_classes_statuses_and_prose_remain_generic(self):
        for info in (SECRET, "Unauthorized", {SECRET: {"httpStatusCode": 401}},
                     {"httpConnectionFailed": {"httpStatusCode": True}},
                     {"httpConnectionFailed": {"httpStatusCode": "401"}},
                     {"httpConnectionFailed": {"httpStatusCode": 401, "message": SECRET}},
                     {"httpConnectionFailed": {"httpStatusCode": 65536}}):
            item = stage_protocol("turn")
            with self.subTest(info=info):
                self.assert_failure(lambda: item.feed(encode({"method": "error", "params": {
                    "error": {"codexErrorInfo": info, "message": SECRET}}})), "client", "codex-server-error")
        self.assertIsNone(codex_diagnostic(SECRET, "client"))

    def test_native_exit_stderr_is_private_and_exit_code_has_fixed_label(self):
        command = [sys.executable, "-I", "-B", "-c",
                   "import sys; sys.stdin.buffer.readline(); sys.stderr.write(" + repr(SECRET) + "); sys.exit(2)"]
        self.assert_failure(lambda: protocol.exchange(command, protocol.Protocol(application()),
                            env={"PATH": "/usr/bin"}, timeout=5), "client", "codex-native-exit-2")

    def test_child_prints_only_finite_check_from_private_failure(self):
        private = Mock()
        private.buffer.read.return_value = b"private"
        failure = contract.AnalysisDenied("client", diagnostic="codex-rpc-thread")
        bridge = Mock()
        bridge.__enter__ = Mock(return_value=bridge)
        bridge.__exit__ = Mock(return_value=False)
        with patch.object(codex_child, "check_boundary"), \
                patch.object(codex_child.Path, "exists", return_value=False), \
                patch.object(codex_child.Path, "iterdir", return_value=iter(())), \
                patch.object(codex_child, "install_mode_filter"), \
                patch.object(codex_child, "private_input", return_value=(SECRET, application(), selected())), \
                patch.object(codex_child.sys, "stdin", private), \
                patch.object(codex_child, "_Bridge", return_value=bridge), \
                patch.object(codex_child.threading, "Thread"), \
                patch.object(codex_child, "run", side_effect=failure), redirect_stderr(io.StringIO()) as output:
            self.assertEqual(codex_child.main(["analyze", "{}"]), 2)
        self.assertEqual(output.getvalue(), "CLIENT: REFUSED stage=native-exec reason=client check=codex-rpc-thread\n")
        self.assertNotIn(SECRET, output.getvalue())

    def test_service_binds_diagnostic_to_exact_approval_not_ambient_profile(self):
        for provider in ("codex", "claude"):
            with execution.using(selected(provider)):
                gate = contract.AnalysisConsent(None, question="Pytanie.", context=[])
                approval = gate.authorize(None, question="Pytanie.", context=[], sha256=gate.sha256, nonce=gate.nonce)
            response = contract.result_packet(approval.sha256, provider="codex", mode="question",
                                              reason="client", diagnostic="codex-rpc-thread")
            conn = Mock()
            conn.__enter__ = Mock(return_value=conn)
            conn.__exit__ = Mock(return_value=False)
            with self.subTest(provider=provider), execution.using(selected("claude")), \
                    patch.object(service.os, "geteuid", return_value=1000), \
                    patch.object(service.os, "getuid", return_value=1000), \
                    patch.object(service.e2_account, "account_ready", return_value=True), \
                    patch.object(service, "endpoint", return_value=(1, 2)), \
                    patch.object(service, "manager_peer"), \
                    patch.object(service.socket, "socket", return_value=conn), \
                    patch.object(service, "packet", return_value=service.READY), \
                    patch.object(service, "send_message"), \
                    patch.object(service, "receive_message", return_value=response):
                self.assert_failure(lambda: service.send_once(approval),
                                    "client" if provider == "codex" else "result",
                                    "codex-rpc-thread" if provider == "codex" else None)

    def test_worker_transfers_only_valid_codex_failure_code_from_exact_request(self):
        with execution.using(selected()):
            gate = contract.AnalysisConsent(None, question="Pytanie.", context=[])
            approval = gate.authorize(None, question="Pytanie.", context=[], sha256=gate.sha256, nonce=gate.nonce)
        marker = b"CLIENT: REFUSED stage=native-exec reason=client check=codex-rpc-thread\n" + SECRET.encode()
        failure = sandbox.child_failure(marker, "client", provider="codex")
        conn, listener = Mock(), Mock()
        for item in (conn, listener):
            item.__enter__ = Mock(return_value=item)
            item.__exit__ = Mock(return_value=False)
        listener.accept.return_value = (conn, None)
        conn.send.side_effect = lambda body: len(body)
        def readiness(readers, *_):
            return (readers if readers == [listener] else [], [], [])
        with patch.object(service, "activation_listener", return_value=listener), \
                patch.object(service.select, "select", side_effect=readiness), \
                patch.object(service.e2_ipc, "_peer_uid"), \
                patch.object(service, "supervisor_ready"), \
                patch.object(service.os, "geteuid", return_value=2000), \
                patch.object(service, "receive_message", return_value=approval.payload.encode()), \
                patch.object(service, "send_message") as output, \
                patch("ai_desktop.codex_runtime.run_client", side_effect=failure) as native, \
                patch.object(service, "run_client") as claude:
            service.receive_once(1000)
        claude.assert_not_called()
        native.assert_called_once()
        body = output.call_args.args[1]
        self.assertNotIn(SECRET.encode(), body)
        self.assert_failure(lambda: contract.read_result(body, approval.sha256, mode="question", provider="codex"),
                            "client", "codex-rpc-thread")
