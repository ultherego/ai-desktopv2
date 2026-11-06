"""Offline native-client command, profile, authentication and result contracts."""
import json
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_desktop import analysis_admin as admin, analysis_sandbox as sandbox, analysis_service, analysis_child
from ai_desktop.analysis_contract import AnalysisDenied, SYSTEM_PROMPT
from ai_desktop import analysis_contract as contract, task_contract
from test_web_search_result import stream as web_stream, web_events


class AnalysisSandboxTests(unittest.TestCase):
    def test_login_failure_preserves_stage_without_exception_or_native_data(self):
        out = io.StringIO()
        with patch.object(analysis_service.os, "isatty", return_value=True), \
                patch.object(sandbox, "supervisor_ready", side_effect=OSError("SECRET-OAUTH")), \
                patch.object(sandbox, "bounded_process") as launch, contextlib.redirect_stdout(out):
            self.assertEqual(analysis_service.worker_main(["login"]), 2)
        self.assertEqual(out.getvalue(), "LOGIN: REFUSED stage=supervisor reason=runtime\n")
        launch.assert_not_called()

    def test_version_failure_classifies_only_fixed_child_errors(self):
        for stderr, stage, reason in (
            (b"SECRET-OAUTH", "version", "version"),
            (b"bwrap: SECRET-PATH", "namespace", "sandbox"),
            (b"CLIENT: REFUSED stage=boundary reason=sandbox\nSECRET", "boundary", "sandbox"),
            (b"CLIENT: REFUSED stage=SECRET reason=SECRET", "version", "version"),
        ):
            with self.subTest(stage=stage), patch.object(sandbox, "supervisor_ready"), \
                    patch.object(sandbox, "profile_lock", return_value=contextlib.nullcontext()), \
                    patch.object(sandbox, "namespace_command", return_value=["unused"]), \
                    patch.object(sandbox, "bounded_process", return_value=(2, b"SECRET", stderr)), \
                    patch.object(sandbox, "_Proxy") as proxy, self.assertRaises(AnalysisDenied) as caught:
                sandbox.run_client("login")
            self.assertEqual((caught.exception.stage, caught.exception.reason), (stage, reason))
            self.assertNotIn("SECRET", str(caught.exception))
            proxy.assert_not_called()

    def test_child_exec_failure_is_redacted_and_available_to_parent(self):
        out = io.StringIO()
        with patch.object(analysis_child, "check_boundary"), \
                patch.object(analysis_child, "install_mode_filter"), \
                patch.object(analysis_child.os, "execve", side_effect=OSError("SECRET")), \
                contextlib.redirect_stderr(out):
            self.assertEqual(analysis_child.main(["version", "{}"]), 2)
        self.assertEqual(out.getvalue(), "CLIENT: REFUSED stage=native-exec reason=runtime\n")
        failure = sandbox.child_failure(out.getvalue().encode(), "version")
        self.assertEqual((failure.stage, failure.reason), ("native-exec", "runtime"))

    def test_filter_failure_stops_native_execution_and_bridge(self):
        out = io.StringIO()
        with patch.object(analysis_child, "check_boundary"), \
                patch.object(analysis_child, "install_mode_filter", side_effect=AnalysisDenied("sandbox")), \
                patch.object(analysis_child.os, "execve") as execute, \
                patch.object(analysis_child, "_Bridge") as bridge, contextlib.redirect_stderr(out):
            for action in ("version", "login", "status", "analyze", "web"):
                self.assertEqual(analysis_child.main([action, "{}"]), 2)
        execute.assert_not_called()
        bridge.assert_not_called()
        self.assertEqual(out.getvalue().count("stage=mode-filter reason=sandbox"), 5)

    def test_diagnostic_vocabulary_rejects_arbitrary_values(self):
        failure = AnalysisDenied("SECRET", stage="SECRET")
        self.assertEqual((failure.stage, failure.reason), ("internal", "internal"))

    def test_runtime_rejects_writable_parent_symlink_and_nonroot_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "binary"
            path.write_text("not a client")
            # /tmp is mutable even when this test runs as root.
            with self.assertRaises(AnalysisDenied):
                sandbox.trusted_file(path)
            link = Path(root) / "link"
            link.symlink_to(path)
            with self.assertRaises(AnalysisDenied):
                sandbox.trusted_file(link)
        with self.assertRaises(AnalysisDenied):
            sandbox.trusted_file("relative")

    def test_client_policy_preserves_oauth_but_removes_tools_and_inherited_configuration(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "SECRET", "ANTHROPIC_BASE_URL": "https://evil"}):
            env = sandbox.client_environment()
        self.assertFalse(any("SECRET" in v or "evil" in v for v in env.values()))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], sandbox.INNER_PROFILE)
        self.assertEqual(env["ENABLE_CLAUDEAI_MCP_SERVERS"], "false")
        argv = sandbox.client_command("analyze")
        self.assertNotIn("--bare", argv)
        for key, value in (("--tools", ""), ("--setting-sources", ""), ("--max-turns", "1"),
                           ("--permission-mode", "dontAsk"), ("--system-prompt", SYSTEM_PROMPT)):
            self.assertEqual(argv[argv.index(key) + 1], value)
        for action in ("sh", "logout", "update", "../login"):
            with self.assertRaises(AnalysisDenied):
                sandbox.client_command(action)

    def test_auth_requires_real_subscription_method_and_exact_private_profile(self):
        base = {"loggedIn": True, "authMethod": "claude.ai", "configDirectory": sandbox.INNER_PROFILE}
        sandbox.check_auth(0, json.dumps(base).encode())
        for method in ("none", "oauth_token", "api_key", "api_key_helper", "third_party", None):
            with self.subTest(method=method), self.assertRaisesRegex(AnalysisDenied, "auth-method"):
                sandbox.check_auth(0, json.dumps(dict(base, authMethod=method)).encode())
        for value in ({**base, "configDirectory": "/home/desktop/.claude"},
                      {**base, "loggedIn": 1}, {}, []):
            with self.assertRaises(AnalysisDenied):
                sandbox.check_auth(0, json.dumps(value).encode())
        with self.assertRaisesRegex(AnalysisDenied, "auth-required"):
            sandbox.check_auth(1, b"SECRET stderr-like content")

    def test_web_exposes_only_fixed_search_with_same_subscription_confinement(self):
        base, env = sandbox.client_environment(), sandbox.client_environment("web")
        extra = {"CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION": "2",
                 "CLAUDE_CODE_WEB_SEARCH_REFILLS_PER_HOUR": "0"}
        self.assertEqual(env, dict(base, **extra))
        for action in ("version", "status", "login", "analyze"):
            self.assertEqual(sandbox.client_environment(action), base)
        argv = sandbox.client_command("web")
        for key, value in (("--tools", "WebSearch"), ("--allowedTools", "WebSearch"),
                           ("--output-format", "stream-json"), ("--setting-sources", ""),
                           ("--permission-mode", "dontAsk"), ("--max-turns", "4"),
                           ("--mcp-config", '{"mcpServers":{}}'), ("--system-prompt", contract.WEB_SYSTEM_PROMPT)):
            self.assertEqual(argv[argv.index(key) + 1], value)
        for flag in ("--restricted", "--safe-mode", "--verbose", "--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, argv)
        start = argv.index("--disallowedTools") + 1
        self.assertEqual(argv[start:start + 2], ["EndConversation", "mcp__*"])
        self.assertNotIn("--bare", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_web_mode_cannot_be_mixed_with_nonweb_native_action(self):
        with patch.object(sandbox, "supervisor_ready") as ready:
            for action, mode in (("web", "analysis"), ("analyze", "web"), ("login", "web")):
                with self.subTest(action=action, mode=mode), self.assertRaises(AnalysisDenied):
                    sandbox.run_client(action, mode=mode)
            ready.assert_not_called()

    def test_web_child_launch_receives_fixed_command_and_only_web_specific_environment(self):
        bridge = Mock()
        body = b'{"mode":"web","question":"synthetic"}'
        with patch.object(analysis_child, "check_boundary"), \
                patch.object(analysis_child, "install_mode_filter"), \
                patch.object(analysis_child, "_Bridge", return_value=contextlib.nullcontext(bridge)), \
                patch.object(analysis_child.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(body))), \
                patch.object(analysis_child.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as call:
            self.assertEqual(analysis_child.main(["web", "{}"]), 0)
        call.assert_called_once_with(sandbox.client_command("web"), input=body, env=sandbox.client_environment("web"),
                                     close_fds=True, timeout=110, check=False)
        bridge.shutdown.assert_called_once()

    def test_web_run_checks_transcript_before_return_and_never_retries(self):
        success = web_stream(web_events())
        error = web_stream([{"type": "result", "subtype": "success", "is_error": True,
                            "api_error_status": 429, "result": "SECRET"}])
        for code, native, errors, expected in ((0, success, b"", None), (1, error, b"", "rate-limit"),
                                              (0, b"invalid stream", b"", "result"),
                                              (1, error, b"CLIENT: REFUSED stage=boundary reason=sandbox", "sandbox")):
            proxy = Mock()
            proxy.begin_phase.return_value.is_set.return_value = False
            results = [(0, (contract.CLIENT_VERSION + " (Claude Code)").encode(), b""),
                       (0, json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                                       "configDirectory": sandbox.INNER_PROFILE}).encode(), b""),
                       (code, native, errors)]
            question = b'SYNTHETIC_PUBLIC_QUESTION'
            with self.subTest(expected=expected), patch.object(sandbox, "supervisor_ready"), \
                    patch.object(sandbox, "profile_lock", return_value=contextlib.nullcontext()), \
                    patch.object(sandbox, "namespace_command", return_value=["fixed-web"]) as command, \
                    patch.object(sandbox, "_Proxy", return_value=contextlib.nullcontext(proxy)), \
                    patch.object(sandbox, "bounded_process", side_effect=results) as process:
                if expected is None:
                    self.assertIn("Sources:", sandbox.run_client("web", question, mode="web"))
                else:
                    with self.assertRaises(AnalysisDenied) as caught:
                        sandbox.run_client("web", question, mode="web")
                    self.assertEqual(caught.exception.reason, expected)
            self.assertEqual(process.call_count, 3)
            self.assertEqual(command.call_args.args[0], "web")
            self.assertEqual(process.call_args.kwargs["input_bytes"], question)
            self.assertEqual(process.call_args.kwargs["limit"], 131072)
            self.assertEqual(process.call_args.kwargs["input_limit"], contract.MAX_PACKET_LARGE)
            self.assertNotIn(question.decode(), repr(command.call_args_list))
            proxy.shutdown.assert_called_once()

    def test_model_errors_partial_results_extra_turns_and_tool_denials_are_refused(self):
        base = {"type": "result", "subtype": "success", "is_error": False,
                "num_turns": 1, "permission_denials": [], "result": "wynik", "email": "PRIVATE"}
        self.assertEqual(sandbox.model_text(0, json.dumps(base).encode()), "wynik")
        for key, value in (("is_error", True), ("subtype", "error_max_turns"), ("num_turns", True),
                           ("num_turns", 2), ("permission_denials", ["Bash"]), ("result", ""),
                           ("result", "x" * (contract.MAX_TEXT + 1)), ("result", "\ud800")):
            with self.subTest(key=key), self.assertRaises(AnalysisDenied):
                sandbox.model_text(0, json.dumps(dict(base, **{key: value})).encode())
        with self.assertRaises(AnalysisDenied):
            sandbox.model_text(1, json.dumps(base).encode())
        with self.assertRaises(AnalysisDenied):
            sandbox.model_text(0, b'{"type":"result","type":"result"}')

    def test_large_valid_workspace_proposal_survives_unicode_and_nested_json_escaping(self):
        # Full 128 KiB replacements in 32 files. Native JSON commonly escapes
        # Unicode too, independently of our UTF-8 canonical proposal envelope.
        for unit in ('"\\\n\t', "zażółć😀", "ż"):
            size = len(unit.encode())
            content = unit * (4096 // size) + "x" * (4096 % size)
            source = {"instruction": "Zmien", "files": [
                {"path": f"file{index:02}.txt", "content": "", "writable": True} for index in range(32)]}
            value = {"summary": "Propozycja", "files": [
                {"path": item["path"], "content": content} for item in source["files"]]}
            proposal = contract.canonical(value)
            self.assertEqual(task_contract.proposal(proposal, source), value)
            self.assertEqual(sum(len(item["content"].encode()) for item in value["files"]), 131072)
            self.assertLessEqual(len(proposal.encode()), contract.MAX_PROPOSAL_TEXT)
            native = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                 "num_turns": 1, "permission_denials": [], "result": proposal}).encode()
            self.assertLessEqual(len(native), 768 * 1024)
            with self.subTest(unit=unit):
                result = sandbox.model_text(0, native, mode="workspace")
                self.assertEqual(result, proposal)
                framed = contract.result_packet("a" * 64, text=result, mode="workspace")
                self.assertLessEqual(len(framed), contract.MAX_RESPONSE_BYTES)
                self.assertEqual(contract.read_result(framed, "a" * 64, mode="workspace"), proposal)
                self.assertEqual(task_contract.proposal(contract.read_result(framed, "a" * 64,
                                                                            mode="workspace"), source), value)
                with self.assertRaises(AnalysisDenied):
                    sandbox.model_text(0, native)
                with self.assertRaises(AnalysisDenied):
                    contract.read_result(framed, "a" * 64)

    def test_response_text_budget_depends_only_on_validated_request_mode(self):
        base = {"type": "result", "subtype": "success", "is_error": False,
                "num_turns": 1, "permission_denials": []}
        for mode, maximum in (("analysis", contract.MAX_TEXT), ("question", contract.MAX_TEXT), ("assistant", contract.MAX_TEXT),
                              ("workspace", contract.MAX_PROPOSAL_TEXT)):
            with self.subTest(mode=mode):
                text = "x" * maximum
                self.assertEqual(sandbox.model_text(0, json.dumps(dict(base, result=text)).encode(), mode=mode), text)
                packet = contract.result_packet("a" * 64, text=text, mode=mode)
                self.assertEqual(contract.read_result(packet, "a" * 64, mode=mode), text)
                with self.assertRaises(AnalysisDenied):
                    sandbox.model_text(0, json.dumps(dict(base, result=text + "x")).encode(), mode=mode)
                with self.assertRaises(AnalysisDenied):
                    contract.result_packet("a" * 64, text=text + "x", mode=mode)
        for mode in ("unknown", None, True, {}):
            with self.subTest(mode=mode), self.assertRaises(AnalysisDenied):
                sandbox.model_text(0, json.dumps(dict(base, result="ok")).encode(), mode=mode)

    def test_native_pipe_limits_are_explicit_and_data_never_enters_argv(self):
        for mode, output_limit in (("question", 256 * 1024), ("workspace", 3 * contract.MAX_PACKET_LARGE)):
            proxy = Mock()
            proxy.begin_phase.return_value.is_set.return_value = False
            native = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                 "num_turns": 1, "permission_denials": [], "result": "wynik"}).encode()
            results = [(0, (contract.CLIENT_VERSION + " (Claude Code)").encode(), b""),
                       (0, json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                                       "configDirectory": sandbox.INNER_PROFILE}).encode(), b""),
                       (0, native, b"")]
            source = b"SYNTHETIC_INPUT" * 20000
            with self.subTest(mode=mode), \
                    patch.object(sandbox, "supervisor_ready"), \
                    patch.object(sandbox, "profile_lock", return_value=contextlib.nullcontext()), \
                    patch.object(sandbox, "namespace_command", return_value=["fixed-namespace"]) as command, \
                    patch.object(sandbox, "_Proxy", return_value=contextlib.nullcontext(proxy)), \
                    patch.object(sandbox, "bounded_process", side_effect=results) as process:
                self.assertEqual(sandbox.run_client("analyze", source, mode=mode), "wynik")
            self.assertEqual(process.call_count, 3)
            final = process.call_args
            self.assertEqual(final.args, (["fixed-namespace"],))
            self.assertEqual(final.kwargs["input_bytes"], source)
            self.assertEqual(final.kwargs["input_limit"], contract.MAX_PACKET_LARGE)
            self.assertEqual(final.kwargs["limit"], output_limit)
            self.assertNotIn("SYNTHETIC_INPUT", repr(command.call_args_list))
            proxy.shutdown.assert_called_once()

    def test_login_requires_flag_root_and_tty_before_any_command(self):
        with patch.object(admin, "runtime_ready") as ready, patch.object(admin.subprocess, "run") as run:
            for allow, uid, tty in ((False, 0, True), (True, 1000, True), (True, 0, False)):
                with patch.object(admin.os, "getuid", return_value=uid), \
                        patch.object(admin.os, "geteuid", return_value=uid), \
                        patch.object(admin.os, "isatty", return_value=tty):
                    self.assertEqual(admin.main(allow=allow), 2)
            ready.assert_not_called()
            run.assert_not_called()

    def test_login_reuses_installed_service_confinement_and_fixed_command(self):
        unit = Path(__file__).resolve().parents[1] / "packaging/systemd/ai-desktop-analysis@.service"
        with patch.object(admin, "UNIT", unit), patch.object(admin, "trusted_file"):
            command = admin.login_command()
        for value in ("--property=User=ai-desktop", "--property=NoNewPrivileges=yes",
                      "--property=CapabilityBoundingSet=", "--property=ProtectHome=yes",
                      "--property=KillMode=control-group", "--property=RuntimeMaxSec=600"):
            self.assertIn(value, command)
        self.assertNotIn("--property=StandardOutput=null", command)
        self.assertEqual(command[-5:], ["/usr/bin/python3", "-I", "-B", "/usr/lib/ai-desktop/analysis-worker.py", "login"])

    def test_login_cancellation_stops_only_its_own_unit_and_checks_cleanup(self):
        unit = Path(__file__).resolve().parents[1] / "packaging/systemd/ai-desktop-analysis@.service"
        calls = []
        def invoke(command, **kwargs):
            calls.append(command)
            if command[0] == "/usr/bin/systemd-run":
                raise KeyboardInterrupt()
            if command[1] == "stop":
                return SimpleNamespace(returncode=0)
            return SimpleNamespace(returncode=0, stdout="LoadState=not-found\nActiveState=inactive\n")
        with patch.object(admin, "UNIT", unit), patch.object(admin, "trusted_file"), \
                patch.object(admin, "runtime_ready"), patch.object(admin.os, "geteuid", return_value=0), \
                patch.object(admin.os, "getuid", return_value=0), patch.object(admin.os, "isatty", return_value=True), \
                patch.object(admin.subprocess, "run", side_effect=invoke):
            self.assertEqual(admin.main(allow=True), 2)
        generated = next(a[len("--unit="):] for a in calls[0] if a.startswith("--unit=")) + ".service"
        self.assertRegex(generated, r"^ai-desktop-login-[0-9a-f]{16}\.service$")
        self.assertEqual(calls[1], ["/usr/bin/systemctl", "stop", generated])
        self.assertEqual(calls[2][:3], ["/usr/bin/systemctl", "show", generated])
