"""Provider-neutral settings use private local profiles, never account actions."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import agent_profiles as profiles, management_cli


class SettingsManagementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aid-settings-", dir=Path.home())
        self.addCleanup(self.temporary.cleanup)
        self.store = profiles.Store(Path(self.temporary.name))

    def call(self, argv, *, neutral=True, **kwargs):
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            guards = [stack.enter_context(patch(target)) for target in (
                "subprocess.run", "subprocess.Popen", "socket.socket",
                "ai_desktop.analysis_session.administrative", "ai_desktop.analysis_admin.main",
                "ai_desktop.codex_cli.main", "ai_desktop.cli.login_menu")]
            code = management_cli.main(list(argv), store=self.store,
                                       prog="ai-desktop settings" if neutral else "ai-desktop claude",
                                       provider_neutral=neutral, **kwargs)
            for guard in guards:
                guard.assert_not_called()
        return code, output.getvalue()

    def select_codex(self):
        value = profiles.Profile(provider="codex", access="read-only", instructions="Moje instrukcje.")
        self.store.mutate(lambda config: config.put("default", value))
        return value

    def test_canonical_help_names_configuration_without_claude_only_title(self):
        with self.assertRaises(SystemExit) as stopped:
            self.call(["--help"])
        self.assertEqual(stopped.exception.code, 0)
        text = management_cli.build_parser(prog="ai-desktop settings", provider_neutral=True).format_help()
        self.assertIn("ai-desktop settings", text)
        self.assertIn("settings for agents, models, reasoning and Skills", text)
        self.assertNotIn("Local Claude settings", text)
        self.assertIn("no login", text)

    def test_neutral_set_updates_codex_effort_and_keeps_role_authority(self):
        before = self.select_codex()
        code, output = self.call(["set", "--effort", "xhigh"])
        self.assertEqual(code, 0)
        selected = self.store.load().config.profiles["default"]
        self.assertEqual(selected, replace(before, effort="xhigh"))
        self.assertIn("codex", output)
        self.assertIn("very high", output)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_legacy_claude_set_still_refuses_codex_profile(self):
        self.select_codex()
        before = self.store.path.read_bytes()
        code, output = self.call(["set", "--effort", "high"], neutral=False)
        self.assertEqual(code, 2)
        self.assertIn("applies to a Claude profile", output)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_invalid_codex_model_and_effort_never_publish_partial_changes(self):
        self.select_codex()
        before = self.store.path.read_bytes()
        for arguments in (("set", "--model", "opus", "--effort", "high"),
                          ("set", "--model", "default", "--effort", "max")):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.call(arguments)[0], 2)
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_neutral_models_show_only_active_client_choices(self):
        self.select_codex()
        before = self.store.load()
        code, text = self.call(["models"])
        self.assertEqual(code, 0)
        self.assertIn("default", text)
        self.assertIn("Codex", text)
        self.assertIn("Reasoning: default, low, medium, high, xhigh.", text)
        for alias in ("sonnet", "opus", "haiku", "max"):
            self.assertNotIn(alias, text)
        self.assertEqual(self.store.load(), before)
        code, legacy = self.call(["models"], neutral=False)
        self.assertEqual(code, 0)
        for alias in ("sonnet", "opus", "haiku", "max"):
            self.assertIn(alias, legacy)

    def test_nonterminal_summary_recommends_the_canonical_settings_command(self):
        with patch.object(management_cli.sys.stdin, "isatty", return_value=False), \
                patch.object(management_cli.sys.stdout, "isatty", return_value=False):
            code, text = self.call([])
        self.assertEqual(code, 0)
        self.assertIn("Settings: ai-desktop settings", text)
        self.assertNotIn("claude menu", text)
        self.assertFalse(self.store.path.exists())

    def test_terminal_settings_reuses_menu_and_cancel_creates_no_config(self):
        selections = []
        def choose(title, options, *, default=0):
            selections.append((title, options))
            return None
        code, _ = self.call([], chooser=choose)
        self.assertEqual(code, 0)
        self.assertEqual(len(selections), 1)
        self.assertIn("Settings", selections[0][0])
        self.assertFalse(self.store.path.exists())

    def test_neutral_injected_menu_can_select_codex_reasoning(self):
        self.select_codex()
        answers = iter(("3", "5"))  # reasoning -> xhigh
        self.assertEqual(self.call(["menu"], reader=lambda _: next(answers))[0], 0)
        self.assertEqual(self.store.load().config.profiles["default"].effort, "xhigh")

    def test_claude_settings_keep_existing_profile_semantics(self):
        self.assertEqual(self.call(["set", "--model", "opus", "--effort", "max"])[0], 0)
        selected = self.store.load().config.profiles["default"]
        self.assertEqual((selected.provider, selected.model, selected.effort), ("claude", "opus", "max"))

    def test_explicit_service_flag_selects_all_roles_and_restores_client_settings(self):
        self.assertEqual(self.call(["set", "--model", "opus", "--effort", "max"])[0], 0)
        self.assertEqual(self.call(["set", "--provider", "codex", "--effort", "high"])[0], 0)
        config = self.store.load().config
        self.assertEqual(config.selected_provider, "codex")
        self.assertTrue(all(selected.provider == "codex" for _, selected in config.entries))
        self.assertEqual(self.call(["agents", "use", "diagnostic"])[0], 0)
        self.assertEqual(self.store.load().config.selected_provider, "codex")
        self.assertEqual(self.call(["set", "--provider", "claude"])[0], 0)
        self.assertEqual(self.call(["agents", "use", "default"])[0], 0)
        selected = self.store.load().config.profiles["default"]
        self.assertEqual((selected.provider, selected.model, selected.effort), ("claude", "opus", "max"))
        self.assertEqual(self.call(["set", "--provider", "codex"])[0], 0)
        self.assertEqual(self.store.load().config.profiles["default"].effort, "high")

    def test_invalid_service_and_model_combination_keeps_previous_complete_snapshot(self):
        self.assertEqual(self.call(["set", "--model", "opus", "--effort", "max"])[0], 0)
        before = self.store.load()
        self.assertEqual(self.call(["set", "--provider", "codex", "--model", "opus"])[0], 2)
        self.assertEqual(self.store.load(), before)

    def test_agent_creation_and_explicit_provider_flags_follow_global_service(self):
        self.assertEqual(self.call(["set", "--provider", "codex"])[0], 0)
        self.assertEqual(self.call(["agents", "create", "team", "--effort", "high"])[0], 0)
        self.assertEqual(self.store.load().config.profiles["team"].provider, "codex")
        self.assertEqual(self.call(["agents", "update", "team", "--provider", "claude", "--model", "opus"])[0], 0)
        config = self.store.load().config
        self.assertTrue(all(selected.provider == "claude" for _, selected in config.entries))
        self.assertEqual(config.profiles["team"].model, "opus")

    def test_legacy_mixed_agent_listing_and_show_use_current_service_without_writing(self):
        self.store.mutate(lambda config: config.put("diagnostic", replace(config.profiles["diagnostic"],
                          provider="codex")).select("diagnostic"))
        before = self.store.load()
        code, output = self.call(["agents", "list"])
        self.assertEqual(code, 0)
        self.assertNotIn("claude", output)
        code, output = self.call(["agents", "show", "reviewer"])
        self.assertEqual(code, 0)
        self.assertIn('"provider": "codex"', output)
        self.assertEqual(self.store.load(), before)
        self.assertEqual(self.call(["agents", "use", "reviewer"])[0], 0)
        config = self.store.load().config
        self.assertEqual((config.service, config.active), ("codex", "reviewer"))

    def test_service_selection_describes_profile_routing_and_never_starts_login(self):
        self.store.mutate(lambda config: config.put("default", profiles.Profile(model="opus", effort="max")))
        calls = []
        def choose(title, options, *, default=0):
            calls.append((title, options))
            if len(calls) == 1:
                return next(index for index, label in enumerate(options) if label.startswith("Service:"))
            if len(calls) == 2:
                self.assertIn("no login", title)
                return next(index for index, label in enumerate(options) if label.startswith("ChatGPT / Codex"))
            return None
        code, _ = self.call([], chooser=choose)
        self.assertEqual(code, 0)
        selected = self.store.load().config.profiles["default"]
        self.assertEqual((selected.provider, selected.model, selected.effort), ("codex", "default", "default"))
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
