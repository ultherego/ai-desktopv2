"""The shipped knowledge must remain usable through the guarded shared loader."""
import re
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_desktop import skills, task_contract


class CachyOSKnowledgeTests(unittest.TestCase):
    def test_every_linked_reference_loads_with_provenance_and_no_network(self):
        references = []
        with patch("socket.create_connection", side_effect=AssertionError("offline loader")), \
                patch("subprocess.run", side_effect=AssertionError("no host probe")):
            for path in sorted(skills.ROOT.glob("*/SKILL.md")):
                snapshot = skills.bundle(path.parent.name, root=skills.ROOT)
                linked = set(re.findall(r"\]\(references/([a-z0-9-]+)\.md\)", snapshot["text"]))
                self.assertEqual(linked, set(snapshot["references"]), path.parent.name)
                for name in linked:
                    with self.subTest(skill=path.parent.name, reference=name):
                        identifier = path.parent.name + "/" + name
                        content = skills.resource_text(snapshot, identifier)
                        metadata = snapshot["references"][name]
                        self.assertIn(metadata["source"], content)
                        self.assertIn(metadata["checked"], content)
                        if metadata["version"] == "rolling":
                            self.assertIn(metadata["content"], content)
                            self.assertIn("not an exact version match", content)
                        else:
                            self.assertNotIn(metadata["content"], content)
                            matched = skills.resource_text(snapshot, identifier, versions={
                                tool: metadata["version"] for tool in metadata["tools"]})
                            self.assertIn(metadata["content"], matched)
                        references.append(identifier)
        self.assertTrue(references)

    def test_requested_update_reference_does_not_load_unrelated_recovery(self):
        snapshot = skills.bundle("cachyos-packages", root=skills.ROOT)
        selected = skills.resource_text(snapshot, "cachyos-packages/cachy-update")
        self.assertIn("arch-update.timer", selected)
        self.assertIn("GNOME", selected)
        self.assertNotIn("# Snapshot tools", selected)
        self.assertIsNone(skills.resource_text(snapshot, "cachyos-boot-snapshots/cachy-chroot"))
        self.assertIsNone(skills.resource_text(snapshot, "cachyos-packages/nonexistent"))

    def test_private_override_does_not_inherit_packaged_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp)
            directory = private / "cachyos-packages"
            directory.mkdir(mode=0o700)
            (directory / "SKILL.md").write_text(
                "---\nname: cachyos-packages\ndescription: My local package procedure.\n---\n"
                "Use the supplied package report.\n", encoding="utf-8")
            with patch.object(skills, "user_root", return_value=private):
                snapshot = skills.bundle("cachyos-packages")
        self.assertEqual(snapshot["references"], {})
        self.assertIsNone(skills.resource_text(snapshot, "cachyos-packages/cachy-update"))

    def test_development_procedure_lists_actual_fixed_suite_recipes(self):
        # A stale Skill used to tell the model that pytest/Go/npm were unsupported,
        # overriding the executor's current capabilities during real tasks.
        procedure = skills.bundle("code-development", root=skills.ROOT)["text"]
        table_recipes = set()
        for line in procedure.splitlines():
            if line.startswith("| `"):
                table_recipes.update(re.findall(r"`([a-z-]+)`", line.split("|")[1]))
        self.assertEqual(table_recipes, set(task_contract.CHECKS))
        self.assertIn("test count is unknown", procedure)
        self.assertIn("separate consent", procedure)


if __name__ == "__main__":
    unittest.main()
