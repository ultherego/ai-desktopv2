"""Synthetic-only validation of the opt-in HOME acceptance proof."""
from contextlib import ExitStack, redirect_stdout
import io
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

import host_home_policy_acceptance as host


class HomeHarnessGuards(unittest.TestCase):
    def test_opt_in_identity_and_tty_precede_setup(self):
        with redirect_stdout(io.StringIO()), patch.object(host.prior, "preflight") as ready, \
                patch.object(host, "create_fixture") as create, patch.object(host.prior, "run") as run:
            for args in ([], [host.FLAG, "extra"], ["--allow-subscription-ask-edit-test"]):
                self.assertEqual(host.main(args), 2)
            for uid, euid, tty in ((0, 0, True), (1000, 1001, True), (1000, 1000, False)):
                with patch.object(host.os, "getuid", return_value=uid), patch.object(host.os, "geteuid", return_value=euid), \
                        patch.object(host.os, "isatty", return_value=tty):
                    self.assertEqual(host.main([host.FLAG]), 2)
            ready.assert_not_called()
            create.assert_not_called()
            run.assert_not_called()

    def exercise(self, failure=None):
        fixture = {"script": Path("/synthetic/home-fixture.py"), "home": Path("/synthetic")}
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
            stack.enter_context(patch.object(host.task_workspace, "status", side_effect=[[], [("plan", "applied")], [("plan", "rolled-back")]]))
            run = stack.enter_context(patch.object(host.prior, "run",
                side_effect=[ValueError("PRIVATE")] if failure == "native" else [None, ValueError("PRIVATE")] if failure == "rollback" else None))
            stopped = stack.enter_context(patch.object(host.prior, "check_stopped",
                side_effect=[None, None, None, ValueError("PRIVATE")] if failure == "cleanup" else None))
            code = host.main([host.FLAG])
            temporary.assert_called_once_with(prefix="ai-home-policy-", dir="/home/user")
        return code, output.getvalue(), rejected, states, run, stopped

    def test_success_has_only_new_native_question_and_manual_rollback(self):
        code, output, rejected, states, run, stopped = self.exercise()
        self.assertEqual(code, 0, output)
        rejected.assert_called_once()
        self.assertEqual([call.args for call in run.call_args_list], [
            ("/usr/bin/python3", "-I", "-B", "/synthetic/home-fixture.py"),
            ("/usr/bin/ai-desktop", "task", "rollback", "--workspace", "/synthetic")])
        self.assertNotIn("capture_output", run.call_args_list[0].kwargs)
        self.assertNotIn("input", run.call_args_list[0].kwargs)
        self.assertEqual([call.kwargs for call in states.call_args_list], [{}, {"applied": True}, {}])
        self.assertEqual(stopped.call_count, 4)
        self.assertIn("HOST HOME: PASS", output)
        self.assertIn("actual-user-policy=NOT-TESTED", output)

    def test_native_rollback_and_cleanup_failures_never_pass_or_retry(self):
        for failure in ("native", "rollback", "cleanup", "fixture-cleanup"):
            with self.subTest(failure=failure):
                code, output, _, _, run, _ = self.exercise(failure)
                self.assertEqual(code, 2)
                self.assertNotIn("PRIVATE", output)
                self.assertNotIn("HOST HOME: PASS", output)
                self.assertEqual(run.call_count, 1 if failure == "native" else 2)


class HomeFixtureTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return host.create_fixture(temporary.name)

    def test_fixture_has_broad_grants_but_narrow_target_and_over_limit_noise(self):
        fixture = self.fixture()
        host.check_state(fixture)
        policy = tomllib.loads(fixture["source"][".config/ai-desktop/permissions.toml"])
        self.assertEqual(policy["version"], 2)
        self.assertEqual(policy["files"], {"read": ["~/"], "write": ["~/"], "deny": ["~/private/", "~/.ssh/"]})
        self.assertEqual(len(host.NOISE), 80)
        self.assertEqual(len(host.DENIED), 2)
        self.assertNotIn(host.MARKER, host.QUESTION)
        self.assertFalse((fixture["home"] / "test/hello.py").exists())
        compile(fixture["script"].read_text(), "home-fixture", "exec")
        compile(host.CREATED, "hello.py", "exec")

    def test_created_state_requires_exact_new_file_and_all_other_files_unchanged(self):
        fixture = self.fixture()
        target = fixture["home"] / "test/hello.py"
        host._new_file(target, host.CREATED)
        host.check_state(fixture, applied=True)
        for name in ("test/context.txt", "noise/000.txt", "private/secret.txt", ".ssh/fixture.txt"):
            path = fixture["home"] / name
            path.write_text("tampered")
            with self.subTest(name=name), self.assertRaises(ValueError):
                host.check_state(fixture, applied=True)
            path.write_text(fixture["source"][name])
        target.unlink()
        host.check_state(fixture)

    @unittest.skipIf(os.geteuid() == 0, "real policy and workspace refuse root")
    def test_real_home_policy_narrow_selection_and_deny_preflight_without_native(self):
        from ai_desktop import ask_cli, cli, permissions
        with tempfile.TemporaryDirectory(prefix="ai-home-preflight-", dir=pwd.getpwuid(os.geteuid()).pw_dir) as home:
            fixture = host.create_fixture(home)
            with patch.object(permissions, "_home", return_value=home), \
                    patch.object(sys, "argv", ["fixture", "policy-check"]), patch.object(sys, "path", list(sys.path)), \
                    patch.object(cli, "main") as native, patch.object(ask_cli, "request") as request, \
                    redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as result:
                exec(compile(fixture["script"].read_text(), "home-policy", "exec"), {"__name__": "__main__"})
            self.assertEqual(result.exception.code, 0)
            self.assertEqual(output.getvalue(), host.POLICY_MARKER + "\n")
            native.assert_not_called()
            request.assert_not_called()
            host.check_state(fixture)

    def simulate(self, behavior):
        fixture = self.fixture()
        package = ModuleType("ai_desktop")
        package.permissions = SimpleNamespace(read_selected=Mock())
        package.ask_cli = SimpleNamespace(request=Mock(return_value="synthetic response"))
        package.terminal_consent = SimpleNamespace(fresh_input=Mock(return_value="yes"),
                                                   PROMPTS={"ask-question": "QUESTION", "write": "WRITE"})
        package.task_cli = SimpleNamespace(task_sandbox=SimpleNamespace(run=Mock(return_value={"passed": True})),
                                          show_diff=Mock(), workspace=SimpleNamespace(apply=Mock()))
        source = {"instruction": host.QUESTION, "files": [
            {"path": "test/context.txt", "content": host.CONTEXT, "writable": True},
            {"path": "test/hello.py", "content": None, "writable": True}]}
        proposed = [{"path": "test/hello.py", "content": host.CREATED}]
        plan = SimpleNamespace(data=lambda: {"proposal": {"files": proposed}, "before": {"workspace": str(fixture["home"])}})
        def main(_):
            package.terminal_consent.fresh_input("QUESTION")
            if behavior == "extra-approval":
                package.terminal_consent.fresh_input("QUESTION")
            package.ask_cli.request(access={"files": [{"id": "f0", "pattern": "~/"}], "write": [{"id": "w0", "pattern": "~/"}]})
            paths = [str(fixture["home"] / ("noise/000.txt" if behavior == "wide-read" else "test/context.txt"))]
            package.permissions.read_selected(None, paths)
            if behavior == "repeat-read":
                package.permissions.read_selected(None, paths)
            if behavior == "extra-file":
                source["files"].append({"path": "noise/000.txt", "content": "noise", "writable": True})
            package.ask_cli.request(task=source)
            if behavior == "changed-context":
                proposed.append({"path": "test/context.txt", "content": "tampered"})
            package.task_cli.task_sandbox.run(source, proposed, "text" if behavior == "wrong-check" else "python-syntax")
            if behavior != "no-diff":
                package.task_cli.show_diff(plan)
            if behavior != "no-write-consent":
                package.terminal_consent.fresh_input("WRITE")
            package.task_cli.workspace.apply(plan, object())
            return 0
        package.cli = SimpleNamespace(main=main)
        with patch.dict(sys.modules, {"ai_desktop": package}), patch.object(sys, "argv", ["fixture"]), \
                patch.object(sys, "path", list(sys.path)), redirect_stdout(io.StringIO()) as output:
            try:
                exec(compile(fixture["script"].read_text(), "home-fixture", "exec"), {"__name__": "__main__"})
            except SystemExit as exc:
                code = exc.code
            except AssertionError:
                code = 2
        return code, output.getvalue()

    def test_native_proof_cannot_hide_extra_reads_files_mutations_or_missing_consent(self):
        for behavior in ("full", "extra-approval", "wide-read", "repeat-read", "extra-file", "changed-context",
                         "wrong-check", "no-diff", "no-write-consent"):
            with self.subTest(behavior=behavior):
                code, output = self.simulate(behavior)
                self.assertEqual(code, 0 if behavior == "full" else 2)
                self.assertEqual(host.NATIVE_MARKER in output, behavior == "full")

    def test_upfront_refusal_cannot_hide_request_read_or_write(self):
        fixture = self.fixture()
        source = compile(fixture["script"].read_text(), "home-fixture", "exec")
        for behavior in ("refuse", "request", "read", "write"):
            with self.subTest(behavior=behavior):
                package = ModuleType("ai_desktop")
                package.permissions = SimpleNamespace(read_selected=Mock())
                package.ask_cli = SimpleNamespace(request=Mock())
                package.terminal_consent = SimpleNamespace(fresh_input=Mock(return_value="no"),
                    PROMPTS={"ask-question": "QUESTION", "write": "WRITE"})
                package.task_cli = SimpleNamespace(task_sandbox=SimpleNamespace(run=Mock()),
                    show_diff=Mock(), workspace=SimpleNamespace(apply=Mock()))
                def main(_):
                    package.terminal_consent.fresh_input("QUESTION")
                    if behavior == "request":
                        package.ask_cli.request(access={"files": [{"id": "f0", "pattern": "~/"}], "write": [{"id": "w0", "pattern": "~/"}]})
                    elif behavior == "read":
                        package.permissions.read_selected(None, ["unused"])
                    elif behavior == "write":
                        package.task_cli.workspace.apply(None, None)
                    return 2
                package.cli = SimpleNamespace(main=main)
                with patch.dict(sys.modules, {"ai_desktop": package}), patch.object(sys, "argv", ["fixture", "refusal"]), \
                        patch.object(sys, "path", list(sys.path)), redirect_stdout(io.StringIO()) as output:
                    try:
                        exec(source, {"__name__": "__main__"})
                    except (SystemExit, AssertionError):
                        pass
                self.assertEqual(host.REFUSAL_MARKER in output.getvalue(), behavior == "refuse")


if __name__ == "__main__":
    unittest.main()
