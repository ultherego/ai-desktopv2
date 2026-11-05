"""E6 acceptance wrapper and native helper: no automation of consent, no host action."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import e6_cycle as e6

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/accept_e6_cycle.fish"
FISH = shutil.which("fish")
SHA = "b" * 40


class E6ScriptTests(unittest.TestCase):
    def test_embedded_python_is_valid_without_execution(self):
        snippets = re.findall(r"python3 -I -B -c '([^']*)'", SCRIPT.read_text())
        self.assertEqual(len(snippets), 1)
        ast.parse(snippets[0])

    def test_app_is_never_fed_input_and_nothing_privileged_runs(self):
        source = SCRIPT.read_text()
        app_lines = [l for l in source.splitlines() if "/usr/bin/ai-desktop" in l]
        self.assertEqual(len(app_lines), 4)  # three task runs and one rollback
        for line in app_lines:
            self.assertNotRegex(line, r"[|<>]|echo|printf|yes|expect|script")
        for forbidden in ("sudo", "pacman", "systemctl", "reboot", "git commit", "git push", "rm -rf",
                          "ANTHROPIC", ".claude", "permissions.toml", "OAuth"):
            self.assertNotIn(forbidden, source)

    def test_scope_and_request_count_are_printed_before_the_first_native_run(self):
        source = SCRIPT.read_text()
        first_run = source.index("/usr/bin/ai-desktop task run")
        for text in ("Requests to Claude: 2", "Data sent per request", "Write scope", "You type the answers"):
            self.assertLess(source.index(text), first_run)
        self.assertLess(source.index("installed_matches"), first_run)
        self.assertLess(source.index("aid_ci -eq 0"), source.index("installed_matches"))

    def test_native_run_stops_after_the_first_failed_case_before_more_requests(self):
        # Regression: a mistyped answer in case A once let B and C spend two more requests.
        source = SCRIPT.read_text()
        native = source[source.index('if test "$aid_prepare" = 0'):]
        guards = native.count('if test -z "$aid_stop"')
        self.assertEqual(guards, 3)  # B, C and D only run while nothing has failed
        for case in ("A", "B", "C", "D"):
            self.assertIn("set aid_stop " + case, native)
        runs = [m.start() for m in re.finditer(r"/usr/bin/ai-desktop task (run|rollback)", native)]
        self.assertEqual(len(runs), 4)
        self.assertLess(native.index('set aid_stop A'), runs[1])
        self.assertLess(native.index('set aid_stop B'), runs[2])
        self.assertLess(native.index('set aid_stop C'), runs[3])
        self.assertIn("further requests to Claude were NOT sent", native)
        self.assertIn("kept for inspection", native)

    def test_literal_expected_answers_are_shown_before_each_prompt(self):
        native = SCRIPT.read_text()
        first = native.index("/usr/bin/ai-desktop task run")
        self.assertLess(native.index("TYPE EXACTLY: n"), first)
        self.assertEqual(native.count(">>> "), 4)

    def test_refusal_case_a_has_expected_exit_and_checks_files_not_messages(self):
        source = SCRIPT.read_text()
        for needle in ("check-unchanged", "check-written", "check-rolled-back",
                       "test $aid_a_cli -eq 2", "test $aid_c_cli -eq 0"):
            self.assertIn(needle, source)

    @unittest.skipUnless(FISH, "fish is available in the kernel CI job")
    def test_fish_syntax_help_and_argument_refusals(self):
        subprocess.run([FISH, "--no-execute", str(SCRIPT)], check=True)
        done = subprocess.run([FISH, "--no-config", str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0)
        with tempfile.TemporaryDirectory(prefix="ai-e6-script-") as tmp:
            for args in ([], ["--offline"], ["--other", SHA, tmp], ["--native", "abc", tmp],
                         ["--offline", SHA, "relative"], ["--offline", SHA, tmp, "../x"],
                         ["--native", SHA, tmp]):
                with self.subTest(args=args):
                    done = subprocess.run([FISH, "--no-config", str(SCRIPT), *args], capture_output=True,
                                          text=True, stdin=subprocess.DEVNULL, cwd=tmp)
                    self.assertEqual(done.returncode, 2, done.stdout)
                    self.assertIn("REFUSED", done.stdout)
            self.assertEqual(list(Path(tmp).iterdir()), [])


class NativeHelperTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.geteuid(), 0)
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ai-e6-home-")))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}))
        (self.home / "hello.py").write_text("mine\n")
        self.mine = e6.manifest(self.home)

    def run_helper(self, action):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = e6.main([action])
        return code, out.getvalue()

    def test_prepare_check_cleanup_cycle_touches_only_the_synthetic_directory(self):
        self.assertEqual(self.run_helper("prepare")[0], 0)
        self.assertEqual(self.run_helper("prepare")[0], 2)
        self.assertEqual(self.run_helper("check-unchanged")[0], 0)
        self.assertEqual(self.run_helper("check-written")[0], 1)       # nothing was written
        self.assertEqual(self.run_helper("check-rolled-back")[0], 1)   # nothing to roll back
        self.assertEqual(self.run_helper("cleanup")[0], 0)
        self.assertEqual(e6.manifest(self.home), self.mine)

    def test_cleanup_refuses_while_an_applied_task_is_not_rolled_back(self):
        self.run_helper("prepare")
        state = self.home / e6.NAME / "ws" / e6.STATE
        state.mkdir()
        (state / ("c" * 64 + ".json")).write_text(json.dumps({"status": "applied"}))
        self.assertEqual(self.run_helper("cleanup")[0], 2)
        self.assertTrue((self.home / e6.NAME).exists())

    def test_unchanged_check_fails_after_any_edit(self):
        self.run_helper("prepare")
        (self.home / e6.NAME / "ws" / "README.txt").write_text("changed\n")
        code, output = self.run_helper("check-unchanged")
        self.assertEqual(code, 1)
        self.assertIn("CHECK-UNCHANGED=FAIL", output)

    def test_symlinked_base_is_refused(self):
        (self.home / "x").mkdir()
        (self.home / e6.NAME).symlink_to(self.home / "x")
        with self.assertRaises(SystemExit):
            e6.main(["prepare"])


if __name__ == "__main__":
    unittest.main()
