"""The file host harness cannot run native work without explicit opt-in/TTY."""
from contextlib import ExitStack, redirect_stdout
import io
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import tomllib
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import host_file_access_acceptance as host


class HostFileGuards(unittest.TestCase):
    def test_flags_uid_and_terminal_refuse_before_any_probe_or_fixture(self):
        with redirect_stdout(io.StringIO()), patch.object(host.prior, "preflight") as ready, \
                patch.object(host, "create_fixture") as create, patch.object(host.prior, "run") as run, \
                patch.object(host, "configured_mode") as local:
            for args in ([], [host.FLAG, "unexpected"], ["--allow-subscription-ask-test"]):
                self.assertEqual(host.main(args), 2)
            for uid, euid, tty in ((0, 0, True), (1000, 1001, True), (1000, 1000, False)):
                with patch.object(host.os, "getuid", return_value=uid), \
                        patch.object(host.os, "geteuid", return_value=euid), \
                        patch.object(host.os, "isatty", return_value=tty):
                    self.assertEqual(host.main([host.FLAG]), 2)
                    self.assertEqual(host.main([host.LOCAL_FLAG, "~/configured/*"]), 2)
            ready.assert_not_called()
            create.assert_not_called()
            run.assert_not_called()
            local.assert_not_called()

    def exercise(self, *, failed_policy=False, fail_native=False, fail_cleanup=False,
                 fixture_cleanup=None):
        fixture = {"script": Path("/synthetic/file-fixture.py")}
        with ExitStack() as stack:
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(host.os, "getuid", return_value=1000))
            stack.enter_context(patch.object(host.os, "geteuid", return_value=1000))
            stack.enter_context(patch.object(host.os, "isatty", return_value=True))
            stack.enter_context(patch.object(host.prior, "preflight", return_value=(1000, ["socket", "service"])))
            stack.enter_context(patch.object(host, "runtime_directory", return_value=Path("/run/user/1000")))
            temporary = stack.enter_context(patch.object(host.tempfile, "TemporaryDirectory"))
            temporary.return_value.__enter__.return_value = "/synthetic"
            if fixture_cleanup is not None:
                index, error = fixture_cleanup
                exits = [False, False]
                exits[index] = error
                temporary.return_value.__exit__.side_effect = exits
            stack.enter_context(patch.object(host, "create_fixture", return_value=fixture))
            checked = subprocess.CompletedProcess([], 2 if failed_policy else 0,
                stdout=b"PRIVATE ERROR" if failed_policy else (host.POLICY_MARKER + "\n").encode(), stderr=b"")
            check = stack.enter_context(patch.object(host.subprocess, "run", return_value=checked))
            reject = stack.enter_context(patch.object(host, "reject"))
            run = stack.enter_context(patch.object(host.prior, "run",
                side_effect=ValueError("PRIVATE NATIVE ERROR") if fail_native else None))
            unchanged = stack.enter_context(patch.object(host, "check_unchanged"))
            stop = stack.enter_context(patch.object(host.prior, "check_stopped",
                side_effect=[None, None, ValueError("PRIVATE CLEANUP")] if fail_cleanup else None))
            code = host.main([host.FLAG])
        return code, output.getvalue(), temporary, check, reject, run, unchanged, stop

    def test_success_is_one_uncaptured_flow_with_two_native_requests(self):
        code, output, temporary, check, reject, run, unchanged, stop = self.exercise()
        self.assertEqual(code, 0, output)
        self.assertEqual(temporary.call_count, 2)
        self.assertEqual(temporary.call_args_list[0].kwargs["dir"], Path("/run/user/1000"))
        self.assertEqual(temporary.call_args_list[1].kwargs["dir"], "/tmp")
        self.assertEqual(reject.call_count, 1)
        check.assert_called_once()
        self.assertEqual(check.call_args.args[0][-1], "policy-check")
        run.assert_called_once_with("/usr/bin/python3", "-I", "-B", "/synthetic/file-fixture.py", timeout=420)
        self.assertNotIn("capture_output", run.call_args.kwargs)
        self.assertNotIn("input", run.call_args.kwargs)
        unchanged.assert_called_once()
        self.assertEqual(stop.call_count, 3)
        self.assertIn("synthetic-policy=PASS", output)
        self.assertIn("native-requests=2 selected-read=PASS", output)
        self.assertIn("actual-user-policy=NOT-TESTED", output)

    def test_policy_failure_prevents_reads_and_native_and_redacts_captured_output(self):
        code, output, _, _, reject, run, unchanged, stop = self.exercise(failed_policy=True)
        self.assertEqual(code, 2)
        self.assertIn("FAIL stage=policy", output)
        self.assertNotIn("PRIVATE", output)
        self.assertNotIn("HOST FILE: PASS", output)
        reject.assert_not_called()
        run.assert_not_called()
        unchanged.assert_not_called()
        stop.assert_not_called()

    def test_native_failure_and_cleanup_failure_cannot_become_success_or_retry(self):
        for kwargs in ({"fail_native": True}, {"fail_cleanup": True}):
            with self.subTest(kwargs=kwargs):
                code, output, _, _, _, run, _, _ = self.exercise(**kwargs)
                self.assertEqual(code, 2)
                run.assert_called_once()
                self.assertNotIn("PRIVATE", output)
                self.assertNotIn("HOST FILE: PASS", output)

    def test_fixture_cleanup_error_or_interrupt_never_reports_pass(self):
        # The inner data directory and outer policy directory both have to
        # finish cleanup before success, even when the native flow succeeded.
        for index in (0, 1):
            for error_type in (OSError, KeyboardInterrupt):
                with self.subTest(exit_index=index, error=error_type.__name__):
                    code, output, temporary, _, _, run, unchanged, stop = self.exercise(
                        fixture_cleanup=(index, error_type("PRIVATE FIXTURE PATH")))
                    self.assertEqual(code, 2)
                    self.assertIn("HOST FILE: FAIL stage=fixture-cleanup", output)
                    self.assertNotIn("HOST FILE: PASS", output)
                    self.assertNotIn("PRIVATE", output)
                    self.assertEqual(temporary.return_value.__exit__.call_count, 2)
                    run.assert_called_once()
                    unchanged.assert_called_once()
                    self.assertEqual(stop.call_count, 3)

    def test_preflight_refusal_never_creates_temporary_state(self):
        with redirect_stdout(io.StringIO()) as output, patch.object(host.os, "getuid", return_value=1000), \
                patch.object(host.os, "geteuid", return_value=1000), patch.object(host.os, "isatty", return_value=True), \
                patch.object(host.prior, "preflight", side_effect=ValueError("PRIVATE")), \
                patch.object(host.tempfile, "TemporaryDirectory") as temporary, patch.object(host.prior, "run") as run:
            self.assertEqual(host.main([host.FLAG]), 2)
        temporary.assert_not_called()
        run.assert_not_called()
        self.assertNotIn("PRIVATE", output.getvalue())


class FileFixtureTests(unittest.TestCase):
    def test_fixture_is_exclusive_private_synthetic_and_detects_mutation(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as data:
            fixture = host.create_fixture(home, data)
            host.check_unchanged(fixture)
            self.assertEqual(tuple(len(value.encode("utf-8")) for value in host.FILE_CONTENTS.values()), host.FILE_SIZES)
            self.assertEqual(sum(host.FILE_SIZES), 12175)
            self.assertGreater(host.CONTENTS.index(host.FINAL_MARKER), 4096)
            self.assertNotIn(host.FINAL_MARKER, host.QUESTION)
            self.assertEqual(sum(content.count(host.FINAL_MARKER) for content in host.FILE_CONTENTS.values()), 1)
            self.assertEqual(len(fixture["files"]), 5)
            policy = tomllib.loads(fixture["policy"].read_text())
            self.assertEqual(policy, {"version": 1, "files": {
                "read": [str(Path(data) / "aidfixture") + "/*"],
                "deny": [str(Path(data) / "aidfixture" / "denied.txt")]},
                "network": {"claude": True, "weather": False}})
            for path in (*fixture["files"], fixture["denied"], fixture["policy"], fixture["script"]):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            script = fixture["script"].read_text()
            compile(script, "file-fixture.py", "exec")
            self.assertIn("sys.path.insert(0, '/usr/lib/ai-desktop')", script)
            self.assertIn("permissions._home = lambda uid:", script)
            self.assertIn("requests != ['assistant', 'assistant', 'question'] or len(reads) != 1", script)
            self.assertIn("ask_cli.managed_session = no_native", script)
            self.assertIn("ask_cli.send_once = no_native", script)
            self.assertIn("item = original_collect(*values, **kwargs)", script)
            self.assertNotIn("_identity =", script)
            self.assertNotIn("load_policy =", script)
            with self.assertRaises(OSError):
                host.create_fixture(home, data)
            fixture["allowed"].write_text("changed")
            with self.assertRaises(ValueError):
                host.check_unchanged(fixture)

    def test_native_fixture_refuses_a_planner_answer_without_the_selected_read(self):
        # Execute the generated wrapper against inert modules. This tests its
        # acceptance decision, never the real CLI, filesystem reader or model.
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as data:
            fixture = host.create_fixture(home, data)
            code = compile(fixture["script"].read_text(), "file-fixture.py", "exec")
            item = host.fixture_context(fixture)
            for behavior in ("answer-without-read", "read-without-final", "wrong-marker", "extra-approval", "full"):
                with self.subTest(behavior=behavior):
                    package = ModuleType("ai_desktop")
                    package.permissions = SimpleNamespace()
                    package.terminal_consent = SimpleNamespace(fresh_input=Mock(return_value="tak"),
                                                               PROMPTS={"ask-question": "QUESTION"})
                    package.ask_cli = SimpleNamespace(request=Mock(
                        return_value="wrong reply" if behavior == "wrong-marker" else host.FINAL_MARKER))
                    package.file_context = SimpleNamespace(collect=Mock(return_value=item))
                    def main(_):
                        package.terminal_consent.fresh_input("QUESTION")
                        if behavior == "extra-approval":
                            package.terminal_consent.fresh_input("ANOTHER")
                        package.ask_cli.request("q", None, None, access={"files": []}, check=lambda: None)
                        if behavior == "answer-without-read":
                            return 0
                        # discovery-first: a second planner request carries the local list observations
                        package.ask_cli.request("q", None, None, access={"files": []}, discovery={"steps": []},
                                                check=lambda: None)
                        package.file_context.collect(None, ["f0"], None, None)
                        if behavior in ("full", "wrong-marker", "extra-approval"):
                            package.ask_cli.request("q", None, None, context=[item], check=lambda: None)
                        return 0
                    package.cli = SimpleNamespace(main=main)
                    with patch.dict(sys.modules, {"ai_desktop": package}), \
                            patch.object(sys, "argv", ["file-fixture.py"]), \
                            patch.object(sys, "path", list(sys.path)), \
                            redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as stopped:
                        exec(code, {"__name__": "__main__"})
                    self.assertEqual(stopped.exception.code, 0 if behavior == "full" else 2)
                    self.assertEqual(host.NATIVE_MARKER in output.getvalue(), behavior == "full")

    def test_fixture_detects_missing_or_truncated_context_before_final_request(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as data:
            fixture = host.create_fixture(home, data)
            code = compile(fixture["script"].read_text(), "file-fixture.py", "exec")
            for kind in ("missing", "truncated"):
                with self.subTest(kind=kind):
                    item = host.fixture_context(fixture)
                    if kind == "missing":
                        item["files"].pop()
                    else:
                        item["files"][0]["content"] = item["files"][0]["content"][:4096]
                    package = ModuleType("ai_desktop")
                    package.permissions = SimpleNamespace()
                    package.terminal_consent = SimpleNamespace(fresh_input=Mock(return_value="tak"),
                                                               PROMPTS={"ask-question": "QUESTION"})
                    request = Mock(return_value=host.FINAL_MARKER)
                    package.ask_cli = SimpleNamespace(request=request)
                    package.file_context = SimpleNamespace(collect=Mock(return_value=item))
                    package.cli = SimpleNamespace(main=lambda _: package.file_context.collect(None, ["f0"], None, None))
                    with patch.dict(sys.modules, {"ai_desktop": package}), \
                            patch.object(sys, "argv", ["file-fixture.py"]), patch.object(sys, "path", list(sys.path)), \
                            redirect_stdout(io.StringIO()) as output, self.assertRaises(AssertionError):
                        exec(code, {"__name__": "__main__"})
                    request.assert_not_called()
                    self.assertNotIn(host.NATIVE_MARKER, output.getvalue())

    def test_upfront_refusal_requires_zero_requests_reads_and_approvals(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as data:
            fixture = host.create_fixture(home, data)
            code = compile(fixture["script"].read_text(), "file-fixture.py", "exec")
            for behavior in ("refuse", "request", "read", "approve"):
                with self.subTest(behavior=behavior):
                    package = ModuleType("ai_desktop")
                    package.permissions = SimpleNamespace()
                    package.terminal_consent = SimpleNamespace(
                        fresh_input=Mock(return_value="tak" if behavior == "approve" else "nie"),
                        PROMPTS={"ask-question": "QUESTION"})
                    package.ask_cli = SimpleNamespace(request=Mock(return_value="unused"))
                    package.file_context = SimpleNamespace(collect=Mock(return_value=host.fixture_context(fixture)))
                    def main(_):
                        package.terminal_consent.fresh_input("QUESTION")
                        if behavior == "request":
                            package.ask_cli.request("q", None, None, access={}, check=lambda: None)
                        elif behavior == "read":
                            package.file_context.collect(None, ["f0"], None, None)
                        return 2
                    package.cli = SimpleNamespace(main=main)
                    with patch.dict(sys.modules, {"ai_desktop": package}), \
                            patch.object(sys, "argv", ["file-fixture.py", "refusal"]), \
                            patch.object(sys, "path", list(sys.path)), redirect_stdout(io.StringIO()) as output:
                        if behavior == "refuse":
                            with self.assertRaises(SystemExit) as stopped:
                                exec(code, {"__name__": "__main__"})
                            self.assertEqual(stopped.exception.code, 2)
                        else:
                            with self.assertRaises(AssertionError):
                                exec(code, {"__name__": "__main__"})
                    self.assertEqual(host.REFUSAL_MARKER in output.getvalue(), behavior == "refuse")


class ConfiguredReadTests(unittest.TestCase):
    def setUp(self):
        from ai_desktop import permissions, terminal_consent, analysis_contract
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(host.os, "getuid", return_value=1000))
        self.stack.enter_context(patch.object(host.os, "geteuid", return_value=1000))
        self.stack.enter_context(patch.object(host.os, "isatty", return_value=True))
        self.policy = Mock()
        self.policy.scopes.return_value = (SimpleNamespace(index=3, pattern="~/configured/*"),)
        self.policy.expand.return_value = ("/synthetic/configured/item.txt",)
        self.load = self.stack.enter_context(patch.object(permissions, "load_policy", return_value=self.policy))
        self.read = self.stack.enter_context(patch.object(permissions, "read_selected", return_value={
            "kind": "files", "files": [{"path": "/synthetic/configured/item.txt", "content": "PRIVATE CONTENT"}]}))
        self.confirm = self.stack.enter_context(patch.object(terminal_consent, "confirm", return_value=True))
        self.native = self.stack.enter_context(patch.object(host.prior, "run"))
        self.wire = self.stack.enter_context(patch.object(analysis_contract, "application_input", wraps=analysis_contract.application_input))

    def test_exact_rule_read_only_after_consent_reports_sizes_without_contents(self):
        actions = Mock()
        for name, function in (("consent", self.confirm), ("recheck", self.policy.recheck),
                               ("expand", self.policy.expand), ("read", self.read)):
            actions.attach_mock(function, name)
        self.assertEqual(host.configured_read("~/configured/*"), 0)
        self.assertEqual([call[0] for call in actions.mock_calls], ["consent", "recheck", "expand", "read", "recheck"])
        self.policy.expand.assert_called_once_with((3,))
        self.wire.assert_called_once()
        self.assertIn("HOST FILE LOCAL: PASS files=1 bytes=15 sizes=15", self.output.getvalue())
        self.assertIn("native-requests=0", self.output.getvalue())
        self.assertNotIn("PRIVATE", self.output.getvalue())
        self.assertNotIn("/synthetic/configured/item.txt", self.output.getvalue())
        self.native.assert_not_called()

    def test_mismatched_rule_or_refusal_never_reads_targets(self):
        self.assertEqual(host.configured_read("~/configured/**"), 2)
        self.confirm.assert_not_called()
        self.confirm.return_value = False
        self.assertEqual(host.configured_read("~/configured/*"), 2)
        self.policy.expand.assert_not_called()
        self.read.assert_not_called()
        self.native.assert_not_called()

    def test_changed_policy_read_failure_or_wire_failure_cannot_print_pass(self):
        for mocked in (self.policy.recheck, self.read, self.wire):
            with self.subTest(mocked=mocked):
                mocked.side_effect = ValueError("PRIVATE ERROR")
                self.assertEqual(host.configured_read("~/configured/*"), 2)
                mocked.side_effect = None
        self.assertNotIn("HOST FILE LOCAL: PASS", self.output.getvalue())
        self.assertNotIn("PRIVATE", self.output.getvalue())

    def test_installed_helper_is_uncaptured_and_never_calls_native_preflight(self):
        with patch.object(host.prior, "installed_matches") as matching, \
                patch.object(host.prior, "preflight") as preflight, \
                patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(host.main([host.LOCAL_FLAG, "~/configured/*"]), 0)
        matching.assert_called_once()
        preflight.assert_not_called()
        self.load.assert_not_called()
        self.read.assert_not_called()
        self.assertEqual(run.call_args.args[0][:4], ["/usr/bin/python3", "-I", "-B", "-c"])
        self.assertEqual(run.call_args.args[0][-1], "~/configured/*")
        self.assertNotIn("capture_output", run.call_args.kwargs)
        self.assertNotIn("input", run.call_args.kwargs)
        self.assertIn("sys.addaudithook(deny_live_operations)", host.CONFIGURED_RUNNER)
        self.assertNotIn("permissions._home", host.CONFIGURED_RUNNER)
        compile(host.CONFIGURED_RUNNER, "configured-read", "exec")

    def test_missing_opt_in_and_failed_installation_prevent_helper(self):
        with patch.object(host.prior, "installed_matches", side_effect=ValueError("PRIVATE")) as matching, \
                patch.object(host.subprocess, "run") as run:
            for args in ([], [host.LOCAL_FLAG], [host.LOCAL_FLAG, ""], [host.LOCAL_FLAG, "x" * 513]):
                self.assertEqual(host.main(args), 2)
            matching.assert_not_called()
            self.assertEqual(host.main([host.LOCAL_FLAG, "~/configured/*"]), 2)
            run.assert_not_called()
        self.assertNotIn("PRIVATE", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
