"""Restart acceptance wrapper: syntax, argument refusal and ordering; no host action."""
import ast
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/accept_restart_binding.fish"
FISH = shutil.which("fish")
SHA = "a" * 40


class RestartScriptTests(unittest.TestCase):
    def test_embedded_python_is_valid_without_execution(self):
        snippets = re.findall(r"python3 -I -B -c '([^']*)'", SCRIPT.read_text())
        self.assertEqual(len(snippets), 1)
        ast.parse(snippets[0])

    def test_helper_runs_only_after_sha_worktree_script_and_installed_checks(self):
        source = SCRIPT.read_text()
        order = [source.index(marker) for marker in (
            "set -l aid_sha $status", "git worktree add --detach", "set aid_script_match $status",
            "set aid_installed $status", "restart_binding.py")]
        self.assertEqual(order, sorted(order))
        self.assertIn("--lib /usr/lib/ai-desktop", source)
        for forbidden in ("sudo", "pacman", "reboot", "systemctl", "git commit", "git push", "hello.py\"", "rm -rf"):
            self.assertNotIn(forbidden, source)

    @unittest.skipUnless(FISH, "fish is available in the kernel CI job")
    def test_fish_syntax_and_help(self):
        subprocess.run([FISH, "--no-execute", str(SCRIPT)], check=True)
        done = subprocess.run([FISH, "--no-config", str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0)
        self.assertIn("--prepare", done.stdout)

    @unittest.skipUnless(FISH, "fish is available in the kernel CI job")
    def test_bad_arguments_and_missing_terminal_refuse_before_any_git_or_files(self):
        with tempfile.TemporaryDirectory(prefix="ai-restart-script-") as tmp:
            for args in ([], ["--prepare"], ["--other", SHA, tmp], ["--prepare", "abc", tmp],
                         ["--prepare", SHA, "relative"], ["--prepare", SHA, tmp, "../x"],
                         ["--verify", SHA, tmp]):
                with self.subTest(args=args):
                    done = subprocess.run([FISH, "--no-config", str(SCRIPT), *args], capture_output=True,
                                          text=True, stdin=subprocess.DEVNULL, cwd=tmp)
                    self.assertEqual(done.returncode, 2, done.stdout)
                    self.assertIn("REFUSED", done.stdout)
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()


class InstalledMatchTests(unittest.TestCase):
    """The tree of a fresh worktree has no untracked native client (regression: PREPARE=2)."""

    def run_check(self, native, write_binary=False, tamper=False, *, staged_codex=False, mutate=None):
        import host_analysis_acceptance as acceptance
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix="ai-installed-match-") as tmp:
            root, installed = Path(tmp) / "tree", Path(tmp) / "installed"
            (root / "ai_desktop").mkdir(parents=True)
            (installed / "ai_desktop").mkdir(parents=True)
            (root / "packaging/systemd").mkdir(parents=True)
            (root / "packaging/arch").mkdir(parents=True)
            (installed / "units").mkdir()
            for name in acceptance.PACKAGED_SKILLS:
                content = f"---\nname: {name}\ndescription: Synthetic acceptance Skill.\n---\n\nUse supplied test evidence.\n"
                for base in (root, installed):
                    skill = base / "skills" / name
                    skill.mkdir(parents=True)
                    (skill / "SKILL.md").write_text(content)
            (root / "ai_desktop/a.py").write_text("x = 1\n")
            (installed / "ai_desktop/a.py").write_text("x = 2\n" if tamper else "x = 1\n")
            helpers = ("analysis-client.py", "analysis-worker.py", "analysis-session.py", "codex-auth.py", "codex-client.py", "codex-quota.py", "client-admin.py")
            units = ("ai-desktop-analysis@.socket", "ai-desktop-analysis@.service",
                     "ai-desktop-codex-limits@.socket", "ai-desktop-codex-limits@.service")
            for name in helpers + units:
                (root / "packaging/systemd" / name).write_text("u\n")
                (installed / name).write_text("u\n")
            system = Path(tmp) / "system"
            system.mkdir()
            for name in units:
                (system / name).write_text("u\n")
            binary = installed / "claude-code"
            binary.write_bytes(b"\x7fELF")
            if write_binary:
                (root / "packaging/arch/claude-code").write_bytes(b"\x7fELF")
            if staged_codex:
                (root / "packaging/arch/codex").write_bytes(b"\x7fELFsynthetic-codex")
                (installed / "codex").write_bytes(b"\x7fELFsynthetic-codex")
            if mutate is not None:
                mutate(root, installed)

            class FakePath(type(Path())):
                def __truediv__(self, other):
                    if str(self) == "/usr/lib/systemd/system":
                        return system / other
                    return super().__truediv__(other)
            with patch.object(acceptance, "ROOT", root), patch.object(acceptance, "INSTALL", installed), \
                    patch.object(acceptance, "BINARY", binary), patch.object(acceptance, "trusted_file"), \
                    patch.object(acceptance, "runtime_ready"), patch.object(acceptance.offline, "_installed_matches"), \
                    patch.object(acceptance, "Path", FakePath):
                acceptance.installed_matches(native=native)

    def setUp(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))

    def test_without_native_client_in_tree_the_code_check_still_passes(self):
        self.run_check(native=False)

    def test_system_clients_never_need_a_copy_in_the_application_tree(self):
        self.run_check(native=True)
        self.run_check(native=True, write_binary=True)

    def test_modified_installed_code_is_refused_in_both_modes(self):
        for native in (False, True):
            with self.subTest(native=native), self.assertRaises(ValueError):
                self.run_check(native=native, write_binary=True, tamper=True)

    def test_codex_bootstraps_are_required_and_compared_even_without_native_binaries(self):
        for native in (False, True):
            for helper in ("codex-auth.py", "codex-client.py", "codex-quota.py", "client-admin.py"):
                for change in ("missing", "modified"):
                    def mutate(root, installed):
                        target = installed / helper
                        if change == "missing":
                            target.unlink()
                        else:
                            target.write_text("different bootstrap\n")
                    with self.subTest(native=native, helper=helper, change=change), self.assertRaises((ValueError, OSError)):
                        self.run_check(native=native, write_binary=native, mutate=mutate)

    def test_legacy_codex_staging_is_ignored_while_application_files_are_still_compared(self):
        self.run_check(native=True, write_binary=True, staged_codex=True)
        for change in ("missing", "modified", "source-symlink"):
            def mutate(root, installed):
                if change == "missing":
                    (installed / "codex").unlink()
                elif change == "modified":
                    (installed / "codex").write_bytes(b"\x7fELFmodified")
                else:
                    source = root / "packaging/arch/codex"
                    source.unlink()
                    source.symlink_to(root / "missing-codex")
            with self.subTest(change=change):
                self.run_check(native=True, write_binary=True, staged_codex=True, mutate=mutate)

    def test_native_false_skips_both_untracked_clients_but_keeps_bootstrap_checks(self):
        def mutate(root, installed):
            (root / "packaging/arch/codex").write_bytes(b"\x7fELFdifferent-from-installed")
            (installed / "claude-code").write_bytes(b"\x7fELFalso-different")
        self.run_check(native=False, write_binary=True, staged_codex=True, mutate=mutate)

    def test_all_packaged_skills_are_compared_even_without_native_binaries(self):
        self.run_check(native=False)
        for name in ("code-review", "project-analysis"):
            for change in ("source-missing", "target-missing", "target-modified",
                           "source-file-symlink", "target-file-symlink", "source-dir-symlink",
                           "target-dir-symlink", "source-root-symlink", "target-root-symlink"):
                def mutate(root, installed):
                    base = root if change.startswith("source") else installed
                    target = base / "skills" / name / "SKILL.md"
                    if change.endswith("missing"):
                        target.unlink()
                    elif change == "target-modified":
                        target.write_text(target.read_text() + "Different instructions.\n")
                    elif change.endswith("file-symlink"):
                        copy = base / "copy-skill.md"
                        target.rename(copy)
                        target.symlink_to(copy)
                    elif change.endswith("dir-symlink"):
                        original = target.parent
                        replacement = base / "copy-skill"
                        original.rename(replacement)
                        original.symlink_to(replacement, target_is_directory=True)
                    else:
                        original = base / "skills"
                        replacement = base / "copy-skills"
                        original.rename(replacement)
                        original.symlink_to(replacement, target_is_directory=True)
                with self.subTest(skill=name, change=change), self.assertRaises((ValueError, OSError)):
                    self.run_check(native=False, mutate=mutate)

    def test_skill_manifest_cannot_shrink_or_gain_untracked_directories(self):
        for change in ("source-missing-directory", "target-missing-directory",
                       "source-empty", "target-empty", "source-extra", "target-extra"):
            def mutate(root, installed):
                import shutil
                base = root if change.startswith("source") else installed
                catalog = base / "skills"
                if change.endswith("missing-directory"):
                    shutil.rmtree(catalog / "code-review")
                elif change.endswith("empty"):
                    shutil.rmtree(catalog)
                    catalog.mkdir()
                else:
                    (catalog / "untracked-skill").mkdir()
            with self.subTest(change=change), self.assertRaises((ValueError, OSError)):
                self.run_check(native=False, mutate=mutate)
