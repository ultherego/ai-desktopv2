"""E2 approved diagnostic transfer: real AF_UNIX packets and synthetic UID pairs.

No Claude, provider connection, OAuth, daemon or privileged user creation.
"""
from contextlib import contextmanager
from dataclasses import replace
import json
import os
import socket
import unittest
from unittest.mock import patch

from ai_desktop import e2_ipc
from ai_desktop.transfer_consent import ConsentSession, TransferAuthorization
from test_report_policy import report_fixture


@contextmanager
def pair_sockets(kind=socket.SOCK_SEQPACKET):
    left, right = socket.socketpair(socket.AF_UNIX, kind)
    with left, right:
        yield left, right


def authorized():
    report = report_fixture()
    gate = ConsentSession()
    preview = gate.preview(report)
    return gate.authorize(report, confirmed_sha256=preview.sha256, nonce=preview.nonce)


def packet(auth):
    return json.dumps({
        "schema": 1, "sha256": auth.sha256, "recipient": auth.recipient,
        "purpose": auth.purpose, "payload": auth.payload,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class UnixHandoffTests(unittest.TestCase):
    def send_as_desktop(self, channel, auth):
        with (patch.object(e2_ipc.os, "geteuid", return_value=1000),
              patch.object(e2_ipc, "_kernel_peer", return_value=(123, 980, 980))):
            e2_ipc.send_approved(channel, auth, worker_uid=980)

    def receive_as_worker(self, channel):
        with (patch.object(e2_ipc.os, "geteuid", return_value=980),
              patch.object(e2_ipc, "_kernel_peer", return_value=(456, 1000, 1000))):
            return e2_ipc.receive_approved(channel, desktop_uid=1000)

    def test_real_kernel_peer_credentials_are_observed_without_launch(self):
        with pair_sockets() as pair:
            for sock in pair:
                pid, uid, gid = e2_ipc._kernel_peer(sock)
                self.assertEqual(pid, os.getpid())
                self.assertEqual(uid, os.geteuid())
                self.assertEqual(gid, os.getegid())

    def test_real_unix_packet_delivers_only_exact_approved_report(self):
        auth = authorized()
        with pair_sockets() as pair:
            sender, worker = pair
            self.send_as_desktop(sender, auth)
            self.assertEqual(self.receive_as_worker(worker), auth.payload)
            self.assertEqual(worker.gettimeout(), None)
            self.assertEqual(sender.gettimeout(), None)

    def test_same_uid_root_and_wrong_kernel_peer_fail_before_send_or_read(self):
        auth = authorized()
        with pair_sockets() as pair:
            sender, worker = pair
            with patch.object(e2_ipc.os, "geteuid", return_value=os.geteuid()):
                with self.assertRaises(e2_ipc.HandoffDenied):
                    e2_ipc.send_approved(sender, auth, worker_uid=os.geteuid())
            with patch.object(e2_ipc.os, "geteuid", return_value=0):
                with self.assertRaises(e2_ipc.HandoffDenied):
                    e2_ipc.send_approved(sender, auth, worker_uid=980)
            with patch.object(e2_ipc.os, "geteuid", return_value=1000):
                with patch.object(e2_ipc, "_kernel_peer", return_value=(123, 444, 444)):
                    with self.assertRaises(e2_ipc.HandoffDenied):
                        e2_ipc.send_approved(sender, auth, worker_uid=980)
                with patch.object(e2_ipc, "_kernel_peer", return_value=(123, 980, 980)):
                    for value in (0, True, 1000, "980"):
                        with self.subTest(value=value), self.assertRaises(e2_ipc.HandoffDenied):
                            e2_ipc.send_approved(sender, auth, worker_uid=value)
            worker.settimeout(0.01)
            with self.assertRaises((OSError, TimeoutError)):
                worker.recv(1)

    def test_stream_and_non_unix_sockets_denied(self):
        with pair_sockets(socket.SOCK_STREAM) as pair:
            with patch.object(e2_ipc.os, "geteuid", return_value=1000):
                with self.assertRaises(e2_ipc.HandoffDenied):
                    e2_ipc.send_approved(pair[0], authorized(), worker_uid=980)

    def test_client_rejects_changed_payload_recipient_hash_or_object(self):
        auth = authorized()
        changes = (
            replace(auth, payload=auth.payload + " "),
            replace(auth, sha256="0" * 64),
            replace(auth, recipient="other"),
            replace(auth, purpose="system.write"),
        )
        with pair_sockets() as pair:
            with (patch.object(e2_ipc.os, "geteuid", return_value=1000),
                  patch.object(e2_ipc, "_kernel_peer", return_value=(123, 980, 980))):
                for value in (None, {}, *changes):
                    with self.subTest(value=type(value)), self.assertRaises(e2_ipc.HandoffDenied):
                        e2_ipc.send_approved(pair[0], value, worker_uid=980)

    def test_worker_denies_malformed_oversized_and_smuggled_payloads(self):
        auth = authorized()
        good = packet(auth)
        messages = (
            b"NOT JSON", b"", b"{" + b"x" * e2_ipc.MAX_PACKET + b"}",
            good + b"x",
            good.replace(b'"schema":1', b'"schema":2'),
            good.replace(b'"schema":1', b'"schema":1,"schema":1'),
            good.replace(b'"recipient":"claude-analysis"', b'"recipient":"other"'),
            good.replace(b'"purpose":"readonly-diagnostics"', b'"purpose":"system.write"'),
            good.replace(b'"sha256":"', b'"sha256":"0'),
            good.replace(b'"payload":"', b'"payload":"EXTRA'),
            good.replace(b'"schema":1', b'"schema":true'),
            b'{"schema":1}',
        )
        for raw in messages:
            with self.subTest(length=len(raw)):
                with pair_sockets() as pair:
                    pair[0].send(raw)
                    with self.assertRaises(e2_ipc.HandoffDenied) as fail:
                        self.receive_as_worker(pair[1])
                    self.assertEqual(str(fail.exception), "HANDOFF: REFUSED")
                    self.assertNotIn(auth.payload, str(fail.exception))

    def test_worker_enforces_kernel_uid_even_with_valid_payload(self):
        auth = authorized()
        with pair_sockets() as pair:
            pair[0].send(packet(auth))
            with (patch.object(e2_ipc.os, "geteuid", return_value=980),
                  patch.object(e2_ipc, "_kernel_peer", return_value=(345, 999, 999))):
                with self.assertRaises(e2_ipc.HandoffDenied):
                    e2_ipc.receive_approved(pair[1], desktop_uid=1000)
            # A different, explicitly verified peer is accepted for the same
            # already queued packet; no hidden fallback or payload inspection.
            self.assertEqual(self.receive_as_worker(pair[1]), auth.payload)


if __name__ == "__main__":
    unittest.main()
