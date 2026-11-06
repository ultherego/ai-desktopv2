"""Real filesystem transactions and local management; no native client or OAuth."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from contextlib import redirect_stdout
from dataclasses import FrozenInstanceError, replace
import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import agent_profiles as profiles, management_cli, skills


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        # A private home below a secure real home; /tmp is deliberately not an
        # accepted ancestor of production configuration paths.
        self.temporary = tempfile.TemporaryDirectory(prefix="aid-profiles-", dir=Path.home())
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.store = profiles.Store(self.home)

    def write(self, value=None):
        value = profiles.defaults().data() if value is None else value
        self.store.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.store.path.write_bytes(raw)
        self.store.path.chmod(0o600)

    def cli(self, argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = management_cli.main(argv, store=self.store)
        return code, output.getvalue()

    def installed_skills(self):
        root = self.home / "installed-presets"
        root.mkdir(mode=0o755)
        for name in {name for group in profiles.PRESET_SKILLS.values() for name in group}:
            directory = root / name
            directory.mkdir(mode=0o755)
            path = directory / "SKILL.md"
            path.write_bytes((skills.ROOT / name / "SKILL.md").read_bytes())
            path.chmod(0o644)
        return root

    def test_reading_defaults_does_not_create_files_and_profiles_are_immutable(self):
        snapshot = self.store.load()
        self.assertEqual(snapshot.config.active, "default")
        self.assertIsNone(snapshot.identity)
        self.assertFalse((self.home / ".config").exists())
        self.assertEqual(set(snapshot.config.profiles), {"default", "reviewer", "diagnostic", "programmer"})
        with self.assertRaises(TypeError):
            snapshot.config.profiles["new"] = profiles.Profile()
        with self.assertRaises(FrozenInstanceError):
            snapshot.config.profiles["reviewer"].access = "changes"

    def test_crud_persists_private_file_and_selection(self):
        self.assertEqual(self.cli(["agents", "create", "my-review", "--model", "opus", "--effort", "high",
                                   "--access", "read-only", "--instructions", "Sprawdź kod."])[0], 0)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.cli(["agents", "use", "my-review"])[0], 0)
        actual = profiles.resolve(store=self.store)
        self.assertEqual(actual, {"provider": "claude", "model": "opus", "effort": "high",
            "instructions": "Sprawdź kod.", "profile": "my-review", "access": "read-only"})
        self.assertEqual(self.cli(["set", "--effort", "low"])[0], 0)
        self.assertEqual(self.store.load().config.profiles["my-review"].effort, "low")
        self.assertEqual(self.cli(["agents", "remove", "my-review"])[0], 2)
        self.assertEqual(self.cli(["agents", "use", "default"])[0], 0)
        self.assertEqual(self.cli(["agents", "remove", "my-review"])[0], 0)
        self.assertNotIn("my-review", self.store.load().config.profiles)

    def test_override_does_not_save_or_change_role_authority(self):
        selection = profiles.resolve(profile="reviewer", model="haiku", effort="medium", store=self.store)
        self.assertEqual(selection["access"], "read-only")
        self.assertEqual(selection["model"], "haiku")
        self.assertEqual(self.store.load().config.profiles["reviewer"].model, "default")
        self.assertFalse(self.store.path.exists())

    def test_prepared_presets_are_explicit_complete_and_leave_defaults_unchanged(self):
        original = profiles.defaults()
        root = self.installed_skills()
        with patch.object(skills, "ROOT", root), patch("subprocess.run") as run, \
                patch("subprocess.Popen") as process, patch("socket.socket") as network:
            prepared = profiles.prepared(original)
            for _, selected in prepared.entries:
                self.assertTrue(profiles.instructions_for(selected))
        self.assertEqual(prepared.active, "default")
        self.assertEqual({name: selected.skills for name, selected in prepared.entries},
                         profiles.PRESET_SKILLS)
        self.assertEqual(prepared.profiles["analyst"].access, "read-only")
        self.assertEqual(prepared.profiles["reviewer"].access, "read-only")
        self.assertEqual(prepared.profiles["diagnostic"].access, "read-only")
        self.assertEqual(original, profiles.defaults())
        self.assertTrue(all(selected.skills for _, selected in original.entries))
        self.assertFalse(self.store.path.exists())
        run.assert_not_called()
        process.assert_not_called()
        network.assert_not_called()

    def test_prepared_preserves_custom_settings_selections_and_nonempty_skills(self):
        root = self.installed_skills()
        default = profiles.Profile(provider="codex", effort="high", access="read-only",
                                   instructions="Moje ustawienia.")
        reviewer = profiles.Profile(model="opus", effort="low", access="changes",
                                    instructions="Moja rola.", skills=("cachyos-diagnostics",))
        analyst = profiles.Profile(model="haiku", effort="medium", access="changes",
                                   instructions="Mój analityk.", skills=("code-review",))
        custom = profiles.Profile(model="sonnet", skills=("code-development",))
        original = profiles.Config("custom", (("default", default), ("reviewer", reviewer),
                                              ("analyst", analyst), ("custom", custom)))
        with patch.object(skills, "ROOT", root):
            prepared = profiles.prepared(original)
            again = profiles.prepared(prepared)
        self.assertEqual(prepared.active, original.active)
        self.assertEqual(prepared.profiles["default"], replace(default, skills=profiles.PRESET_SKILLS["default"]))
        for name in ("reviewer", "analyst", "custom"):
            self.assertEqual(prepared.profiles[name], original.profiles[name])
        self.assertIn("diagnostic", prepared.profiles)
        self.assertIn("programmer", prepared.profiles)
        self.assertEqual(again, prepared)

    def test_ask_resolution_never_falls_back_to_bodies_when_a_long_catalog_overflows(self):
        root = self.home / "long-descriptions"
        root.mkdir()
        names = ("one", "two")
        for name in names:
            directory = root / name
            directory.mkdir()
            data = "---\nname: " + name + "\ndescription: " + "a" * 53050 + "\n---\nprivate body\n"
            (directory / "SKILL.md").write_text(data)
        config = profiles.defaults().put("default", profiles.Profile(skills=names))
        self.write(config.data())
        with patch.object(skills, "ROOT", root):
            self.assertIn("private body", profiles.resolve(store=self.store, catalog=False)["instructions"])
            with self.assertRaises(profiles.ProfileError):
                profiles.resolve(store=self.store, catalog=True)

    def test_prepared_diagnostic_can_load_the_btrfs_and_systemd_procedures_it_references(self):
        with patch.object(skills, "ROOT", self.installed_skills()):
            selected = profiles.prepared(profiles.defaults()).profiles["diagnostic"]
            catalog = profiles.catalog_for(selected)
            for name in ("arch-btrfs", "arch-systemd", "cachyos-boot-snapshots"):
                self.assertIn("- " + name + ":", catalog)
            self.assertNotIn("# Btrfs diagnostics", catalog)
            original = profiles.defaults().put("diagnostic", replace(selected, skills=("arch-audio",)))
            self.assertEqual(profiles.prepared(original).profiles["diagnostic"].skills, ("arch-audio",))

    def test_prepared_publishes_with_existing_snapshot_transaction(self):
        root = self.installed_skills()
        before = self.store.load()
        with patch.object(skills, "ROOT", root):
            after = self.store.mutate(profiles.prepared, expected=before)
        self.assertEqual(after.config.profiles["default"].skills, profiles.PRESET_SKILLS["default"])
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.store.mutate(lambda value: value.select("reviewer"), expected=after)
        with patch.object(skills, "ROOT", root), self.assertRaises(profiles.ProfileError):
            self.store.mutate(profiles.prepared, expected=after)
        self.assertEqual(self.store.load().config.active, "reviewer")

    def test_missing_preset_skill_never_publishes_partial_preparation(self):
        root = self.installed_skills()
        (root / "code-review" / "SKILL.md").unlink()
        self.write()
        original = self.store.path.read_bytes()
        before = self.store.load()
        with patch.object(skills, "ROOT", root), self.assertRaises(profiles.ProfileError):
            self.store.mutate(profiles.prepared, expected=before)
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertNotIn("analyst", self.store.load().config.profiles)
        self.assertFalse(list(self.store.path.parent.glob("*.tmp")))

    def test_preparation_refuses_profile_limit_without_dropping_custom_profiles(self):
        entries = profiles.defaults().entries + tuple(("custom-%d" % n, profiles.Profile()) for n in range(20))
        self.write(profiles.Config("custom-0", entries).data())
        original = self.store.path.read_bytes()
        with self.assertRaises(profiles.ProfileError):
            self.store.mutate(profiles.prepared, expected=self.store.load())
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_preparation_validates_combined_instructions_of_existing_custom_profiles(self):
        root = self.installed_skills()
        big = root / "big"
        big.mkdir(mode=0o755)
        path = big / "SKILL.md"
        path.write_text("---\nname: big\ndescription: Large test skill\n---\n" + "x" * (90 * 1024))
        path.chmod(0o644)
        (root / "big2").mkdir(mode=0o755)
        (root / "big2" / "SKILL.md").write_text("---\nname: big2\ndescription: Large test skill\n---\n" + "x" * (90 * 1024))
        (root / "big2" / "SKILL.md").chmod(0o644)
        original = profiles.defaults().put("custom", profiles.Profile(
            instructions="a" * profiles.MAX_INSTRUCTIONS, skills=("big", "big2")))
        self.write(original.data())
        before = self.store.path.read_bytes()
        with patch.object(skills, "ROOT", root), self.assertRaises(profiles.ProfileError):
            self.store.mutate(profiles.prepared, expected=self.store.load())
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_invalid_instruction_controls_models_and_names_fail_closed(self):
        for value in ("hello\x1b[2J", "a" * 4097, "x\u202ey", "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(profiles.ProfileError):
                profiles.Profile(instructions=value)
        for value in ("../new", "abc/def", "--flag", "A", "a" * 49):
            with self.subTest(value=value), self.assertRaises(profiles.ProfileError):
                profiles.slug(value)
        with self.assertRaises(profiles.ProfileError):
            profiles.Profile(model="--allowedTools")
        with self.assertRaises(profiles.ProfileError):
            profiles.Profile(provider="codex", model="opus")
        with self.assertRaises(profiles.ProfileError):
            profiles.Profile(skills=({},))

    def test_effort_choices_are_bound_to_the_profile_provider(self):
        for provider in ("claude", "codex"):
            expected = ("default", "low", "medium", "high", "xhigh", "max") if provider == "claude" else (
                "default", "low", "medium", "high", "xhigh")
            self.assertEqual(profiles.efforts_for(provider), expected)
            for effort in expected:
                with self.subTest(provider=provider, effort=effort):
                    self.assertEqual(profiles.Profile(provider=provider, effort=effort).effort, effort)
        for provider, effort in (("codex", "max"), ("claude", "ultracode"), ("codex", "ultracode")):
            with self.subTest(provider=provider, effort=effort), self.assertRaises(profiles.ProfileError):
                profiles.Profile(provider=provider, effort=effort)
        self.assertEqual(self.cli(["set", "--effort", "max"])[0], 0)
        self.assertEqual(profiles.resolve(store=self.store)["effort"], "max")
        self.assertEqual(self.cli(["agents", "create", "codex-high", "--provider", "codex", "--effort", "xhigh"])[0], 0)
        self.assertEqual(self.cli(["agents", "update", "codex-high", "--effort", "max"])[0], 2)
        self.assertEqual(self.store.load().config.profiles["codex-high"].effort, "xhigh")

    def test_broken_oversized_duplicate_and_unknown_config_never_falls_back(self):
        examples = [b"{}", b"{" + b" " * profiles.MAX_BYTES, b'{"schema":1,"schema":1}',
                    b'{"schema":' + b"1" * 5000 + b'}',
                    json.dumps({**profiles.defaults().data(), "extra": 1}).encode(),
                    json.dumps({**profiles.defaults().data(), "schema": True}).encode()]
        for raw in examples:
            self.write(raw)
            with self.subTest(size=len(raw)), self.assertRaises(profiles.ProfileError):
                self.store.load()

    def test_unsafe_config_modes_symlink_and_hardlink_are_refused(self):
        self.write()
        self.store.path.chmod(0o644)
        with self.assertRaises(profiles.ProfileError):
            self.store.load()
        self.store.path.chmod(0o600)
        other = self.home / "other.json"
        os.link(self.store.path, other)
        with self.assertRaises(profiles.ProfileError):
            self.store.load()
        other.unlink()
        self.store.path.rename(other)
        self.store.path.symlink_to(other)
        with self.assertRaises(profiles.ProfileError):
            self.store.load()
        self.assertEqual(other.read_bytes(), json.dumps(profiles.defaults().data()).encode())

    def test_unsafe_directory_and_symlink_parent_are_refused(self):
        self.write()
        self.store.path.parent.chmod(0o777)
        with self.assertRaises(profiles.ProfileError):
            self.store.load()
        self.store.path.parent.chmod(0o700)
        old = self.store.path.parent.with_name("other")
        self.store.path.parent.rename(old)
        self.store.path.parent.symlink_to(old, target_is_directory=True)
        with self.assertRaises(profiles.ProfileError):
            self.store.load()

    def test_stale_snapshot_does_not_clobber_another_settings_writer(self):
        before = self.store.load()
        self.store.mutate(lambda config: config.select("reviewer"))
        with self.assertRaises(profiles.ProfileError):
            self.store.mutate(lambda config: config.select("programmer"), expected=before)
        self.assertEqual(self.store.load().config.active, "reviewer")

    def test_external_change_while_preparing_write_is_preserved(self):
        self.write()
        original = self.store.path.read_bytes()
        concurrent = profiles.defaults().select("diagnostic").data()
        def operation(config):
            self.write(concurrent)
            return config.select("programmer")
        with self.assertRaises(profiles.ProfileError):
            self.store.mutate(operation)
        self.assertEqual(self.store.load().config.active, "diagnostic")
        self.assertNotEqual(self.store.path.read_bytes(), original)
        self.assertFalse(list(self.store.path.parent.glob("*.tmp")))

    def test_first_creation_never_replaces_concurrent_config(self):
        original = profiles.rename_noreplace
        concurrent = profiles.defaults().select("reviewer").data()
        def race(source_fd, source, destination_fd, destination):
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=destination_fd)
            try:
                os.write(descriptor, json.dumps(concurrent).encode())
            finally:
                os.close(descriptor)
            return original(source_fd, source, destination_fd, destination)
        with patch.object(profiles, "rename_noreplace", side_effect=race), self.assertRaises(profiles.ProfileError):
            self.store.mutate(lambda config: config.select("programmer"))
        self.assertEqual(self.store.load().config.active, "reviewer")

    def test_busy_lock_refuses_without_waiting_or_changing_config(self):
        self.write()
        lock = os.open(self.store.path.parent / ".agents.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = self.store.path.read_bytes()
            with self.assertRaises(profiles.ProfileError):
                self.store.mutate(lambda config: config.select("reviewer"))
            self.assertEqual(self.store.path.read_bytes(), before)
        finally:
            os.close(lock)

    def test_directory_rebound_during_preparation_never_writes_new_directory(self):
        self.write()
        detached = self.store.path.parent.with_name("detached")
        def operation(config):
            self.store.path.parent.rename(detached)
            self.store.path.parent.mkdir(mode=0o700)
            self.write(profiles.defaults().select("diagnostic").data())
            return config.select("reviewer")
        with self.assertRaises(profiles.ProfileError):
            self.store.mutate(operation)
        self.assertEqual(self.store.load().config.active, "diagnostic")
        self.assertEqual(json.loads((detached / "agents.json").read_text())["active"], "default")
        self.assertFalse(list(detached.glob("*.tmp")))

    def test_skills_are_selected_as_bounded_instructions_without_new_authority(self):
        root = self.home / "installed-skills"
        (root / "cachyos-diagnostics").mkdir(parents=True)
        source = root / "cachyos-diagnostics" / "SKILL.md"
        source.write_text("---\nname: cachyos-diagnostics\ndescription: Diagnosis\n---\nSprawdź dowody.\n")
        source.chmod(0o644)
        with patch.object(skills, "ROOT", root):
            self.assertEqual(self.cli(["agents", "update", "reviewer", "--skill", "cachyos-diagnostics"])[0], 0)
            execution = profiles.resolve(profile="reviewer", store=self.store)
        self.assertIn("- cachyos-diagnostics:", execution["instructions"])
        self.assertNotIn("Sprawdź dowody.", execution["instructions"])
        self.assertEqual(execution["access"], "read-only")
        self.assertLessEqual(len(execution["instructions"].encode()), 8192)
        self.assertEqual(self.cli(["agents", "update", "reviewer", "--no-skills"])[0], 0)
        self.assertNotIn("- cachyos-diagnostics:", profiles.resolve(profile="reviewer", store=self.store)["instructions"])

    def test_menu_uses_snapshot_and_returns_clean_summary(self):
        reader = iter(("2", "4"))
        output = io.StringIO()
        with redirect_stdout(output):
            code = management_cli.main(["menu"], store=self.store, reader=lambda _: next(reader))
        self.assertEqual(code, 0)
        self.assertEqual(self.store.load().config.profiles["default"].model, "opus")
        self.assertIn("model: Opus", output.getvalue())


class BoundedSkillTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "demo").mkdir()
        self.path = self.root / "demo" / "SKILL.md"
        self.path.write_text("---\nname: demo\ndescription: Test\n---\nInstrukcje\n")

    def test_valid_frontmatter_and_oversize_refusal(self):
        self.assertEqual(skills.inspect_skill(self.root, "demo")[0], "Test")
        self.path.write_bytes(b"x" * (skills.MAX_SKILL_BYTES + 1))
        with self.assertRaises(ValueError):
            skills.inspect_skill(self.root, "demo")

    def test_linked_and_writable_skill_file_is_not_loaded(self):
        self.path.chmod(0o666)
        with self.assertRaises(ValueError):
            skills.inspect_skill(self.root, "demo", secure=True)
        self.path.chmod(0o644)
        link = self.root / "linked"
        os.link(self.path, link)
        with self.assertRaises(ValueError):
            skills.inspect_skill(self.root, "demo")
        link.unlink()
        self.path.rename(link)
        self.path.symlink_to(link)
        with self.assertRaises((ValueError, OSError)):
            skills.inspect_skill(self.root, "demo")


if __name__ == "__main__":
    unittest.main()
