"""Real private configuration migration and provider selection; no AI or account IO."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import agent_profiles as profiles


class ServiceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aid-service-", dir=Path.home())
        self.addCleanup(self.temporary.cleanup)
        self.store = profiles.Store(Path(self.temporary.name))

    def publish(self, config):
        return self.store.mutate(lambda _: config, expected=self.store.load())

    def switch(self, provider):
        return self.store.mutate(lambda value: value.with_provider(provider), expected=self.store.load())

    def test_legacy_read_and_unrelated_edits_keep_schema_one_without_implicit_migration(self):
        config = profiles.defaults().put("codex-role", profiles.Profile(provider="codex", effort="high"))
        snapshot = self.publish(config)
        before = self.store.path.read_bytes()
        self.assertEqual(json.loads(before)["schema"], 1)
        self.assertIsNone(self.store.load().config.service)
        self.assertEqual(self.store.load(), snapshot)
        self.assertEqual(self.store.path.read_bytes(), before)
        after = self.store.mutate(lambda value: value.select("codex-role"), expected=snapshot)
        self.assertEqual(after.config.selected_provider, "codex")
        self.assertEqual(json.loads(self.store.path.read_bytes())["schema"], 1)

    def test_explicit_service_is_global_for_every_role_and_agent_selection(self):
        self.publish(profiles.defaults().select("diagnostic"))
        after = self.switch("codex")
        self.assertEqual(after.config.active, "diagnostic")
        self.assertEqual(after.config.selected_provider, "codex")
        for name in after.config.profiles:
            selected = self.store.mutate(lambda value, name=name: value.select(name), expected=self.store.load())
            self.assertEqual(selected.config.selected_provider, "codex")
            execution = profiles.resolve(store=self.store)
            self.assertEqual(execution["provider"], "codex")
            self.assertEqual(execution["profile"], name)
            self.assertEqual(execution["access"], after.config.profiles[name].access)
        self.assertEqual({profile.provider for profile in self.store.load().config.profiles.values()}, {"codex"})

    def test_provider_roundtrip_restores_each_model_effort_and_keeps_shared_role_edits(self):
        config = profiles.defaults().put("reviewer", profiles.Profile(model="opus", effort="max",
            instructions="Instrukcje Claude i Codex.", access="read-only", skills=("code-review",)))
        config = config.put("programmer", profiles.Profile(model="sonnet", effort="low"))
        self.publish(config)
        codex = self.switch("codex")
        self.assertEqual(codex.config.profiles["reviewer"].model, "default")
        self.assertEqual(codex.config.profiles["reviewer"].effort, "default")
        self.store.mutate(lambda value: value.put("reviewer", replace(value.profiles["reviewer"],
            effort="xhigh", instructions="Wspólna nowa rola.", access="changes")), expected=codex)
        claude = self.switch("claude")
        self.assertEqual((claude.config.profiles["reviewer"].model, claude.config.profiles["reviewer"].effort),
                         ("opus", "max"))
        self.assertEqual((claude.config.profiles["programmer"].model, claude.config.profiles["programmer"].effort),
                         ("sonnet", "low"))
        again = self.switch("codex").config.profiles["reviewer"]
        self.assertEqual((again.model, again.effort), ("default", "xhigh"))
        self.assertEqual(again.instructions, "Wspólna nowa rola.")
        self.assertEqual(again.access, "changes")
        self.assertEqual(again.skills, ("code-review",))

    def test_mixed_legacy_profiles_preserve_their_own_provider_settings(self):
        config = profiles.defaults().put("diagnostic", profiles.Profile(provider="codex", effort="high",
            instructions="Diagnozuj.", access="read-only"))
        config = config.put("default", profiles.Profile(model="haiku", effort="medium"))
        self.publish(config)
        self.switch("claude")
        codex = self.switch("codex")
        self.assertEqual(codex.config.profiles["diagnostic"].effort, "high")
        self.assertEqual(codex.config.profiles["diagnostic"].access, "read-only")
        self.assertEqual(self.switch("claude").config.profiles["default"].model, "haiku")

    def test_legacy_role_override_uses_active_service_without_migrating_saved_bytes(self):
        config = profiles.defaults().put("diagnostic", profiles.Profile(provider="codex", effort="high",
            instructions="Diagnozuj.", access="read-only"))
        config = config.put("reviewer", profiles.Profile(model="opus", effort="max",
            instructions="Sprawdź kod.", access="read-only"))
        self.publish(config.select("diagnostic"))
        raw = self.store.path.read_bytes()
        for name in config.profiles:
            with self.subTest(role=name):
                execution = profiles.resolve(profile=name, store=self.store)
                self.assertEqual(execution["provider"], "codex")
                self.assertEqual(execution["profile"], name)
                self.assertEqual(execution["instructions"], profiles.catalog_for(config.profiles[name]))
                self.assertEqual(execution["access"], config.profiles[name].access)
        reviewer = profiles.resolve(profile="reviewer", store=self.store)
        self.assertEqual((reviewer["model"], reviewer["effort"]), ("default", "default"))
        self.assertEqual(profiles.resolve(store=self.store)["effort"], "high")
        self.assertEqual(self.store.path.read_bytes(), raw)
        self.assertEqual(json.loads(raw)["schema"], 1)
        self.assertEqual(self.store.load().config.profiles["reviewer"].model, "opus")

    def test_legacy_role_override_cannot_switch_claude_to_codex_or_persist_flag_overrides(self):
        self.publish(profiles.defaults().put("codex-role", profiles.Profile(provider="codex", effort="high",
            instructions="Przejrzyj.", access="read-only")))
        raw = self.store.path.read_bytes()
        execution = profiles.resolve(profile="codex-role", model="opus", effort="max", store=self.store)
        self.assertEqual((execution["provider"], execution["model"], execution["effort"]),
                         ("claude", "opus", "max"))
        self.assertEqual(execution["access"], "read-only")
        self.assertEqual(self.store.path.read_bytes(), raw)

    def test_schema_two_roundtrip_is_exact_and_never_adds_cached_settings_to_execution(self):
        config = profiles.defaults().put("default", profiles.Profile(model="opus", effort="max"))
        after = self.publish(config.with_provider("codex"))
        self.assertEqual(profiles._decode(self.store.path.read_bytes()), after.config)
        data = json.loads(self.store.path.read_bytes())
        self.assertEqual(data["schema"], 2)
        self.assertEqual(data["service"], "codex")
        self.assertEqual(data["provider_settings"]["default"]["claude"], {"model": "opus", "effort": "max"})
        execution = profiles.resolve(store=self.store)
        self.assertEqual(set(execution), {"provider", "model", "effort", "instructions", "profile", "access"})
        self.assertEqual(execution["model"], "default")
        self.assertEqual(execution["effort"], "default")
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_repeated_selection_is_idempotent_and_cannot_reset_model_effort(self):
        config = profiles.defaults().with_provider("codex")
        config = config.put("default", replace(config.profiles["default"], effort="high"))
        self.assertIs(config.with_provider("codex"), config)
        self.publish(config)
        self.assertEqual(self.switch("codex").config.profiles["default"].effort, "high")

    def test_new_roles_follow_selected_service_and_removed_role_has_no_restorable_settings(self):
        config = profiles.defaults().with_provider("codex")
        config = config.put("custom", profiles.Profile(provider="codex", effort="high"))
        config = config.with_provider("claude")
        self.assertTrue(any(item[0] == "custom" for item in config.provider_settings))
        config = config.remove("custom")
        self.assertFalse(any(item[0] == "custom" for item in config.provider_settings))
        config = config.put("custom", profiles.Profile(model="sonnet"))
        self.assertEqual(config.with_provider("codex").profiles["custom"].effort, "default")
        with self.assertRaises(profiles.ProfileError):
            config.put("mixed", profiles.Profile(provider="codex"))

    def test_copy_preserves_both_providers_settings_and_role_without_selecting_it(self):
        config = profiles.defaults().put("reviewer", profiles.Profile(model="opus", effort="max",
            access="read-only", instructions="Przegląd.", skills=("code-review",)))
        config = config.with_provider("codex")
        config = config.put("reviewer", replace(config.profiles["reviewer"], effort="high"))
        copied = config.copy("reviewer", "copy")
        self.assertEqual(copied.active, config.active)
        self.assertEqual(copied.profiles["copy"], config.profiles["reviewer"])
        self.assertEqual(copied.with_provider("claude").profiles["copy"].model, "opus")
        self.assertEqual(copied.with_provider("claude").profiles["copy"].effort, "max")
        self.assertEqual(copied.with_provider("claude").with_provider("codex").profiles["copy"].effort, "high")
        with self.assertRaises(profiles.ProfileError):
            copied.copy("reviewer", "copy")
        with self.assertRaises(profiles.ProfileError):
            copied.copy("missing", "copy-new")

    def test_prepare_adds_missing_roles_for_current_service_and_preserves_cached_models(self):
        config = profiles.Config("default", (("default", profiles.Profile(model="opus", effort="max")),))
        config = config.with_provider("codex")
        with patch.object(profiles, "instructions_for", return_value="validated Skill"):
            prepared = profiles.prepared(config)
        self.assertEqual({profile.provider for profile in prepared.profiles.values()}, {"codex"})
        self.assertEqual(prepared.profiles["analyst"].access, "read-only")
        self.assertEqual(prepared.profiles["diagnostic"].skills, profiles.PRESET_SKILLS["diagnostic"])
        self.assertEqual(prepared.with_provider("claude").profiles["default"].model, "opus")
        self.assertEqual(prepared.provider_settings, config.provider_settings)

    def test_stale_provider_selection_preserves_concurrent_role_edit(self):
        before = self.publish(profiles.defaults().with_provider("claude"))
        self.store.mutate(lambda value: value.put("default", replace(value.profiles["default"],
            instructions="Równoległa zmiana.")), expected=before)
        raw = self.store.path.read_bytes()
        with self.assertRaises(profiles.ProfileError):
            self.store.mutate(lambda value: value.with_provider("codex"), expected=before)
        self.assertEqual(self.store.path.read_bytes(), raw)
        self.assertEqual(self.store.load().config.selected_provider, "claude")

    def test_invalid_schema_two_cache_or_service_never_falls_back(self):
        good = profiles.defaults().with_provider("codex").data()
        cases = []
        for service in (None, True, "gemini", ["codex"]):
            cases.append({**good, "service": service})
        for settings in ([], {"missing": {"claude": {"model": "opus", "effort": "max"}}},
                         {"default": {}}, {"default": {"codex": {"model": "default", "effort": "high"}}},
                         {"default": {"claude": {"model": "--flag", "effort": "high"}}},
                         {"default": {"claude": {"model": "opus", "effort": "ultracode"}}},
                         {"default": {"claude": {"model": "opus", "effort": "high", "extra": 1}}}):
            cases.append({**good, "provider_settings": settings})
        cases.append({**good, "profiles": profiles.defaults().data()["profiles"]})
        cases.append({**good, "extra": True})
        for value in cases:
            with self.subTest(value=value), self.assertRaises(profiles.ProfileError):
                profiles._decode(json.dumps(value).encode())
        duplicate = json.dumps(good).replace('"service": "codex"', '"service": "codex", "service": "codex"')
        with self.assertRaises(profiles.ProfileError):
            profiles._decode(duplicate.encode())

    def test_invalid_direct_service_state_is_bounded_and_immutable(self):
        entries = profiles.defaults().entries
        for saved in ((('default', {}, 'default', 'default'),),
                      (('default', 'claude', 'default', 'high'),),
                      (('default', 'codex', 'default', 'high'),) * (profiles.MAX_PROFILES + 1)):
            with self.subTest(saved=repr(saved)), self.assertRaises(profiles.ProfileError):
                profiles.Config("default", entries, "claude", saved)
        with self.assertRaises(profiles.ProfileError):
            profiles.Config("default", entries, None, (("default", "codex", "default", "high"),))
        with self.assertRaises(profiles.ProfileError):
            profiles.defaults().with_provider("gemini")


if __name__ == "__main__":
    unittest.main()
