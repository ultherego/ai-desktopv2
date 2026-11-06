"""Offline Codex isolation commands, private token pipe and closed results."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import analysis_contract as contract
from ai_desktop import codex_child as child
from ai_desktop import codex_runtime as runtime
from ai_desktop import e2_connect_proxy as proxy
from ai_desktop import process_io
from ai_desktop.siwc_auth import AuthError, Secret
from ai_desktop.codex_protocol import CLIENT_VERSION
from system_client_fixtures import FakeBinding

TOKEN = "synthetic-private-access-token"


def selection(**overrides):
    return dict(dict(provider="codex", model="default", effort="medium", instructions="Review carefully.",
                     profile="reviewer", access="read-only"), **overrides)


def request(**overrides):
    options = dict(question="synthetic question", context=[], execution=selection())
    options.update(overrides)
    payload = contract.application_input(None, **options)
    return contract.parse_request(payload.encode())[0].encode()


def header(host):
    return ("CONNECT " + host + ":443 HTTP/1.1\r\nHost: " + host + ":443\r\n\r\n").encode()


class CodexEgressTests(unittest.TestCase):
    def test_compiled_provider_hosts_are_disjoint_and_never_configurable(self):
        self.assertFalse(proxy.HOSTS & proxy.OPENAI_HOSTS)
        with self.assertRaises(proxy.Denied):
            proxy.connect_host(header("api.openai.com"))
        self.assertEqual(proxy.connect_host(header("api.openai.com"), hosts=proxy.OPENAI_HOSTS), "api.openai.com")
        for host in proxy.HOSTS | {"auth.openai.com", "chatgpt.com", "127.0.0.1"}:
            with self.subTest(host=host), self.assertRaises(proxy.Denied):
                proxy.connect_host(header(host), hosts=proxy.OPENAI_HOSTS)
        for hosts in (set(proxy.OPENAI_HOSTS), proxy.HOSTS | proxy.OPENAI_HOSTS,
                      frozenset({"evil.invalid"}), frozenset()):
            with self.subTest(hosts=hosts), self.assertRaises(proxy.Denied):
                proxy.connect_host(header("api.openai.com"), hosts=hosts)

    def test_openai_dns_is_pinned_and_all_nonpublic_answers_fail(self):
        good = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))
        with patch.object(proxy.socket, "getaddrinfo", return_value=[good]) as dns, \
                patch.object(proxy.socket, "socket") as opened:
            proxy.open_public("api.openai.com", hosts=proxy.OPENAI_HOSTS)
        dns.assert_called_once()
        opened.return_value.connect.assert_called_once_with(("1.1.1.1", 443))
        with self.assertRaises(proxy.Denied):
            proxy.public_addresses("api.openai.com", lambda *a, **k: [good, private], hosts=proxy.OPENAI_HOSTS)
        resolver = Mock()
        with self.assertRaises(proxy.Denied):
            proxy.public_addresses("api.anthropic.com", resolver, hosts=proxy.OPENAI_HOSTS)
        resolver.assert_not_called()


class CodexCommandTests(unittest.TestCase):
    def test_namespace_has_fresh_homes_all_six_namespaces_and_no_auth_mount(self):
        binding = FakeBinding()
        command = runtime.namespace_command("analyze", Path("/tmp/synthetic-codex"), binding=binding)
        for flag in ("--unshare-user", "--unshare-pid", "--unshare-net", "--unshare-ipc", "--unshare-uts",
                     "--die-with-parent", "--disable-userns", "--assert-userns-disabled", "--clearenv"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--cap-drop") + 1], "ALL")
        self.assertNotIn(str(runtime.PROFILE), command)
        self.assertNotIn("/home/client/profile", command)
        self.assertNotIn(TOKEN, command)
        self.assertNotIn(runtime.TOKEN_ENV, command)
        self.assertIn(runtime.INNER_HOME, command)
        self.assertIn("/run/egress.sock", command)
        for action in ("version", "compat-help", "compat-reject", "compat-rpc", "catalog"):
            offline = runtime.namespace_command(action, Path("/tmp/synthetic-codex"), binding=binding)
            self.assertNotIn("/run/egress.sock", offline)
            self.assertNotIn("--bind", offline)
        self.assertEqual(command[-3:-1], [str(runtime.INSTALL / "codex-client.py"), "analyze"])
        for action in ("login", "web", "sh", "../analyze"):
            with self.assertRaises(contract.AnalysisDenied):
                runtime.namespace_command(action, Path("/tmp/synthetic-codex"), binding=binding)

    def test_native_environment_is_constructed_and_token_exists_only_when_explicit(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "host-secret", "CODEX_ACCESS_TOKEN": "host-secret",
                                    "OPENAI_BASE_URL": "https://evil.invalid", "ACCESS_TOKEN": "host-secret"}):
            empty, selected = runtime.client_environment(), runtime.client_environment(Secret(TOKEN))
        self.assertNotIn(runtime.TOKEN_ENV, empty)
        self.assertEqual(selected, dict(empty, **{runtime.TOKEN_ENV: TOKEN}))
        for key in ("OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL", "ALL_PROXY"):
            self.assertNotIn(key, selected)
        self.assertFalse(any("host-secret" in value or "evil.invalid" in value for value in selected.values()))
        self.assertEqual(selected["CODEX_HOME"], runtime.INNER_HOME)
        self.assertEqual(selected["NO_PROXY"], "")
        for token in ("", "with space", "newline\n", "ż", "\x00", "x" * (runtime.MAX_TOKEN_BYTES + 1)):
            with self.subTest(token=repr(token)[:30]), self.assertRaises(contract.AnalysisDenied):
                runtime.checked_token(token)

    def test_private_envelope_is_bounded_closed_and_never_changes_the_request(self):
        source = request()
        packet = runtime.private_packet(Secret(TOKEN), source)
        token, decoded, selected = child.private_input(packet)
        self.assertEqual((token, decoded, selected), (TOKEN, source, selection()))
        self.assertEqual(set(json.loads(packet)), {"token", "request"})
        for raw in (b"", b"[]", b'{}', packet + b"x", b"x" * (runtime.PRIVATE_PACKET_BYTES + 1),
                    contract.canonical({"token": TOKEN, "request": json.loads(source), "endpoint": "evil"}).encode()):
            with self.subTest(raw=raw[:40]), self.assertRaises(contract.AnalysisDenied):
                child.private_input(raw)
        with self.assertRaises(contract.AnalysisDenied):
            runtime.private_packet(TOKEN, b"x" * (contract.MAX_PACKET_LARGE + 1))

    def test_private_pipe_extension_keeps_default_input_ceiling(self):
        with patch.object(process_io.subprocess, "Popen") as start:
            for private in (False, 1, "true"):
                with self.subTest(private=private), self.assertRaises(contract.AnalysisDenied):
                    process_io.bounded_process(["unused"], env={}, input_bytes=b"", private_input=private,
                                               input_limit=process_io.MAX_INPUT_BYTES + 1)
            with self.assertRaises(contract.AnalysisDenied):
                process_io.bounded_process(["unused"], env={}, private_input=True,
                                           input_limit=process_io.MAX_PRIVATE_INPUT_BYTES + 1)
            start.assert_not_called()

    def test_real_private_pipe_accepts_only_its_bounded_extra_envelope_space(self):
        raw = b"x" * process_io.MAX_PRIVATE_INPUT_BYTES
        command = [sys.executable, "-I", "-B", "-c",
                   "import hashlib,sys; data=sys.stdin.buffer.read(); print(hashlib.sha256(data).hexdigest())"]
        code, output, errors = process_io.bounded_process(command, env={"PATH": "/usr/bin"},
            input_bytes=raw, input_limit=process_io.MAX_PRIVATE_INPUT_BYTES, private_input=True, timeout=5)
        self.assertEqual((code, output.strip(), errors), (0, hashlib.sha256(raw).hexdigest().encode(), b""))


class CodexRuntimeTests(unittest.TestCase):
    def fixture(self, *, version=None, native=None, code=0, error=b"", auth_error=None):
        calls, proxy_server = [], Mock()
        client = Mock()
        def credentials():
            calls.append("auth")
            if auth_error is not None:
                raise auth_error
            return Secret(TOKEN)
        client.access_token.side_effect = credentials
        def process(command, **kwargs):
            action = command[1]
            calls.append(action)
            self.assertNotIn(TOKEN, command)
            self.assertNotIn(TOKEN, kwargs["env"].values())
            if action == "version":
                self.assertNotIn("input_bytes", kwargs)
                return 0, version if version is not None else ("codex-cli " + CLIENT_VERSION + "\n").encode(), b""
            if action == "compat-help":
                return 0, b"synthetic help", b""
            if action == "compat-reject":
                return 2, b"", b"synthetic unknown option"
            if action == "compat-rpc":
                return 0, b'{"initialize":true,"signed_out":true,"models":true}', b""
            self.assertTrue(kwargs["private_input"])
            self.assertEqual(kwargs["input_limit"], runtime.PRIVATE_PACKET_BYTES)
            self.assertEqual(child.private_input(kwargs["input_bytes"])[0], TOKEN)
            return code, native if native is not None else b'{"text":"synthetic answer","meta":null}\n', error
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(runtime, "supervisor_ready"))
        stack.enter_context(patch.object(runtime, "profile_lock", return_value=contextlib.nullcontext()))
        stack.enter_context(patch.object(runtime.client_binding, "bind", side_effect=lambda _: FakeBinding()))
        stack.enter_context(patch.object(runtime, "namespace_command", side_effect=lambda action, _, **kw: ["fixed", action]))
        stack.enter_context(patch.object(runtime, "OpenAIProxy", return_value=contextlib.nullcontext(proxy_server)))
        stack.enter_context(patch.object(runtime, "AuthStore"))
        auth = stack.enter_context(patch.object(runtime, "AuthClient", return_value=client))
        start = stack.enter_context(patch.object(runtime, "bounded_process", side_effect=process))
        return stack, calls, proxy_server, auth, start

    def test_version_precedes_credentials_and_one_request_has_no_fallback(self):
        stack, calls, proxy_server, auth, process = self.fixture()
        with stack:
            answer = runtime.run_client("analyze", request(), mode="question")
        self.assertEqual(str(answer), "synthetic answer")
        self.assertIsInstance(answer, contract.ModelText)
        self.assertIsNone(answer.meta)
        self.assertEqual(calls, ["version", "compat-help", "compat-reject", "compat-rpc", "auth", "analyze"])
        self.assertEqual(process.call_count, 5)
        auth.assert_called_once()
        proxy_server.shutdown.assert_called_once()

    def test_bad_version_never_reads_credentials_or_opens_the_proxy(self):
        stack, calls, proxy_server, auth, process = self.fixture(version=b"codex-cli unexpected\n")
        with stack, self.assertRaisesRegex(contract.AnalysisDenied, "reason=client-version"):
            runtime.run_client("analyze", request(), mode="question")
        self.assertEqual(calls, ["version"])
        auth.assert_not_called()
        proxy_server.shutdown.assert_not_called()

    def test_web_wrong_provider_and_bad_input_fail_before_auth_or_runtime(self):
        with patch.object(runtime, "supervisor_ready") as ready, patch.object(runtime, "AuthClient") as auth:
            for action, raw, mode in (("web", request(), "web"), ("login", b"", "analysis"),
                                      ("analyze", b"[]", "question"), ("analyze", request(), "analysis"),
                                      ("analyze", request(execution=selection(provider="claude")), "question")):
                with self.subTest(action=action, raw=raw[:40]), self.assertRaises(contract.AnalysisDenied):
                    runtime.run_client(action, raw, mode=mode)
            with self.assertRaisesRegex(contract.AnalysisDenied, "reason=cancelled"):
                runtime.run_client("analyze", request(), mode="question", cancelled=lambda: True)
            ready.assert_not_called()
            auth.assert_not_called()

    def test_auth_failure_returns_only_closed_reason_without_native_call(self):
        stack, calls, _, _, _ = self.fixture(auth_error=AuthError("network"))
        with stack, self.assertRaisesRegex(contract.AnalysisDenied, "reason=network$"):
            runtime.run_client("analyze", request(), mode="question")
        self.assertEqual(calls, ["version", "compat-help", "compat-reject", "compat-rpc", "auth"])

    def test_native_error_or_secret_in_result_never_escapes_or_retries(self):
        variants = [dict(code=1, native=TOKEN.encode(), error=TOKEN.encode()),
                    dict(native=contract.canonical({"text": TOKEN, "meta": None}).encode()),
                    dict(native=b'{"text":"x","extra":true}'), dict(native=b'{"text":"\\u001b[31m"}'),
                    dict(native=b'{"text":""}')]
        for variant in variants:
            stack, calls, server, _, _ = self.fixture(**variant)
            with self.subTest(variant=variant), stack, self.assertRaises(contract.AnalysisDenied) as caught:
                runtime.run_client("analyze", request(), mode="question")
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertEqual(calls, ["version", "compat-help", "compat-reject", "compat-rpc", "auth", "analyze"])
            server.shutdown.assert_called_once()

    def test_compatible_newer_client_is_allowed_after_all_offline_checks(self):
        stack, calls, _, _, _ = self.fixture(version=b"codex-cli 0.166.1\n")
        with stack:
            self.assertEqual(str(runtime.run_client("analyze", request(), mode="question")), "synthetic answer")
        self.assertEqual(calls[:4], ["version", "compat-help", "compat-reject", "compat-rpc"])

    def test_incompatible_rpc_and_changed_client_never_receive_credentials(self):
        stack, calls, proxy_server, auth, process = self.fixture()
        original = process.side_effect
        def incompatible(command, **kwargs):
            if command[1] == "compat-rpc":
                return 0, b'{"initialize":true,"signed_out":false,"models":true}', b""
            return original(command, **kwargs)
        process.side_effect = incompatible
        with stack, self.assertRaisesRegex(contract.AnalysisDenied, "client-protocol"):
            runtime.run_client("analyze", request(), mode="question")
        auth.assert_not_called()
        proxy_server.shutdown.assert_not_called()
        stack, calls, proxy_server, auth, _ = self.fixture()
        binding = FakeBinding()
        binding.failure = contract.AnalysisDenied("client-changed")
        with stack, patch.object(runtime.client_binding, "bind", return_value=binding), \
                self.assertRaisesRegex(contract.AnalysisDenied, "client-changed"):
            runtime.run_client("analyze", request(), mode="question")
        self.assertTrue(binding.closed)
        auth.assert_not_called()
        proxy_server.shutdown.assert_not_called()

    def test_probe_and_analysis_share_binding_and_pass_only_its_descriptors(self):
        stack, _, _, _, process = self.fixture()
        binding = FakeBinding(pass_fds=(72, 73))
        with stack, patch.object(runtime.client_binding, "bind", return_value=binding):
            runtime.run_client("analyze", request(), mode="question")
            for call in runtime.namespace_command.call_args_list:
                self.assertIs(call.kwargs["binding"], binding)
        self.assertTrue(binding.closed)
        self.assertEqual([call.kwargs["pass_fds"] for call in process.call_args_list], [(72, 73)] * 5)

    def test_update_during_analysis_is_reported_after_the_bound_process_finishes(self):
        stack, calls, server, _, process = self.fixture()
        binding = FakeBinding()
        original = process.side_effect
        def changed(command, **kwargs):
            result = original(command, **kwargs)
            if command[1] == "analyze":
                binding.failure = contract.AnalysisDenied("client-changed")
            return result
        process.side_effect = changed
        with stack, patch.object(runtime.client_binding, "bind", return_value=binding), \
                self.assertRaisesRegex(contract.AnalysisDenied, "client-changed"):
            runtime.run_client("analyze", request(), mode="question")
        self.assertTrue(binding.closed)
        self.assertEqual(calls[-1], "analyze")
        server.shutdown.assert_called_once()

    def test_compatibility_probes_share_a_single_deadline(self):
        now, calls = [0.0], []
        def run(command, **kwargs):
            calls.append((command[1], kwargs["timeout"]))
            now[0] += .6
            return (0, b"codex-cli 0.166.0\n", b"") if command[1] == "version" else (0, b"help", b"")
        with patch.object(runtime.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(runtime, "namespace_command", side_effect=lambda action, _, **kw: ["fixed", action]), \
                patch.object(runtime, "bounded_process", side_effect=run), \
                self.assertRaisesRegex(contract.AnalysisDenied, "timeout"):
            runtime.compatibility(Path("/tmp/synthetic"), FakeBinding(), timeout=1)
        self.assertEqual(calls, [("version", 1.0), ("compat-help", .4)])

    def test_slow_mount_metadata_reduces_budget_before_each_native_start(self):
        now, calls, binding = [0.0], [], FakeBinding()
        def namespace(action, directory, *, binding):
            binding.mount_args()
            now[0] += .6  # A filesystem recheck need not complete immediately.
            return ["fixed", action]
        def run(command, **kwargs):
            calls.append((command[1], kwargs["timeout"]))
            return 0, b"codex-cli 0.166.0\n", b""
        with patch.object(runtime.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(runtime, "namespace_command", side_effect=namespace), \
                patch.object(runtime, "bounded_process", side_effect=run), \
                self.assertRaisesRegex(contract.AnalysisDenied, "timeout"):
            runtime.compatibility(Path("/tmp/synthetic"), binding, timeout=1)
        self.assertEqual(calls, [("version", .4)])

    def test_final_metadata_recheck_over_deadline_cannot_accept_compatibility(self):
        now, binding = [0.0], FakeBinding()
        original = binding.recheck
        def recheck():
            original()
            if binding.rechecks == 4:
                now[0] = 1.01
        stack, calls, _, auth, _ = self.fixture()
        with stack, patch.object(runtime.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(binding, "recheck", side_effect=recheck), \
                self.assertRaisesRegex(contract.AnalysisDenied, "timeout"):
            runtime.compatibility(Path("/tmp/synthetic"), binding, timeout=1)
        self.assertEqual(calls, ["version", "compat-help", "compat-reject", "compat-rpc"])
        auth.assert_not_called()


class CodexChildTests(unittest.TestCase):
    def boundary(self, stack):
        stack.enter_context(patch.object(child, "check_boundary"))
        stack.enter_context(patch.object(child, "install_mode_filter"))
        stack.enter_context(patch.object(child, "Path", side_effect=lambda _: SimpleNamespace(
            exists=lambda: False, iterdir=lambda: iter(()))))

    def test_child_gives_only_explicit_token_env_to_protocol_and_only_text_back(self):
        bridge, output = Mock(), io.StringIO()
        with contextlib.ExitStack() as stack:
            self.boundary(stack)
            stack.enter_context(patch.object(child.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
                runtime.private_packet(TOKEN, request())))))
            stack.enter_context(patch.object(child, "_Bridge", return_value=contextlib.nullcontext(bridge)))
            run = stack.enter_context(patch.object(child, "run", return_value="synthetic answer"))
            stack.enter_context(contextlib.redirect_stdout(output))
            self.assertEqual(child.main(["analyze", "{}"]), 0)
        args, kwargs = run.call_args
        self.assertEqual(args, (str(runtime.BINARY), request()))
        self.assertNotIn(TOKEN, repr(args))
        self.assertEqual(kwargs["env"][runtime.TOKEN_ENV], TOKEN)
        self.assertEqual(json.loads(output.getvalue()), {"text": "synthetic answer", "meta": None})
        self.assertNotIn(TOKEN, output.getvalue())
        bridge.shutdown.assert_called_once()

    def test_filter_failure_precedes_private_input_or_native_execution(self):
        errors, stream = io.StringIO(), Mock()
        with contextlib.ExitStack() as stack:
            self.boundary(stack)
            stack.enter_context(patch.object(child, "install_mode_filter", side_effect=contract.AnalysisDenied("sandbox")))
            stack.enter_context(patch.object(child.sys, "stdin", SimpleNamespace(buffer=stream)))
            bridge = stack.enter_context(patch.object(child, "_Bridge"))
            run = stack.enter_context(patch.object(child, "run"))
            stack.enter_context(contextlib.redirect_stderr(errors))
            self.assertEqual(child.main(["analyze", "{}"]), 2)
        stream.read.assert_not_called()
        bridge.assert_not_called()
        run.assert_not_called()
        self.assertEqual(errors.getvalue(), "CLIENT: REFUSED stage=mode-filter reason=sandbox\n")

    def test_version_uses_no_token_or_private_input(self):
        errors, stream = io.StringIO(), Mock()
        with contextlib.ExitStack() as stack:
            self.boundary(stack)
            stack.enter_context(patch.object(child.sys, "stdin", SimpleNamespace(buffer=stream)))
            execute = stack.enter_context(patch.object(child.os, "execve", side_effect=OSError(TOKEN)))
            stack.enter_context(contextlib.redirect_stderr(errors))
            self.assertEqual(child.main(["version", "{}"]), 2)
        args = execute.call_args.args
        self.assertEqual(args[1], [str(runtime.BINARY), "--version"])
        self.assertNotIn(runtime.TOKEN_ENV, args[2])
        stream.read.assert_not_called()
        self.assertNotIn(TOKEN, errors.getvalue())

    def test_actual_model_metadata_stays_closed_and_separate_from_text(self):
        meta = {"models": [{"model": "gpt-6.1-sol", "input": 123, "output": 17, "window": None}]}
        frame = contract.canonical({"text": "synthetic answer", "meta": meta}).encode()
        answer = runtime.result_text(frame, mode="question", token=TOKEN)
        self.assertEqual(str(answer), "synthetic answer")
        self.assertEqual(answer.meta, meta)
        for bad in ({"models": [{"model": TOKEN, "input": None, "output": None, "window": None}]},
                    {"models": [{**meta["models"][0], "token": TOKEN}]},
                    {"models": [{**meta["models"][0], "input": True}]}):
            with self.subTest(meta=bad), self.assertRaises(contract.AnalysisDenied):
                runtime.result_text(contract.canonical({"text": "synthetic answer", "meta": bad}).encode(),
                                    mode="question", token=TOKEN)
