import tempfile
import contextlib
import io
import json
from pathlib import Path
import unittest

from ai_desktop.skills import inspect_skill, ROOT
from ai_desktop import skills
from ai_desktop import agent_profiles
from ai_desktop import analysis_child, analysis_contract, execution
from unittest.mock import patch


class SkillTests(unittest.TestCase):
    def test_default_list_matches_explicit_list_and_starts_no_process(self):
        outputs = []
        with patch("subprocess.run") as run, patch("subprocess.Popen") as spawn:
            for arguments in ([], ["list"]):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(skills.main(arguments), 0)
                outputs.append(output.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertIn("cachyos-diagnostics:", outputs[0])
        run.assert_not_called()
        spawn.assert_not_called()

    def test_project_skill(self):
        description, text = inspect_skill(ROOT, "cachyos-diagnostics")
        self.assertIn("CachyOS", description)
        self.assertIn("read-only", text)

    def test_packaged_preset_skills_validate_and_fit_full_instruction_budget(self):
        names = tuple(agent_profiles.PRESET_SKILLS.values())
        for name in names:
            with self.subTest(name=name):
                description, text = inspect_skill(ROOT, name)
                self.assertTrue(description)
                self.assertLessEqual(len(text.encode("utf-8")), 4096)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in names:
                directory = root / name
                directory.mkdir(mode=0o755)
                path = directory / "SKILL.md"
                path.write_bytes((ROOT / name / "SKILL.md").read_bytes())
                path.chmod(0o644)
            from itertools import combinations
            with patch.object(skills, "ROOT", root):
                for selection in combinations(names, 4):
                    selected = agent_profiles.Profile(instructions="a" * agent_profiles.MAX_INSTRUCTIONS,
                                                      skills=selection)
                    instructions = agent_profiles.instructions_for(selected)
                    self.assertLessEqual(len(instructions.encode("utf-8")),
                                         agent_profiles.MAX_EXECUTION_INSTRUCTIONS)

    def test_reject_path_traversal(self):
        with self.assertRaises(ValueError):
            inspect_skill(ROOT, "../x")

    def test_all_packaged_procedures_fit_a_real_workspace_request_together(self):
        """Other modes still send full bodies: improving procedures must not break those requests."""
        names = tuple(sorted(path.parent.name for path in ROOT.glob("*/SKILL.md")))
        selected = agent_profiles.Profile(instructions="r" * agent_profiles.MAX_INSTRUCTIONS, skills=names)
        instructions = agent_profiles.instructions_for(selected)
        config = {"provider": "claude", "model": "default", "effort": "default", "profile": "programmer",
                  "access": "changes", "instructions": instructions}
        task = {"instruction": "Popraw wybrany plik.",
                "files": [{"path": "example.py", "content": "answer = 1\n", "writable": True}]}
        with execution.using(config):
            packet = analysis_contract.application_input(None, task=task)
        parsed, _ = analysis_contract.parse_request(packet.encode("utf-8"))
        body, frozen = analysis_child.native_input("analyze", parsed.encode("utf-8"))
        self.assertEqual(json.loads(body)["task"], task)
        self.assertEqual(frozen["instructions"], instructions)
        for name in names:
            with self.subTest(skill=name):
                self.assertIn(inspect_skill(ROOT, name)[1], frozen["instructions"])

    def test_reject_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "external").mkdir()
            (root / "linked").symlink_to(
                root / "external", target_is_directory=True
            )
            with self.assertRaises(ValueError):
                inspect_skill(root, "linked")

    def test_wrong_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "valid").mkdir()
            (root / "valid" / "SKILL.md").write_text(
                "---\nname: wrong\ndescription: x\n---\ntext\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                inspect_skill(root, "valid")
