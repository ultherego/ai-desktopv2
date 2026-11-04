"""Material acceptance wrapper: ordering, plain-sentence sessions, no automation of consent."""
import ast
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/accept_material.fish"
FISH = shutil.which("fish")
SHA = "c" * 40


class MaterialScriptTests(unittest.TestCase):
    def test_embedded_python_is_valid_without_execution(self):
        snippets = re.findall(r"python3 -I -B -c '([^']*)'", SCRIPT.read_text())
        self.assertEqual(len(snippets), 1)
        ast.parse(snippets[0])

    def test_the_app_is_started_four_times_with_the_terminal_and_never_fed_input(self):
        source = SCRIPT.read_text()
        lines = [l for l in source.splitlines() if "/usr/bin/ai-desktop" in l]
        self.assertEqual(len(lines), 4)                       # A, B, C in --native and C again in --native-c
        for line in lines:
            self.assertNotRegex(line, r"[|<>]|echo|printf|yes|expect")
        for forbidden in ("sudo", "pacman", "systemctl", "reboot", "git commit", "git push", "rm -rf",
                          "ANTHROPIC", ".claude", "OAuth"):
            self.assertNotIn(forbidden, source)

    def test_plan_checks_and_stop_come_before_the_first_request(self):
        source = SCRIPT.read_text()
        run = source.index('/usr/bin/ai-desktop material "$aid_project/small.py"')
        order = [source.index(text) for text in (
            "installed_matches(native=False)", "PLAN ODBIORU NATYWNEGO", "najwyzej ok. 30",
            "lc1_check.py\" prepare", "lc1_check.py\" refusals --lib /usr/lib/ai-desktop")]
        self.assertEqual(order, sorted(order))
        self.assertLess(order[-1], run)
        self.assertIn('if test "$aid_refusals" = 0', source[:run])
        self.assertIn("PRZERWANO przed sesja", source)
        self.assertIn("zadne zadanie do Claude nie zostalo wyslane", source)
        self.assertIn("--v", source[run:run + 120])

    def test_questions_are_plain_sentences_and_each_mode_is_exercised(self):
        source = SCRIPT.read_text()
        for text in ("Przeanalizuj ten plik i wskaz mozliwe bledy", "Co oznaczaja wpisy ERROR-MARKER", "nie  (0 zadan)",
                     "pamiec rozmowy", "Przeanalizuj caly plik i powiedz", "pokrycie 3 z 3", "CALY ten plik",
                     "bez /pytaj i numerow wierszy", "medium.log"):
            self.assertIn(text, source)
        self.assertNotIn(">>> tak   (zadanie", source)       # no hand-picked line ranges left from LC1

    def test_results_name_every_stage_and_check_files_after_the_sessions(self):
        source = SCRIPT.read_text()
        for field in ("REFUSALS=", "SESSION_A=", "SESSION_B=", "SESSION_C=", "SOURCES_UNCHANGED=", "CLEANUP=", "MAIN_UNCHANGED="):
            self.assertIn(field, source)
        for marker in ('/usr/bin/ai-desktop material "$aid_project/medium.log" --v',):
            for start in [i for i in range(len(source)) if source.startswith(marker, i)]:
                self.assertLess(start, source.index('lc1_check.py" check-unchanged', start))

    @unittest.skipUnless(FISH, "fish is available in the kernel CI job")
    def test_fish_syntax_help_and_argument_refusals(self):
        subprocess.run([FISH, "--no-execute", str(SCRIPT)], check=True)
        self.assertEqual(subprocess.run([FISH, "--no-config", str(SCRIPT), "--help"], capture_output=True).returncode, 0)
        with tempfile.TemporaryDirectory(prefix="ai-lc1-script-") as tmp:
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


class NativeCTests(unittest.TestCase):
    def test_native_c_repeats_only_session_c_in_the_existing_directory(self):
        source = SCRIPT.read_text()
        block = source[source.index('if test "$aid_mode" = --native-c; and test "$aid_installed" = 0'):
                       source.index('if test "$aid_mode" = --native; and test "$aid_installed" = 0')]
        self.assertEqual(block.count("/usr/bin/ai-desktop"), 1)
        self.assertIn("medium.log", block)
        for text in ("small.py", "log1m.txt", "prepare", "refusals"):
            self.assertNotIn(text, block)
        self.assertIn("check-unchanged", block)
        self.assertIn("--native-c", source[:source.index("set -l aid_mode")])
        self.assertIn("medium.log missing", source)
