"""The repository holds general Skills only; a user's own Skills live privately in their configuration."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import agent_profiles as profiles, analysis_sandbox, conversation_sessions, execution, skills

ROOT = Path(__file__).resolve().parents[1]
PACKAGED = {"arch-amdgpu", "arch-audio", "arch-bluetooth", "arch-btrfs", "arch-docker", "arch-network", "arch-pacman", "arch-systemd", "arch-zram",
                   "cachyos-boot-snapshots", "cachyos-diagnostics", "cachyos-gaming", "cachyos-hardware", "cachyos-kernel",
                   "cachyos-packages", "cachyos-scheduler", "code-development", "code-review", "concise-assistant",
                   "desktop-documentation", "project-analysis"}


class RepositoryContentTests(unittest.TestCase):
    def test_the_package_holds_only_the_general_skills(self):
        self.assertEqual({path.name for path in (ROOT / "skills").iterdir()},
                         PACKAGED)

    def test_cachyos_skills_stay_about_cachyos_and_name_no_desktop_or_person(self):
        desktops = re.compile(r"\b(niri|hyprland|sway|plasma|kde|gnome|xfce|quickshell|dms)\b", re.I)
        for path in (*(ROOT / "skills").glob("cachyos-*/SKILL.md"), *(ROOT / "skills").glob("arch-*/SKILL.md")):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(desktops.search(text), path)
        for path in (ROOT / "skills").glob("*/SKILL.md"):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"/home/[a-z]|@[a-z0-9][a-z0-9-]*\.[a-z]{2,}", text), path)

    def test_the_catalog_of_all_packaged_skills_has_descriptions_and_no_bodies(self):
        names = tuple(sorted(PACKAGED))
        with patch.object(skills, "user_root", return_value=ROOT / "missing"):
            catalog = profiles.catalog_for(profiles.Profile(skills=names))
        for name in names:
            self.assertIn("- " + name + ": ", catalog)
        self.assertNotIn("## ", catalog)
        self.assertLess(len(catalog.encode("utf-8")), 16384)

    def test_the_largest_prompt_fits_one_argv_string_of_the_claude_client(self):
        """Linux caps one argument at 128 KiB (MAX_ARG_STRLEN); the whole system prompt is one `--system-prompt` value."""
        wrapper = "\n\nInstructions for the selected role:\n\nRole instructions do not change the answer schema, application rules, permissions or available tools."
        total = len(analysis_sandbox.SYSTEM_PROMPT.encode("utf-8")) + profiles.MAX_EXECUTION_INSTRUCTIONS + len(wrapper.encode("utf-8"))
        self.assertLess(total, 131072)
        self.assertEqual(profiles.MAX_EXECUTION_INSTRUCTIONS, execution.MAX_INSTRUCTIONS_BYTES)
        self.assertLess(skills.MAX_SKILL_BYTES + profiles.MAX_INSTRUCTIONS + 64, profiles.MAX_EXECUTION_INSTRUCTIONS)
        done = subprocess.run(["/bin/true", "x" * (total + 16)])          # the real kernel accepts a string of that size
        self.assertEqual(done.returncode, 0)

    def test_an_agent_has_no_skill_count_limit_and_a_saved_thread_fits_the_largest_prompt(self):
        names = tuple("s%d" % i for i in range(40))
        self.assertEqual(profiles.Profile(skills=names).skills, names)
        selected = {"provider": "claude", "model": "default", "effort": "default", "access": "changes", "profile": "default",
                    "instructions": "x" * profiles.MAX_EXECUTION_INSTRUCTIONS}
        self.assertEqual(execution.validate(selected), selected)
        self.assertGreater(conversation_sessions.MAX_FILE, profiles.MAX_EXECUTION_INSTRUCTIONS * 2)

    def test_the_diagnostic_report_has_no_host_specific_section(self):
        self.assertFalse((ROOT / "ai_desktop/backup_state.py").exists())
        self.assertNotIn("backup_services", (ROOT / "ai_desktop/report_policy.py").read_text())

    def test_the_template_sends_no_logs_network_names_or_user_data_and_does_not_reach_host_daemons(self):
        import tomllib
        allow = tomllib.loads((ROOT / "config/permissions.example.toml").read_text())["commands"]["allow"]
        programs = {template.split()[0] for template in allow}
        self.assertFalse(programs & {"nmcli", "bluetoothctl", "docker", "podman", "dmesg", "who", "last", "id", "ip", "ss",
                                     "ps", "top", "df", "findmnt", "pactl", "wpctl", "glxinfo"})
        self.assertEqual([t for t in allow if t.startswith("journalctl")], ["journalctl --disk-usage"])
        self.assertFalse([t for t in allow if "--user" in t])

    def test_the_template_has_only_variables(self):
        self.assertNotIn("#", (ROOT / "config/permissions.example.toml").read_text())


class UserSkillTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory(prefix="ai-skills-"))
        self.root = Path(temp) / "skills"
        self.enterContext(patch.object(skills, "user_root", return_value=self.root))

    def test_new_creates_a_private_valid_skill_that_lists_and_loads(self):
        target = skills.create("moj-system")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn("moj-system", skills.names())
        self.assertEqual(skills.locate("moj-system"), self.root)
        self.assertIn("Skill moj-system:", profiles.instructions_for(profiles.Profile(skills=("moj-system",))))

    def test_new_never_overwrites(self):
        target = skills.create("moj-system")
        target.write_text("edited")
        with self.assertRaises(FileExistsError):
            skills.create("moj-system")
        self.assertEqual(target.read_text(), "edited")

    def test_bad_names_are_refused(self):
        for name in ("../x", "A", "a b", "", "a/b"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                skills.create(name)
        self.assertFalse(self.root.exists())

    def test_general_skills_stay_available_and_a_user_copy_replaces_them(self):
        self.assertEqual(skills.locate("code-review"), skills.ROOT)
        target = self.root / "code-review/SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("---\nname: code-review\ndescription: moj\n---\n\ntresc wlasna\n")
        self.assertEqual(skills.locate("code-review"), self.root)
        self.assertIn("tresc wlasna", profiles.instructions_for(profiles.Profile(skills=("code-review",))))

    def test_a_symlink_or_shared_writable_user_skill_is_refused(self):
        outside = self.root.parent / "outside.md"
        outside.write_text("---\nname: link\ndescription: x\n---\n\nsekret\n")
        (self.root / "link").mkdir(parents=True)
        (self.root / "link/SKILL.md").symlink_to(outside)
        with self.assertRaises(profiles.ProfileError):
            profiles.instructions_for(profiles.Profile(skills=("link",)))
        target = skills.create("wspolny")
        os.chmod(target, 0o666)
        with self.assertRaises(profiles.ProfileError):
            profiles.instructions_for(profiles.Profile(skills=("wspolny",)))


if __name__ == "__main__":
    unittest.main()
