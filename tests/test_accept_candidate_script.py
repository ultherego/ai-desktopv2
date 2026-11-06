"""Acceptance-script syntax and refusal only; never opt into host operations."""
import ast
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts/accept_candidate.fish"
FISH = shutil.which("fish")


def tree_snapshot(root):
    """Include every name, entry type, mode and file/link contents."""
    result = {}
    for path in (root, *sorted(root.rglob("*"))):
        info = path.lstat()
        data = (path.read_bytes() if stat.S_ISREG(info.st_mode) else
                os.fsencode(os.readlink(path)) if stat.S_ISLNK(info.st_mode) else None)
        result[path.relative_to(root).as_posix()] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), data)
    return result


class CandidateScriptTests(unittest.TestCase):
    def test_embedded_python_parses_without_executing_any_payload(self):
        snippets = re.findall(r"python3 -I -B -c '([^']*)'", SCRIPT.read_text(encoding="utf-8"))
        self.assertEqual(len(snippets), 1)
        for snippet in snippets:
            ast.parse(snippet)

    @unittest.skipUnless(FISH, "fish is checked in the kernel CI job")
    def test_missing_or_invalid_opt_in_refuses_before_commands_or_writes(self):
        cases = (
            ([], "required: --allow-cachyos-acceptance SHA REPO_ROOT CONFIGURED_PATTERN"),
            (["--allow-cachyos-acceptance"], "required: --allow-cachyos-acceptance SHA REPO_ROOT CONFIGURED_PATTERN"),
            (["--other", "a" * 40, "/tmp", "~/configured/*"], "required: --allow-cachyos-acceptance SHA REPO_ROOT CONFIGURED_PATTERN"),
            (["--allow-cachyos-acceptance", "a" * 40, "/tmp"], "required: --allow-cachyos-acceptance SHA REPO_ROOT CONFIGURED_PATTERN"),
            (["--allow-cachyos-acceptance", "invalid", "/tmp", "~/configured/*"], "invalid SHA"),
            (["--allow-cachyos-acceptance", "a" * 40, "relative", "~/configured/*"], "absolute repository root required"),
            (["--allow-cachyos-acceptance", "a" * 40, "/tmp", ""], "configured scope required"),
        )
        for args, message in cases:
            with self.subTest(args=args), tempfile.TemporaryDirectory(prefix="aid-accept-refusal-") as temporary:
                # fish itself creates .config/.local even with --no-config.
                # Check for additional application setup, not zero shell
                # startup footprint; no directory or file is excluded.
                baseline = subprocess.run([FISH, "--no-config", "-c", "exit 0"],
                    env={"PATH": temporary, "HOME": temporary}, cwd=temporary,
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(baseline.returncode, 0, baseline.stderr)
                self.assertEqual(baseline.stdout, "")
                self.assertEqual(baseline.stderr, "")
                before = tree_snapshot(Path(temporary))
                # External commands remain unavailable through this empty PATH;
                # an unexpected error also fails the exact output check.
                result = subprocess.run([FISH, "--no-config", str(SCRIPT), *args],
                    env={"PATH": temporary, "HOME": temporary}, cwd=temporary,
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 2, result.stderr)
                separator = "; " if message.startswith("required:") else " "
                self.assertEqual(result.stdout, "ACCEPTANCE: REFUSED" + separator + message + "\n")
                self.assertEqual(result.stderr, "")
                self.assertEqual(tree_snapshot(Path(temporary)), before)


if __name__ == "__main__":
    unittest.main()
