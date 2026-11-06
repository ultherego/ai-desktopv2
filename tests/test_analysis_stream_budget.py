"""Real bounded relay streams for larger analysis; account tunnels stay small."""
import contextlib
import socket
import socketserver
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from ai_desktop import analysis_child, analysis_sandbox, codex_runtime, codex_quota_runtime
from ai_desktop import e2_connect_proxy as proxy
from test_e2_connect_proxy import header, hello


class AnalysisStreamBudgetTests(unittest.TestCase):
    def relay_bytes(self, count, limit):
        client, accepted = socket.socketpair()
        upstream, fixture = socket.socketpair()
        errors, sent_errors = [], []
        with contextlib.ExitStack() as stack:
            for stream in (client, accepted, upstream, fixture):
                stack.enter_context(stream)
            client.settimeout(15)
            fixture.settimeout(15)

            def forward():
                try:
                    proxy.relay(accepted, upstream, seconds=15, limit=limit)
                except proxy.Denied as error:
                    errors.append(error)
                finally:
                    accepted.close()
                    upstream.close()

            def send():
                remaining, block = count, b"x" * 65536
                try:
                    while remaining:
                        part = block[:min(remaining, len(block))]
                        client.sendall(part)
                        remaining -= len(part)
                    client.shutdown(socket.SHUT_WR)
                except OSError as error:
                    sent_errors.append(type(error))

            fixture.shutdown(socket.SHUT_WR)
            forwarding = threading.Thread(target=forward)
            sending = threading.Thread(target=send)
            forwarding.start()
            sending.start()
            received = 0
            while block := fixture.recv(65536):
                received += len(block)
            sending.join(16)
            forwarding.join(16)
            self.assertFalse(sending.is_alive())
            self.assertFalse(forwarding.is_alive())
        return received, errors, sent_errors

    def test_analysis_stream_carries_more_than_four_mib(self):
        count = proxy.STREAM_LIMIT + 65536
        received, errors, sent_errors = self.relay_bytes(count, proxy.ANALYSIS_STREAM_LIMIT)
        self.assertEqual(received, count)
        self.assertEqual(errors, [])
        self.assertEqual(sent_errors, [])

    def test_account_and_analysis_streams_both_stop_at_their_compiled_ceiling(self):
        for limit in (proxy.STREAM_LIMIT, proxy.ANALYSIS_STREAM_LIMIT):
            with self.subTest(limit=limit):
                received, errors, unused = self.relay_bytes(limit + 1, limit)
                self.assertLessEqual(received, limit)
                self.assertEqual(len(errors), 1)

    def test_only_compiled_stream_budgets_are_accepted(self):
        with patch.object(socketserver.UnixStreamServer, "__init__") as start:
            for invalid in (True, "4194304", 0, proxy.STREAM_LIMIT + 1, proxy.ANALYSIS_STREAM_LIMIT + 1):
                with self.subTest(invalid=invalid), self.assertRaises(proxy.Denied):
                    proxy.ConnectProxy("unused", dial=lambda host: None, stream_limit=invalid)
            start.assert_not_called()

    def test_both_handlers_forward_the_selected_budget_to_the_relay(self):
        for limit in (proxy.STREAM_LIMIT, proxy.ANALYSIS_STREAM_LIMIT):
            with self.subTest(limit=limit), contextlib.ExitStack() as stack:
                client, accepted = socket.socketpair()
                upstream, fixture = socket.socketpair()
                for stream in (client, accepted, upstream, fixture):
                    stack.enter_context(stream)
                client.sendall(header() + hello())
                server = SimpleNamespace(dial=lambda host: upstream, stream_seconds=120, stream_limit=limit)
                with patch.object(proxy, "relay") as relay:
                    proxy._Handler(accepted, None, server)
                relay.assert_called_once_with(accepted, upstream, seconds=120, limit=limit)
            handler = object.__new__(analysis_child._BridgeHandler)
            handler.request = object()
            handler.server = SimpleNamespace(stream_seconds=120, stream_limit=limit)
            outer = MagicMock()
            with patch.object(analysis_child.socket, "socket") as socket_factory, \
                    patch.object(analysis_child, "relay") as relay:
                socket_factory.return_value.__enter__.return_value = outer
                handler.handle()
            outer.connect.assert_called_once_with("/run/egress.sock")
            relay.assert_called_once_with(handler.request, outer, seconds=120, limit=limit)
        with patch.object(socketserver.TCPServer, "__init__") as start:
            with self.assertRaises(proxy.Denied):
                analysis_child._Bridge(stream_limit=proxy.ANALYSIS_STREAM_LIMIT + 1)
            start.assert_not_called()

    def test_analysis_proxies_opt_in_and_account_proxy_and_bridge_keep_four_mib(self):
        with patch.object(socketserver.UnixStreamServer, "__init__"):
            claude = analysis_sandbox._Proxy("unused", dial=lambda host: None)
            codex = codex_runtime.OpenAIProxy("unused")
            account = codex_quota_runtime._AccountProxy("unused", dial=lambda host: None,
                                                       hosts=proxy.CODEX_ACCOUNT_HOSTS)
        self.assertEqual(claude.stream_limit, proxy.ANALYSIS_STREAM_LIMIT)
        self.assertEqual(codex.stream_limit, proxy.ANALYSIS_STREAM_LIMIT)
        self.assertEqual(account.stream_limit, proxy.STREAM_LIMIT)
        self.assertEqual(account.hosts, proxy.CODEX_ACCOUNT_HOSTS)
        with patch.object(socketserver.TCPServer, "__init__"):
            self.assertEqual(analysis_child._Bridge().stream_limit, proxy.STREAM_LIMIT)
            self.assertEqual(analysis_child._Bridge(stream_limit=proxy.ANALYSIS_STREAM_LIMIT).stream_limit,
                             proxy.ANALYSIS_STREAM_LIMIT)


if __name__ == "__main__":
    unittest.main()
