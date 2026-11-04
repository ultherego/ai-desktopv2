"""E2 one-shot worker integration on real abstract Unix sockets, no OAuth.

The CI success case stubs the DIFFERENT UID; only the owner CachyOS host test
can prove actual Linux kernel cross-UID peer credentials.
"""
import contextlib
from dataclasses import replace
import io
import os
import secrets
import socket
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_desktop import e2_account, e2_handoff_cli, e2_ipc, e2_worker_once
from ai_desktop.transfer_consent import ConsentSession
from test_report_policy import report_fixture


def auth():
    report = report_fixture()
    gate = ConsentSession()
    preview = gate.preview(report)
    return gate.authorize(report, confirmed_sha256=preview.sha256, nonce=preview.nonce)


def session():
    return secrets.token_hex(16)


class OneShotWorkerTests(unittest.TestCase):
    def test_session_id_rejects_injection_types_and_oversize(self):
        for invalid in (None, 1, True, "", "x" * 32, "A" * 32,
                        "a" * 31, "a" * 33, "a" * 16 + "/tmp/x",
                        "a" * 32 + "\x00other"):
            with self.subTest(value=invalid):
                with self.assertRaises(e2_ipc.HandoffDenied):
                    e2_worker_once._address(invalid)
        self.assertTrue(e2_worker_once._address(session()).startswith("\0"))

    def test_worker_identity_refuses_current_desktop_or_root(self):
        self.assertFalse(e2_worker_once._worker_ready())
        uid = os.geteuid() + 77
        fake = SimpleNamespace(pw_uid=uid, pw_gid=uid, pw_dir=e2_account.HOME,
                               pw_shell="/usr/bin/nologin")
        home = SimpleNamespace(st_mode=0o40700, st_uid=uid, st_gid=uid)
        with patch.object(e2_worker_once.pwd, "getpwnam", return_value=fake), patch.object(
                e2_worker_once.os, "lstat", return_value=home):
            self.assertFalse(e2_worker_once._worker_ready())

    def test_real_socket_one_packet_and_matching_digest_ack(self):
        token, authorization = session(), auth()
        ready = threading.Event()
        result = {}
        target_uid = os.geteuid() + 77

        def worker():
            try:
                result["digest"] = e2_worker_once.serve_once(
                    token, desktop_uid=target_uid, on_ready=ready.set)
            except Exception as exc:
                result["error"] = type(exc).__name__

        fake_account = SimpleNamespace(pw_uid=target_uid)
        with (patch.object(e2_worker_once, "_worker_ready", return_value=True),
              patch.object(e2_account, "account_ready", return_value=True),
              patch.object(e2_worker_once.pwd, "getpwnam", return_value=fake_account),
              patch.object(e2_ipc, "_peer_uid", return_value=None)):
            task = threading.Thread(target=worker, daemon=True)
            task.start()
            self.assertTrue(ready.wait(2), result)
            self.assertEqual(e2_worker_once.send_once(token, authorization), authorization.sha256)
            task.join(2)
        self.assertFalse(task.is_alive())
        self.assertEqual(result, {"digest": authorization.sha256})
        with self.assertRaises(e2_ipc.HandoffDenied):
            e2_worker_once.send_once(token, authorization)  # one-shot endpoint is gone

    def test_wrong_actual_kernel_uid_denied_even_for_valid_packet(self):
        token, authorization = session(), auth()
        ready = threading.Event()
        result = {}
        def worker():
            try:
                e2_worker_once.serve_once(token, desktop_uid=os.geteuid() + 77,
                                          on_ready=ready.set)
                result["result"] = "accepted"
            except e2_ipc.HandoffDenied:
                result["result"] = "denied"
        with patch.object(e2_worker_once, "_worker_ready", return_value=True):
            task = threading.Thread(target=worker, daemon=True)
            task.start()
            self.assertTrue(ready.wait(2))
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
                client.connect(e2_worker_once._address(token))
                # An actual peer has the current desktop UID, not the synthetic
                # expected value. No success ACK may be emitted.
                client.send(b"malformed")
                client.settimeout(2)
                # Linux may close with EOF or ECONNRESET because the worker
                # refuses before consuming the queued packet. Neither is ACK.
                try:
                    response = client.recv(64)
                except ConnectionResetError:
                    response = b""
                self.assertEqual(response, b"")
            task.join(2)
        self.assertFalse(task.is_alive())
        self.assertEqual(result["result"], "denied")

    def test_worker_refuses_root_same_uid_and_wrong_peer_before_bind(self):
        for uid in (0, True, os.geteuid(), -1, "1000"):
            with self.subTest(uid=uid), patch.object(
                    e2_worker_once, "_worker_ready", return_value=True):
                with self.assertRaises(e2_ipc.HandoffDenied):
                    e2_worker_once.serve_once(session(), desktop_uid=uid)
        with patch.object(e2_worker_once, "_worker_ready", return_value=False):
            with self.assertRaises(e2_ipc.HandoffDenied):
                e2_worker_once.serve_once(session(), desktop_uid=1000)

    def test_desktop_refuses_unprovisioned_worker_and_invalid_authorization(self):
        valid = auth()
        with patch.object(e2_account, "account_ready", return_value=False):
            with self.assertRaises(e2_ipc.HandoffDenied):
                e2_worker_once.send_once(session(), valid)
        with patch.object(e2_account, "account_ready", return_value=True):
            for value in (None, {}, replace(valid, sha256="0" * 64)):
                # An invalid transfer cannot succeed. A valid TYPE carrying an
                # invalid hash is caught by send_approved if transport exists.
                if type(value) is not type(valid):
                    with self.assertRaises(e2_ipc.HandoffDenied):
                        e2_worker_once.send_once(session(), value)

    def test_wrong_digest_ack_refused(self):
        token, authorization = session(), auth()
        ready = threading.Event()
        def fake_worker():
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server:
                server.bind(e2_worker_once._address(token))
                server.listen(1)
                ready.set()
                with server.accept()[0] as conn:
                    conn.recv(8192)
                    conn.send(b"ACK1:" + b"0" * 64)
        with (patch.object(e2_account, "account_ready", return_value=True),
              patch.object(e2_worker_once.pwd, "getpwnam",
                           return_value=SimpleNamespace(pw_uid=os.geteuid() + 77)),
              patch.object(e2_ipc, "_peer_uid", return_value=None)):
            task = threading.Thread(target=fake_worker, daemon=True)
            task.start()
            self.assertTrue(ready.wait(2))
            with self.assertRaises(e2_ipc.HandoffDenied):
                e2_worker_once.send_once(token, authorization)
            task.join(2)
        self.assertFalse(task.is_alive())

    def test_interactive_user_confirmation_exact_hash_and_refusals(self):
        report = report_fixture()
        stream = io.StringIO()
        forwarded = []
        def confirm(prompt):
            return "UDOSTEPNIJ " + prompt.split("UDOSTEPNIJ ", 1)[1].split(" ", 1)[0]
        with (patch.object(e2_handoff_cli.diagnose, "collect", return_value=report),
              patch.object(e2_handoff_cli.e2_worker_once, "send_once",
                           side_effect=lambda t, a: forwarded.append((t, a)) or a.sha256),
              contextlib.redirect_stdout(stream)):
            self.assertEqual(e2_handoff_cli.desktop_main(session(), reader=confirm), 0)
        self.assertEqual(len(forwarded), 1)
        self.assertIn("HANDOFF: PASS sha256=", stream.getvalue())
        for response in ("", "UDOSTEPNIJ", "UDOSTEPNIJ 0", "TAK"):
            with self.subTest(response=response), patch.object(
                    e2_handoff_cli.diagnose, "collect", return_value=report), patch.object(
                    e2_handoff_cli.e2_worker_once, "send_once") as sent, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(e2_handoff_cli.desktop_main(session(), reader=lambda _: response), 2)
                sent.assert_not_called()

    def test_worker_cli_denial_never_echoes_payload(self):
        out = io.StringIO()
        with patch.object(e2_handoff_cli.e2_worker_once, "serve_once",
                          side_effect=e2_ipc.HandoffDenied("HANDOFF: REFUSED")), contextlib.redirect_stdout(out):
            self.assertEqual(e2_handoff_cli.worker_main(session(), 1000), 2)
        self.assertEqual(out.getvalue(), "WORKER: REFUSED\n")


if __name__ == "__main__":
    unittest.main()
