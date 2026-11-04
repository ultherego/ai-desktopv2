"""Project acceptance wrapper: ordering, plain-sentence session, no automation of consent, no policy edits."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/accept_project.fish"
FISH = shutil.which("fish")
SHA = "c" * 40


class ProjectScriptTests(unittest.TestCase):
    def test_the_app_is_started_once_with_the_terminal_and_never_fed_input_or_touching_config(self):
        source = SCRIPT.read_text()
        lines = [l for l in source.splitlines() if "/usr/bin/ai-desktop" in l]
        self.assertEqual(len(lines), 1)
        self.assertNotRegex(lines[0], r"[|<>]|echo|printf|yes|expect")
        for forbidden in ("sudo", "pacman", "systemctl", "reboot", "git commit", "git push", "rm -rf",
                          "ANTHROPIC", ".claude", "OAuth", "permissions.toml >", "tee "):
            self.assertNotIn(forbidden, source)

    def test_checks_and_stop_come_before_the_first_request_and_cleanup_after_it(self):
        source = SCRIPT.read_text()
        run = source.index("/usr/bin/ai-desktop project")
        order = [source.index(text) for text in (
            "installed_matches(native=False)", "PLAN ODBIORU NATYWNEGO", 'lc4_check.py" prepare',
            'lc4_check.py" policy --lib /usr/lib/ai-desktop', 'lc4_check.py" scan --lib /usr/lib/ai-desktop')]
        self.assertEqual(order, sorted(order))
        self.assertLess(order[-1], run)
        self.assertIn('if test "$aid_scan" = 0', source[:run])
        self.assertLess(run, source.index('lc4_check.py" check-unchanged'))
        self.assertIn("PRZERWANO przed sesja", source)
        self.assertIn("skrypt jej NIE zmienia", source)

    def test_results_name_every_stage(self):
        source = SCRIPT.read_text()
        for field in ("POLICY=", "SCAN=", "SESSION=", "SOURCES_UNCHANGED=", "CLEANUP=", "MAIN_UNCHANGED="):
            self.assertIn(field, source)

    @unittest.skipUnless(FISH, "fish is available in the kernel CI job")
    def test_fish_syntax_help_and_argument_refusals(self):
        subprocess.run([FISH, "--no-execute", str(SCRIPT)], check=True)
        self.assertEqual(subprocess.run([FISH, "--no-config", str(SCRIPT), "--help"], capture_output=True).returncode, 0)
        with tempfile.TemporaryDirectory(prefix="ai-lc4-script-") as tmp:
            for args in ([], ["--offline"], ["--other", SHA, tmp], ["--native", "abc", tmp],
                         ["--offline", SHA, "relative"], ["--offline", SHA, tmp, "../x"], ["--native", SHA, tmp]):
                with self.subTest(args=args):
                    done = subprocess.run([FISH, "--no-config", str(SCRIPT), *args], capture_output=True, text=True,
                                          stdin=subprocess.DEVNULL, cwd=tmp)
                    self.assertEqual(done.returncode, 2, done.stdout)
                    self.assertIn("REFUSED", done.stdout)
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
