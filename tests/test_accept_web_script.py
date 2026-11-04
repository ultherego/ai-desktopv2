"""Web acceptance syntax and fail-closed opt-in; no native execution."""
import ast
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from test_accept_candidate_script import tree_snapshot

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/accept_web_candidate.fish"
FISH = shutil.which("fish")


class WebCandidateScriptTests(unittest.TestCase):
    def test_embedded_python_is_valid_without_execution(self):
        snippets = re.findall(r"python3 -I -B -c '([^']*)'", SCRIPT.read_text())
        self.assertEqual(len(snippets), 2)
        for snippet in snippets:
            ast.parse(snippet)

    def test_modes_stop_before_forbidden_operations_and_do_not_publish(self):
        source = SCRIPT.read_text()
        local_exit = source.index('if test "$aid_mode" = --local-only')
        native_copy = source.index('__aid18_step NATIVE_COPY')
        install_exit = source.index('if contains -- "$aid_mode" --allow-install --allow-codex-install')
        self.assertLess(source.index('__aid18_logged LOCAL'), local_exit)
        self.assertLess(local_exit, native_copy)
        self.assertLess(native_copy, source.index('__aid18_step UPGRADE'))
        self.assertLess(source.index('__aid18_step INSTALLED'), install_exit)
        self.assertLess(install_exit, source.index('__aid18_step HOST_WEB'))
        for command in ('git commit', 'git push', 'git merge', 'ROADMAP.md', 'README.md'):
            self.assertNotIn(command, source)
        self.assertEqual(source.count('worktree add --detach'), 1)
        self.assertIn('set -l aid_codes $pipestatus', source)
        self.assertIn('HOST_WEB=%s', source)
        self.assertIn('set -g aid18_web NOT_RUN', source)

    def test_the_part_profile_mode_runs_only_after_installation_never_automates_answers_and_never_edits_the_config(self):
        source = SCRIPT.read_text()
        branch = source.index('if test "$aid_mode" = --part-profile-acceptance')
        self.assertLess(source.index('if contains -- "$aid_mode" --allow-install --allow-codex-install'), branch)
        self.assertLess(source.index('__aid18_step INSTALLED'), branch)
        self.assertLess(branch, source.index('__aid18_step HOST_WEB'))
        function = source[source.index('function __aid18_part_profile'):source.index('function __aid18_cycle')]
        self.assertLess(function.index("check-config"), function.index("execute"))        # the config is read BEFORE each series
        self.assertIn("or return $status", function[function.index("check-config"):function.index("for aid_label")])
        for forbidden in ("yes", "| ai-desktop", "sed -i", "permissions.toml\"", "> ~/.config", "tee", "printf 't", "echo t"):
            self.assertNotIn(forbidden, function)
        self.assertIn("plan --limit 45", function)                                       # the request limit is checked first
        self.assertIn('mktemp -d "$HOME/ai-part-bench-', function)                         # a fresh directory, never ~/ai-part-bench

    @unittest.skipUnless(FISH, "fish is available in the kernel CI job")
    def test_the_runner_stops_at_the_first_failed_run_and_never_starts_the_next_case(self):
        source = SCRIPT.read_text()
        step = source[source.index("function __aid18_step"):source.index("function __aid18_logged")]
        part = source[source.index("function __aid18_part_profile"):source.index("function __aid18_cycle")]
        scenarios = {                       # which execute call fails -> the execute calls that must have happened
            None: ["log-240", "json-240", "log-512", "json-512"],
            "log-240": ["log-240"],
            "json-240": ["log-240", "json-240"],
            "log-512": ["log-240", "json-240", "log-512"],
        }
        for failing, expected in scenarios.items():
            with self.subTest(failing=failing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "bin").mkdir()
                (root / "tree").mkdir()
                log = root / "calls.log"
                stub = root / "bin/python3"
                stub.write_text("#!/bin/sh\n"
                                "echo \"$*\" >> '%s'\n"
                                "case \"$*\" in\n"
                                "  *'labels 240'*) printf 'log-240\\njson-240\\n';;\n"
                                "  *'labels 512'*) printf 'log-512\\njson-512\\n';;\n"
                                "  *'execute %s '*) exit 130;;\n"
                                "esac\nexit 0\n" % (log, failing or "none"))
                stub.chmod(0o755)
                runner = root / "run.fish"
                runner.write_text("set -g aid18_results\nset -g aid18_web NOT_RUN\nset -g aid18_install NOT_RUN\n"
                                  + step + part + "__aid18_part_profile '%s'\nprintf 'RC=%%s\\n' $status\n" % (root / "tree"))
                env = {"PATH": "%s/bin:/usr/bin:/bin" % root, "HOME": temporary}
                result = subprocess.run([FISH, "--no-config", str(runner)], input="\n\n", env=env, cwd=temporary,
                                        capture_output=True, text=True, timeout=30)
                calls = log.read_text().splitlines()
                executed = [line.split("execute ")[1].split()[0] for line in calls if "execute " in line]
                self.assertEqual(executed, expected, result.stdout + result.stderr)
                self.assertEqual("RC=0" in result.stdout, failing is None, result.stdout)
                if failing is not None:
                    self.assertIn("RC=130", result.stdout)                            # the runner returns the failure's own code
                    self.assertFalse(any("report" in line for line in calls))         # no report of a failed acceptance
                else:
                    self.assertTrue(any(" report " in " " + line + " " for line in calls))
                configs = [line for line in calls if "check-config" in line]
                self.assertTrue(all(line.rstrip().endswith(("240", "512")) for line in configs))

    @unittest.skipUnless(FISH, "fish is available in the kernel CI job")
    def test_help_has_no_host_effects_and_describes_separate_consents(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = {"PATH": temporary, "HOME": temporary}
            subprocess.run([FISH, "--no-config", "-c", "exit 0"], env=env, cwd=temporary, check=True)
            before = tree_snapshot(Path(temporary))
            result = subprocess.run([FISH, "--no-config", str(SCRIPT), "--help"],
                env=env, cwd=temporary, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            for mode in ('--local-only', '--allow-install', '--allow-cachyos-acceptance', '--part-profile-acceptance'):
                self.assertIn(mode, result.stdout)
            self.assertEqual(tree_snapshot(Path(temporary)), before)

    @unittest.skipUnless(FISH, "fish is available in the kernel CI job")
    def test_invalid_opt_in_has_no_additional_effects_beyond_fish_startup(self):
        required = "required: MODE SHA REPO_ROOT [BRANCH]"
        for args, reason in (([], required), (["--allow-cachyos-acceptance"], required),
                (["--other", "a" * 40, "/tmp"], required),
                (["--allow-cachyos-acceptance", "bad", "/tmp"], "invalid SHA"),
                (["--allow-cachyos-acceptance", "a" * 40, "relative"], "absolute repository root required"),
                (["--local-only", "a" * 40, "/tmp", "../main"], "invalid branch"),
                (["--allow-install", "a" * 40, "/tmp", "-main"], "invalid branch"),
                (["--local-only", "bad", "/tmp"], "invalid SHA"),
                (["--part-profile-acceptance", "bad", "/tmp"], "invalid SHA"),
                (["--part-profile-acceptance", "a" * 40, "relative"], "absolute repository root required")):
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
                    (2, "ACCEPTANCE: REFUSED" + separator + reason + "\n", ""))
                self.assertEqual(tree_snapshot(Path(temporary)), before)


if __name__ == "__main__":
    unittest.main()
