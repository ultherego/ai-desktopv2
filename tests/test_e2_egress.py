"""E2 outbound transport perimeter tests: all sockets, TLS and DNS are fake."""
import contextlib
import io
import os
import socket
import ssl
import unittest
from unittest.mock import MagicMock, patch

from ai_desktop import e2_egress as eg


def address(ip="1.1.1.1", family=socket.AF_INET, port=443):
    sockaddr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


class FakeResponse:
    def __init__(self, status=200, content=b""):
        self.status = status
        self.content = content
    def read(self, count):
        assert count == eg.MAX_REPLY_BYTES + 1
        return self.content


class FakeConnection:
    def __init__(self, status=200, content=b""):
        self.response = FakeResponse(status, content)
        self.calls = []
        self.closed = False
    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
    def getresponse(self):
        return self.response
    def close(self):
        self.closed = True


class E2PinnedEgressTests(unittest.TestCase):
    def test_no_network_unless_exact_boolean_true(self):
        for state in (False, None, "true", 1, 0, {}, []):
            with self.subTest(state=state), self.assertRaises(eg.EgressDenied):
                eg.probe(allow_network=state, connection_factory=lambda *a, **k:
                         self.fail("must not construct connector"))

    def test_dns_allows_only_global_public_ips_and_pins_results(self):
        for ip, fam in (("1.1.1.1", socket.AF_INET),
                        ("2606:4700:4700::1111", socket.AF_INET6)):
            result = eg._permitted_addresses(
                eg.HOST, resolver=lambda *a, **k: [address(ip, fam)])
            self.assertEqual(result, [address(ip, fam)])
        for ip in ("127.0.0.1", "10.2.3.4", "192.168.1.1",
                   "169.254.169.254", "0.0.0.0", "224.0.0.1",
                   "::1", "fc00::2", "fe80::1", "ff02::1",
                   "::ffff:127.0.0.1", "2001:db8::1"):
            with self.subTest(ip=ip), self.assertRaises(eg.EgressDenied):
                eg._permitted_addresses(
                    eg.HOST, resolver=lambda *a, **k: [address(ip)])
        with self.assertRaises(eg.EgressDenied):
            eg._permitted_addresses("localhost",
                                    resolver=lambda *a, **k: self.fail("no DNS"))
        with self.assertRaises(eg.EgressDenied):
            eg._permitted_addresses("api.anthropic.com.evil.test",
                                    resolver=lambda *a, **k: self.fail("no DNS"))

    def test_dns_fails_closed_if_any_answer_is_private(self):
        for answers in (
            [], [address(), address("127.0.0.1")],
            [address(port=8443)], [address(family=socket.AF_UNIX)],
            [address()] * 33,
        ):
            with self.subTest(size=len(answers)), self.assertRaises(eg.EgressDenied):
                eg._permitted_addresses(eg.HOST, resolver=lambda *a, **k: answers)

    def test_dns_failure_and_bad_metadata_are_sanitized(self):
        for resolver in (
            lambda *a, **k: (_ for _ in ()).throw(OSError("SECRET_DNS_ERROR")),
            lambda *a, **k: [(socket.AF_INET, socket.SOCK_DGRAM,
                               socket.IPPROTO_UDP, "", ("1.1.1.1", 443))],
            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM,
                               socket.IPPROTO_TCP, "", ("1.1.1.1", 443, 12))],
        ):
            with self.assertRaises(eg.EgressDenied) as refused:
                eg._permitted_addresses(eg.HOST, resolver=resolver)
            self.assertEqual(str(refused.exception), "EGRESS: REFUSED")

    def test_verified_tls_uses_pinned_sockaddr_and_hostname(self):
        raw = MagicMock()
        context = MagicMock()
        context.wrap_socket.return_value = "TLS-VERIFIED"
        with patch.object(eg, "_permitted_addresses", return_value=[
            address("1.1.1.1")]) as dns, patch.object(eg.socket, "socket",
            return_value=raw) as create:
            conn = eg._PinnedTLS(eg.HOST, context=context)
            conn.connect()
        dns.assert_called_once_with(eg.HOST)
        create.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM,
                                       socket.IPPROTO_TCP)
        raw.settimeout.assert_called_once_with(eg.TIMEOUT_SECONDS)
        raw.connect.assert_called_once_with(("1.1.1.1", 443))
        context.wrap_socket.assert_called_once_with(
            raw, server_hostname=eg.HOST)
        self.assertEqual(conn.sock, "TLS-VERIFIED")

    def test_tls_failure_closes_raw_socket_and_redacts_error(self):
        raw = MagicMock()
        context = MagicMock()
        context.wrap_socket.side_effect = ssl.SSLError("PRIVATE_TLS_ERROR")
        with patch.object(eg, "_permitted_addresses", return_value=[
            address("1.1.1.1")]), patch.object(eg.socket, "socket",
            return_value=raw):
            conn = eg._PinnedTLS(eg.HOST, context=context)
            with self.assertRaises(eg.EgressDenied) as refused:
                conn.connect()
        self.assertEqual(str(refused.exception), "EGRESS: REFUSED")
        raw.close.assert_called_once()

    def test_probe_fixed_head_request_no_credentials_no_redirect(self):
        instance = FakeConnection(status=401)
        created = []
        def factory(*args, **kwargs):
            created.append((args, kwargs))
            return instance
        with patch.object(eg.os, "geteuid", return_value=1000):
            result = eg.probe(allow_network=True, connection_factory=factory)
        self.assertEqual(result, {"tls": "VERIFIED", "endpoint": eg.HOST,
                                  "authentication": "NOT_TESTED"})
        self.assertEqual(created[0][0], (eg.HOST,))
        self.assertEqual(created[0][1]["port"], 443)
        self.assertIsInstance(created[0][1]["context"], ssl.SSLContext)
        args, kwargs = instance.calls[0]
        self.assertEqual(args, ("HEAD", "/"))
        self.assertEqual(kwargs["headers"]["Host"], eg.HOST)
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertNotIn("x-api-key", kwargs["headers"])
        self.assertTrue(instance.closed)

    def test_bad_status_redirect_or_body_denied_without_second_request(self):
        for status, content in ((302,b""), (307,b""), (500,b""),
                                (200,b"x"*(eg.MAX_REPLY_BYTES+3))):
            c = FakeConnection(status, content)
            with self.subTest(status=status), patch.object(
                eg.os, "geteuid", return_value=1000):
                with self.assertRaises(eg.EgressDenied):
                    eg.probe(allow_network=True,
                             connection_factory=lambda *a, **k: c)
            self.assertEqual(len(c.calls), 1)
            self.assertTrue(c.closed)

    def test_root_and_network_error_fail_closed(self):
        with patch.object(eg.os, "geteuid", return_value=0):
            with self.assertRaises(eg.EgressDenied):
                eg.probe(allow_network=True, connection_factory=lambda *a, **k:
                         self.fail("root cannot connect"))
        c = FakeConnection()
        c.getresponse = lambda: (_ for _ in ()).throw(OSError("TOKEN_VALUE"))
        with patch.object(eg.os, "geteuid", return_value=1000):
            with self.assertRaises(eg.EgressDenied) as refused:
                eg.probe(allow_network=True, connection_factory=lambda *a, **k: c)
        self.assertEqual(str(refused.exception), "EGRESS: REFUSED")
        self.assertIsNone(refused.exception.__cause__)
        self.assertTrue(c.closed)

    def test_public_cli_is_offline_only(self):
        for args in ([], ["probe"], ["--allow-network"], ["send"],
                     ["status", "https://api.anthropic.com"]):
            with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(eg.main(args), 2)
                self.assertEqual(out.getvalue(), "EGRESS: REFUSED\n")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(eg.main(["status"]), 0)
        self.assertIn("OFFLINE", out.getvalue())


if __name__ == "__main__":
    unittest.main()
