"""Offline adversarial CONNECT/TLS tests with real socket pairs, no DNS/uplink."""
import contextlib
import io
import socket
import ssl
import threading
import types
import unittest
from unittest.mock import Mock, patch

from ai_desktop import e2_connect_proxy as proxy


def header(authority="api.anthropic.com:443", extra=""):
    return ("CONNECT " + authority + " HTTP/1.1\r\nHost: " + authority + "\r\n" + extra + "\r\n").encode()


def hello(host="api.anthropic.com"):
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    context = ssl.create_default_context()
    if host is None:
        # A real no-SNI ClientHello must not enable hostname verification,
        # which Python 3.14 correctly rejects without server_hostname.
        context.check_hostname = False
    tls = context.wrap_bio(incoming, outgoing, server_side=False, server_hostname=host)
    try:
        tls.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return outgoing.read()


class ConnectPolicyTests(unittest.TestCase):
    def test_exact_host_port_only_no_body_credentials_or_header_smuggling(self):
        for host in sorted(proxy.HOSTS):
            self.assertEqual(proxy.connect_host(header(host + ":443")), host)
        invalid = [header(a) for a in (
            "127.0.0.1:443", "[::1]:443", "169.254.169.254:443", "api.anthropic.com:80",
            "api.anthropic.com.evil:443", "API.ANTHROPIC.COM:443", "api.anthropic.com.:443",
            "user@api.anthropic.com:443", "api.anthropic.com:0443", "claude.com:443")]
        invalid += [header(extra=e) for e in (
            "Host: api.anthropic.com:443\r\n", "Content-Length: 0\r\n", "Transfer-Encoding: chunked\r\n",
            "Authorization: SYNTHETIC\r\n", "Cookie: SYNTHETIC\r\n", "X-Forwarded-For: 127.0.0.1\r\n",
            " Host: api.anthropic.com:443\r\n", "Connection : close\r\n")]
        invalid += [header().replace(b"CONNECT", b"GET"), header().replace(b"\r\n", b"\n"),
                    header() + b"BODY", b"A" * (proxy.HEADER_LIMIT + 1)]
        for raw in invalid:
            with self.subTest(raw=raw[:80]), self.assertRaises(proxy.Denied):
                proxy.connect_host(raw)

    def test_public_resolution_rejects_entire_mixed_answer_and_encoded_local_addresses(self):
        def answer(ip):
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            addr = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
            return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", addr)
        good = answer("1.1.1.1")
        self.assertEqual(proxy.public_addresses("api.anthropic.com", lambda *a, **k: [good]), [good])
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "0.0.0.0", "100.64.0.1",
                   "224.0.0.1", "192.0.2.1", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1",
                   "2002:7f00:1::", "64:ff9b::7f00:1", "ff02::1"):
            with self.subTest(ip=ip), self.assertRaises(proxy.Denied):
                proxy.public_addresses("api.anthropic.com", lambda *a, **k: [good, answer(ip)])
        resolver = Mock()
        with self.assertRaises(proxy.Denied):
            proxy.public_addresses("forbidden.invalid", resolver)
        resolver.assert_not_called()

    def test_dns_pin_never_resolves_again_on_connect(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443))]
        with patch.object(proxy.socket, "getaddrinfo", return_value=answer) as dns, patch.object(proxy.socket, "socket") as sock:
            proxy.open_public("api.anthropic.com")
        dns.assert_called_once()
        sock.return_value.connect.assert_called_once_with(("1.1.1.1", 443))

    def test_real_clienthello_and_fragmented_records(self):
        original = hello()
        # Fragment both the TLS record and the transport independently.
        body = original[5:]
        fragmented = b"".join(b"\x16\x03\x03" + len(part).to_bytes(2, "big") + part
                              for part in (body[:10], body[10:]))
        for wire in (original, fragmented):
            with contextlib.ExitStack() as stack:
                left, right = socket.socketpair()
                stack.enter_context(left); stack.enter_context(right)
                right.sendall(wire)
                self.assertEqual(proxy.read_hello(left, "api.anthropic.com"), wire)

    def test_wrong_or_absent_sni_and_plaintext_fail_before_dial(self):
        for wire in (hello("evil.invalid"), hello(None), b"GET / HTTP/1.1\r\n\r\n", b"\x16\x03\x03\xff\xff"):
            with contextlib.ExitStack() as stack:
                left, right = socket.socketpair()
                stack.enter_context(left); stack.enter_context(right)
                right.sendall(wire); right.shutdown(socket.SHUT_WR)
                with self.assertRaises(proxy.Denied):
                    proxy.read_hello(left, "api.anthropic.com")

    def test_ech_and_duplicate_sni_are_denied(self):
        # Minimal well-formed TLS 1.2 ClientHello carrying controlled extensions.
        name = b"api.anthropic.com"
        names = b"\x00" + len(name).to_bytes(2, "big") + name
        value = len(names).to_bytes(2, "big") + names
        sni = b"\x00\x00" + len(value).to_bytes(2, "big") + value
        prefix = b"\x03\x03" + bytes(32) + b"\x00\x00\x02\x13\x01\x01\x00"
        for extra in (sni, b"\xfe\x0d\x00\x00"):
            extensions = sni + extra
            with self.assertRaises(proxy.Denied):
                proxy.check_hello(prefix + len(extensions).to_bytes(2, "big") + extensions, "api.anthropic.com")


class TransportTests(unittest.TestCase):
    def test_actual_proxy_relays_opaque_bytes_without_auth_parsing(self):
        client, accepted = socket.socketpair()
        upstream, fixture = socket.socketpair()
        with client, accepted, upstream, fixture:
            client.settimeout(2); fixture.settimeout(2)
            dial = Mock(return_value=upstream)
            server = types.SimpleNamespace(dial=dial, stream_seconds=proxy.TIME_LIMIT)
            thread = threading.Thread(target=proxy._Handler, args=(accepted, None, server))
            thread.start()
            client.sendall(header())
            response = bytearray()
            while not response.endswith(b"\r\n\r\n"):
                response.extend(client.recv(1))
            self.assertIn(b"200", response)
            wire = hello()
            client.sendall(wire)
            received = bytearray()
            while len(received) < len(wire):
                received.extend(fixture.recv(len(wire) - len(received)))
            self.assertEqual(received, wire)
            fixture.sendall(b"SYNTHETIC_OPAQUE_REPLY")
            self.assertEqual(client.recv(64), b"SYNTHETIC_OPAQUE_REPLY")
            client.shutdown(socket.SHUT_WR); fixture.shutdown(socket.SHUT_WR)
            thread.join(3)
            self.assertFalse(thread.is_alive())
            dial.assert_called_once_with("api.anthropic.com")

    def test_forbidden_authority_no_dial_no_sensitive_error_output(self):
        with contextlib.ExitStack() as stack:
            client, accepted = socket.socketpair()
            stack.enter_context(client); stack.enter_context(accepted)
            dial = Mock()
            client.sendall(header("SYNTHETIC_SECRET.invalid:443"))
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                proxy._Handler(accepted, None, types.SimpleNamespace(dial=dial))
            dial.assert_not_called()
            self.assertEqual(out.getvalue() + err.getvalue(), "")

    def test_stalled_or_excessive_tunnel_is_closed(self):
        for seconds, limit, payload in ((.02, 100, b""), (1, 2, b"EXCESS")):
            with contextlib.ExitStack() as stack:
                client, accepted = socket.socketpair(); upstream, fixture = socket.socketpair()
                for stream in (client, accepted, upstream, fixture):
                    stack.enter_context(stream)
                client.sendall(payload)
                with self.assertRaises(proxy.Denied):
                    proxy.relay(accepted, upstream, seconds=seconds, limit=limit)


if __name__ == "__main__":
    unittest.main()
