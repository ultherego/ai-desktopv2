"""Quiet administrative login preserves private TTY and verified cleanup."""
from contextlib import redirect_stdout
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import analysis_admin, analysis_service, analysis_session, analysis_session_admin
from ai_desktop.analysis_contract import AnalysisDenied


class LoginCommandTests(unittest.TestCase):
    def test_quiet_or_verbose_changes_only_diagnostics_and_fixed_worker_action(self):
        unit = Path(__file__).resolve().parents[1] / "packaging/systemd/ai-desktop-analysis@.service"
        with patch.object(analysis_admin, "UNIT", unit), patch.object(analysis_admin, "trusted_file"), \
                patch.object(analysis_admin.secrets, "token_hex", return_value="a" * 16):
            quiet = analysis_admin.login_command()
            verbose = analysis_admin.login_command(details=True)
        self.assertIn("--quiet", quiet)
        self.assertNotIn("--quiet", verbose)
        self.assertEqual(quiet[-1], "login")
        self.assertEqual(verbose[-1], "login-verbose")
        self.assertEqual([arg for arg in quiet[:-1] if arg != "--quiet"], verbose[:-1])
        for command in (quiet, verbose):
            self.assertIn("--pty", command)
            self.assertIn("--wait", command)
            self.assertIn("--property=KillMode=control-group", command)
            self.assertIn("--property=User=ai-desktop", command)

    def test_invalid_details_refused_before_reading_unit(self):
        for value in (None, 0, 1, "yes"):
            with patch.object(analysis_admin, "trusted_file") as trusted, \
                    self.assertRaises(AnalysisDenied):
                analysis_admin.login_command(details=value)
            trusted.assert_not_called()


class ClaudeAdminOutputTests(unittest.TestCase):
    def run_admin(self, *, details=False, native_code=0, state_code=0,
                  state="LoadState=not-found\n", exception=None):
        commands = []
        def run(command, **kwargs):
            commands.append((command, kwargs))
            if command[0] == "/usr/bin/systemd-run":
                if exception:
                    raise exception
                return SimpleNamespace(returncode=native_code)
            if command[1] == "stop":
                return SimpleNamespace(returncode=0)
            return SimpleNamespace(returncode=state_code, stdout=state)
        unit = Path(__file__).resolve().parents[1] / "packaging/systemd/ai-desktop-analysis@.service"
        output = io.StringIO()
        with patch.object(analysis_admin, "UNIT", unit), patch.object(analysis_admin, "trusted_file"), \
                patch.object(analysis_admin, "runtime_ready"), \
                patch.object(analysis_admin.os, "getuid", return_value=0), \
                patch.object(analysis_admin.os, "geteuid", return_value=0), \
                patch.object(analysis_admin.os, "isatty", return_value=True), \
                patch.object(analysis_admin.subprocess, "run", side_effect=run), redirect_stdout(output):
            code = analysis_admin.main(allow=True, details=details)
        return code, output.getvalue(), commands

    def test_default_success_is_silent_and_preserves_tty_without_capture(self):
        code, text, calls = self.run_admin()
        self.assertEqual((code, text), (0, ""))
        self.assertIn("--quiet", calls[0][0])
        self.assertFalse(set(calls[0][1]) & {"stdin", "stdout", "stderr", "capture_output"})
        self.assertEqual(calls[-1][0][1], "show")

    def test_verbose_preserves_explanations_and_metadata(self):
        code, text, calls = self.run_admin(details=True)
        self.assertEqual(code, 0)
        self.assertIn("Separate claude.ai login", text)
        self.assertNotIn("--quiet", calls[0][0])
        self.assertEqual(calls[0][0][-1], "login-verbose")

    def test_errors_and_cancellation_do_not_turn_into_success(self):
        for kwargs in ({"native_code": 2}, {"exception": KeyboardInterrupt()},
                       {"state": "LoadState=loaded\nActiveState=active\nMainPID=123\n"},
                       {"state_code": 2, "state": "LoadState=not-found\n"},
                       {"state_code": 1, "state": "LoadState=loaded\nActiveState=inactive\nMainPID=0\n"}):
            with self.subTest(kwargs=kwargs):
                code, text, calls = self.run_admin(**kwargs)
                self.assertEqual(code, 2)
                self.assertIn("REFUSED", text)
                self.assertIn("stop", calls[1][0])
                self.assertNotIn("PASS", text)
                self.assertNotIn("Logged in", text)
        code, text, _ = self.run_admin(state_code=1)
        self.assertEqual((code, text), (0, ""))


class LoginWorkerOutputTests(unittest.TestCase):
    def test_claude_worker_has_success_diagnostics_only_in_verbose(self):
        for details in (False, True):
            with self.subTest(details=details), patch.object(analysis_service.os, "isatty", return_value=True), \
                    patch.object(analysis_service, "run_client") as run, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(analysis_service.worker_main(["login-verbose" if details else "login"]), 0)
            run.assert_called_once_with("login")
            self.assertEqual(output.getvalue(), "LOGIN: PASS auth=claude.ai\n" if details else "")

    def test_claude_worker_failure_remains_visible_in_both_modes(self):
        for action in ("login", "login-verbose"):
            with patch.object(analysis_service.os, "isatty", return_value=True), \
                    patch.object(analysis_service, "run_client", side_effect=AnalysisDenied("client")), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(analysis_service.worker_main([action]), 2)
            self.assertIn("LOGIN: REFUSED", output.getvalue())


class LoginHelperTests(unittest.TestCase):
    def test_desktop_verbose_action_is_fixed_and_auth_output_is_not_captured(self):
        for details in (False, True):
            with patch.object(analysis_session, "authenticate") as authenticate, \
                    patch.object(analysis_session, "trusted_file"), \
                    patch.object(analysis_session.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
                self.assertEqual(analysis_session.administrative("login", allow=True, details=details), 0)
            authenticate.assert_called_once_with(**({} if details else {"quiet": True}))
            self.assertEqual(run.call_args.args[0][-1], "login-verbose" if details else "login")
            self.assertFalse(set(run.call_args.kwargs) & {"stdin", "stdout", "stderr", "capture_output"})

    def test_root_helper_maps_only_fixed_verbose_login_action(self):
        for args, kwargs in ((["login"], {"allow": True}),
                             (["login-verbose"], {"allow": True, "details": True})):
            with patch.object(analysis_session_admin.os, "getuid", return_value=0), \
                    patch.object(analysis_session_admin.os, "geteuid", return_value=0), \
                    patch.object(analysis_session_admin, "desktop_uid", return_value=1000), \
                    patch.object(analysis_admin, "main", return_value=0) as login:
                self.assertEqual(analysis_session_admin.main(args), 0)
            login.assert_called_once_with(**kwargs)
        with patch.object(analysis_session_admin.os, "getuid", return_value=0), \
                patch.object(analysis_session_admin.os, "geteuid", return_value=0), \
                patch.object(analysis_session_admin.os, "write"), patch.object(analysis_admin, "main") as login:
            for args in (["login", "--verbose"], ["login-verbose", "TOKEN"]):
                self.assertEqual(analysis_session_admin.main(args), 2)
        login.assert_not_called()

    def test_invalid_details_stops_before_desktop_authentication(self):
        with patch.object(analysis_session, "authenticate") as authenticate, \
                patch.object(analysis_session.subprocess, "run") as run, redirect_stdout(io.StringIO()):
            for details in (None, 0, "yes"):
                self.assertEqual(analysis_session.administrative("login", allow=True, details=details), 2)
        authenticate.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
