import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
import contextlib
import io
import unittest
from unittest.mock import patch

from ai_desktop.cli import main


class CliTests(unittest.TestCase):
    def test_settings_routes_to_neutral_configuration_without_account_operations(self):
        for arguments in ([], ["show"], ["set", "--effort", "xhigh"], ["--help"]):
            with self.subTest(arguments=arguments), \
                    patch("ai_desktop.management_cli.main", return_value=7) as settings, \
                    patch("ai_desktop.analysis_session.administrative") as administrative, \
                    patch("ai_desktop.codex_cli.main") as codex, \
                    patch("subprocess.run") as run, patch("subprocess.Popen") as spawn:
                self.assertEqual(main(["settings", *arguments]), 7)
            settings.assert_called_once_with(arguments, prog="ai-desktop settings", provider_neutral=True)
            administrative.assert_not_called()
            codex.assert_not_called()
            run.assert_not_called()
            spawn.assert_not_called()

    def test_legacy_provider_commands_keep_their_existing_routes(self):
        with patch("ai_desktop.management_cli.main", return_value=0) as claude, \
                patch("ai_desktop.codex_cli.main", return_value=0) as codex:
            self.assertEqual(main(["claude", "set", "--model", "opus"]), 0)
            self.assertEqual(main(["codex", "status"]), 0)
        claude.assert_called_once_with(["set", "--model", "opus"])
        codex.assert_called_once_with(["status"])

    def test_settings_typo_suggests_only_the_canonical_public_entry(self):
        with patch("ai_desktop.management_cli.main") as settings, \
                patch("ai_desktop.codex_cli.main") as codex, \
                contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as stopped:
                main(["settngs"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("Did you mean: ai-desktop settings", output.getvalue())
        self.assertNotIn("claude", output.getvalue())
        self.assertNotIn("codex", output.getvalue())
        settings.assert_not_called()
        codex.assert_not_called()

    def test_hidden_provider_helpers_are_not_suggested_for_typos(self):
        for token in ("claud", "codx"):
            with self.subTest(token=token), contextlib.redirect_stderr(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as stopped:
                    main([token])
            self.assertEqual(stopped.exception.code, 2)
            self.assertNotIn("Did you mean: ai-desktop claude", output.getvalue())
            self.assertNotIn("Did you mean: ai-desktop codex", output.getvalue())

    def test_typo_suggests_public_command_without_execution_or_internal_usage(self):
        with patch("ai_desktop.session_cli.main") as sessions, \
                patch("subprocess.run") as run, patch("subprocess.Popen") as spawn, \
                contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as stopped:
                main(["ssessions"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("Did you mean: ai-desktop sessions", output.getvalue())
        self.assertNotIn("usage:", output.getvalue())
        self.assertNotIn("worker-once", output.getvalue())
        self.assertLessEqual(len(output.getvalue().splitlines()), 3)
        sessions.assert_not_called()
        run.assert_not_called()
        spawn.assert_not_called()

    def test_invalid_command_never_echoes_terminal_controls_or_suggests_internal_tools(self):
        for token in ("\x1b[2Jssessions", "\u202essessions", "z" * 1000, "workr-once"):
            with self.subTest(token=token), contextlib.redirect_stderr(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as stopped:
                    main([token])
            self.assertEqual(stopped.exception.code, 2)
            self.assertEqual(output.getvalue(), "Unknown command.\nHelp: ai-desktop --help\n")

    def test_internal_command_remains_explicitly_available(self):
        with patch("ai_desktop.cli.agent.main", return_value=0) as agent:
            self.assertEqual(main(["agent", "version"]), 0)
        agent.assert_called_once_with(["version"])

    def test_nonterminal_login_stops_before_privilege_or_auth_checks(self):
        from ai_desktop.terminal_menu import MenuInputError
        with patch("ai_desktop.cli.os.geteuid", side_effect=AssertionError("no privilege probe")), \
                patch("ai_desktop.terminal_menu.choose", side_effect=MenuInputError()), \
                patch("ai_desktop.analysis_session.administrative") as administrative, \
                patch("ai_desktop.analysis_admin.main") as admin, \
                patch("subprocess.run") as run, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["login"]), 2)
        self.assertIn("interactive terminal", output.getvalue())
        self.assertNotIn("REFUSED", output.getvalue())
        administrative.assert_not_called()
        admin.assert_not_called()
        run.assert_not_called()

    def test_login_menu_choice_is_one_explicit_provider_authorization(self):
        for selected in (0, 1):
            with self.subTest(selected=selected), patch("ai_desktop.terminal_menu.choose", return_value=selected) as choose, \
                    patch("ai_desktop.cli.os.geteuid", return_value=1000), \
                    patch("ai_desktop.login_flow.login", side_effect=lambda provider, authenticate: authenticate()), \
                    patch("ai_desktop.analysis_session.administrative", return_value=0) as claude, \
                    patch("ai_desktop.codex_cli.main", return_value=0) as codex, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["login"]), 0)
            choose.assert_called_once()
            if selected == 0:
                claude.assert_called_once_with("login", allow=True)
                codex.assert_not_called()
            else:
                codex.assert_called_once_with(["login", "--allow-chatgpt-login"])
                claude.assert_not_called()

    def test_login_menu_cancellation_never_starts_authentication(self):
        for selected in (None, 2):
            with self.subTest(selected=selected), patch("ai_desktop.terminal_menu.choose", return_value=selected), \
                    patch("ai_desktop.cli.os.geteuid", side_effect=AssertionError("no privilege probe")), \
                    patch("ai_desktop.analysis_session.administrative") as claude, \
                    patch("ai_desktop.codex_cli.main") as codex, contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["login"]), 0)
            self.assertIn("Login cancelled", output.getvalue())
            claude.assert_not_called()
            codex.assert_not_called()

    def test_explicit_login_keeps_existing_admin_routes(self):
        for uid in (0, 1000):
            with self.subTest(uid=uid), patch("ai_desktop.cli.os.geteuid", return_value=uid), \
                    patch("ai_desktop.login_flow.login", side_effect=lambda provider, authenticate: authenticate()), \
                    patch("ai_desktop.analysis_session.administrative", return_value=0) as administrative, \
                    patch("ai_desktop.analysis_admin.main", return_value=0) as admin:
                self.assertEqual(main(["login", "--allow-claude-login"]), 0)
            if uid == 0:
                admin.assert_called_once_with(allow=True)
                administrative.assert_not_called()
            else:
                administrative.assert_called_once_with("login", allow=True)
                admin.assert_not_called()

    def test_skills_without_action_lists_skills(self):
        with patch("ai_desktop.cli.skills.main", return_value=0) as skills:
            self.assertEqual(main(["skills"]), 0)
        skills.assert_called_once_with(["list"])

    def test_doctor_verbose_routes_to_the_details_output(self):
        for flag in ("--v", "--verbose", "-v", "--details"):
            with self.subTest(flag=flag), patch("ai_desktop.cli.doctor.main", return_value=0) as doctor:
                self.assertEqual(main(["doctor", flag]), 0)
            doctor.assert_called_once_with(["--v"])

    def test_version_does_not_start_agent(self):
        with patch("ai_desktop.cli.agent.main") as agent:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as exited:
                    main(["--version"])
            self.assertEqual(exited.exception.code, 0)
            self.assertIn("ai-desktop", output.getvalue())
            agent.assert_not_called()

    def test_invalid_command_does_not_start_agent(self):
        with patch("ai_desktop.cli.agent.main") as agent:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exited:
                    main(["unknown"])
            self.assertEqual(exited.exception.code, 2)
            agent.assert_not_called()

    def test_no_implicit_ai_call(self):
        with patch("ai_desktop.cli.diagnose.ask") as ask:
            with patch("ai_desktop.cli.diagnose.collect", return_value={"kernel": "test"}):
                with patch("ai_desktop.cli.diagnose.serialize_report", return_value='{"kernel": "test"}'):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(main(["diagnose", "preview"]), 0)
            ask.assert_not_called()

    def test_diagnose_snapshot_is_explicit_and_not_ai(self):
        with patch("ai_desktop.cli.diagnose.main", return_value=0) as run:
            with patch("ai_desktop.cli.diagnose.ask") as ask:
                self.assertEqual(main(["diagnose", "snapshot"]), 0)
        run.assert_called_once_with(["snapshot"])
        ask.assert_not_called()

    def test_diagnose_save_requires_explicit_path_without_ai(self):
        with patch("ai_desktop.cli.diagnose.main", return_value=0) as run:
            with patch("ai_desktop.cli.diagnose.ask") as ask:
                self.assertEqual(main(["diagnose", "save", "/tmp/new-report.json"]), 0)
        run.assert_called_once_with(["save", "/tmp/new-report.json"])
        ask.assert_not_called()

    def test_diagnose_ask_explicit(self):
        with patch("ai_desktop.cli.diagnose.main", return_value=7) as run:
            self.assertEqual(main(["diagnose", "ask"]), 7)
            run.assert_called_once_with(["ask"])

    def test_doctor_routing_no_ai(self):
        with patch("ai_desktop.cli.doctor.main", return_value=0) as doctor:
            with patch("ai_desktop.cli.diagnose.ask") as ask:
                self.assertEqual(main(["doctor"]), 0)
                doctor.assert_called_once_with([])
                ask.assert_not_called()

    def test_validate_report_routes_without_ai(self):
        with patch("ai_desktop.cli.validate_snapshot.main", return_value=0) as run:
            with patch("ai_desktop.cli.diagnose.ask") as ask:
                self.assertEqual(main(["validate-report", "report.json"]), 0)
        run.assert_called_once_with(["report.json"])
        ask.assert_not_called()

    def test_validate_report_requires_exactly_one_file(self):
        with patch("ai_desktop.cli.validate_snapshot.main") as run:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as missing:
                    main(["validate-report"])
                with self.assertRaises(SystemExit) as extra:
                    main(["validate-report", "a", "b"])
        self.assertEqual(missing.exception.code, 2)
        self.assertEqual(extra.exception.code, 2)
        run.assert_not_called()

    def test_compare_routes_to_offline_module(self):
        with patch("ai_desktop.cli.compare.main", return_value=0) as run:
            with patch("ai_desktop.cli.diagnose.ask") as ask:
                self.assertEqual(main(["compare", "a.json", "b.json"]), 0)
        run.assert_called_once_with(["a.json", "b.json"])
        ask.assert_not_called()

    def test_compare_summary_routes_without_model(self):
        with patch("ai_desktop.cli.compare.main", return_value=0) as run:
            with patch("ai_desktop.cli.diagnose.ask") as ask:
                self.assertEqual(main(["compare", "--summary", "a.json", "b.json"]), 0)
        run.assert_called_once_with(["--summary", "a.json", "b.json"])
        ask.assert_not_called()

    def test_compare_exit_on_change_routes_without_model(self):
        with patch("ai_desktop.cli.compare.main", return_value=3) as run:
            with patch("ai_desktop.cli.diagnose.ask") as ask:
                self.assertEqual(main(["compare", "--exit-on-change", "a.json", "b.json"]), 3)
                self.assertEqual(main(["compare", "--summary", "--exit-on-change", "a.json", "b.json"]), 3)
        self.assertEqual(run.call_args_list[0].args[0], ["--exit-on-change", "a.json", "b.json"])
        self.assertEqual(run.call_args_list[1].args[0], ["--summary", "--exit-on-change", "a.json", "b.json"])
        ask.assert_not_called()

    def test_compare_requires_exactly_two_paths(self):
        with patch("ai_desktop.cli.compare.main") as run:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exited:
                    main(["compare", "a.json"])
        self.assertEqual(exited.exception.code, 2)
        run.assert_not_called()

    def test_controller_forward_args(self):
        with patch("ai_desktop.cli.controller.main", return_value=0) as run:
            self.assertEqual(main(["controller", "preview"]), 0)
            run.assert_called_once_with(["preview"])

    def test_controller_rejects_arbitrary_action(self):
        with patch("ai_desktop.cli.controller.main") as run:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exited:
                    main(["controller", "exec", "echo", "secret"])
            self.assertEqual(exited.exception.code, 2)
            run.assert_not_called()

    def test_executor_forward_args(self):
        with patch("ai_desktop.cli.executor.main", return_value=0) as worker:
            self.assertEqual(main(["executor", "self-test"]), 0)
            worker.assert_called_once_with(["self-test"])

    def test_executor_has_no_arbitrary_command(self):
        with patch("ai_desktop.cli.executor.main") as worker:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exited:
                    main(["executor", "bash", "-c", "echo something"])
            self.assertEqual(exited.exception.code, 2)
            worker.assert_not_called()

    def test_skills_forward_args(self):
        with patch("ai_desktop.cli.skills.main", return_value=0) as run:
            self.assertEqual(main(["skills", "show", "cachyos-diagnostics"]), 0)
            run.assert_called_once_with(["show", "cachyos-diagnostics"])

    def test_audit_only_reads(self):
        with patch("ai_desktop.cli.audit.audit_lines", return_value=["one", "two"]):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["audit"]), 0)
            self.assertEqual(output.getvalue(), "one\ntwo\n")

    def test_offline_commands_forward_args(self):
        for name, target in (
            ("agent", "ai_desktop.cli.agent.main"),
            ("launcher", "ai_desktop.cli.launcher.main"),
            ("sandbox", "ai_desktop.cli.sandbox.main"),
            ("skill-mount", "ai_desktop.cli.skill_mount.main"),
        ):
            action = "self-test" if name in ("sandbox", "skill-mount") else "version"
            with self.subTest(command=name), patch(target, return_value=0) as run:
                self.assertEqual(main([name, action]), 0)
                run.assert_called_once_with([action])

    def test_dangerous_arguments_rejected(self):
        with patch("ai_desktop.cli.agent.main") as agent:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exited:
                    main(["agent", "--dangerously-skip-permissions"])
            self.assertEqual(exited.exception.code, 2)
            agent.assert_not_called()



class HelpTests(unittest.TestCase):
    DAILY = ("ask", "material", "project", "task", "status", "stop", "settings", "agents", "sessions", "skills", "login", "doctor")
    LEGACY = ("claude", "codex")
    INTERNAL = ("controller", "executor", "handoff-once", "handoff", "worker-once", "workspace", "launcher", "sandbox",
                "skill-mount", "agent", "diagnose", "validate-report", "compare", "audit")

    def text(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("subprocess.run", side_effect=AssertionError("help runs nothing")), \
                patch("subprocess.Popen", side_effect=AssertionError("help runs nothing")):
            code = main(argv)
        return code, out.getvalue()

    def test_the_default_help_is_short_task_oriented_and_points_to_the_full_list(self):
        for flag in (["--help"], ["-h"]):
            code, text = self.text(flag)
            self.assertEqual(code, 0)
            for command in self.DAILY:
                self.assertRegex(text, r"(?m)^  %s\s" % command)
            for command in self.INTERNAL + self.LEGACY:
                self.assertNotRegex(text, r"(?m)^  %s\s" % command)
            self.assertIn("ai-desktop --help-all", text)
            self.assertLess(len(text.splitlines()), 22)

    def test_the_full_help_lists_every_command_and_still_has_no_host_effects(self):
        code, text = self.text(["--help-all"])
        self.assertEqual(code, 0)
        for command in self.DAILY + self.INTERNAL + self.LEGACY:
            self.assertIn(command, text)

    def test_hiding_commands_from_the_short_help_does_not_remove_them(self):
        from ai_desktop.cli import build_parser
        parser = build_parser()
        choices = next(action.choices for action in parser._actions if getattr(action, "choices", None))
        for command in self.DAILY + self.INTERNAL + self.LEGACY:
            self.assertIn(command, choices)                                        # still parsed, only absent from the short text

    def test_project_is_not_described_as_read_only_because_it_can_change_and_delete_after_approval(self):
        from ai_desktop.cli import build_parser, SHORT_HELP
        full = build_parser().format_help()
        for text in (SHORT_HELP, full):
            self.assertNotIn("project    Work with one project directory read-only", text)
        self.assertIn("changes and deletions only after your approval", SHORT_HELP)
        self.assertIn("changes and deletions only after approval", full)


if __name__ == "__main__":
    unittest.main()
