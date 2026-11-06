"""Synthetic-only guards and proof validation for the native ask-edit harness."""
from contextlib import ExitStack, chdir, redirect_stdout
import io
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
import tomllib
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import host_ask_edit_acceptance as host


class HostAskEditGuards(unittest.TestCase):
    def test_opt_in_identity_and_tty_precede_every_host_operation(self):
        with redirect_stdout(io.StringIO()), patch.object(host.prior, "preflight") as ready, \
                patch.object(host, "create_fixture") as create, patch.object(host.prior, "run") as run:
            for args in ([], [host.FLAG, "extra"], ["--allow-subscription-file-test"]):
                self.assertEqual(host.main(args), 2)
            for uid, euid, tty in ((0, 0, True), (1000, 1001, True), (1000, 1000, False)):
                with patch.object(host.os, "getuid", return_value=uid), patch.object(host.os, "geteuid", return_value=euid), \
                        patch.object(host.os, "isatty", return_value=tty):
                    self.assertEqual(host.main([host.FLAG]), 2)
            ready.assert_not_called()
            create.assert_not_called()
            run.assert_not_called()

    def exercise(self, *, failure=None):
        fixture = {"script": Path("/synthetic/script.py"), "project": Path("/synthetic/project")}
        with ExitStack() as stack:
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            for name in ("getuid", "geteuid"):
                stack.enter_context(patch.object(host.os, name, return_value=1000))
            stack.enter_context(patch.object(host.os, "isatty", return_value=True))
            ready = stack.enter_context(patch.object(host.prior, "preflight", return_value=(1000, ["socket", "service"])))
            stack.enter_context(patch.object(host.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir="/home/synthetic")))
            temporary = stack.enter_context(patch.object(host.tempfile, "TemporaryDirectory"))
            temporary.return_value.__enter__.return_value = "/synthetic"
            if failure == "fixture-cleanup":
                temporary.return_value.__exit__.side_effect = [OSError("PRIVATE"), False]
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
            self.assertEqual(temporary.call_args_list[0].kwargs,
                             {"prefix": "ai-edit-policy-", "dir": "/home/synthetic"})
            self.assertEqual(temporary.call_args_list[1].kwargs,
                             {"prefix": "ai-edit-data-", "dir": "/synthetic"})
        return code, output.getvalue(), ready, rejected, states, run, stopped

    def test_success_has_one_native_ask_then_manual_public_rollback(self):
        code, output, ready, rejected, states, run, stopped = self.exercise()
        self.assertEqual(code, 0, output)
        ready.assert_called_once()
        rejected.assert_called_once()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args, ("/usr/bin/python3", "-I", "-B", "/synthetic/script.py"))
        self.assertEqual(run.call_args_list[1].args,
                         ("/usr/bin/ai-desktop", "task", "rollback", "--workspace", "/synthetic/project"))
        self.assertNotIn("capture_output", run.call_args_list[0].kwargs)
        self.assertNotIn("input", run.call_args_list[0].kwargs)
        self.assertEqual([call.kwargs for call in states.call_args_list], [{}, {"applied": True}, {}])
        self.assertEqual(stopped.call_count, 4)
        self.assertIn("HOST ASK EDIT: PASS", output)
        self.assertIn("actual-user-policy=NOT-TESTED", output)

    def test_failure_or_cleanup_failure_never_becomes_pass_or_retry(self):
        for failure in ("native", "rollback", "cleanup", "fixture-cleanup"):
            with self.subTest(failure=failure):
                code, output, _, _, _, run, _ = self.exercise(failure=failure)
                self.assertEqual(code, 2)
                self.assertNotIn("PRIVATE", output)
                self.assertNotIn("HOST ASK EDIT: PASS", output)
                self.assertEqual(run.call_count, 1 if failure == "native" else 2)


class AskEditFixtureTests(unittest.TestCase):
    @unittest.skipIf(os.geteuid() == 0, "real policy and workspace correctly refuse root")
    def test_real_policy_and_snapshot_preflight_under_private_desktop_home(self):
        from ai_desktop import ask_cli, cli, permissions
        desktop_home = pwd.getpwuid(os.geteuid()).pw_dir
        with tempfile.TemporaryDirectory(prefix="ai-edit-policy-test-", dir=desktop_home) as home, \
                tempfile.TemporaryDirectory(prefix="ai-edit-data-", dir=home) as data:
            fixture = host.create_fixture(home, data)
            code = compile(fixture["script"].read_text(), "ask-edit-policy-check", "exec")
            # Only the HOME lookup changes; production UID, owner, path and
            # ancestor checks remain active, including the secure write root.
            with patch.object(permissions, "_home", return_value=home), \
                    patch.object(sys, "argv", ["fixture", "policy-check"]), \
                    patch.object(sys, "path", list(sys.path)), \
                    patch.object(cli, "main") as native_cli, \
                    patch.object(ask_cli, "request") as request, \
                    patch.object(host.task_workspace, "snapshot", wraps=host.task_workspace.snapshot) as snapshot:
                with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as result:
                    exec(code, {"__name__": "__main__"})
                self.assertEqual(result.exception.code, 0)
                self.assertEqual(output.getvalue(), host.POLICY_MARKER + "\n")
                snapshot.assert_called_once_with(str(fixture["project"]), [*host.FILES, "result.txt"],
                                                 must_be_absent=["result.txt"])
                host.check_state(fixture)
                # A target appearing before the native run must fail preflight.
                host._new_file(fixture["project"] / "result.txt", "already exists\n")
                with redirect_stdout(io.StringIO()) as refused, self.assertRaises(ValueError):
                    exec(code, {"__name__": "__main__"})
                self.assertEqual(refused.getvalue(), "")
                native_cli.assert_not_called()
                request.assert_not_called()

    def fixture(self):
        home = tempfile.TemporaryDirectory()
        data = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.addCleanup(data.cleanup)
        return host.create_fixture(home.name, data.name)

    def test_nine_files_nested_readonly_context_large_transport_and_private_policy(self):
        fixture = self.fixture()
        host.check_state(fixture)
        self.assertEqual(len(host.FILES), 9)
        self.assertEqual(len(host.ORIGINAL_MAIN.encode()), 9222)
        self.assertEqual(len(host.LARGE_CONTEXT.encode()), 40960)
        self.assertGreater(host.LARGE_CONTEXT.index(host.TAIL_MARKER), 32768)
        self.assertNotIn(host.TAIL_MARKER, host.QUESTION)
        self.assertGreater(sum(len(value.encode()) for value in host.FILES.values()), 32768)
        policy = tomllib.loads(fixture["policy"].read_text())
        self.assertEqual(policy["files"]["read"], [str(fixture["project"]) + "/**"])
        self.assertEqual(policy["files"]["write"], [str(fixture["project"]) + "/*"])
        self.assertEqual(policy["files"]["deny"], [str(fixture["project"] / "denied.txt")])
        for path in (fixture["script"], fixture["policy"], *(fixture["project"] / name for name in host.FILES)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        compile(fixture["script"].read_text(), "ask-edit-fixture", "exec")
        compile(host.TEST_SOURCE, "test_main.py", "exec")
        self.assertFalse((fixture["project"] / "result.txt").exists())
        self.assertIn("permissions._home = lambda uid:", fixture["script"].read_text())

    def test_applied_and_rollback_checks_require_exact_changes_and_preserved_context(self):
        fixture = self.fixture()
        project = fixture["project"]
        (project / "main.py").write_text(host.EDITED_MAIN)
        host._new_file(project / "result.txt", host.TAIL_MARKER + "\n")
        host.check_state(fixture, applied=True)
        (project / "context/02.txt").write_text("tampered")
        with self.assertRaises(ValueError):
            host.check_state(fixture, applied=True)
        (project / "context/02.txt").write_text(host.FILES["context/02.txt"])
        (project / "main.py").write_text(host.ORIGINAL_MAIN)
        (project / "result.txt").unlink()
        host.check_state(fixture)
        host._new_file(project / "unexpected.txt", "extra")
        with self.assertRaises(ValueError):
            host.check_state(fixture)

    def simulate_wrapper(self, behavior):
        fixture = self.fixture()
        source = {"instruction": "synthetic", "files": [
            {"path": name, "content": content, "writable": name == "main.py"} for name, content in host.FILES.items()]
            + [{"path": "result.txt", "content": None, "writable": True}]}
        proposed = [{"path": "main.py", "content": host.EDITED_MAIN},
                    {"path": "result.txt", "content": host.TAIL_MARKER + "\n"}]
        package = ModuleType("ai_desktop")
        package.permissions = SimpleNamespace()
        package.ask_cli = SimpleNamespace(request=Mock(return_value="synthetic response"))
        package.terminal_consent = SimpleNamespace(fresh_input=Mock(return_value="yes"),
            PROMPTS={"ask-question": "QUESTION", "project-test": "TEST", "write": "WRITE"})
        package.task_cli = SimpleNamespace(task_sandbox=SimpleNamespace(run=Mock(),
            run_copy=Mock(return_value={"passed": behavior != "test-failed"})),
                                          show_diff=Mock(), workspace=SimpleNamespace(apply=Mock()))
        contract = ModuleType("ai_desktop.analysis_contract")
        contract.application_input = Mock(return_value="x" * (100 if behavior == "small-request" else 40000))
        plan = SimpleNamespace(data=lambda: {"proposal": {"files": proposed},
                                            "before": {"workspace": str(fixture["project"])}})
        def main(_):
            self.assertEqual(Path.cwd(), fixture["project"])
            package.terminal_consent.fresh_input("QUESTION")
            if behavior == "extra-approval":
                package.terminal_consent.fresh_input("QUESTION")
            package.ask_cli.request(access={"files": []})
            if behavior == "truncated":
                source["files"] = source["files"][:8]
            package.ask_cli.request(task=source)
            if behavior != "no-test-consent":
                package.terminal_consent.fresh_input("TEST")
            if behavior == "extra-test-consent":
                package.terminal_consent.fresh_input("TEST")
            files = {**host.FILES, **{item["path"]: item["content"] for item in proposed}}
            if behavior == "copy-missing-file":
                files.pop("tests/test_main.py")
            elif behavior == "copy-wrong-overlay":
                files["main.py"] = host.ORIGINAL_MAIN
            elif behavior == "copy-denied":
                files["denied.txt"] = host.DENIED
            if behavior != "no-test":
                package.task_cli.task_sandbox.run_copy(files,
                    "text" if behavior == "wrong-check" else "python-unittest",
                    executable=["main.py"] if behavior == "copy-executable" else [])
            if behavior != "no-diff":
                package.task_cli.show_diff(plan)
            if behavior != "no-write-consent":
                package.terminal_consent.fresh_input("WRITE")
            package.task_cli.workspace.apply(plan, object())
            return 0
        package.cli = SimpleNamespace(main=main)
        with patch.dict(sys.modules, {"ai_desktop": package, "ai_desktop.analysis_contract": contract}), \
                patch.object(sys, "argv", ["fixture"]), patch.object(sys, "path", list(sys.path)), \
                chdir(os.getcwd()), redirect_stdout(io.StringIO()) as output:
            try:
                exec(compile(fixture["script"].read_text(), "ask-edit-fixture", "exec"), {"__name__": "__main__"})
            except SystemExit as exc:
                code = exc.code
            except AssertionError:
                code = 2
        return code, output.getvalue()

    def test_native_proof_requires_exact_source_full_overlay_and_three_distinct_consents(self):
        for behavior in ("full", "small-request", "extra-approval", "truncated", "wrong-check", "no-diff",
                         "no-write-consent", "no-test-consent", "extra-test-consent", "copy-missing-file",
                         "copy-wrong-overlay", "copy-denied", "copy-executable", "test-failed", "no-test"):
            with self.subTest(behavior=behavior):
                code, output = self.simulate_wrapper(behavior)
                self.assertEqual(code, 0 if behavior == "full" else 2)
                self.assertEqual(host.NATIVE_MARKER in output, behavior == "full")

    def test_upfront_refusal_cannot_hide_a_request_or_write(self):
        fixture = self.fixture()
        code = compile(fixture["script"].read_text(), "ask-edit-fixture", "exec")
        for behavior in ("refuse", "request", "test", "write"):
            with self.subTest(behavior=behavior):
                package = ModuleType("ai_desktop")
                package.permissions = SimpleNamespace()
                package.ask_cli = SimpleNamespace(request=Mock(return_value="unused"))
                package.terminal_consent = SimpleNamespace(fresh_input=Mock(return_value="no"),
                    PROMPTS={"ask-question": "QUESTION", "project-test": "TEST", "write": "WRITE"})
                package.task_cli = SimpleNamespace(task_sandbox=SimpleNamespace(run=Mock(), run_copy=Mock()),
                    show_diff=Mock(), workspace=SimpleNamespace(apply=Mock()))
                contract = ModuleType("ai_desktop.analysis_contract")
                contract.application_input = Mock()
                def main(_):
                    package.terminal_consent.fresh_input("QUESTION")
                    if behavior == "request":
                        package.ask_cli.request(access={"files": []})
                    elif behavior == "test":
                        package.task_cli.task_sandbox.run_copy({}, "python-unittest")
                    elif behavior == "write":
                        package.task_cli.workspace.apply(None, None)
                    return 2
                package.cli = SimpleNamespace(main=main)
                with patch.dict(sys.modules, {"ai_desktop": package, "ai_desktop.analysis_contract": contract}), \
                        patch.object(sys, "argv", ["fixture", "refusal"]), \
                        patch.object(sys, "path", list(sys.path)), chdir(os.getcwd()), redirect_stdout(io.StringIO()) as output:
                    if behavior == "refuse":
                        with self.assertRaises(SystemExit) as stopped:
                            exec(code, {"__name__": "__main__"})
                        self.assertEqual(stopped.exception.code, 2)
                    else:
                        with self.assertRaises(AssertionError):
                            exec(code, {"__name__": "__main__"})
                self.assertEqual(host.REFUSAL_MARKER in output.getvalue(), behavior == "refuse")


if __name__ == "__main__":
    unittest.main()
