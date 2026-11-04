"""Offline update syntax and fail-closed opt-in; never invoke installation."""
import ast
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from test_accept_candidate_script import tree_snapshot

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/update_candidate.fish"
FISH = shutil.which("fish")


class UpdateCandidateScriptTests(unittest.TestCase):
    def test_embedded_python_is_valid_without_execution(self):
        snippets = re.findall(r"python3 -I -B -c '([^']*)'", SCRIPT.read_text())
        self.assertEqual(len(snippets), 2)
        for snippet in snippets:
            ast.parse(snippet)

    @unittest.skipUnless(FISH, "fish is available in the kernel CI job")
    def test_invalid_opt_in_has_no_additional_effects_beyond_fish_startup(self):
        required = "required: --allow-cachyos-update SHA REPO_ROOT"
        for args, reason in (([], required), (["--allow-cachyos-update"], required),
                (["--other", "a" * 40, "/tmp"], required),
                (["--allow-cachyos-update", "bad", "/tmp"], "invalid SHA"),
                (["--allow-cachyos-update", "a" * 40, "relative"], "absolute repository root required")):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as temporary:
                env = {"PATH": temporary, "HOME": temporary}
                baseline = subprocess.run([FISH, "--no-config", "-c", "exit 0"], cwd=temporary,
                    env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual((baseline.returncode, baseline.stdout, baseline.stderr), (0, "", ""))
                before = tree_snapshot(Path(temporary))
                result = subprocess.run([FISH, "--no-config", str(SCRIPT), *args], cwd=temporary,
                    env=env, capture_output=True, text=True, timeout=5)
                separator = "; " if reason == required else " "
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                    (2, "UPDATE: REFUSED" + separator + reason + "\n", ""))
                self.assertEqual(tree_snapshot(Path(temporary)), before)


if __name__ == "__main__":
    unittest.main()
