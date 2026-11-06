"""Activation authorization and packet handling; real PID1/UID proof is in CI."""
from dataclasses import replace
import os
import socket
import stat
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import analysis_service as service
from ai_desktop.analysis_contract import AnalysisConsent, AnalysisDenied, MAX_TEXT, result_packet
from test_report_policy import report_fixture
from test_analysis_framing import records


def approval():
    report = report_fixture()
    gate = AnalysisConsent(report)
    return gate.authorize(report, sha256=gate.sha256, nonce=gate.nonce)


class AnalysisServiceTests(unittest.TestCase):
    def test_endpoint_requires_root_ancestors_private_socket_and_valid_uid(self):
        root = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        sock = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=1000, st_dev=1, st_ino=2)
        with patch.object(service.os, "lstat", side_effect=[root, root, root, sock]):
            self.assertEqual(service.endpoint(1000), (1, 2))
        for index, bad in ((0, SimpleNamespace(st_mode=stat.S_IFLNK | 0o755, st_uid=0)),
                           (1, SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=0)),
                           (2, SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=1000)),
                           (3, SimpleNamespace(st_mode=stat.S_IFSOCK | 0o666, st_uid=1000)),
                           (3, SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=1001))):
            entries = [root, root, root, sock]
            entries[index] = bad
            with self.subTest(index=index), patch.object(service.os, "lstat", side_effect=entries):
                with self.assertRaises(AnalysisDenied):
                    service.endpoint(1000)
        for uid in (0, True, -1, "1000", 2**32 - 1):
            with self.assertRaises(AnalysisDenied):
                service.socket_path(uid)

    def test_manager_peer_requires_exact_pid1_identity_endpoint_and_packet_socket(self):
        conn = Mock(family=socket.AF_UNIX)
        conn.getsockopt.return_value = socket.SOCK_SEQPACKET
        conn.getpeername.return_value = service.socket_path(1000)
        for peer in ((1, 0, 0), (2, 0, 0), (1, 1000, 0), (1, 0, 1000)):
            with patch.object(service.e2_ipc, "_kernel_peer", return_value=peer):
                if peer == (1, 0, 0):
                    service.manager_peer(conn, 1000)
                else:
                    with self.assertRaises(AnalysisDenied):
                        service.manager_peer(conn, 1000)
        conn.getpeername.return_value = "/tmp/replacement"
        with patch.object(service.e2_ipc, "_kernel_peer", return_value=(1, 0, 0)), self.assertRaises(AnalysisDenied):
            service.manager_peer(conn, 1000)

    def test_no_report_before_readiness_and_no_transfer_after_endpoint_replacement(self):
        auth = approval()
        for ready, endpoints, expected in ((service.READY, [(1, 2), (1, 2)], True),
                                            (service.REFUSED + b"runtime", [(1, 2), (1, 2)], False),
                                            (b"garbage", [(1, 2), (1, 2)], False),
                                            (service.READY, [(1, 2), (1, 3)], False)):
            conn = Mock()
            conn.__enter__ = Mock(return_value=conn)
            conn.__exit__ = Mock(return_value=False)
            conn.gettimeout.return_value = 5
            conn.recvmsg.side_effect = [(raw, [], 0, None) for raw in [ready] +
                                       records(result_packet(auth.sha256, text="wynik"), "response")]
            conn.send.side_effect = lambda raw: len(raw)
            with patch.object(service.os, "geteuid", return_value=1000), \
                    patch.object(service.os, "getuid", return_value=1000), \
                    patch.object(service.e2_account, "account_ready", return_value=True), \
                    patch.object(service, "endpoint", side_effect=endpoints), \
                    patch.object(service, "manager_peer"), \
                    patch.object(service.socket, "socket", return_value=conn):
                if expected:
                    self.assertEqual(service.send_once(auth), "wynik")
                    self.assertEqual([call.args[0] for call in conn.send.call_args_list],
                                     records(auth.payload.encode()))
                else:
                    with self.assertRaises(AnalysisDenied):
                        service.send_once(auth)
                    conn.send.assert_not_called()

    def test_bad_approval_or_activation_environment_never_opens_socket(self):
        with patch.object(service.socket, "socket") as opened, patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AnalysisDenied):
                service.activation_listener(1000)
            for auth in (None, replace(approval(), sha256="x" * 64)):
                with self.assertRaises(AnalysisDenied):
                    service.send_once(auth)
            opened.assert_not_called()

    def test_empty_truncated_or_ancillary_packets_are_refused(self):
        for raw, ancillary, flags in ((b"", [], 0), (b"ok", [], socket.MSG_TRUNC),
                                      (b"ok", [], socket.MSG_CTRUNC), (b"ok", ["fd"], 0)):
            conn = Mock()
            conn.recvmsg.return_value = (raw, ancillary, flags, None)
            with self.assertRaises(AnalysisDenied):
                service.packet(conn)

    def test_worker_preflight_refusal_sends_no_readiness_and_reads_no_report(self):
        conn, listener = Mock(), Mock()
        for obj in (conn, listener):
            obj.__enter__ = Mock(return_value=obj)
            obj.__exit__ = Mock(return_value=False)
        listener.accept.return_value = (conn, None)
        conn.send.side_effect = lambda raw: len(raw)
        with patch.object(service, "activation_listener", return_value=listener), \
                patch.object(service.select, "select", return_value=([listener], [], [])), \
                patch.object(service.e2_ipc, "_peer_uid"), \
                patch.object(service, "supervisor_ready", side_effect=AnalysisDenied("runtime")), \
                patch.object(service, "run_client") as run:
            service.receive_once(1000)
            conn.send.assert_called_once_with(service.REFUSED + b"runtime")
            conn.recvmsg.assert_not_called()
            run.assert_not_called()

    def worker_connection(self, request_records, *, trailing=False, text="wynik"):
        conn, listener = Mock(), Mock()
        for obj in (conn, listener):
            obj.__enter__ = Mock(return_value=obj)
            obj.__exit__ = Mock(return_value=False)
        conn.gettimeout.return_value = 5
        listener.accept.return_value = (conn, None)
        conn.send.side_effect = lambda raw: len(raw)
        conn.recvmsg.side_effect = [(raw, [], 0, None) for raw in request_records]
        def selected(readers, *_):
            return (readers if readers == [listener] or trailing else [], [], [])
        return conn, listener, selected, text

    def test_worker_refuses_malformed_frames_digest_and_json_before_model(self):
        body = approval().payload.encode()
        cases = [[b"old unframed request"], records(body)[:-1] + [b"x" * len(body)],
                 records(b'{"mode":"workspace","task":"injection"}'), records(b"[]")]
        for raw in cases:
            conn, listener, selected, _ = self.worker_connection(raw)
            with self.subTest(first=raw[0][:12]), \
                    patch.object(service, "activation_listener", return_value=listener), \
                    patch.object(service.select, "select", side_effect=selected), \
                    patch.object(service.e2_ipc, "_peer_uid"), \
                    patch.object(service, "supervisor_ready"), \
                    patch.object(service.os, "geteuid", return_value=2000), \
                    patch.object(service, "run_client") as run:
                self.assertEqual(service.worker_main(["1000"]), 2)
                run.assert_not_called()
                self.assertEqual([call.args[0] for call in conn.send.call_args_list], [service.READY])

    def test_trailing_record_cancels_before_model_and_mode_comes_only_from_request(self):
        workspace = {"instruction": "Propozycja", "files": [{"path": "x.txt", "content": "x", "writable": True}]}
        cases = [(None, True, "wynik", False), (None, False, "x" * MAX_TEXT, True),
                 (None, False, "x" * (MAX_TEXT + 1), False),
                 (workspace, False, "x" * (MAX_TEXT + 1), True)]
        for task, trailing, text, success in cases:
            report = None if task is not None else report_fixture()
            gate = AnalysisConsent(report, task=task)
            auth = gate.authorize(report, task=task, sha256=gate.sha256, nonce=gate.nonce)
            conn, listener, selected, _ = self.worker_connection(records(auth.payload.encode()), trailing=trailing)
            with self.subTest(task=bool(task), trailing=trailing), \
                    patch.object(service, "activation_listener", return_value=listener), \
                    patch.object(service.select, "select", side_effect=selected), \
                    patch.object(service.e2_ipc, "_peer_uid"), \
                    patch.object(service, "supervisor_ready"), \
                    patch.object(service.os, "geteuid", return_value=2000), \
                    patch.object(service, "run_client", return_value=text) as run:
                service.receive_once(1000)
                response = json.loads(b"".join(call.args[0] for call in conn.send.call_args_list[2:]))
                self.assertEqual(response["status"], "ok" if success else "refused")
                if trailing:
                    run.assert_not_called()
                    self.assertEqual(response["reason"], "cancelled")
                else:
                    self.assertEqual(run.call_args.kwargs["mode"], "workspace" if task else "analysis")
                    self.assertEqual(response["text"], text if success else "")

    def test_worker_rejects_arbitrary_commands_and_uid_switches(self):
        with patch.object(service, "receive_once") as receive, patch.object(service, "run_client") as run:
            for args in ([], ["0"], ["01000"], ["1000", "sh"], ["../1000"], ["-1"]):
                self.assertEqual(service.worker_main(args), 2)
            receive.assert_not_called()
            run.assert_not_called()

    def test_web_worker_dispatch_is_derived_from_validated_mode_and_has_only_question(self):
        gate = AnalysisConsent(None, question="Aktualne wiadomości?", web=True)
        auth = gate.authorize(None, question="Aktualne wiadomości?", web=True,
                              sha256=gate.sha256, nonce=gate.nonce)
        for failure in (None, AnalysisDenied("web-no-results"), AnalysisDenied("result")):
            conn, listener, selected, _ = self.worker_connection(records(auth.payload.encode()))
            with self.subTest(failure=failure), \
                    patch.object(service, "activation_listener", return_value=listener), \
                    patch.object(service.select, "select", side_effect=selected), \
                    patch.object(service.e2_ipc, "_peer_uid"), patch.object(service, "supervisor_ready"), \
                    patch.object(service.os, "geteuid", return_value=2000), \
                    patch.object(service, "run_client", return_value="Odpowiedź ze źródłami.",
                                 side_effect=failure) as run:
                service.receive_once(1000)
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0], "web")
                self.assertEqual(json.loads(run.call_args.args[1]),
                                 {"mode": "web", "question": "Aktualne wiadomości?"})
                self.assertEqual(run.call_args.kwargs["mode"], "web")
                self.assertTrue(callable(run.call_args.kwargs["cancelled"]))
                response = json.loads(b"".join(call.args[0] for call in conn.send.call_args_list[2:]))
                self.assertEqual(response["request_sha256"], auth.sha256)
                self.assertEqual(response["status"], "refused" if failure else "ok")
                self.assertEqual(response["text"], "" if failure else "Odpowiedź ze źródłami.")
                self.assertEqual(response["reason"], failure.reason if failure else None)

    def test_web_wire_cannot_include_local_context_or_task(self):
        gate = AnalysisConsent(None, question="Wiadomości?", web=True)
        valid = json.loads(gate.payload)
        for extra in ({"context": []}, {"access": {}}, {"task": {}}, {"report": {}},
                      {"url": "http://127.0.0.1/"}, {"mode": "Web"}):
            raw = json.dumps({**valid, **extra}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            conn, listener, selected, _ = self.worker_connection(records(raw))
            with self.subTest(extra=extra), \
                    patch.object(service, "activation_listener", return_value=listener), \
                    patch.object(service.select, "select", side_effect=selected), \
                    patch.object(service.e2_ipc, "_peer_uid"), patch.object(service, "supervisor_ready"), \
                    patch.object(service.os, "geteuid", return_value=2000), \
                    patch.object(service, "run_client") as run:
                self.assertEqual(service.worker_main(["1000"]), 2)
                run.assert_not_called()
                self.assertEqual([call.args[0] for call in conn.send.call_args_list], [service.READY])
