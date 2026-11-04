"""E2 client compatibility contracts; no Claude, provider traffic or live sockets."""
import contextlib
from email.message import Message
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from ai_desktop import e2_client_compat as compat
from ai_desktop import e2_compat_fixture as fixture


def headers(**changes):
    result = Message()
    result['Authorization'] = 'Bearer ' + fixture.TOKEN
    for key, value in changes.items():
        result[key] = value
    return result


def body(**changes):
    return {"model": fixture.MODEL, "messages": [{"role": "user", "content": fixture.PROMPT}],
            "tools": [], "stream": True, **changes}


class ClientCompatibilityBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="aid-client-contract-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.binary = self.root / "claude"
        self.binary.write_bytes(b"\x7fELFSYNTHETIC NOT EXECUTABLE CODE")
        self.binary.chmod(0o700)
        self.source = self.root / "package"
        self.source.mkdir()
        for name in compat.FILES:
            (self.source / name).write_text("# SYNTHETIC\n")

    def build(self):
        return compat.build_command(self.binary, self.source, "/home/synthetic", "net:[100]", "reply")

    def proof(self, scenario="version"):
        return {"schema": 1, "version": "2.1.312" if scenario == "version" else None,
                "scenario": scenario, "boundary": "PASS", "result": "PASS",
                "real_auth": False, "upstream": False}

    def test_no_execution_without_exact_opt_in_or_as_root(self):
        for allowed in (False, None, 1, "true", [], {}):
            with self.subTest(allowed=allowed), patch.object(compat, "bounded_run") as run:
                with self.assertRaises(compat.CompatibilityDenied):
                    compat.execute(allow_claude_compat=allowed)
                run.assert_not_called()
        with patch.object(compat.os, "geteuid", return_value=0), patch.object(compat, "claude_binary") as locate:
            with self.assertRaises(compat.CompatibilityDenied):
                compat.execute(allow_claude_compat=True)
            locate.assert_not_called()

    def test_command_has_all_isolation_and_only_exact_readonly_bindings(self):
        command = self.build()
        self.assertEqual(command[0], "/usr/bin/bwrap")
        for flag in ("--unshare-user", "--unshare-pid", "--unshare-net", "--unshare-ipc",
                     "--unshare-uts", "--clearenv", "--die-with-parent", "--new-session"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--cap-drop")+1], "ALL")
        self.assertNotIn("--bind", command)
        mounts = [command[i+1:i+3] for i, value in enumerate(command) if value == "--ro-bind"]
        self.assertEqual(mounts, [["/usr", "/usr"], [str(self.binary), "/opt/claude"]] +
                         [[str(self.source / name), "/opt/ai_desktop/" + name] for name in compat.FILES])
        self.assertIn("--tmpfs", command)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", command)
        self.assertNotIn("CLAUDE_CONFIG_DIR", command)
        self.assertNotIn("--share-net", command)

    def test_mounted_files_import_production_classifier_without_checkout_dependencies(self):
        isolated = self.root / "isolated"
        package = isolated / "ai_desktop"
        package.mkdir(parents=True)
        for name in compat.FILES:
            shutil.copyfile(compat.SOURCE / name, package / name)
        payload = json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401}).encode()
        script = ("import sys; sys.path.insert(0, sys.argv[1]); "
                  "from ai_desktop.e2_compat_fixture import classify_failure; "
                  "assert classify_failure(1, " + repr(payload) + ") == 'auth-required'; "
                  "print('IMPORT: PASS')")
        done = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(isolated)],
                              cwd=isolated, env={"PATH": "/usr/bin"}, capture_output=True, timeout=5, check=False)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"IMPORT: PASS\n", b""))

    def test_all_failures_have_distinct_fresh_namespace_recovery_phases(self):
        expected = (("deny", "recovery-deny"), ("rate-limit", "recovery-rate"),
                    ("unavailable", "recovery-unavailable"), ("disconnect", "recovery-disconnect"),
                    ("timeout", "recovery-timeout"))
        self.assertEqual(fixture.SCENARIOS, ("version", "reply") + tuple(item for pair in expected for item in pair))
        for failure, recovery in expected:
            failed_command = compat.build_command(self.binary, self.source, "/home/synthetic", "net:[100]", failure)
            recovered_command = compat.build_command(self.binary, self.source, "/home/synthetic", "net:[100]", recovery)
            self.assertIn("--unshare-pid", recovered_command)
            self.assertIn("--unshare-net", recovered_command)
            self.assertNotEqual(failed_command[-1], recovered_command[-1])

    def test_failure_stops_matrix_before_any_later_phase_can_mask_it(self):
        for index, scenario in enumerate(fixture.SCENARIOS[1:], 1):
            earlier = [(0, json.dumps(self.proof(s)).encode(), b"") for s in fixture.SCENARIOS[:index]]
            failure = fixture.failure_metadata(fixture.FixtureDenied("client-denial"), scenario)
            with self.subTest(scenario=scenario), patch.object(compat.os, "geteuid", return_value=1000), \
                    patch.object(compat.shutil, "which", return_value="/usr/bin/bwrap"), \
                    patch.object(compat, "claude_binary", return_value=self.binary), patch.object(compat, "SOURCE", self.source), \
                    patch.object(compat, "bounded_run", side_effect=earlier + [(1, json.dumps(failure).encode(), b"PRIVATE")]) as run:
                with self.assertRaises(compat.CompatibilityDenied) as denied:
                    compat.execute(allow_claude_compat=True)
                self.assertEqual(denied.exception.phase, scenario)
                self.assertEqual(run.call_count, index + 1)

    def test_missing_script_symlink_binary_or_non_native_binary_refused(self):
        target = self.source / compat.FILES[-1]
        target.unlink()
        with self.assertRaises(compat.CompatibilityDenied):
            self.build()
        target.symlink_to(self.binary)
        with self.assertRaises(compat.CompatibilityDenied):
            self.build()
        target.unlink()
        target.write_text("# SYNTHETIC\n")
        self.binary.write_text("#!/bin/sh\nexit 0\n")
        with self.assertRaises(compat.CompatibilityDenied):
            self.build()
        self.binary.unlink()
        self.binary.symlink_to(target)
        with self.assertRaises(compat.CompatibilityDenied):
            self.build()

    def test_exact_proof_and_sanitized_runner_environment(self):
        with patch.object(compat.os, "geteuid", return_value=1000), \
             patch.object(compat.shutil, "which", return_value="/usr/bin/bwrap"), \
             patch.object(compat, "claude_binary", return_value=self.binary), \
             patch.object(compat, "SOURCE", self.source), \
             patch.object(compat, "bounded_run", side_effect=[(0, json.dumps(self.proof(s)).encode(), b"") for s in fixture.SCENARIOS]) as run:
            self.assertEqual(compat.execute(allow_claude_compat=True), {
                "schema": 1, "version": "2.1.312", "boundary": "PASS", "reply": "PASS", "denial": "PASS",
                "rate-limit": "PASS", "unavailable": "PASS", "disconnect": "PASS", "timeout-stop": "PASS",
                "recovery": "PASS", "real_auth": False, "upstream": False})
            self.assertEqual(run.call_count, len(fixture.SCENARIOS))
            self.assertEqual([c.args[0][-1] for c in run.call_args_list], list(fixture.SCENARIOS))
            self.assertEqual(run.call_args.kwargs, {"env": {"PATH": "/usr/bin"}, "timeout": 55, "limit": 4096})

    def test_forged_duplicate_or_failure_proof_refused_without_raw_output(self):
        replies = [
            (0, json.dumps({**self.proof(), "upstream": True}).encode(), b""),
            (0, json.dumps({**self.proof(), "real_auth": 0}).encode(), b""),
            (0, json.dumps({**self.proof(), "schema": True}).encode(), b""),
            (0, json.dumps({**self.proof(), "secret": "SYNTHETIC_PRIVATE"}).encode(), b""),
            (0, json.dumps({**self.proof(), "version": "2.1.312\nPRIVATE"}).encode(), b""),
            (0, json.dumps({**self.proof(), "version": "2.1.313"}).encode(), b""),
            (0, b'{"schema":0,' + json.dumps(self.proof()).encode()[1:], b""),
            (1, json.dumps(self.proof()).encode(), b"PRIVATE STDERR"),
            (0, b"PRIVATE NON JSON", b""),
        ]
        for reply in replies:
            with self.subTest(reply=reply), patch.object(compat.os, "geteuid", return_value=1000), \
                 patch.object(compat.shutil, "which", return_value="/usr/bin/bwrap"), \
                 patch.object(compat, "claude_binary", return_value=self.binary), \
                 patch.object(compat, "SOURCE", self.source), \
                 patch.object(compat, "bounded_run", return_value=reply):
                with self.assertRaisesRegex(compat.CompatibilityDenied, "^COMPAT: REFUSED$"):
                    compat.execute(allow_claude_compat=True)

    def test_public_cli_status_and_refusal_never_execute(self):
        with patch.object(compat, "execute") as execute:
            for argv in ([], ["self-test"], ["send"], ["self-test", "--allow-network"], ["status", "extra"]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(compat.main(argv), 2)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(compat.main(["status"]), 0)
            execute.assert_not_called()

    def test_client_flags_and_environment_never_inherit_host_credentials_or_proxy(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "PRIVATE", "HTTPS_PROXY": "PRIVATE",
                "CLAUDE_CODE_MAX_RETRIES": "15", "CLAUDE_CODE_RETRY_WATCHDOG": "1"}):
            env = fixture.client_environment(12345)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], fixture.TOKEN)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:12345")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/home/model/profile")
        self.assertEqual(env["CLAUDE_CODE_MAX_RETRIES"], "0")
        self.assertNotIn("CLAUDE_CODE_RETRY_WATCHDOG", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        command = fixture.client_command()
        self.assertEqual(command[0], "/opt/claude")
        self.assertEqual(command[command.index("--tools")+1], "")
        self.assertEqual(command[command.index("--mcp-config")+1], '{"mcpServers":{}}')
        for flag in ("--bare", "--restricted", "--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, command)
        for port in (True, 0, -1, 65536, "12345"):
            with self.assertRaises(fixture.FixtureDenied):
                fixture.client_environment(port)

    def test_protocol_accepts_only_synthetic_request_and_empty_tools(self):
        raw = json.dumps(body()).encode()
        for path in ("/v1/messages", "/v1/messages?beta=true"):
            self.assertEqual(fixture.validate_request(path, headers(), raw), body())
        for path in ("https://api.anthropic.com/v1/messages", "/v1/messages?target=host", "/v1/messages/../oauth", "/oauth", "/"):
            with self.subTest(path=path), self.assertRaises(fixture.FixtureDenied):
                fixture.validate_request(path, headers(), raw)
        for changes in ({"tools": [{"name": "Bash"}]}, {"stream": 1}, {"model": "different"},
                        {"messages": []}, {"messages": [{"role": "assistant", "content": fixture.PROMPT}]},
                        {"messages": [{"role": "user", "content": "NOT OUR INPUT"}]},
                        {"messages": [{"role": "user", "content": fixture.PROMPT + " UNAPPROVED"}]}):
            with self.subTest(changes=changes), self.assertRaises(fixture.FixtureDenied):
                fixture.validate_request("/v1/messages", headers(), json.dumps(body(**changes)).encode())

    def test_native_extra_context_is_refused_even_with_synthetic_marker(self):
        # CachyOS 5fa1638: one user message, five text blocks, marker present.
        # Preserve this rejection rather than allowing arbitrary client context.
        content = [{"type": "text", "text": text} for text in (
            "SYNTHETIC CONTEXT A", "SYNTHETIC CONTEXT B", fixture.PROMPT,
            "SYNTHETIC CONTEXT C", "SYNTHETIC CONTEXT D")]
        raw = json.dumps(body(messages=[{"role": "user", "content": content}])).encode()
        handler = self.handler("/v1/messages", raw)
        handler.do_POST()
        self.assertEqual(handler.server.invalid_reason, "request-content")
        self.assertEqual(handler.server.messages, 0)
        self.assertEqual(handler.server.shape, {"input_messages": 1, "text_blocks": 5,
                                               "marker": True, "exact": False})
        handler.send_response.assert_called_once_with(403)
        content = [{"type": "text", "text": fixture.PROMPT}]
        raw = json.dumps(body(messages=[{"role": "user", "content": content}])).encode()
        self.assertEqual(fixture.validate_request("/v1/messages", headers(), raw)["messages"][0]["content"], content)

    def test_wrong_duplicate_or_real_authentication_material_refused(self):
        bad = [headers(Authorization="Bearer duplicate"), headers(Cookie="SYNTHETIC"), headers(**{"x-api-key": "SYNTHETIC"}), headers(**{"Transfer-Encoding": "chunked"})]
        no_auth = Message()
        bad.append(no_auth)
        wrong_auth = Message()
        wrong_auth["Authorization"] = "Bearer NOT OUR SYNTHETIC TOKEN"
        bad.append(wrong_auth)
        for h in bad:
            with self.assertRaises(fixture.FixtureDenied):
                fixture.validate_request("/v1/messages", h, json.dumps(body()).encode())
        for raw in (b"", b"x"*(fixture.MAX_BODY+1), b'{"model":"a","model":"b"}', b"null", b"NaN"):
            with self.assertRaises(fixture.FixtureDenied):
                fixture.validate_request("/v1/messages", headers(), raw)

    def handler(self, path, raw, scenario="reply", extra_headers=None):
        h = object.__new__(fixture.FixtureHandler)
        h.path = path
        h.headers = headers()
        h.headers["Content-Length"] = str(len(raw))
        if extra_headers:
            for name, value in extra_headers:
                h.headers[name] = value
        h.rfile = io.BytesIO(raw)
        h.wfile = io.BytesIO()
        h.server = types.SimpleNamespace(requests=0, messages=0, invalid=False,
                                         scenario=scenario, lock=threading.Lock())
        h.send_response = Mock()
        h.send_header = Mock()
        h.end_headers = Mock()
        return h

    def test_handler_reply_denial_and_optional_endpoint_are_local_and_bounded(self):
        raw = json.dumps(body()).encode()
        for scenario, expected in (("reply", 200), ("recovery-deny", 200), ("deny", 401),
                                   ("rate-limit", 429), ("unavailable", 503)):
            h = self.handler("/v1/messages?beta=true", raw, scenario)
            h.do_POST()
            h.send_response.assert_called_once_with(expected)
            self.assertFalse(h.server.invalid)
            self.assertEqual(h.server.messages, 1)
        h = self.handler("/v1/messages/count_tokens?beta=true", raw)
        h.do_POST()
        h.send_response.assert_called_once_with(404)
        self.assertFalse(h.server.invalid)
        self.assertEqual(h.server.messages, 0)

    def test_disconnect_and_stall_handlers_apply_only_after_validated_message(self):
        for scenario in ("disconnect", "timeout"):
            with self.subTest(scenario=scenario):
                h = self.handler("/v1/messages", json.dumps(body()).encode(), scenario)
                h.connection = Mock()
                h.connection.recv.return_value = b""
                h.do_POST()
                self.assertTrue(h.server.fault_applied)
                self.assertFalse(h.server.invalid)
                self.assertEqual(h.server.messages, 1)
                h.send_response.assert_not_called()
                if scenario == "disconnect":
                    h.connection.shutdown.assert_called_once_with(socket.SHUT_RDWR)
                else:
                    self.assertTrue(h.server.stall_closed)
                    h.connection.settimeout.assert_called_once_with(fixture.STALL_SOCKET_DEADLINE)
                    h.connection.recv.assert_called_once_with(1)
        h = self.handler("/v1/messages", json.dumps(body(tools=[{"name": "Bash"}])).encode(), "disconnect")
        h.connection = Mock()
        h.do_POST()
        self.assertTrue(h.server.invalid)
        h.connection.shutdown.assert_not_called()

    @staticmethod
    def scenario_server(**changes):
        return types.SimpleNamespace(server_port=12345, invalid=False, requests=1, messages=1,
                                     serve_forever=lambda **kw: None, shutdown=lambda: None,
                                     **{"fault_applied": True, "stall_closed": True, **changes})

    def test_http_fault_proof_requires_matching_production_metadata_and_observed_fault(self):
        for scenario, (status, _, reason) in fixture.HTTP_FAULTS.items():
            good = {"type": "result", "subtype": "success", "is_error": True, "api_error_status": status,
                    "result": "PRIVATE", "errors": ["no useful prose whatsoever"]}
            for output, witnessed, accepted in ((good, True, True), (good, False, False),
                    ({**good, "api_error_status": 418}, True, False),
                    ({**good, "api_error_status": str(status)}, True, False),
                    ({"type": "result", "subtype": "success", "is_error": True,
                      "errors": [str(status) + " SYNTHETIC_DENIAL"]}, True, False)):
                with self.subTest(scenario=scenario, accepted=accepted), \
                        patch.object(fixture, "FixtureServer", return_value=contextlib.nullcontext(
                            self.scenario_server(fault_applied=witnessed))), \
                        patch.object(fixture, "bounded_run", return_value=(1, json.dumps(output).encode(), b"PRIVATE")):
                    if accepted:
                        self.assertTrue(fixture.run_scenario(scenario))
                        self.assertEqual(fixture.classify_failure(1, json.dumps(output).encode()), reason)
                    else:
                        with self.assertRaises(fixture.FixtureDenied):
                            fixture.run_scenario(scenario)
            with patch.object(fixture, "FixtureServer", return_value=contextlib.nullcontext(self.scenario_server())), \
                    patch.object(fixture, "bounded_run", return_value=(0, json.dumps(good).encode(), b"")):
                self.assertTrue(fixture.run_scenario(scenario))

    def test_disconnect_requires_witness_and_error_envelope_not_a_generic_failure(self):
        good = {"type": "result", "subtype": "success", "is_error": True}
        for code, output, witnessed, accepted in ((1, good, True, True), (0, good, True, True), (1, good, False, False),
                (-11, good, True, False), (2, good, True, False), (True, good, True, False),
                (1, {"error": "network failed"}, True, False),
                (1, {**good, "api_error_status": 401}, True, False),
                (1, {**good, "is_error": False}, True, False)):
            with self.subTest(code=code, output=output, witnessed=witnessed), \
                    patch.object(fixture, "FixtureServer", return_value=contextlib.nullcontext(
                        self.scenario_server(fault_applied=witnessed))), \
                    patch.object(fixture, "bounded_run", return_value=(code, json.dumps(output).encode(), b"PRIVATE")):
                if accepted:
                    self.assertTrue(fixture.run_scenario("disconnect"))
                else:
                    with self.assertRaises(fixture.FixtureDenied):
                        fixture.run_scenario("disconnect")

    def test_timeout_requires_watchdog_after_stalled_request_and_confirmed_client_close(self):
        variants = (({}, "timeout", True), ({"fault_applied": False}, "timeout", False),
                    ({"stall_closed": False}, "timeout", False), ({}, "process-io", False))
        for changes, reason, accepted in variants:
            with self.subTest(changes=changes, reason=reason), \
                    patch.object(fixture, "FixtureServer", return_value=contextlib.nullcontext(self.scenario_server(**changes))), \
                    patch.object(fixture, "bounded_run", side_effect=fixture.FixtureDenied(reason)) as run:
                if accepted:
                    self.assertTrue(fixture.run_scenario("timeout"))
                else:
                    with self.assertRaises(fixture.FixtureDenied):
                        fixture.run_scenario("timeout")
                self.assertEqual(run.call_args.kwargs["timeout"], fixture.STALL_DEADLINE)
        server = self.scenario_server()
        server.messages = 0
        with patch.object(fixture, "FixtureServer", return_value=contextlib.nullcontext(server)), \
                patch.object(fixture, "bounded_run", side_effect=fixture.FixtureDenied("timeout")):
            with self.assertRaises(fixture.FixtureDenied):
                fixture.run_scenario("timeout")
        with patch.object(fixture, "FixtureServer", return_value=contextlib.nullcontext(self.scenario_server())), \
                patch.object(fixture, "bounded_run", return_value=(1, b'{"type":"result","subtype":"success","is_error":true}', b"")):
            with self.assertRaises(fixture.FixtureDenied):
                fixture.run_scenario("timeout")

    def test_recovery_requires_exact_single_turn_response_without_permission_denials(self):
        good = {"type": "result", "subtype": "success", "is_error": False, "result": fixture.REPLY,
                "num_turns": 1, "permission_denials": []}
        for scenario in fixture.RECOVERY_SCENARIOS | {"reply"}:
            for change, accepted in (({}, True), ({"num_turns": 2}, False), ({"num_turns": True}, False),
                    ({"permission_denials": [{"tool": "Bash"}]}, False), ({"api_error_status": 401}, False),
                    ({"result": "WRONG"}, False), ({"is_error": True}, False)):
                with self.subTest(scenario=scenario, change=change), \
                        patch.object(fixture, "FixtureServer", return_value=contextlib.nullcontext(self.scenario_server())), \
                        patch.object(fixture, "bounded_run", return_value=(0, json.dumps({**good, **change}).encode(), b"")):
                    if accepted:
                        self.assertTrue(fixture.run_scenario(scenario))
                    else:
                        with self.assertRaises(fixture.FixtureDenied):
                            fixture.run_scenario(scenario)

    def test_handler_refuses_bad_length_unknown_paths_and_transfer_encoding(self):
        raw = json.dumps(body()).encode()
        for path, raw_value, extras in (("/oauth", raw, []), ("/v1/messages", raw, [("Content-Length", "999")]),
                    ("/v1/messages", b"x"*(fixture.MAX_BODY+1), []),
                    ("/v1/messages", raw, [("Transfer-Encoding", "chunked")])):
            h = self.handler(path, raw_value, extra_headers=extras)
            h.do_POST()
            h.send_response.assert_called_once_with(403)
            self.assertTrue(h.server.invalid)
            self.assertEqual(h.server.messages, 0)

    def test_parser_error_unsupported_method_and_deep_json_fail_closed(self):
        h = self.handler("/v1/messages", b"{}")
        h.send_error(501, "PRIVATE INPUT")
        self.assertTrue(h.server.invalid)
        h.send_response.assert_called_once_with(403)
        with self.assertRaises(fixture.FixtureDenied):
            fixture.validate_request("/v1/messages", headers(), b"["*2000 + b"0" + b"]"*2000)

    def test_sse_response_has_complete_order_and_synthetic_content(self):
        content_type, raw = fixture.reply_body(True)
        self.assertEqual(content_type, "text/event-stream")
        events = [json.loads(x.split("\ndata: ", 1)[1]) for x in raw.decode().strip().split("\n\n")]
        self.assertEqual([e["type"] for e in events], ["message_start", "content_block_start",
            "content_block_delta", "content_block_stop", "message_delta", "message_stop"])
        self.assertEqual(events[2]["delta"]["text"], fixture.REPLY)
        content_type, raw = fixture.reply_body(False)
        self.assertEqual(json.loads(raw)["content"][0]["text"], fixture.REPLY)

    def test_401_then_crash_or_unrelated_error_is_not_auth_denial_proof(self):
        server = types.SimpleNamespace(server_port=12345, invalid=False, messages=1, fault_applied=True,
                                       serve_forever=lambda **kw: None, shutdown=lambda: None)
        class Context:
            def __enter__(self): return server
            def __exit__(self, *_): pass
        good = {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401,
                "errors": ["PRIVATE ERROR PROSE IS NOT INSPECTED"]}
        for code, output in ((-11, good), (2, good), (1, {"is_error": True, "errors": ["OTHER"]}),
                             (1, {**good, "is_error": False}), (1, {**good, "api_error_status": 403}),
                             (1, {"type": "result", "is_error": True,
                                  "errors": ["API Error: 401 SYNTHETIC_DENIAL"]})):
            with patch.object(fixture, "FixtureServer", return_value=Context()), \
                 patch.object(fixture, "bounded_run", return_value=(code, json.dumps(output).encode(), b"")):
                with self.assertRaises(fixture.FixtureDenied):
                    fixture.run_scenario("deny")
        with patch.object(fixture, "FixtureServer", return_value=Context()), \
             patch.object(fixture, "bounded_run", return_value=(1, json.dumps(good).encode(), b"")):
            self.assertTrue(fixture.run_scenario("deny"))

    def test_repeated_denials_are_refused_even_after_structured_401_exit(self):
        # CachyOS e59cc4c: seven accepted synthetic messages before timeout.
        # Even a later correct 401 exit must not pass a single-attempt fixture.
        server = types.SimpleNamespace(server_port=12345, invalid=False, requests=7, messages=7,
                    shape={"input_messages": 1, "text_blocks": 1, "marker": True, "exact": True},
                    serve_forever=lambda **kw: None, shutdown=lambda: None)
        class Context:
            def __enter__(self): return server
            def __exit__(self, *_): pass
        output = {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401}
        with patch.object(fixture, "FixtureServer", return_value=Context()), \
             patch.object(fixture, "bounded_run", return_value=(1, json.dumps(output).encode(), b"")) as run:
            with self.assertRaises(fixture.FixtureDenied) as failure:
                fixture.run_scenario("deny")
        self.assertEqual(failure.exception.reason, "request-limit")
        self.assertEqual(failure.exception.context["messages"], 7)
        self.assertEqual(run.call_args.kwargs["env"]["CLAUDE_CODE_MAX_RETRIES"], "0")
        self.assertEqual(run.call_args.kwargs["timeout"], 40)
        self.assertEqual(run.call_args.kwargs["input_bytes"], fixture.PROMPT.encode())

    def test_bounded_process_success_output_limit_and_timeout(self):
        command = [sys.executable, "-I", "-B", "-c", "import sys; print('SYNTHETIC'); print('ERROR',file=sys.stderr)"]
        code, out, err = fixture.bounded_run(command, timeout=2)
        self.assertEqual((code, out, err), (0, b"SYNTHETIC\n", b"ERROR\n"))
        for script, kwargs in (("print('x'*10000)", {"limit": 50}),
                               ("import time; time.sleep(10)", {"timeout": 0.1})):
            with self.assertRaises(fixture.FixtureDenied):
                fixture.bounded_run([sys.executable, "-I", "-B", "-c", script], **kwargs)

    def test_failed_boundary_never_starts_native_client_or_fixture(self):
        with patch.object(fixture, "boundary_check", side_effect=fixture.FixtureDenied), \
             patch.object(fixture, "bounded_run") as native, \
             patch.object(fixture, "run_scenario") as scenario, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(fixture.main(["reply"]), 1)
        native.assert_not_called()
        scenario.assert_not_called()
        metadata = json.loads(output.getvalue())
        self.assertEqual(metadata["error"], "COMPAT REFUSED")
        self.assertEqual(metadata["phase"], "reply")
        self.assertEqual(metadata["reason"], "internal")
        self.assertNotIn("SYNTHETIC", output.getvalue())
        for args in ([], ["send"], ["reply", "extra"]):
            with patch.object(fixture, "boundary_check") as boundary, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fixture.main(args), 2)
                boundary.assert_not_called()

    def test_wrong_namespace_is_denied_before_any_socket_creation(self):
        with patch.object(fixture.Path, "home", return_value=Path("/home/model")), \
             patch.object(fixture.Path, "exists", return_value=False), \
             patch.dict(fixture.os.environ, {"HOST_HOME": "/home/synthetic", "HOST_NETNS": "net:[same]"}, clear=True), \
             patch.object(fixture.os, "readlink", return_value="net:[same]"), \
             patch.object(fixture.socket, "socket") as socket:
            with self.assertRaises(fixture.FixtureDenied):
                fixture.boundary_check()
            socket.assert_not_called()

    def test_failure_reason_and_message_shape_expose_no_request_values(self):
        raw = json.dumps(body(messages=[{"role": "user", "content": fixture.PROMPT + " PRIVATE_TEXT"}])).encode()
        h = self.handler("/v1/messages", raw)
        h.do_POST()
        self.assertEqual(h.server.invalid_reason, "request-content")
        self.assertEqual(h.server.shape, {"input_messages": 1, "text_blocks": 1,
                                         "marker": True, "exact": False})
        self.assertNotIn("PRIVATE_TEXT", repr(h.server.shape))
        h = self.handler("/v1/messages", json.dumps(body(model="PRIVATE_MODEL")).encode())
        h.do_POST()
        self.assertEqual(h.server.invalid_reason, "request-model")
        self.assertNotIn("PRIVATE_MODEL", h.server.invalid_reason)

    def test_failure_metadata_is_bounded_fixed_vocabulary_and_exact_types(self):
        error = fixture.FixtureDenied("PRIVATE_REASON")
        error.context = {"client_exit": True, "requests": 10**20, "messages": -10,
                         "input_messages": "PRIVATE", "text_blocks": 1, "marker": "PRIVATE",
                         "exact": True, "unlisted": "PRIVATE", "result_type": "PRIVATE",
                         "success_subtype": 1, "is_error": True, "api_status": "PRIVATE"}
        details = fixture.failure_metadata(error, "reply")
        self.assertEqual(details["reason"], "internal")
        self.assertIsNone(details["client_exit"])
        self.assertEqual(details["requests"], 32)
        self.assertEqual(details["messages"], 0)
        self.assertEqual(details["input_messages"], 0)
        self.assertIs(details["marker"], False)
        self.assertIs(details["result_type"], False)
        self.assertIs(details["success_subtype"], False)
        self.assertIs(details["is_error"], True)
        self.assertEqual(details["api_status"], "none")
        self.assertNotIn("PRIVATE", json.dumps(details))
        self.assertEqual(compat.parse_failure(json.dumps(details), "reply"), details)

    def test_result_shape_uses_fixed_vocabulary_without_native_prose_or_wrong_types(self):
        native = {"type": "result", "subtype": "success", "is_error": True,
                  "result": "PRIVATE RESPONSE", "errors": ["PRIVATE ERROR"]}
        expected = {"result_type": True, "success_subtype": True, "is_error": True,
                    "api_status": "none"}
        for status in (None, *fixture.HTTP_REASONS, "401", True, {}, 400, "PRIVATE"):
            with self.subTest(status=status):
                shape = fixture.result_shape(json.dumps({**native, "api_error_status": status}).encode())
                wanted = ("none" if status is None else str(status)
                          if type(status) is int and status in fixture.HTTP_REASONS else "other")
                self.assertEqual(shape, {**expected, "api_status": wanted})
                self.assertNotIn("PRIVATE", json.dumps(shape))
        blank = dict.fromkeys(fixture.RESULT_FLAGS, False) | {"api_status": "none"}
        for raw in (b"PRIVATE", b"[]", b'{"is_error":true,"is_error":false}',
                    b'{"type":"result","x":NaN}', b" " * (fixture.MAX_FAILURE_BYTES + 1), "PRIVATE"):
            self.assertEqual(fixture.result_shape(raw), blank)
        self.assertEqual(fixture.result_shape(json.dumps({"type": "PRIVATE", "subtype": "PRIVATE",
                            "is_error": 1}).encode()), blank)

    def test_native_missing_status_refusal_preserves_only_result_shape(self):
        raw = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "result": "PRIVATE RESPONSE", "errors": ["PRIVATE ERROR"]}).encode()
        with patch.object(fixture, "FixtureServer", return_value=contextlib.nullcontext(self.scenario_server())), \
                patch.object(fixture, "bounded_run", return_value=(1, raw, b"PRIVATE STDERR")):
            with self.assertRaises(fixture.FixtureDenied) as failure:
                fixture.run_scenario("deny")
        details = fixture.failure_metadata(failure.exception, "deny")
        self.assertEqual(details["api_status"], "none")
        self.assertTrue(all(details[k] for k in fixture.RESULT_FLAGS))
        self.assertEqual(compat.parse_failure(json.dumps(details), "deny"), details)
        self.assertNotIn("PRIVATE", json.dumps(details))

    def test_forged_failure_metadata_and_duplicate_fields_are_refused(self):
        details = fixture.failure_metadata(fixture.FixtureDenied("request-content"), "reply")
        for change in ({"reason": "PRIVATE"}, {"phase": "deny"}, {"client_exit": True},
                       {"requests": 33}, {"marker": 1}, {"result_type": 1}, {"is_error": "true"},
                       {"success_subtype": None}, {"api_status": 401}, {"api_status": "403"},
                       {"api_status": "PRIVATE"}, {"secret": "PRIVATE"}):
            raw = json.dumps({**details, **change})
            with self.assertRaises(ValueError):
                compat.parse_failure(raw, "reply")
        with self.assertRaises(ValueError):
            compat.parse_failure('{"error":"PRIVATE",' + json.dumps(details)[1:], "reply")

    def test_failure_details_propagate_without_changing_fail_closed_result(self):
        details = fixture.failure_metadata(fixture.FixtureDenied("request-content"), "reply")
        details.update(client_exit=1, requests=2, input_messages=1, text_blocks=2,
                       marker=True, exact=False)
        with patch.object(compat.os, "geteuid", return_value=1000), \
             patch.object(compat.shutil, "which", return_value="/usr/bin/bwrap"), \
             patch.object(compat, "claude_binary", return_value=self.binary), \
             patch.object(compat, "SOURCE", self.source), \
             patch.object(compat, "bounded_run", side_effect=[
                (0, json.dumps(self.proof()).encode(), b""),
                (1, json.dumps(details).encode(), b"PRIVATE STDERR")]):
            with self.assertRaises(compat.CompatibilityDenied) as failure:
                compat.execute(allow_claude_compat=True)
        self.assertEqual(failure.exception.details, details)
        with patch.object(compat, "execute", side_effect=failure.exception), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(compat.main(["self-test", "--allow-claude-compat"]), 1)
        self.assertEqual(output.getvalue().strip(),
            "COMPAT: REFUSED phase=reply reason=request-content exit=1 requests=2 messages=0 input_messages=1 text_blocks=2 marker=1 exact=0 result_type=0 success_subtype=0 is_error=0 api_status=none")
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertEqual(fixture.client_failure_reason(1, b"", b"error: unknown option PRIVATE", "no-message"), "client-options")

    def test_timeout_retains_observed_request_counts_and_shape(self):
        server = types.SimpleNamespace(server_port=12345, invalid=False, requests=3, messages=1,
                    shape={"input_messages": 1, "text_blocks": 1, "marker": True, "exact": True},
                    serve_forever=lambda **kw: None, shutdown=lambda: None)
        class Context:
            def __enter__(self): return server
            def __exit__(self, *_): pass
        with patch.object(fixture, "FixtureServer", return_value=Context()), \
             patch.object(fixture, "bounded_run", side_effect=fixture.FixtureDenied("timeout")):
            with self.assertRaises(fixture.FixtureDenied) as failure:
                fixture.run_scenario("reply")
        metadata = fixture.failure_metadata(failure.exception, "reply")
        self.assertEqual(metadata["reason"], "timeout")
        self.assertIsNone(metadata["client_exit"])
        self.assertEqual(metadata["requests"], 3)
        self.assertEqual(metadata["messages"], 1)
        self.assertEqual(metadata["text_blocks"], 1)
        self.assertIs(metadata["exact"], True)

    def test_no_false_success_on_auth_error_without_message_or_wrong_reply(self):
        # Avoid creating any live socket even in integration-policy unit tests.
        server = types.SimpleNamespace(server_port=12345, invalid=False, messages=0,
                                       serve_forever=lambda **kw: None, shutdown=lambda: None)
        class Context:
            def __enter__(self): return server
            def __exit__(self, *_): pass
        with patch.object(fixture, "FixtureServer", return_value=Context()), \
             patch.object(fixture, "bounded_run", return_value=(1, b"", b"")):
            with self.assertRaises(fixture.FixtureDenied):
                fixture.run_scenario("deny")
        server.messages = 1
        with patch.object(fixture, "FixtureServer", return_value=Context()), \
             patch.object(fixture, "bounded_run", return_value=(0, b'{"is_error":false,"result":"WRONG"}', b"")):
            with self.assertRaises(fixture.FixtureDenied):
                fixture.run_scenario("reply")


if __name__ == "__main__":
    unittest.main()
