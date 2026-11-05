"""Complete settings flows through real private config transactions; no AI/auth."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import agent_profiles as profiles, management_cli, skills
from ai_desktop.terminal_menu import MenuInputError


class Choices:
    def __init__(self, test, *steps):
        self.test, self.steps = test, list(steps)
        self.calls = []

    def __call__(self, title, options, *, default=0):
        self.calls.append((title, options, default))
        self.test.assertLessEqual(len(options), 12)
        self.test.assertTrue(self.steps, "unexpected menu: " + title)
        step = self.steps.pop(0)
        if step is None:
            return None
        matches = [number for number, label in enumerate(options) if label.startswith(step)]
        self.test.assertEqual(len(matches), 1, (step, options))
        return matches[0]


class CompleteManagementMenuTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aid-menu-", dir=Path.home())
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.store = profiles.Store(self.home)
        self.root = self.home / "installed-skills"
        self.root.mkdir(mode=0o755)
        for name in ("demo", "other", "third", "fourth", "fifth", "concise-assistant",
                     "cachyos-diagnostics", "code-development", "code-review", "project-analysis"):
            self.skill(name)
        self.patch = patch.object(skills, "ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def skill(self, name, body="Sprawdź dostępne dowody."):
        directory = self.root / name
        directory.mkdir(mode=0o755)
        path = directory / "SKILL.md"
        path.write_text("---\nname: " + name + "\ndescription: Test\n---\n" + body + "\n")
        path.chmod(0o644)

    def install_diagnostic_procedures(self):
        for name in profiles.PRESET_SKILLS["diagnostic"]:
            if not (self.root / name).exists():
                self.skill(name)

    def run_menu(self, *steps, argv=("menu",), text=()):
        chooser = Choices(self, *steps)
        answers = iter(text)
        output = io.StringIO()
        with redirect_stdout(output), patch("subprocess.run") as run, patch("subprocess.Popen") as spawn:
            code = management_cli.main(list(argv), store=self.store, chooser=chooser,
                                       reader=lambda _: next(answers))
        run.assert_not_called()
        spawn.assert_not_called()
        self.assertFalse(chooser.steps, "unused menu choices: " + repr(chooser.steps))
        return code, output.getvalue(), chooser

    def test_no_argument_entries_and_nested_cancel_do_not_create_configuration(self):
        for argv in ((), ("agents",)):
            with self.subTest(argv=argv):
                code, _, _ = self.run_menu(None, argv=argv)
                self.assertEqual(code, 0)
                self.assertFalse(self.store.path.parent.exists())
        code, _, _ = self.run_menu("Model:", None, "Exit")
        self.assertEqual(code, 0)
        self.assertFalse(self.store.path.parent.exists())

    def test_model_effort_access_and_instructions_persist_together(self):
        code, output, _ = self.run_menu("Model:", "Opus", "Reasoning:", "high", "Access:",
                                       "Read-only", "Instructions:", "Type new", "Exit",
                                       text=("Wskaż dowód i ograniczenia.",))
        self.assertEqual(code, 0)
        selected = self.store.load().config.profiles["default"]
        self.assertEqual((selected.model, selected.effort, selected.access), ("opus", "high", "read-only"))
        self.assertEqual(selected.instructions, "Wskaż dowód i ograniczenia.")
        self.assertIn("model: Opus", output)

    def test_codex_selection_keeps_claude_settings_and_only_offers_default(self):
        self.store.mutate(lambda config: config.put("default", replace(config.profiles["default"], model="opus", effort="max")))
        code, _, choices = self.run_menu("Service:", "ChatGPT / Codex", "Model:", "Back",
                                         "Reasoning:", "high", "Exit")
        self.assertEqual(code, 0)
        selected = self.store.load().config.profiles["default"]
        self.assertEqual((selected.provider, selected.model, selected.effort), ("codex", "default", "high"))
        model = next(options for title, options, _ in choices.calls if title == "Model · codex")
        self.assertEqual(model, ("Auto (client default)", "Other ID (type it)", "Back"))
        efforts = next(options for title, options, _ in choices.calls if title == "Reasoning level")
        self.assertIn("very high", efforts)
        self.assertNotIn("maximum", efforts)
        provider = next((title, options) for title, options, _ in choices.calls if title.startswith("Service ·"))
        self.assertIn("no login", provider[0])
        self.assertIn("for all agents", provider[0])
        restored = self.store.load().config.with_provider("claude").profiles["default"]
        self.assertEqual((restored.model, restored.effort), ("opus", "max"))

    def test_claude_can_select_both_extended_effort_levels(self):
        code, _, _ = self.run_menu("Reasoning:", "very high", "Reasoning:", "maximum", "Exit")
        self.assertEqual(code, 0)
        self.assertEqual(self.store.load().config.profiles["default"].effort, "max")

    def test_every_role_uses_selected_service_without_reopening_settings(self):
        self.install_diagnostic_procedures()
        self.store.mutate(profiles.prepared)
        before = self.store.load().config
        steps = ["Service:", "ChatGPT / Codex"]
        for name in before.profiles:
            steps.extend(("Choose agent", name + " ·"))
        steps.append("Exit")
        code, _, choices = self.run_menu(*steps)
        self.assertEqual(code, 0)
        after = self.store.load().config
        self.assertEqual(after.selected_provider, "codex")
        for name, previous in before.entries:
            current = after.profiles[name]
            self.assertEqual(current.provider, "codex")
            self.assertEqual((current.instructions, current.access, current.skills),
                             (previous.instructions, previous.access, previous.skills))
        for title, options, _ in choices.calls:
            if title == "Choose agent":
                self.assertTrue(all(label.endswith(" · Codex") for label in options[:-1]), options)
            elif title.startswith("Settings") and "Service: Codex" in options:
                self.assertNotIn("Service: Claude", options)
        self.assertEqual(sum(title.startswith("Settings") for title, _, _ in choices.calls),
                         len(before.entries) + 2)

    def test_legacy_mixed_roles_display_current_service_and_cancel_does_not_migrate(self):
        self.store.mutate(lambda config: config.put("diagnostic", replace(config.profiles["diagnostic"],
                          provider="codex")).select("diagnostic"))
        before = self.store.load()
        self.assertIsNone(before.config.service)
        code, _, choices = self.run_menu("Choose agent", "Back", "Exit")
        self.assertEqual(code, 0)
        self.assertEqual(self.store.load(), before)
        picker = next(options for title, options, _ in choices.calls if title == "Choose agent")
        self.assertTrue(all(label.endswith(" · Codex") for label in picker[:-1]))

    def test_legacy_role_pick_keeps_active_codex_and_caches_claude_model(self):
        self.store.mutate(lambda config: config.put("reviewer", replace(config.profiles["reviewer"],
                          model="opus", effort="max")).put("diagnostic", replace(config.profiles["diagnostic"],
                          provider="codex")).select("diagnostic"))
        code, _, choices = self.run_menu("Choose agent", "reviewer · Codex", "Exit")
        self.assertEqual(code, 0)
        config = self.store.load().config
        self.assertEqual((config.active, config.service, config.profiles["reviewer"].provider),
                         ("reviewer", "codex", "codex"))
        last = next(options for title, options, _ in reversed(choices.calls) if title.startswith("Settings"))
        self.assertIn("Service: Codex", last)
        self.assertIn("Model: Auto (client default)", last)
        restored = config.with_provider("claude").profiles["reviewer"]
        self.assertEqual((restored.model, restored.effort), ("opus", "max"))

    def test_legacy_other_role_editor_previews_current_service_and_publishes_it(self):
        self.store.mutate(lambda config: config.put("diagnostic", replace(config.profiles["diagnostic"],
                          provider="codex")).select("diagnostic"))
        code, _, choices = self.run_menu("Edit profile", "reviewer · Codex", "Reasoning:", "high",
                                        "Back", "Back", argv=("agents", "menu"))
        self.assertEqual(code, 0)
        config = self.store.load().config
        self.assertEqual((config.selected_provider, config.profiles["reviewer"].provider,
                          config.profiles["reviewer"].effort), ("codex", "codex", "high"))
        frames = [options for title, options, _ in choices.calls if title == "Profile: reviewer"]
        self.assertTrue(all("Service: Codex" in frame for frame in frames))

    def test_service_roundtrip_restores_each_clients_model_and_effort_in_place(self):
        self.store.mutate(lambda config: config.put("default", replace(config.profiles["default"],
                          model="opus", effort="max", instructions="Moja rola.")))
        code, _, choices = self.run_menu("Service:", "ChatGPT / Codex", "Reasoning:", "high",
                                        "Service:", "Claude", "Service:", "ChatGPT / Codex", "Exit")
        self.assertEqual(code, 0)
        root_frames = [options for title, options, _ in choices.calls if title.startswith("Settings")]
        self.assertIn("Model: Opus", root_frames[-2])
        self.assertIn("Reasoning: maximum", root_frames[-2])
        self.assertIn("Service: Codex", root_frames[-1])
        self.assertIn("Reasoning: high", root_frames[-1])
        current = self.store.load().config.profiles["default"]
        self.assertEqual((current.provider, current.effort, current.instructions),
                         ("codex", "high", "Moja rola."))

    def test_nested_profile_service_edit_refreshes_agent_picker_and_parent(self):
        code, _, choices = self.run_menu("Manage agents", "Edit profile", "diagnostic ·",
                                        "Service:", "ChatGPT / Codex", "Back", "Back",
                                        "Choose agent", "reviewer ·", "Exit")
        self.assertEqual(code, 0)
        config = self.store.load().config
        self.assertEqual((config.active, config.selected_provider), ("reviewer", "codex"))
        self.assertEqual(config.profiles["reviewer"].access, "read-only")
        last_options = next(options for title, options, _ in reversed(choices.calls)
                            if title.startswith("Settings"))
        self.assertIn("Service: Codex", last_options)
        pickers = [options for title, options, _ in choices.calls if title == "Choose agent"]
        self.assertEqual(len(pickers), 1)
        self.assertTrue(all(label.endswith(" · Codex") for label in pickers[0][:-1]))

    def test_new_profile_and_copy_inherit_service_and_preserve_other_client_settings(self):
        self.store.mutate(lambda config: config.put("default", replace(config.profiles["default"], model="opus", effort="max")))
        self.store.mutate(lambda config: config.with_provider("codex"))
        code, _, _ = self.run_menu("Create profile", "Reasoning:", "high", "Create profile",
                                   "Copy profile", "default ·", "Back",
                                   argv=("agents", "menu"), text=("team", "default-copy"))
        self.assertEqual(code, 0)
        config = self.store.load().config
        self.assertEqual((config.profiles["team"].provider, config.profiles["team"].effort), ("codex", "high"))
        restored = config.with_provider("claude")
        self.assertEqual((restored.profiles["default-copy"].model, restored.profiles["default-copy"].effort),
                         ("opus", "max"))

    def test_field_interleaving_cannot_overwrite_another_config_change(self):
        calls = 0
        def choose(title, options, *, default=0):
            nonlocal calls
            calls += 1
            if calls == 1:
                return next(i for i, value in enumerate(options) if value.startswith("Model:"))
            if calls == 3:
                self.assertIn("Reasoning: medium", options)
                return None
            self.store.mutate(lambda config: config.put("default", replace(config.profiles["default"], effort="medium")))
            return options.index("Opus")
        with redirect_stdout(io.StringIO()) as output:
            code = management_cli.main(["menu"], store=self.store, chooser=choose)
        self.assertEqual(code, 0)
        selected = self.store.load().config.profiles["default"]
        self.assertEqual((selected.model, selected.effort), ("default", "medium"))
        self.assertIn("changed elsewhere at the same time", output.getvalue())
        self.assertIn("The menu was refreshed", output.getvalue())

    def test_later_read_failure_does_not_claim_an_earlier_committed_change_was_lost(self):
        original_load = self.store.load
        original_mutate = self.store.mutate
        committed = False
        def mutate(*args, **kwargs):
            nonlocal committed
            result = original_mutate(*args, **kwargs)
            committed = True
            return result
        def load():
            if committed:
                raise OSError("synthetic later read failure")
            return original_load()
        chooser = Choices(self, "Model:", "Opus")
        with patch.object(self.store, "load", side_effect=load), \
                patch.object(self.store, "mutate", side_effect=mutate), redirect_stdout(io.StringIO()) as output:
            code = management_cli.main(["menu"], store=self.store, chooser=chooser)
        self.assertEqual(code, 2)
        self.assertFalse(chooser.steps)
        # Read the actual committed bytes after removing only the failed-read fixture.
        self.assertEqual(self.store.load().config.profiles["default"].model, "opus")
        self.assertIn("The current operation did not finish", output.getvalue())
        self.assertNotIn("Settings not changed", output.getvalue())

    def test_skills_assignment_is_draft_until_save_and_has_direct_shortcut(self):
        code, _, _ = self.run_menu("Add Skill", "demo", "Back", argv=("skills",))
        self.assertEqual(code, 0)
        self.assertFalse(self.store.path.parent.exists())
        code, _, _ = self.run_menu("Add Skill", "demo", "Save", argv=("skills",))
        self.assertEqual(code, 0)
        self.assertEqual(self.store.load().config.profiles["default"].skills, ("demo",))
        code, _, _ = self.run_menu("Unassign", "demo", "Save", argv=("skills",))
        self.assertEqual(code, 0)
        self.assertEqual(self.store.load().config.profiles["default"].skills, ())

    def test_no_skill_count_cap_and_combined_instruction_limit_does_not_partially_write(self):
        selected = profiles.Profile(skills=("demo", "other", "third", "fourth"))
        self.store.mutate(lambda config: config.put("default", selected))
        code, output, _ = self.run_menu("Add Skill", "fifth", "Save", argv=("skills",))
        self.assertEqual(code, 0)
        self.assertEqual(self.store.load().config.profiles["default"].skills, ("demo", "other", "third", "fourth", "fifth"))
        self.skill("big-one", "x" * (60 * 1024))
        self.skill("oversize-total", "x" * (60 * 1024))
        self.store.mutate(lambda config: config.put("default", profiles.Profile(skills=("big-one",))))
        before = self.store.load()
        code, _, _ = self.run_menu("Add Skill", "Next page", "oversize-total", argv=("skills",))
        self.assertEqual(code, 2)
        self.assertEqual(self.store.load(), before)

    def test_create_cancel_copy_and_remove_use_real_store_without_auth(self):
        code, _, _ = self.run_menu("Create profile", "Cancel", "Back", argv=("agents", "menu"), text=("unused",))
        self.assertEqual(code, 0)
        self.assertFalse(self.store.path.parent.exists())
        code, _, _ = self.run_menu("Create profile", "Model:", "Opus", "Access:", "Read-only",
                                   "Create profile", "Copy profile", "team ·", "Remove profile",
                                   "team-copy ·", "Remove: team-copy", "Back",
                                   argv=("agents", "menu"), text=("team", "team-copy"))
        self.assertEqual(code, 0)
        config = self.store.load().config
        self.assertEqual(config.profiles["team"].model, "opus")
        self.assertEqual(config.profiles["team"].access, "read-only")
        self.assertNotIn("team-copy", config.profiles)
        self.assertEqual(config.active, "default")

    def test_setup_is_explicit_idempotent_and_preserves_custom_fields(self):
        self.install_diagnostic_procedures()
        self.store.mutate(lambda config: config.put("default", replace(config.profiles["default"], model="opus", effort="high")))
        code, _, _ = self.run_menu("Prepare profiles and Skills", "Exit")
        self.assertEqual(code, 0)
        before = self.store.load()
        selected = before.config.profiles["default"]
        self.assertEqual((selected.model, selected.effort, selected.skills), ("opus", "high", ("concise-assistant",)))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(management_cli.main(["agents", "setup"], store=self.store), 0)
        self.assertEqual(self.store.load(), before)

    def test_profile_picker_pages_to_twenty_fourth_profile(self):
        def build(config):
            for index in range(20):
                config = config.put("role%02d" % index, profiles.Profile())
            return config
        self.store.mutate(build)
        code, _, _ = self.run_menu("Choose agent", "Next page", "Next page",
                                   "role19 ·", "Back", argv=("agents", "menu"))
        self.assertEqual(code, 0)
        self.assertEqual(self.store.load().config.active, "role19")

    def test_empty_instruction_edit_cancels_and_clear_is_explicit(self):
        self.store.mutate(lambda config: config.put("default", profiles.Profile(instructions="Keep this.")))
        before = self.store.load()
        code, _, _ = self.run_menu("Instructions:", "Type new", "Exit", text=("",))
        self.assertEqual(code, 0)
        self.assertEqual(self.store.load(), before)
        code, _, _ = self.run_menu("Instructions:", "Clear", "Exit")
        self.assertEqual(code, 0)
        self.assertEqual(self.store.load().config.profiles["default"].instructions, "")

    def test_nonterminal_and_invalid_selector_do_not_write_or_start_process(self):
        for chooser in (lambda *a, **k: True, lambda *a, **k: -1):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(management_cli.main(["menu"], store=self.store, chooser=chooser), 2)
            self.assertFalse(self.store.path.parent.exists())
        with patch("ai_desktop.terminal_menu.choose", side_effect=MenuInputError()), \
                patch("subprocess.run") as run, patch("subprocess.Popen") as spawn, redirect_stdout(io.StringIO()):
            self.assertEqual(management_cli.main(["menu"], store=self.store), 2)
        run.assert_not_called()
        spawn.assert_not_called()
        self.assertFalse(self.store.path.parent.exists())


if __name__ == "__main__":
    unittest.main()
