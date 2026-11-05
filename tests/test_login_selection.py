"""Desktop login completion with real private settings and synthetic auth only."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from dataclasses import replace
import io
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import agent_profiles as profiles, cli, codex_cli, login_flow


class LoginSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aid-login-selection-", dir=Path.home())
        self.addCleanup(self.temporary.cleanup)
        self.store = profiles.Store(Path(self.temporary.name))

    def run_flow(self, provider, authenticate):
        output = io.StringIO()
        with redirect_stdout(output):
            code = login_flow.login(provider, authenticate, store=self.store)
        return code, output.getvalue()

    def transports(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        # Stub only controller dispatch identity, never Store's real UID checks.
        stack.enter_context(patch.object(cli, "os", SimpleNamespace(geteuid=lambda: 1000)))
        stack.enter_context(patch.object(codex_cli, "os", SimpleNamespace(
            geteuid=lambda: 1000, getuid=lambda: 1000, isatty=lambda fd: True)))
        stack.enter_context(patch.object(profiles, "Store", return_value=self.store))
        stack.enter_context(patch("ai_desktop.codex_runtime.runtime_ready"))
        stack.enter_context(patch("ai_desktop.analysis_sandbox.trusted_file", side_effect=lambda path: path))
        process = stack.enter_context(patch.object(codex_cli.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0)))
        claude = stack.enter_context(patch("ai_desktop.analysis_session.administrative", return_value=0))
        return claude, process

    def test_success_selects_every_role_and_next_execution_without_changing_role_data(self):
        config = profiles.defaults().put("default", profiles.Profile(model="opus", effort="max",
            instructions="Własna rola.", access="read-only"))
        self.store.mutate(lambda _: config)
        calls = []
        def authenticate():
            calls.append(self.store.load().config.selected_provider)
            return 0
        code, output = self.run_flow("codex", authenticate)
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["claude"])
        self.assertEqual(output, "Logged in. Active service: Codex.\n")
        for name in config.profiles:
            self.assertEqual(profiles.resolve(name, store=self.store)["provider"], "codex")
            self.assertEqual(self.store.load().config.profiles[name].access, config.profiles[name].access)
            self.assertEqual(self.store.load().config.profiles[name].instructions, config.profiles[name].instructions)
        self.assertEqual(self.run_flow("claude", lambda: 0)[0], 0)
        self.assertEqual(profiles.resolve(store=self.store)["model"], "opus")
        self.assertEqual(profiles.resolve(store=self.store)["effort"], "max")

    def test_failed_or_cancelled_auth_never_creates_or_changes_settings(self):
        for result in (2, 130, 7):
            with self.subTest(result=result):
                code, output = self.run_flow("codex", lambda: result)
                self.assertEqual(code, result)
                self.assertNotIn("Logged in", output)
                self.assertFalse(self.store.path.exists())

    def test_concurrent_edit_survives_login_and_partial_success_is_explicit(self):
        initial = self.store.mutate(lambda value: value)
        def authenticate():
            self.store.mutate(lambda value: value.put("default", replace(value.profiles["default"],
                instructions="Równoległa zmiana.")), expected=initial)
            return 0
        code, output = self.run_flow("codex", authenticate)
        self.assertEqual(code, 3)
        self.assertIn("Logged in to Codex", output)
        self.assertIn("The service was not switched", output)
        actual = self.store.load().config
        self.assertEqual(actual.selected_provider, "claude")
        self.assertEqual(actual.profiles["default"].instructions, "Równoległa zmiana.")

    def test_unwritable_postlogin_config_is_not_misreported_as_failed_login(self):
        with patch.object(self.store, "mutate", side_effect=profiles.ProfileError("access")):
            code, output = self.run_flow("claude", lambda: 0)
        self.assertEqual(code, 3)
        self.assertIn("Logged in to Claude", output)
        self.assertIn("not switched", output)
        self.assertFalse(self.store.path.exists())

    def test_invalid_preflight_config_stops_before_auth(self):
        with patch.object(self.store, "load", side_effect=profiles.ProfileError("file")), \
                patch("subprocess.run", side_effect=AssertionError("no auth")) as auth:
            code, output = self.run_flow("codex", auth)
        self.assertEqual(code, 2)
        self.assertIn("the login was not started", output)
        auth.assert_not_called()

    def test_boolean_or_missing_success_is_not_an_auth_receipt(self):
        for result in (True, False, None):
            with self.subTest(result=result):
                code, output = self.run_flow("codex", lambda: result)
                self.assertEqual(code, 2)
                self.assertNotIn("Logged in", output)
                self.assertFalse(self.store.path.exists())

    def test_canonical_provider_flags_and_legacy_codex_all_activate_once(self):
        claude, process = self.transports()
        for arguments in (["login", "--codex"], ["login", "--provider", "codex"],
                          ["login", "--allow-chatgpt-login"], ["codex", "login", "--allow-chatgpt-login"],
                          ["login", "--claude"], ["login", "--provider", "claude"],
                          ["login", "--allow-claude-login"]):
            with self.subTest(arguments=arguments), redirect_stdout(io.StringIO()) as output:
                previous = claude.call_count + process.call_count
                self.assertEqual(cli.main(arguments), 0)
                expected = "claude" if "claude" in arguments[-1] else "codex"
                self.assertEqual(self.store.load().config.selected_provider, expected)
                self.assertEqual(claude.call_count + process.call_count, previous + 1)
                self.assertEqual(output.getvalue().count("Logged in."), 1)

    def test_menu_uses_same_activation_path_and_cancel_is_read_only(self):
        claude, process = self.transports()
        with patch("ai_desktop.terminal_menu.choose", return_value=1), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["login"]), 0)
        self.assertEqual(self.store.load().config.selected_provider, "codex")
        self.assertEqual(process.call_count, 1)
        before = self.store.load()
        with patch("ai_desktop.terminal_menu.choose", return_value=None), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["login"]), 0)
        self.assertEqual(self.store.load(), before)
        self.assertEqual(process.call_count, 1)
        claude.assert_not_called()

    def test_verbose_flags_reach_only_fixed_privileged_actions(self):
        claude, process = self.transports()
        for flag in ("--v", "--verbose", "-v", "--details"):
            with self.subTest(flag=flag), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["login", "--codex", flag]), 0)
                self.assertEqual(process.call_args.args[0][-1], "login-verbose")
                self.assertEqual(cli.main(["login", "--claude", flag]), 0)
                self.assertEqual(claude.call_args.kwargs, {"allow": True, "details": True})

    def test_conflicting_service_flags_and_help_have_no_account_or_config_effect(self):
        claude, process = self.transports()
        for arguments in (["login", "--codex", "--claude"], ["login", "--provider", "codex", "--claude"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                cli.main(arguments)
            self.assertEqual(error.exception.code, 2)
        with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as error:
            cli.main(["login", "--help"])
        self.assertEqual(error.exception.code, 0)
        self.assertIn("--provider", output.getvalue())
        self.assertIn("--codex", output.getvalue())
        self.assertIn("--v", output.getvalue())
        claude.assert_not_called()
        process.assert_not_called()
        self.assertFalse(self.store.path.exists())
