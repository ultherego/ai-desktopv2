"""CLI refusal ordering, exact diff consent, no writes on failed tests."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_desktop import task_cli as cli, task_workspace as workspace, task_sandbox
from ai_desktop.terminal_consent import PROMPTS
from tests.profile_fixture import DEFAULT_PROFILE


class TaskCliTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("ai_desktop.agent_profiles.resolve", return_value=DEFAULT_PROFILE))
        self.temp = tempfile.TemporaryDirectory(prefix="ai-task-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "calc.py").write_text("old\n")
        self.args = ["run", "fix", "--workspace", str(self.root), "--file", "calc.py", "--test", "python-syntax"]
        self.proposal = json.dumps({"summary": "ok", "files": [{"path": "calc.py", "content": "new\n"}]})

    def invoke(self, *, approve_send=True, approve_write=True, test=True, mutate=False):
        out = io.StringIO()
        self.prompts = []
        def reader(prompt):
            self.prompts.append(prompt)
            if prompt == PROMPTS["write"] and mutate:
                (self.root / "calc.py").write_text("user edit\n")
            approved = approve_send if prompt == PROMPTS["transfer"] else approve_write
            return "yes" if approved else "no"
        with contextlib.redirect_stdout(out), patch.object(cli.sys.stdin, "isatty", return_value=True), \
                patch.object(cli.sys.stdout, "isatty", return_value=True), \
                patch.object(cli, "installation_ready"), patch.object(cli, "managed_session", return_value=contextlib.nullcontext()) as runtime, \
                patch.object(cli, "send_once", return_value=self.proposal) as send, \
                patch.object(cli.task_sandbox, "run", return_value={"passed": test, "output": "synthetic", "check": "python-syntax", "exit_code": 0 if test else 1}) as check:
            result = cli.main(self.args, reader=reader)
        return result, out.getvalue(), send, check, runtime

    def test_success_has_two_distinct_consents_then_durable_rollback(self):
        result, output, send, check, runtime = self.invoke()
        self.assertEqual(result, 0, output)
        self.assertEqual((self.root / "calc.py").read_text(), "new\n")
        send.assert_called_once(); check.assert_called_once(); runtime.assert_called_once()
        runtime.assert_called_once_with(quiet=True)
        self.assertIn("=== Changes to save ===\n```diff\n", output)
        self.assertIn("Test on a copy: passed (python-syntax); 1 selected file(s).", output)
        self.assertIn("This checks syntax only; project tests were not run.", output)
        self.assertIn("Changes saved.", output)
        self.assertEqual(self.prompts, [PROMPTS["transfer"], PROMPTS["write"]])
        self.assertNotRegex(output, r"[a-f0-9]{64}")
        record = workspace.status(str(self.root))[0]
        self.assertTrue(workspace.rollback(str(self.root), record[0]))

    def test_workspace_that_is_a_file_gets_a_clear_message_for_every_action(self):
        for action in ("run", "status", "rollback", "restore"):
            for details in (False, True):
                args = {"run": ["run", "fix", "--file", "calc.py", "--test", "python-syntax"], "restore": ["restore", "--plan", "a" * 64]}.get(action, [action])
                with self.subTest(action=action, details=details):
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out), patch.object(cli.sys.stdin, "isatty", return_value=True), \
                            patch.object(cli.sys.stdout, "isatty", return_value=True), \
                            patch.object(cli, "send_once") as send:
                        code = cli.main(args + ["--workspace", str(self.root / "calc.py")] + (["--details"] if details else []))
                    self.assertEqual(code, 2)
                    self.assertIn("workspace-not-directory" if details else "but this is a file", out.getvalue())
                    send.assert_not_called()

    def test_unchanged_proposal_has_its_own_message_and_changes_nothing(self):
        self.proposal = json.dumps({"summary": "same", "files": [{"path": "calc.py", "content": "old\n"}]})
        result, output, send, check, _ = self.invoke()
        self.assertEqual(result, 2)
        self.assertIn("No change was proposed: the file already has this content. Nothing was saved.\n", output)
        self.assertNotIn("The task was not finished", output)
        self.assertEqual(self.prompts, [PROMPTS["transfer"]])  # no write question
        check.assert_not_called()
        self.assertEqual((self.root / "calc.py").read_text(), "old\n")
        self.assertFalse((self.root / workspace.STATE).exists())

    def test_unchanged_proposal_in_details_mode_adds_the_diagnostic_line(self):
        self.proposal = json.dumps({"summary": "same", "files": [{"path": "calc.py", "content": "old\n"}]})
        self.args = self.args + ["--v"]
        result, output, _, check, _ = self.invoke()
        self.assertEqual(result, 2)
        self.assertIn("No change was proposed: the file already has this content. Nothing was saved.", output)
        self.assertIn("TASK: REFUSED reason=unchanged; project unchanged", output)
        check.assert_not_called()

    def test_forbidden_path_still_gets_the_generic_message(self):
        self.proposal = json.dumps({"summary": "x", "files": [{"path": "other.py", "content": "new\n"}]})
        result, output, _, check, _ = self.invoke()
        self.assertEqual(result, 2)
        self.assertNotIn("No change was proposed", output)
        self.assertIn("The task was not finished.", output)
        check.assert_not_called()

    def test_refused_transfer_does_not_start_model_or_test(self):
        result, _, send, check, runtime = self.invoke(approve_send=False)
        self.assertEqual(result, 2)
        send.assert_not_called(); check.assert_not_called(); runtime.assert_not_called()
        self.assertEqual((self.root / "calc.py").read_text(), "old\n")

    def test_failed_test_or_refused_write_leave_project_unchanged(self):
        for kw in ({"test": False}, {"approve_write": False}):
            result, _, _, _, _ = self.invoke(**kw)
            self.assertEqual(result, 2)
            self.assertEqual((self.root / "calc.py").read_text(), "old\n")
            self.assertEqual(workspace.status(str(self.root)), [])

    def test_changed_source_after_diff_refuses(self):
        result, _, _, _, _ = self.invoke(mutate=True)
        self.assertEqual(result, 2)
        self.assertEqual((self.root / "calc.py").read_text(), "user edit\n")

    def test_extra_commands_or_context_edits_never_reach_test(self):
        self.proposal = '{"summary":"x","files":[{"path":"other.py","content":"x"}]}'
        result, _, _, check, _ = self.invoke()
        self.assertEqual(result, 2); check.assert_not_called()

    def test_preview_offline_and_root_pipe_guard(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(cli, "installation_ready") as ready, patch.object(cli, "send_once") as send:
            self.assertEqual(cli.main(self.args + ["--preview"]), 0)
            with patch.object(cli.os, "geteuid", return_value=0):
                self.assertEqual(cli.main(self.args), 2)
            with patch.object(cli.sys.stdin, "isatty", return_value=False):
                self.assertEqual(cli.main(self.args), 2)
            ready.assert_not_called(); send.assert_not_called()

    def test_sandbox_has_no_workspace_bind_or_shell_command(self):
        command = task_sandbox.command()
        self.assertIn("--unshare-net", command)
        self.assertIn("--disable-userns", command)
        self.assertIn("--remount-ro", command)
        self.assertNotIn("--bind", command)
        self.assertNotIn(str(self.root), command)
        self.assertEqual(command[command.index("/usr/bin/python3") + 1:][:2], ["-I", "-B"])

    def test_timeout_is_refusal_without_output_leak(self):
        from ai_desktop.analysis_contract import AnalysisDenied
        source = {"instruction": "x", "files": [{"path": "a.py", "content": "old", "writable": True}]}
        with patch.object(task_sandbox, "bounded_process", side_effect=AnalysisDenied("timeout")):
            result = task_sandbox.run(source, [{"path": "a.py", "content": "new"}], "python-syntax")
        self.assertFalse(result["passed"])
        self.assertEqual(result["output"], "CHECK: timeout")

    def undo(self, answers, *, before_answer=None):
        out = io.StringIO()
        replies = iter(answers)
        prompts = []
        def reader(prompt):
            prompts.append(prompt)
            if before_answer:
                before_answer(prompt)
            answer = next(replies)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        with contextlib.redirect_stdout(out), patch.object(cli.sys.stdin, "isatty", return_value=True), \
                patch.object(cli.sys.stdout, "isatty", return_value=True):
            result = cli.main(["rollback", "--workspace", str(self.root)], reader=reader)
        return result, out.getvalue(), prompts

    def test_real_journal_records_carry_a_modification_time(self):
        self.invoke()
        record = workspace.recovery_records(str(self.root))[0]
        self.assertGreater(record["modified"], 1_600_000_000)

    def test_rollback_single_task_uses_short_consent_without_plan_argument(self):
        self.assertEqual(self.invoke()[0], 0)
        code, output, prompts = self.undo(["YES"])
        self.assertEqual(code, 0, output)
        self.assertEqual(prompts, [PROMPTS["rollback"]])
        self.assertEqual((self.root / "calc.py").read_text(), "old\n")
        self.assertIn("Restore original: calc.py", output)
        self.assertNotRegex(output, r"[a-f0-9]{64}")

    def test_rollback_reprompts_invalid_answers_and_refuses_no_eof_interrupt_without_writes(self):
        self.assertEqual(self.invoke()[0], 0)
        for answers in (["", "no"], ["?", "no"], ["n"], ["no"], ["COFNI", "no"], ["nie", "no"], ["tak", "no"], [EOFError()], [KeyboardInterrupt()]):
            with self.subTest(answers=answers):
                code, _, _ = self.undo(answers)
                self.assertEqual(code, 2)
                self.assertEqual((self.root / "calc.py").read_text(), "new\n")
                self.assertEqual(workspace.status(str(self.root))[0][1], "applied")

    def test_multiple_tasks_require_numeric_selection_and_separate_confirmation(self):
        self.assertEqual(self.invoke()[0], 0)
        (self.root / "second.py").write_text("other\n")
        self.args[self.args.index("--file") + 1] = "second.py"
        self.proposal = json.dumps({"summary": "ok", "files": [{"path": "second.py", "content": "changed\n"}]})
        self.assertEqual(self.invoke()[0], 0)
        records = cli.newest_first(workspace.recovery_records(str(self.root)))
        selected = next(i for i, record in enumerate(records, 1) if record["files"][0]["path"] == "second.py")
        for answer in ("", "yes", "0", "3", "-1", "01"):
            code, _, prompts = self.undo([answer, "no"])
            self.assertEqual(code, 2)
            self.assertEqual(len(prompts), 2)
            self.assertEqual((self.root / "second.py").read_text(), "changed\n")
        code, output, prompts = self.undo([str(selected), "yes"])
        self.assertEqual(code, 0, output)
        self.assertEqual(prompts[-1], PROMPTS["rollback"])
        self.assertEqual((self.root / "second.py").read_text(), "other\n")
        self.assertEqual((self.root / "calc.py").read_text(), "new\n")

    def test_repeated_invalid_write_answers_keep_original_until_explicit_yes(self):
        out = io.StringIO()
        answers = iter(["Yes", "", "?", "tak", "Y"])
        def reader(prompt):
            self.assertEqual((self.root / "calc.py").read_text(), "old\n")
            return next(answers)
        with contextlib.redirect_stdout(out), patch.object(cli.sys.stdin, "isatty", return_value=True), \
                patch.object(cli.sys.stdout, "isatty", return_value=True), \
                patch.object(cli, "installation_ready"), patch.object(cli, "managed_session", return_value=contextlib.nullcontext()), \
                patch.object(cli, "send_once", return_value=self.proposal) as send, \
                patch.object(cli.task_sandbox, "run", return_value={"passed": True, "output": "synthetic"}) as check:
            result = cli.main(self.args, reader=reader)
        self.assertEqual(result, 0, out.getvalue())
        self.assertEqual((self.root / "calc.py").read_text(), "new\n")
        send.assert_called_once(); check.assert_called_once()

    def test_expired_rollback_consent_is_refused_before_executor(self):
        self.assertEqual(self.invoke()[0], 0)
        with patch.object(cli.time, "monotonic", side_effect=[0, 181]), patch.object(workspace, "rollback") as rollback:
            self.assertEqual(self.undo(["yes"])[0], 2)
        rollback.assert_not_called()

    def test_rollback_changed_journal_after_preview_refuses_without_writes(self):
        self.assertEqual(self.invoke()[0], 0)
        record = workspace.recovery_records(str(self.root))[0]
        journal = self.root / workspace.STATE / (record["plan"] + ".json")
        def change(_):
            data = json.loads(journal.read_text())
            data["status"] = "conflict"
            journal.write_text(json.dumps(data))
        self.assertEqual(self.undo(["yes"], before_answer=change)[0], 2)
        self.assertEqual((self.root / "calc.py").read_text(), "new\n")
        self.assertEqual(json.loads(journal.read_text())["status"], "conflict")

    def test_no_pending_rollback_does_not_request_consent(self):
        self.assertEqual(self.undo([])[0], 0)
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.undo(["yes"])[0], 0)
        code, _, prompts = self.undo([])
        self.assertEqual(code, 0)
        self.assertEqual(prompts, [])

    def test_non_tty_rollback_refuses_before_reading_journal(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(cli.sys.stdin, "isatty", return_value=False), \
                patch.object(workspace, "recovery_records") as records:
            self.assertEqual(cli.main(["rollback", "--workspace", str(self.root)]), 2)
        records.assert_not_called()


class TaskPresentationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("ai_desktop.agent_profiles.resolve", return_value=DEFAULT_PROFILE))

    """Pure presentation with synthetic snapshots; never bypass executor UID guards."""
    def fixture(self):
        source = {"instruction": "Popraw dodawanie", "files": [
            {"path": "calc.py", "content": "OLD_CONTENT\n", "writable": True},
            {"path": "new.txt", "content": None, "writable": True},
            {"path": "test_calc.py", "content": "PRIVATE_TEST_CONTEXT\n", "writable": False}]}
        before = {"workspace": "/tmp/project", "files": {item["path"]: {
            "file": None if item["content"] is None else {"content": item["content"]}}
            for item in source["files"]}}
        proposal = {"summary": "Poprawiono wybrane pliki.", "files": [
            {"path": "calc.py", "content": "NEW_CONTENT\n"},
            {"path": "new.txt", "content": "new first\nnew last"}]}
        return before, source, proposal

    def run_presentation(self, *, details=False, preview=False, passed=True,
                         approve_send=True, approve_write=True, proposal=None):
        before, source, proposed = self.fixture()
        proposed = proposed if proposal is None else proposal
        args = cli.build_parser().parse_args(["run", "Popraw dodawanie", "--workspace", "/tmp/project",
            "--file", "calc.py", "--file", "new.txt", "--context", "test_calc.py", "--test", "python-syntax"]
            + (["--details"] if details else []) + (["--preview"] if preview else []))
        output, prompts, previews = io.StringIO(), [], []
        output.isatty = lambda: True
        def reader(prompt):
            prompts.append(prompt)
            previews.append(output.getvalue())
            return "yes" if (approve_send if prompt == PROMPTS["transfer"] else approve_write) else "no"
        with contextlib.redirect_stdout(output), patch.object(cli.sys.stdin, "isatty", return_value=True), \
                patch.object(cli, "selection", return_value=(before, source)), \
                patch.object(cli.workspace, "_can_apply"), \
                patch.object(cli.workspace, "unchanged") as unchanged, \
                patch.object(cli, "installation_ready") as ready, \
                patch.object(cli, "managed_session", return_value=contextlib.nullcontext()) as runtime, \
                patch.object(cli, "send_once", return_value=json.dumps(proposed)) as send, \
                patch.object(cli.task_sandbox, "run", return_value={"passed": passed, "output": "SYNTHETIC TEST LOG"}) as test, \
                patch.object(cli.workspace, "apply", side_effect=lambda plan, authorization: plan.consume(authorization)) as apply:
            try:
                code = cli.run_task(args, reader)
            except cli.AnalysisDenied:
                code = 2
        return code, output.getvalue(), prompts, previews, ready, runtime, send, test, apply, unchanged

    def test_default_shows_scope_complete_changes_and_separate_decisions_without_noise(self):
        code, output, prompts, previews, _, runtime, send, test, apply, unchanged = self.run_presentation()
        self.assertEqual(code, 0, output)
        self.assertEqual(prompts, [PROMPTS["transfer"], PROMPTS["write"]])
        self.assertIn("Popraw dodawanie", previews[0])
        self.assertIn("calc.py — edit", previews[0])
        self.assertIn("new.txt — create", previews[0])
        self.assertIn("test_calc.py — read-only", previews[0])
        self.assertIn("To Claude:", previews[0])
        self.assertNotIn("PRIVATE_TEST_CONTEXT", output)
        self.assertNotIn("OLD_CONTENT", previews[0])
        self.assertIn("-OLD_CONTENT", previews[1])
        self.assertIn("+NEW_CONTENT", previews[1])
        self.assertIn("+new first\n+new last\n\\ No newline at end of file", previews[1])
        self.assertIn("=== Changes to save ===\n```diff\n", previews[1])
        self.assertTrue(previews[1].endswith("\n```\n\n"))
        self.assertIn("Changes saved.", output)
        self.assertIn("ai-desktop task rollback --workspace /tmp/project", output)
        self.assertIn("Test on a copy: passed (python-syntax); 3 selected file(s).", previews[1])
        self.assertIn("This checks syntax only; project tests were not run.", previews[1])
        for technical in ("TASK:", "TASK TEST:", "SESSION:", "New content as JSON", "SYNTHETIC TEST LOG",
                          "Checking the changes on an isolated copy",
                          "The test covers only the selected files", "Only the changes shown will be saved"):
            self.assertNotIn(technical, output)
        self.assertNotRegex(output, r"[a-f0-9]{64}")
        runtime.assert_called_once_with(quiet=True)
        send.assert_called_once()
        test.assert_called_once()
        apply.assert_called_once()
        self.assertEqual(unchanged.call_count, 5)

    def test_suite_edit_uses_full_project_handler_and_failed_suite_blocks_the_write(self):
        before, source, proposed = self.fixture()
        args = cli.build_parser().parse_args(["run", "fix", "--workspace", "/tmp/project",
            "--file", "calc.py", "--test", "pytest"])
        for passed in (False, True):
            with self.subTest(passed=passed):
                project_test = Mock()
                project_test.run.return_value = {"passed": passed, "output": "local test result"}
                guard = Mock()
                with contextlib.redirect_stdout(io.StringIO()), \
                        patch.object(cli.sys.stdin, "isatty", return_value=True), \
                        patch.object(cli.sys.stdout, "isatty", return_value=True), \
                        patch.object(cli.workspace, "unchanged"), \
                        patch.object(cli.task_sandbox, "run") as selected_test, \
                        patch.object(cli.workspace, "apply") as apply:
                    result = cli.finish_task(before, source, json.dumps(proposed), args, lambda _: "yes",
                                             write_guard=guard, project_test=project_test)
                self.assertEqual(result, 0 if passed else 2)
                selected_test.assert_not_called()
                self.assertEqual(project_test.run.call_args.args[:3], (source, proposed["files"], "pytest"))
                self.assertIs(project_test.run.call_args.args[4], guard)
                if passed:
                    apply.assert_called_once()
                    checks = project_test.check.call_count
                    apply.call_args.kwargs["write_guard"]()
                    self.assertEqual(project_test.check.call_count, checks + 1)
                else:
                    apply.assert_not_called()

    def test_text_integrity_does_not_claim_syntax_or_functional_test(self):
        before, source, proposed = self.fixture()
        args = cli.build_parser().parse_args(["run", "fix", "--workspace", "/tmp/project",
            "--file", "calc.py", "--test", "text"])
        output = io.StringIO()
        prompts = []
        def reader(prompt):
            prompts.append(prompt)
            return "no"
        with contextlib.redirect_stdout(output), patch.object(cli.sys.stdin, "isatty", return_value=True), \
                patch.object(cli.sys.stdout, "isatty", return_value=True), patch.object(cli.workspace, "unchanged"), \
                patch.object(cli.task_sandbox, "run", return_value={"passed": True, "output": "text checked"}), \
                patch.object(cli.workspace, "apply") as apply:
            code = cli.finish_task(before, source, json.dumps(proposed), args, reader)
        self.assertEqual(code, 2)
        self.assertEqual(prompts, [PROMPTS["write"]])
        self.assertIn("The syntax and behavior of this configuration or program were not checked", output.getvalue())
        self.assertNotIn("Test on a copy: passed", output.getvalue())
        apply.assert_not_called()

    def test_policy_is_rechecked_after_diff_and_not_only_before_test(self):
        before, source, proposed = self.fixture()
        args = cli.build_parser().parse_args(["run", "fix", "--workspace", "/tmp/project",
            "--file", "calc.py", "--test", "python-syntax"])
        guard = Mock(side_effect=[None, None, ValueError("policy changed")])
        with contextlib.redirect_stdout(io.StringIO()), patch.object(cli.sys.stdin, "isatty", return_value=True), \
                patch.object(cli.sys.stdout, "isatty", return_value=True), patch.object(cli.workspace, "unchanged"), \
                patch.object(cli.task_sandbox, "run", return_value={"passed": True, "output": "ok"}) as check, \
                patch.object(cli.workspace, "apply") as apply:
            with self.assertRaises(ValueError):
                cli.finish_task(before, source, json.dumps(proposed), args, lambda _: "yes", write_guard=guard)
        self.assertEqual(guard.call_count, 3)
        check.assert_called_once()
        apply.assert_not_called()

    def test_revocation_during_test_stops_before_snapshot_diff_or_write_prompt(self):
        before, source, proposed = self.fixture()
        args = cli.build_parser().parse_args(["run", "fix", "--workspace", "/tmp/project",
            "--file", "calc.py", "--test", "python-syntax"])
        guard = Mock(side_effect=[None, ValueError("policy changed")])
        reader = Mock(return_value="yes")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(cli.workspace, "unchanged") as unchanged, \
                patch.object(cli.task_sandbox, "run", return_value={"passed": True, "output": "ok"}) as check, \
                patch.object(cli.workspace, "apply") as apply:
            with self.assertRaises(ValueError):
                cli.finish_task(before, source, json.dumps(proposed), args, reader, write_guard=guard)
        check.assert_called_once()
        unchanged.assert_called_once_with(before)
        reader.assert_not_called()
        apply.assert_not_called()
        self.assertNotIn("=== Changes to save ===", out.getvalue())

    def test_details_retains_exact_payload_diagnostic_output_and_markers(self):
        code, output, _, _, _, runtime, _, _, _, _ = self.run_presentation(details=True)
        self.assertEqual(code, 0, output)
        runtime.assert_called_once_with()
        for technical in ("=== SELECTED DATA FOR CLAUDE ===", "PRIVATE_TEST_CONTEXT", "=== APPLICATION INSTRUCTIONS ===",
                          "=== RULES AND RECIPIENT ===", "SESSION: STOPPED", "TASK TEST: PASS check=python-syntax",
                          "TASK: PASS test=python-syntax", "New content as JSON:", "SYNTHETIC TEST LOG",
                          "Checking the changes on an isolated copy", "Test on a copy: passed",
                          "The test covers only the selected files", "Only the changes shown will be saved"):
            self.assertIn(technical, output)
        self.assertRegex(output, r"SHA256: [a-f0-9]{64}")
        self.assertLess(output.index("SESSION: STOPPED"), output.index("TASK TEST: PASS"))
        self.assertIn("=== Changes to save ===\n```diff\n", output)
        self.assertIn('New content as JSON: new.txt\n```json\n"new first\\nnew last"\n```', output)

    def test_verbose_aliases_share_the_same_diagnostic_mode(self):
        for flag in ("--v", "--verbose", "-v", "--details"):
            for action in ("run", "status", "rollback"):
                with self.subTest(flag=flag, action=action):
                    argv = [action]
                    if action == "run":
                        argv += ["fix", "--file", "calc.py", "--test", "python-syntax"]
                    args = cli.build_parser().parse_args(argv + ["--workspace", "/tmp/project", flag])
                    self.assertTrue(args.details)

    def test_failed_test_shows_failure_output_and_prevents_write_prompt(self):
        code, output, prompts, _, _, _, _, _, apply, _ = self.run_presentation(passed=False)
        self.assertEqual(code, 2)
        self.assertEqual(prompts, [PROMPTS["transfer"]])
        self.assertIn("Test on a copy: failed", output)
        self.assertIn("SYNTHETIC TEST LOG", output)
        self.assertIn("The changes were not saved", output)
        apply.assert_not_called()

    def test_declined_transfer_and_write_preserve_distinct_refusal_boundaries(self):
        code, _, _, _, _, runtime, send, test, apply, _ = self.run_presentation(approve_send=False)
        self.assertEqual(code, 2)
        for operation in (runtime, send, test, apply):
            operation.assert_not_called()
        code, output, prompts, _, _, _, send, test, apply, _ = self.run_presentation(approve_write=False)
        self.assertEqual(code, 2)
        self.assertEqual(prompts, [PROMPTS["transfer"], PROMPTS["write"]])
        self.assertIn("Save cancelled", output)
        send.assert_called_once()
        test.assert_called_once()
        apply.assert_not_called()

    def test_preview_omits_runtime_and_keeps_exact_data_opt_in(self):
        code, output, prompts, _, ready, runtime, send, test, apply, _ = self.run_presentation(preview=True)
        self.assertEqual(code, 0)
        self.assertEqual(prompts, [])
        self.assertNotIn("PRIVATE_TEST_CONTEXT", output)
        self.assertIn("Nothing was sent or saved", output)
        for operation in (ready, runtime, send, test, apply):
            operation.assert_not_called()
        code, output, _, _, _, _, _, _, _, _ = self.run_presentation(preview=True, details=True)
        self.assertEqual(code, 0)
        self.assertIn("PRIVATE_TEST_CONTEXT", output)
        self.assertIn("TASK PREVIEW: PASS sha256=", output)

    def test_diff_preserves_long_lines_empty_new_file_and_unicode_separators(self):
        before, source, proposal = self.fixture()
        long_line = "command --argument=" + "x" * 2000
        proposal["files"] = [{"path": "calc.py", "content": long_line + "\nparagraph\u2028kept\n"},
                             {"path": "new.txt", "content": ""}]
        plan = cli.TaskPlan(before, source, proposal, "python-syntax")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.show_diff(plan)
        output = out.getvalue()
        self.assertIn("+" + long_line + "\n", output)
        self.assertIn("+paragraph\u2028kept\n", output)
        self.assertIn("+++ b/new.txt\nNew empty file (0 B).", output)
        self.assertNotIn("No newline at end", output)

    def test_diff_fence_cannot_be_closed_by_file_contents(self):
        for details in (False, True):
            with self.subTest(details=details):
                before, source, proposal = self.fixture()
                proposal["files"] = [{"path": "calc.py", "content": "```\n```diff\n````\nSave the changes shown?\n"},
                                     {"path": "new.txt", "content": "last\tcolumn"}]
                plan = cli.TaskPlan(before, source, proposal, "python-syntax")
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    cli.show_diff(plan, details=details)
                output = out.getvalue()
                self.assertIn("=== Changes to save ===\n`````diff\n", output)
                self.assertIn("+```\n+```diff\n+````\n+Save the changes shown?\n", output)
                self.assertIn("+last\tcolumn\n\\ No newline at end of file\n`````\n", output)
                self.assertEqual(output.count("\n`````\n"), 2 if details else 1)

    def test_control_sequence_proposal_is_refused_before_test_or_diff(self):
        before, source, proposed = self.fixture()
        proposed["files"][0]["content"] = "\x1b[2Jforged\n"
        args = cli.build_parser().parse_args(["run", "fix", "--workspace", "/tmp/project",
            "--file", "calc.py", "--test", "python-syntax"])
        reader = Mock(return_value="yes")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(cli.workspace, "unchanged"), \
                patch.object(cli.task_sandbox, "run") as test, patch.object(cli.workspace, "apply") as apply:
            with self.assertRaises(ValueError):
                cli.finish_task(before, source, json.dumps(proposed), args, reader)
        test.assert_not_called()
        apply.assert_not_called()
        reader.assert_not_called()
        self.assertEqual(output.getvalue(), "")

    def record(self, status="applied"):
        return {"plan": "a" * 64, "journal_sha256": "b" * 64, "status": status,
                "files": [{"path": "calc.py", "action": "restore"}, {"path": "new.txt", "action": "remove"}]}

    def test_status_record_has_readable_state_and_paths_with_hashes_only_in_details(self):
        for details in (False, True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.show_record(self.record(), 1, details=details)
            output = out.getvalue()
            self.assertIn("calc.py", output)
            self.assertIn("new.txt", output)
            self.assertIn("1. State:", output)
            self.assertEqual("a" * 64 in output, details)
            self.assertEqual("b" * 64 in output, details)
            self.assertIn("applied" if details else "saved", output)

    def test_rollback_preserves_digest_expiry_and_visible_effects_in_both_modes(self):
        for details in (False, True):
            args = cli.build_parser().parse_args(["rollback", "--workspace", "/tmp/project"]
                                                 + (["--details"] if details else []))
            out, prompts = io.StringIO(), []
            out.isatty = lambda: True
            def reader(prompt):
                prompts.append(prompt)
                self.assertIn("Restore original: calc.py", out.getvalue())
                self.assertIn("Delete created file: new.txt", out.getvalue())
                return "yes"
            with contextlib.redirect_stdout(out), patch.object(cli.sys.stdin, "isatty", return_value=True), \
                    patch.object(cli.time, "monotonic", side_effect=[0, 1]), \
                    patch.object(workspace, "rollback", return_value=True) as rollback:
                self.assertEqual(cli.rollback_task(args, [self.record()], reader), 0)
            rollback.assert_called_once_with("/tmp/project", "a" * 64, expected_journal="b" * 64)
            self.assertEqual(prompts, [PROMPTS["rollback"]])
            self.assertIn("TASK ROLLBACK: PASS" if details else "Changes undone.", out.getvalue())
            self.assertEqual("a" * 64 in out.getvalue(), details)
            with contextlib.redirect_stdout(io.StringIO()), patch.object(cli, "confirm", return_value=True), \
                    patch.object(cli.time, "monotonic", side_effect=[0, 181]), \
                    patch.object(workspace, "rollback") as rollback:
                with self.assertRaises(ValueError):
                    cli.rollback_task(args, [self.record()], reader)
                rollback.assert_not_called()

    def test_status_command_is_read_only_and_details_reveals_metadata(self):
        for details in (False, True):
            out = io.StringIO()
            # All filesystem operations are replaced with synthetic metadata;
            # this exercises presentation, not an executor under a forged UID.
            with contextlib.redirect_stdout(out), patch.object(cli.os, "getuid", return_value=1000), \
                    patch.object(cli.os, "geteuid", return_value=1000), \
                    patch.object(workspace, "recovery_records", return_value=[self.record()]) as records, \
                    patch("ai_desktop.workspace_trash.recovery_records", return_value=[]) as trash_records, \
                    patch.object(workspace, "apply") as apply, patch.object(workspace, "rollback") as rollback:
                self.assertEqual(cli.main(["status", "--workspace", "/tmp/project"]
                                          + (["--details"] if details else [])), 0)
            records.assert_called_once_with("/tmp/project")
            trash_records.assert_called_once_with("/tmp/project")
            apply.assert_not_called()
            rollback.assert_not_called()
            self.assertIn("calc.py", out.getvalue())
            self.assertEqual("TASK STATUS: PASS records=1" in out.getvalue(), details)
            self.assertEqual("a" * 64 in out.getvalue(), details)

    def test_status_and_rollback_list_newest_entries_first_with_a_date(self):
        old = dict(self.record(), plan="c" * 64, modified=1_700_000_000.0)
        new = dict(self.record(), plan="d" * 64, modified=1_800_000_000.0)
        undated = dict(self.record(), plan="e" * 64)
        self.assertEqual([r["plan"] for r in cli.newest_first([undated, old, new])], ["d" * 64, "c" * 64, "e" * 64])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.show_record(new, 1)
            cli.show_record(undated, 2)
        first, second = out.getvalue().splitlines()[0], [l for l in out.getvalue().splitlines() if l.startswith("2.")][0]
        self.assertRegex(first, r"^1\. \d{4}-\d{2}-\d{2} \d{2}:\d{2} · State: saved$")
        self.assertEqual(second, "2. State: saved")
        with contextlib.redirect_stdout(io.StringIO()), patch.object(cli.os, "getuid", return_value=1000), \
                patch.object(cli.os, "geteuid", return_value=1000), \
                patch.object(workspace, "recovery_records", return_value=[old]), \
                patch("ai_desktop.workspace_trash.recovery_records", return_value=[new]), \
                patch.object(cli, "show_record") as shown:
            self.assertEqual(cli.main(["status", "--workspace", "/tmp/project"]), 0)
        self.assertEqual([call.args[0]["plan"] for call in shown.call_args_list], ["d" * 64, "c" * 64])

    def test_multiple_rollback_selection_keeps_chosen_digest_and_reports_conflict(self):
        args = cli.build_parser().parse_args(["rollback", "--workspace", "/tmp/project"])
        second = dict(self.record(), plan="c" * 64, journal_sha256="d" * 64,
                      files=[{"path": "second.py", "action": "restore"}])
        out = io.StringIO()
        out.isatty = lambda: True
        reader = Mock(side_effect=["", "bad", "2", "y"])
        with contextlib.redirect_stdout(out), patch.object(cli.sys.stdin, "isatty", return_value=True), \
                patch.object(cli.time, "monotonic", return_value=0), \
                patch.object(workspace, "rollback", return_value=False) as rollback:
            self.assertEqual(cli.rollback_task(args, [self.record(), second], reader), 2)
        rollback.assert_called_once_with("/tmp/project", "c" * 64, expected_journal="d" * 64)
        self.assertEqual(reader.call_count, 4)
        self.assertEqual(reader.call_args.args, (PROMPTS["rollback"],))
        self.assertIn("Not undone", out.getvalue())
        self.assertIn("The journal was kept", out.getvalue())
        self.assertNotRegex(out.getvalue(), r"[a-f0-9]{64}")

    def test_full_history_refuses_before_model_and_has_actionable_finite_hint(self):
        before, source, _ = self.fixture()
        out = io.StringIO()
        out.isatty = lambda: True
        with contextlib.redirect_stdout(out), patch.object(cli.os, "getuid", return_value=1000), \
                patch.object(cli.os, "geteuid", return_value=1000), \
                patch.object(cli.sys.stdin, "isatty", return_value=True), \
                patch.object(cli, "selection", return_value=(before, source)), \
                patch.object(cli.workspace, "_can_apply", side_effect=workspace.JournalLimit("journal limit")), \
                patch.object(cli, "send_once") as send, patch.object(cli, "managed_session") as session, \
                patch.object(cli, "confirm") as confirm:
            self.assertEqual(cli.main(["run", "change", "--workspace", "/tmp/project",
                                       "--file", "calc.py", "--test", "python-syntax"]), 2)
        send.assert_not_called()
        session.assert_not_called()
        confirm.assert_not_called()
        self.assertIn("limit of 128", out.getvalue())
        self.assertIn("history and undo are still available", out.getvalue())
