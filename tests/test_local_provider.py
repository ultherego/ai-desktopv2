"""A real loopback fixture serves synthetic text; no model, account, external network or native client."""
from contextlib import ExitStack, contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import analysis_contract as contract, analysis_service, ask_cli, assistant_flow
from ai_desktop import agent_profiles, execution, local_provider, management_cli, permissions, doctor


def selection(port=8080, model="default"):
    return {"provider": "local", "model": model, "effort": "default", "instructions": "Synthetic role",
            "profile": "default", "access": "read-only", "local_port": port}


def reply(text="Synthetic answer"):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


@contextmanager
def server(respond):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, dict(self.headers), None))
            respond(self)

        def do_POST(self):
            seen.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            respond(self)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
        worker = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        try:
            yield http.server_port, seen
        finally:
            http.shutdown()
            worker.join()


def response(document, code=200, **headers):
    def write(handler):
        raw = json.dumps(document).encode()
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        for key, value in headers.items():
            handler.send_header(key, str(value))
        handler.end_headers()
        handler.wfile.write(raw)
    return write


class LocalProviderTests(unittest.TestCase):
    def setup_transport(self, stack, port):
        policy = Mock(local_port=port)
        policy.provider_allowed.side_effect = lambda name: name == "local"
        stack.enter_context(patch.object(local_provider, "load_policy", return_value=policy))
        stack.enter_context(patch("os.getuid", return_value=1000))
        stack.enter_context(patch("os.geteuid", return_value=1000))
        stack.enter_context(patch("os.isatty", return_value=True))
        stack.enter_context(patch("sys.stdin.isatty", return_value=True))
        stack.enter_context(patch("sys.stdout.isatty", return_value=True))
        return policy

    def send(self, port):
        with execution.using(selection(port)):
            gate = contract.AnalysisConsent(None, question="Synthetic question?", context=[])
            approval = gate.authorize(None, question="Synthetic question?", context=[], nonce=gate.nonce, sha256=gate.sha256)
            return analysis_service.send_once(approval)

    def test_approved_ask_uses_only_the_local_route_and_canonical_data(self):
        with server(response(reply())) as (port, seen), ExitStack() as stack:
            stack.enter_context(redirect_stdout(io.StringIO()))
            policy = self.setup_transport(stack, port)
            stack.enter_context(patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1",
                                                        "HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}))
            for target in ("ai_desktop.e2_account.account_ready", "ai_desktop.analysis_service.run_client",
                           "ai_desktop.analysis_session.authenticate", "subprocess.Popen"):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            stack.enter_context(execution.using(selection(port, "Qwen-test-Q4")))
            prompts = []
            answer = ask_cli.request("Synthetic question?", SimpleNamespace(details=False, format="plain"),
                                     lambda prompt: prompts.append(prompt) or "y", context=[], check=lambda: None)
            self.assertEqual(answer, "Synthetic answer")
            self.assertEqual(len(prompts), 1)
            self.assertIn(f"127.0.0.1:{port}", prompts[0])
            self.assertGreaterEqual(policy.recheck.call_count, 2)
            self.assertEqual(len(seen), 1)
            path, headers, body = seen[0]
            self.assertEqual(path, "/v1/chat/completions")
            self.assertNotIn("Authorization", headers)
            self.assertEqual(set(body), {"model", "messages", "stream"})
            self.assertEqual(body["model"], "Qwen-test-Q4")
            self.assertIn("Synthetic role", body["messages"][0]["content"])
            self.assertEqual(json.loads(body["messages"][1]["content"]), {
                "mode": "question", "question": "Synthetic question?", "context": []})

    def test_large_ascii_escaped_unicode_workspace_response_keeps_semantic_limit(self):
        text = "ż" * (contract.MAX_PROPOSAL_TEXT // 2)
        self.assertEqual(len(text.encode()), contract.MAX_PROPOSAL_TEXT)
        self.assertGreater(len(json.dumps(reply(text)).encode()), contract.MAX_RESPONSE_BYTES)
        task = {"instruction": "Replace selected text", "files": [
            {"path": "synthetic.txt", "content": "original", "writable": True}]}
        with server(response(reply(text))) as (port, seen), ExitStack() as stack:
            self.setup_transport(stack, port)
            selected = dict(selection(port), access="changes")
            with execution.using(selected):
                gate = contract.AnalysisConsent(None, task=task)
                approved = gate.authorize(None, task=task, nonce=gate.nonce, sha256=gate.sha256)
                parsed, _ = contract.parse_request(approved.payload.encode())
                self.assertEqual(local_provider.complete(json.loads(parsed), approved), text)
            self.assertEqual(len(seen), 1)

    def test_settings_to_normal_ask_planner_and_offline_doctor(self):
        plan = json.dumps({"action": "answer", "text": "Synthetic planner answer"})
        with server(response(reply(plan))) as (port, seen), tempfile.TemporaryDirectory(dir=Path.home()) as folder:
            store = agent_profiles.Store(Path(folder))
            directory = Path(folder) / ".config/ai-desktop"
            directory.mkdir(parents=True, mode=0o700)
            path = directory / "permissions.toml"
            path.write_text(f"version=2\n[files]\nread=[]\ndeny=[]\n[network]\nlocal=true\n[local]\nport={port}\n")
            path.chmod(0o600)
            with ExitStack() as stack:
                output = stack.enter_context(redirect_stdout(io.StringIO()))
                stack.enter_context(patch.object(permissions, "_identity", return_value=(os.getuid(), folder)))
                stack.enter_context(patch.object(agent_profiles, "Store", return_value=store))
                stack.enter_context(patch("os.getuid", return_value=1000))
                stack.enter_context(patch("os.geteuid", return_value=1000))
                stack.enter_context(patch("os.isatty", return_value=True))
                stack.enter_context(patch("sys.stdin.isatty", return_value=True))
                stack.enter_context(patch("sys.stdout.isatty", return_value=True))
                stack.enter_context(patch.object(doctor, "inspect", side_effect=AssertionError("cloud prerequisites")))
                stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("native client")))
                stack.enter_context(patch("ai_desktop.analysis_session.authenticate", side_effect=AssertionError("sudo")))
                self.assertEqual(management_cli.main(["set", "--provider", "local", "--model", "Synthetic-Q4"],
                                                      store=store, provider_neutral=True), 0)
                self.assertEqual(management_cli.main(["set", "--provider", "codex", "--model", "Synthetic-Q4"],
                                                      store=store, provider_neutral=True), 2)
                self.assertEqual(store.load().config.selected_provider, "local")
                self.assertEqual(doctor.main(["--v"]), 0)
                self.assertIn(f"127.0.0.1:{port}", output.getvalue())
                self.assertEqual(seen, [])
                prompts = []
                self.assertEqual(ask_cli.main(["--question", "Synthetic question?", "--format", "plain"],
                                              reader=lambda prompt: prompts.append(prompt) or "y"), 0, output.getvalue())
                self.assertEqual(len(prompts), 1)
                self.assertIn("Synthetic planner answer", output.getvalue())
                self.assertEqual(len(seen), 1)
                self.assertEqual(seen[0][2]["model"], "Synthetic-Q4")
                data = json.loads(seen[0][2]["messages"][1]["content"])
                self.assertEqual(data["mode"], "assistant")
                self.assertNotIn("web", data["access"])

    def test_changed_port_revocation_and_unsupported_web_refuse_before_disclosure(self):
        with server(response(reply())) as (port, seen), ExitStack() as stack:
            policy = self.setup_transport(stack, port)
            policy.local_port = 1024 if port != 1024 else 1025
            with self.assertRaises(contract.AnalysisDenied) as error:
                self.send(port)
            self.assertEqual(error.exception.reason, "authorization")
            policy.local_port = port
            policy.recheck.side_effect = (None, ValueError("policy-changed"))
            with self.assertRaises(contract.AnalysisDenied) as error:
                self.send(port)
            self.assertEqual(error.exception.reason, "authorization")
            with execution.using(selection(port)), self.assertRaises(ValueError):
                contract.application_input(None, "Synthetic question", web=True)
            self.assertEqual(seen, [])

    def test_refuses_redirection_headers_tools_and_incomplete_results(self):
        bad = reply()
        bad["choices"][0]["message"]["tool_calls"] = [{"id": "dangerous"}]
        for responder, reason in ((response(reply(), 307, Location="http://127.0.0.1:1/unapproved"), "provider-unavailable"),
                                  (response(bad), "result"), (response(reply(), **{"Content-Length": 9000}), "result"),
                                  (response(reply(), **{"Content-Length": 99999999}), "output-limit")):
            with self.subTest(reason=reason), server(responder) as (port, seen), ExitStack() as stack:
                self.setup_transport(stack, port)
                with self.assertRaises(contract.AnalysisDenied) as error:
                    self.send(port)
                self.assertEqual(error.exception.reason, reason)
                self.assertEqual(len(seen), 1)
                self.assertNotIn("model", seen[0][2])

    def test_explicit_models_and_status_use_only_bounded_metadata(self):
        from ai_desktop import cli
        for document, expected in (({"object": "list", "data": [{"id": "Qwen-local"}]}, 0),
                                   ({"object": "list", "data": [{"id": "unsafe\x1b[2J"}]}, 2),
                                   ({"object": "list", "data": [{"id": "Qwen-local"}] * 129}, 2)):
            with self.subTest(expected=expected), server(response(document)) as (port, seen), ExitStack() as stack:
                output = stack.enter_context(redirect_stdout(io.StringIO()))
                self.setup_transport(stack, port)
                for action in ("status", "models"):
                    self.assertEqual(cli.main(["local", action]), expected)
                self.assertEqual([(path, body) for path, unused, body in seen], [("/v1/models", None)] * 2)
                self.assertNotIn("unsafe", output.getvalue())
                if not expected:
                    self.assertIn("Qwen-local", output.getvalue())

    def test_slow_drip_has_a_single_wall_clock_deadline(self):
        def drip(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            for unused in range(100):
                try:
                    handler.wfile.write(b" ")
                except OSError:
                    break
                time.sleep(.02)

        with server(drip) as (port, unused), ExitStack() as stack:
            self.setup_transport(stack, port)
            stack.enter_context(patch.object(local_provider, "request_seconds", return_value=.2))
            start = time.monotonic()
            with self.assertRaises(contract.AnalysisDenied) as error:
                self.send(port)
            self.assertEqual(error.exception.reason, "timeout")
            self.assertLess(time.monotonic() - start, 1.5)

    def test_policy_is_opt_in_and_no_config_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as folder:
            directory = Path(folder) / ".config/ai-desktop"
            directory.mkdir(parents=True, mode=0o700)
            config = directory / "permissions.toml"
            with patch.object(permissions, "_identity", return_value=(os.getuid(), folder)), execution.using(selection()):
                snapshot = assistant_flow.capture_access()
                with self.assertRaises(assistant_flow.AccessDenied):
                    snapshot.require_claude()
                for raw in ('[local]\nport = 65536', '[local]\nport = true', '[local]\nhost = "localhost"'):
                    config.write_text("version = 2\n[files]\nread = []\ndeny = []\n[network]\nlocal = true\n" + raw)
                    config.chmod(0o600)
                    with self.assertRaises(ValueError):
                        permissions.load_policy()
                config.write_text("version = 2\n[files]\nread = []\ndeny = []\n[network]\nlocal = true\nweb = true\n[local]\nport = 19091\n")
                config.chmod(0o600)
                policy = permissions.load_policy()
                self.assertEqual(policy.local_port, 19091)
                self.assertTrue(policy.provider_allowed("local"))
                snapshot = assistant_flow.capture_access()
                self.assertNotIn("web", snapshot.access)
                with self.assertRaises(contract.AnalysisDenied):
                    local_provider.authorized_policy(8080)
