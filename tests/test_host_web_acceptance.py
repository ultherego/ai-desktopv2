"""Synthetic-only checks for the native WebSearch acceptance harness."""
from contextlib import ExitStack, contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import tomllib
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import host_web_acceptance as host


class WebHarnessGuards(unittest.TestCase):
    def test_opt_in_identity_and_tty_precede_setup(self):
        with redirect_stdout(io.StringIO()), patch.object(host.prior, "preflight") as ready, \
                patch.object(host, "create_fixture") as create, patch.object(host.prior, "run") as run:
            for args in ([], [host.FLAG, "extra"], ["--allow-subscription-home-test"]):
                self.assertEqual(host.main(args), 2)
            for uid, euid, tty in ((0, 0, True), (1000, 1001, True), (1000, 1000, False)):
                with patch.object(host.os, "getuid", return_value=uid), patch.object(host.os, "geteuid", return_value=euid), \
                        patch.object(host.os, "isatty", return_value=tty):
                    self.assertEqual(host.main([host.FLAG]), 2)
            ready.assert_not_called()
            create.assert_not_called()
            run.assert_not_called()

    def exercise(self, failure=None):
        fixture = {"script": Path("/synthetic/web-fixture.py"), "home": Path("/synthetic")}
        with ExitStack() as stack:
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            for name in ("getuid", "geteuid"):
                stack.enter_context(patch.object(host.os, name, return_value=1000))
            stack.enter_context(patch.object(host.os, "isatty", return_value=True))
            stack.enter_context(patch.object(host.prior, "preflight", return_value=(1000, ["socket", "service"])))
            stack.enter_context(patch.object(host.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir="/home/user")))
            temporary = stack.enter_context(patch.object(host.tempfile, "TemporaryDirectory"))
            temporary.return_value.__enter__.return_value = "/synthetic"
            if failure == "fixture-cleanup":
                temporary.return_value.__exit__.side_effect = OSError("PRIVATE")
            stack.enter_context(patch.object(host, "create_fixture", return_value=fixture))
            stack.enter_context(patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess([], 0,
                stdout=(host.POLICY_MARKER + "\n").encode(), stderr=b"")))
            rejected = stack.enter_context(patch.object(host, "reject"))
            states = stack.enter_context(patch.object(host, "check_state"))
            run = stack.enter_context(patch.object(host.prior, "run",
                side_effect=ValueError("PRIVATE") if failure == "native" else None))
            stopped = stack.enter_context(patch.object(host.prior, "check_stopped",
                side_effect=[None, None, ValueError("PRIVATE")] if failure == "cleanup" else None))
            code = host.main([host.FLAG])
            temporary.assert_called_once_with(prefix="ai-web-policy-", dir="/home/user")
        return code, output.getvalue(), rejected, states, run, stopped

    def test_success_has_one_uncaptured_native_question_and_no_retry(self):
        code, output, rejected, states, run, stopped = self.exercise()
        self.assertEqual(code, 0, output)
        rejected.assert_called_once()
        self.assertEqual([call.args for call in run.call_args_list], [
            ("/usr/bin/python3", "-I", "-B", "/synthetic/web-fixture.py")])
        self.assertNotIn("capture_output", run.call_args.kwargs)
        self.assertNotIn("input", run.call_args.kwargs)
        self.assertEqual(states.call_count, 2)
        self.assertEqual(stopped.call_count, 3)
        self.assertIn("HOST WEB: PASS", output)
        self.assertIn("actual-user-policy=NOT-TESTED", output)

    def test_native_and_cleanup_failures_never_pass_or_retry(self):
        for failure in ("native", "cleanup", "fixture-cleanup"):
            with self.subTest(failure=failure):
                code, output, _, _, run, _ = self.exercise(failure)
                self.assertEqual(code, 2)
                self.assertNotIn("PRIVATE", output)
                self.assertNotIn("HOST WEB: PASS", output)
                self.assertEqual(run.call_count, 1)


class WebFixtureTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return host.create_fixture(temporary.name)

    def test_fixture_grants_only_claude_and_web_and_rejects_mutation(self):
        fixture = self.fixture()
        host.check_state(fixture)
        policy = tomllib.loads(fixture["source"][".config/ai-desktop/permissions.toml"])
        self.assertEqual(policy, {"version": 2, "network": {"claude": True, "web": True, "weather": False},
            "system": {"systemd": []}, "files": {"read": [], "write": [], "deny": []}})
        compile(fixture["script"].read_text(), "web-fixture", "exec")
        target = fixture["home"] / "unexpected.txt"
        host._new_file(target, "unexpected")
        with self.assertRaises(ValueError):
            host.check_state(fixture)
        target.unlink()
        target = fixture["home"] / ".config/ai-desktop/permissions.toml"
        target.write_text("changed")
        with self.assertRaises(ValueError):
            host.check_state(fixture)

    @unittest.skipIf(os.geteuid() == 0, "real policy refuses root")
    def test_real_policy_preflight_never_contacts_native(self):
        from ai_desktop import agent_profiles, ask_cli, cli, permissions
        with tempfile.TemporaryDirectory(prefix="ai-web-preflight-", dir=pwd.getpwuid(os.geteuid()).pw_dir) as home:
            fixture = host.create_fixture(home)
            with patch.object(permissions, "_home", return_value=home), \
                    patch.object(agent_profiles, "Store", agent_profiles.Store), \
                    patch.object(permissions, "_read", wraps=permissions._read), \
                    patch.object(sys, "argv", ["fixture", "policy-check"]), patch.object(sys, "path", list(sys.path)), \
                    patch.object(cli, "main") as native, patch.object(ask_cli, "request") as request, \
                    redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as result:
                exec(compile(fixture["script"].read_text(), "web-policy", "exec"), {"__name__": "__main__"})
            self.assertEqual(result.exception.code, 0)
            self.assertEqual(output.getvalue(), host.POLICY_MARKER + "\n")
            native.assert_not_called()
            request.assert_not_called()
            host.check_state(fixture)

    def simulate(self, behavior, *, refusal=False):
        from ai_desktop import agent_profiles
        fixture = self.fixture()
        access = {"files": [], "systemd": [], "weather": False, "web": True}
        profile = agent_profiles.defaults().profiles["default"]
        controls = {"provider": profile.provider, "model": profile.model, "effort": profile.effort,
                    "instructions": agent_profiles.catalog_for(profile), "profile": "default", "access": profile.access}
        package = ModuleType("ai_desktop")
        profile_store = Mock()
        def resolve():
            package.agent_profiles.Store()
            return dict(controls)
        package.agent_profiles = SimpleNamespace(Store=profile_store, resolve=resolve)
        policy = SimpleNamespace(access=lambda: access, sources=lambda: {
            "claude": True, "weather": False, "systemd": [], "web": True}, recheck=Mock())
        package.permissions = SimpleNamespace(_read=Mock(), read_selected=Mock(), load_policy=lambda: policy)
        package.terminal_consent = SimpleNamespace(fresh_input=Mock(return_value="no" if refusal else "yes"),
            PROMPTS={"ask-question": "QUESTION"})
        package.assistant_flow = SimpleNamespace(collect=Mock())
        package.file_context = SimpleNamespace(collect=Mock())
        package.ask_context = SimpleNamespace(_systemd=Mock(), _weather=Mock())
        package.ask_edit = SimpleNamespace(prepare=Mock())
        @contextmanager
        def original_session(**kwargs):
            yield
        def original_send(approval):
            data = json.loads(approval.payload)
            if data["mode"] == "assistant":
                return json.dumps({"action": "web"})
            if behavior == "no-proof":
                return "I searched, https://www.python.org/downloads/"
            if behavior == "wrong-source":
                return "Answer\nSources:\n[1] Title — https://example.com/"
            if behavior == "malformed-source":
                return "Answer\nSources:\n[1] Python downloads — ``https://www.python.org/downloads/``"
            return "Answer\nSources:\n[1] Python downloads — `https://www.python.org/downloads/`"
        def original_request(question, args, reader, **kwargs):
            data = {"mode": "web" if kwargs.get("web") else "assistant", "question": question,
                    "execution": dict(controls)}
            if data["mode"] == "assistant":
                data["access"] = kwargs["access"]
                if behavior == "planner-knowledge":
                    data["knowledge"] = {"resources": []}
            if behavior == "extra-payload" and data["mode"] == "web":
                data["context"] = ["PRIVATE LOCAL DATA"]
            if data["mode"] == "web":
                if behavior == "missing-execution":
                    del data["execution"]
                elif behavior == "changed-execution":
                    data["execution"]["instructions"] += "UNAPPROVED"
                elif behavior == "changed-provider":
                    data["execution"]["provider"] = "codex"
                elif behavior == "extra-execution-field":
                    data["execution"]["tools"] = "Bash"
                elif behavior in ("knowledge", "conversation", "memory"):
                    data[behavior] = {"local": "PRIVATE LOCAL DATA"}
            approval = SimpleNamespace(payload=json.dumps(data), sha256="digest")
            if behavior == "no-session":
                return package.ask_cli.send_once(approval)
            with package.ask_cli.managed_session():
                return package.ask_cli.send_once(approval)
        package.ask_cli = SimpleNamespace(request=original_request, send_once=original_send,
                                         managed_session=original_session)
        def main(_):
            package.terminal_consent.fresh_input("QUESTION")
            if behavior == "extra-approval":
                package.terminal_consent.fresh_input("QUESTION")
            if behavior == "local-read":
                package.permissions.read_selected(None, ["private"])
            if behavior == "collector":
                package.assistant_flow.collect(None, None, None, None)
            if refusal and behavior == "full":
                return 2
            package.ask_cli.request(host.QUESTION, None, None, access=access)
            if behavior != "missing-web":
                package.ask_cli.request(host.QUESTION, None, None, web=True,
                    context=[] if behavior == "context" else None)
            if behavior == "extra-session":
                with package.ask_cli.managed_session():
                    pass
            return 0
        package.cli = SimpleNamespace(main=main)
        contract = ModuleType("ai_desktop.analysis_contract")
        contract.parse_request = lambda payload: (payload.decode(), "digest")
        with patch.dict(sys.modules, {"ai_desktop": package, "ai_desktop.analysis_contract": contract}), \
                patch.object(sys, "argv", ["fixture"] + (["refusal"] if refusal else [])), \
                patch.object(sys, "path", list(sys.path)), redirect_stdout(io.StringIO()) as output:
            try:
                exec(compile(fixture["script"].read_text(), "web-fixture", "exec"), {"__name__": "__main__"})
            except SystemExit as exc:
                code = exc.code
            except AssertionError:
                code = 2
        profile_store.assert_called_once_with(home=fixture["home"])
        return code, output.getvalue()

    def test_native_proof_rejects_extra_payload_reads_sessions_and_missing_sources(self):
        for behavior in ("full", "extra-approval", "local-read", "collector", "extra-payload", "no-session",
                         "missing-web", "context", "extra-session", "no-proof", "wrong-source", "malformed-source",
                         "missing-execution", "changed-execution", "changed-provider", "extra-execution-field",
                         "planner-knowledge", "knowledge", "conversation", "memory"):
            with self.subTest(behavior=behavior):
                code, output = self.simulate(behavior)
                self.assertEqual(code, 0 if behavior == "full" else 2)
                self.assertEqual(host.NATIVE_MARKER in output, behavior == "full")

    def test_refusal_never_permits_requests_or_collectors(self):
        for behavior in ("full", "request", "local-read", "collector"):
            with self.subTest(behavior=behavior):
                code, output = self.simulate(behavior, refusal=True)
                self.assertEqual(code, 2)
                self.assertEqual(host.REFUSAL_MARKER in output, behavior == "full")


if __name__ == "__main__":
    unittest.main()
