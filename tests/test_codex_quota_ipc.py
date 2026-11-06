"""Explicit one-request native quota activation with synthetic peers and credentials."""
from contextlib import ExitStack
import json
import os
import socket
import unittest
from unittest.mock import MagicMock, patch

from ai_desktop import codex_quota_ipc as ipc
from ai_desktop.analysis_contract import AnalysisDenied


SAMPLE = {"windows": {"five_hour": {"percent": 25, "reset": 1800000000}}, "plan": "pro"}


class CodexQuotaIPCTests(unittest.TestCase):
    def test_canonical_uid_and_local_alias_are_the_only_request_parameters(self):
        self.assertEqual(ipc.socket_path("1000"), "/run/ai-desktop-codex-limits/1000.sock")
        for uid in (0, 999, -1, 2**32-1, True, "01000", "../1001", "+1000", "１２３４"):
            with self.subTest(uid=uid), self.assertRaises(ipc.QuotaError):
                ipc.socket_path(uid)
        for alias in ("../default", "owner/other", "a" * 33, "", "default\n", True, None):
            with self.subTest(alias=alias), patch.object(ipc.socket, "socket") as opened:
                with self.assertRaises(ipc.QuotaError):
                    ipc.read_limits(alias)
                opened.assert_not_called()

    def client(self, replies, *, inodes=((1, 2), (1, 2)), peer_error=None):
        stack = self.enterContext(ExitStack())
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.send.side_effect = len
        conn.recvmsg.side_effect = [(packet, [], 0, None) for packet in replies]
        stack.enter_context(patch.object(ipc.socket, "socket", return_value=conn))
        stack.enter_context(patch.object(ipc.os, "getuid", return_value=1001))
        stack.enter_context(patch.object(ipc.os, "geteuid", return_value=1001))
        stack.enter_context(patch.object(ipc.e2_account, "account_ready", return_value=True))
        stack.enter_context(patch.object(ipc, "endpoint", side_effect=inodes))
        peer = stack.enter_context(patch.object(ipc.activation_socket, "manager_peer", side_effect=peer_error))
        return conn, peer

    def test_client_checks_manager_peer_before_sending_the_selected_alias(self):
        conn, peer = self.client([ipc.READY, json.dumps(SAMPLE).encode()])
        self.assertEqual(ipc.read_limits("work"), SAMPLE)
        conn.connect.assert_called_once_with(ipc.socket_path(1001))
        peer.assert_called_once_with(conn, ipc.socket_path(1001))
        conn.send.assert_called_once_with(b"READ work")

    def test_client_never_sends_before_all_trust_checks_and_readiness(self):
        for ready, inodes, error in ((ipc.READY, ((3, 4), (3, 4)), AnalysisDenied("endpoint")),
                                     (ipc.READY, ((3, 4), (3, 5)), None),
                                     (b"ANALYSIS2 READY", ((3, 4), (3, 4)), None)):
            with self.subTest(ready=ready, inodes=inodes):
                conn, _ = self.client([ready], inodes=inodes, peer_error=error)
                with self.assertRaises(ipc.QuotaError):
                    ipc.read_limits("default")
                conn.send.assert_not_called()

    def test_absent_socket_is_unavailable_so_it_does_not_invalidate_a_grant(self):
        conn, _ = self.client([], inodes=FileNotFoundError())
        with self.assertRaises(ipc.QuotaError) as error:
            ipc.read_limits("work")
        self.assertEqual(error.exception.reason, "unavailable")
        conn.send.assert_not_called()

    def test_client_accepts_only_finite_errors_and_public_window_fields(self):
        for reply, reason in ((b"REFUSED auth-required", "auth-required"),
                              (b"REFUSED network", "network"), (b"REFUSED PRIVATE", "unavailable"),
                              (b'{"plan":"pro","windows":{},"email":"private"}', "unavailable"),
                              (b'{"plan":"pro","plan":null,"windows":{}}', "unavailable"),
                              (b'{"plan":null,"windows":{"five_hour":{"percent":NaN,"reset":5}}}', "unavailable")):
            with self.subTest(reply=reply):
                self.client([ipc.READY, reply])
                with self.assertRaises(ipc.QuotaError) as error:
                    ipc.read_limits("default")
                self.assertEqual(error.exception.reason, reason)

    def exchange(self, requests, *, value=None, error=None):
        with ExitStack() as stack:
            client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            stack.enter_context(client)
            stack.enter_context(server)
            peer = stack.enter_context(patch.object(ipc.e2_ipc, "_peer_uid"))
            native = stack.enter_context(patch.object(ipc, "_read_native", return_value=value, side_effect=error))
            for packet in requests:
                client.send(packet)
            ipc._serve_connection(server, 1001)
            self.assertEqual(client.recv(1024), ipc.READY)
            peer.assert_called_once_with(server, 1001)
            return client.recv(1024), native

    def test_worker_calls_read_for_only_authenticated_uid_and_one_alias(self):
        response, native = self.exchange([b"READ work"], value=SAMPLE)
        native.assert_called_once_with(1001, "work")
        self.assertEqual(json.loads(response), SAMPLE)
        response, _ = self.exchange([b"READ default"], value={"windows": {}, "plan": None})
        self.assertEqual(json.loads(response), {"windows": {}, "plan": None})

    def test_malformed_oversized_or_pipelined_requests_never_contact_the_service(self):
        for packets in ([b"CONNECT default"], [b"READ 1002 default"], [b"default"],
                        [b"READ bad\xff"], [b"READ " + b"a"*3000], [b"READ work", b"READ other"]):
            with self.subTest(packets=len(packets)):
                reply, native = self.exchange(packets)
                self.assertEqual(reply, b"REFUSED unavailable")
                native.assert_not_called()

    def test_sensitive_or_invalid_native_result_never_crosses_the_socket(self):
        for value, error, reason in (({"windows":{}, "plan":None, "token":"PRIVATE"}, None, "unavailable"),
                                     ({"windows":{"five_hour":{"percent":101,"reset":3}}, "plan":"pro"}, None, "unavailable"),
                                     (None, OSError("PRIVATE"), "unavailable"),
                                     (None, AnalysisDenied("network"), "network"),
                                     (None, AnalysisDenied("auth-required"), "auth-required"),
                                     (None, TimeoutError("PRIVATE"), "timeout")):
            with self.subTest(reason=reason):
                reply, _ = self.exchange([b"READ default"], value=value, error=error)
                self.assertEqual(reply, ("REFUSED " + reason).encode())

    def test_wrong_desktop_peer_never_sees_readiness(self):
        conn = MagicMock()
        with patch.object(ipc.e2_ipc, "_peer_uid", side_effect=ipc.e2_ipc.HandoffDenied()), \
                patch.object(ipc, "_read_native") as native, self.assertRaises(ipc.e2_ipc.HandoffDenied):
            ipc._serve_connection(conn, 1001)
        conn.sendall.assert_not_called()
        native.assert_not_called()

    def test_wrong_systemd_descriptor_is_rejected_before_accept(self):
        env = {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS":"1", "LISTEN_FDNAMES":"analysis"}
        with patch.dict(os.environ, env), patch.object(ipc, "service_identity"), \
                patch.object(ipc.socket, "socket") as opened, self.assertRaises(ipc.QuotaError):
            ipc.serve(1001)
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
