"""Real bounded packet framing and hostile records; no host/model access."""
import hashlib
import socket
import threading
import unittest
from unittest.mock import Mock

from ai_desktop import analysis_framing as wire
from ai_desktop.analysis_contract import AnalysisDenied, MAX_PACKET_LARGE as MAX_PACKET, MAX_RESPONSE_BYTES


def records(body, kind="request"):
    tag = b"Q" if kind == "request" else b"R"
    raw = [wire.HEADER.pack(wire.MAGIC, tag, len(body), hashlib.sha256(body).digest())]
    return raw + [body[index:index + wire.CHUNK_BYTES] for index in range(0, len(body), wire.CHUNK_BYTES)]


def incoming(packets):
    channel = Mock()
    channel.gettimeout.return_value = 3
    channel.recvmsg.side_effect = [(raw, [], 0, None) for raw in packets]
    return channel


class AnalysisFramingTests(unittest.TestCase):
    def test_largest_request_and_response_cross_real_small_packet_sockets_exactly(self):
        for kind, limit in (("request", MAX_PACKET), ("response", MAX_RESPONSE_BYTES)):
            with self.subTest(kind=kind):
                sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                body, failures = bytes(range(256)) * (limit // 256), []
                def transmit():
                    try:
                        wire.send_message(sender, body, kind=kind)
                    except BaseException as exc:
                        failures.append(exc)
                with sender, receiver:
                    # A logical message is larger than either socket buffer.
                    sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, wire.CHUNK_BYTES)
                    self.assertLess(sender.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF), limit)
                    thread = threading.Thread(target=transmit)
                    thread.start()
                    try:
                        self.assertEqual(wire.receive_message(receiver, kind=kind), body)
                    finally:
                        thread.join(timeout=6)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(failures, [])

    def test_real_peer_close_mid_message_and_oversized_record_are_refused(self):
        for payload in (b"", b"xx"):
            with self.subTest(payload=payload):
                sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                with sender, receiver:
                    sender.send(records(b"x")[0])
                    if payload:
                        sender.send(payload)
                    sender.shutdown(socket.SHUT_WR)
                    with self.assertRaises(AnalysisDenied):
                        wire.receive_message(receiver, kind="request")

    def test_exact_record_boundaries_and_digest_are_verified(self):
        for size in (1, wire.CHUNK_BYTES, wire.CHUNK_BYTES + 1, MAX_PACKET):
            with self.subTest(size=size):
                body = bytes(range(256)) * (size // 256) + b"x" * (size % 256)
                channel = incoming(records(body))
                self.assertEqual(wire.receive_message(channel, kind="request"), body)
                self.assertEqual(channel.recvmsg.call_count, 1 + (size + wire.CHUNK_BYTES - 1) // wire.CHUNK_BYTES)
                channel.settimeout.assert_called_with(3)

    def test_wrong_version_direction_length_or_header_refuses_before_body_read(self):
        digest = hashlib.sha256(b"x").digest()
        headers = [b"ANALYSIS1 READY", b"{" * wire.HEADER.size,
                   wire.HEADER.pack(b"AIDFRM1\0", b"Q", 1, digest),
                   wire.HEADER.pack(wire.MAGIC, b"R", 1, digest),
                   wire.HEADER.pack(wire.MAGIC, b"Q", 0, digest),
                   wire.HEADER.pack(wire.MAGIC, b"Q", MAX_PACKET + 1, digest)]
        for header in headers:
            with self.subTest(header_size=len(header)):
                channel = incoming([header])
                with self.assertRaises(AnalysisDenied):
                    wire.receive_message(channel, kind="request")
                self.assertEqual(channel.recvmsg.call_count, 1)

    def test_corruption_short_extra_duplicate_and_out_of_order_records_refuse(self):
        valid = records(b"a" * wire.CHUNK_BYTES + b"b" * wire.CHUNK_BYTES + b"c")
        cases = [valid[:1] + [b""], valid[:1] + [valid[1][:-1]],
                 valid[:1] + [valid[1] + b"x"], valid[:1] + [valid[2], valid[1], valid[3]],
                 valid[:1] + [valid[1], valid[1], valid[3]],
                 valid[:-1] + [b"d"], valid[:-1] + [valid[-1] + b"x"]]
        for index, packets in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(AnalysisDenied):
                wire.receive_message(incoming(packets), kind="request")

    def test_ancillary_and_truncated_records_never_become_payload(self):
        packets = records(b"x")
        for at in (0, 1):
            for ancillary, flags in (([], socket.MSG_TRUNC), ([], socket.MSG_CTRUNC), (["fd"], 0)):
                with self.subTest(at=at, flags=flags):
                    channel = incoming(packets)
                    events = [(raw, [], 0, None) for raw in packets]
                    events[at] = (packets[at], ancillary, flags, None)
                    channel.recvmsg.side_effect = events
                    with self.assertRaises(AnalysisDenied):
                        wire.receive_message(channel, kind="request")

    def test_deadline_is_absolute_across_records_and_cannot_be_renewed(self):
        packets = iter(records(b"x"))
        now = [0.0]
        channel = Mock()
        channel.gettimeout.return_value = None
        def receive(*_):
            now[0] += .6
            return next(packets), [], 0, None
        channel.recvmsg.side_effect = receive
        with self.assertRaisesRegex(AnalysisDenied, "reason=timeout"):
            wire.receive_message(channel, kind="request", timeout=1, clock=lambda: now[0])
        self.assertEqual(channel.recvmsg.call_count, 2)
        channel.settimeout.assert_called_with(None)
        now[0] = 0
        channel.send.side_effect = lambda raw: (now.__setitem__(0, now[0] + .6) or len(raw))
        with self.assertRaisesRegex(AnalysisDenied, "reason=timeout"):
            wire.send_message(channel, b"x", kind="request", timeout=1, clock=lambda: now[0])

    def test_invalid_send_and_partial_send_never_succeed(self):
        for body in (b"", "not bytes", b"x" * (MAX_PACKET + 1)):
            channel = Mock()
            with self.assertRaises(AnalysisDenied):
                wire.send_message(channel, body, kind="request")
            channel.send.assert_not_called()
        channel = Mock()
        channel.send.side_effect = lambda raw: len(raw) - 1
        with self.assertRaises(AnalysisDenied):
            wire.send_message(channel, b"x", kind="request")
        self.assertEqual(channel.send.call_count, 1)

    def test_peer_close_and_socket_timeout_are_finite_redacted_failures(self):
        for error, reason in ((socket.timeout("PRIVATE"), "timeout"), (OSError("PRIVATE"), "endpoint")):
            channel = Mock()
            channel.recvmsg.side_effect = error
            with self.assertRaises(AnalysisDenied) as caught:
                wire.receive_message(channel, kind="request")
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn("PRIVATE", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
