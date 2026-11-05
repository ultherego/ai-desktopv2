"""Project test copies use a real owner policy. Only code execution and terminal consent are replaced."""
import contextlib
import io
import json
import os
import pwd
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_desktop import permissions, project_cli, project_scope, project_step, task_cgroup, task_sandbox


class ProjectTestRecipeTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(dir=pwd.getpwuid(os.getuid()).pw_dir)))
        self.enterContext(patch.object(permissions, "_identity", return_value=(os.getuid(), str(self.home))))
        self.config = self.home / ".config/ai-desktop/permissions.toml"
        self.config.parent.mkdir(parents=True)
        self.policy = ('version = 2\n[network]\nclaude = false\n[files]\n'
                       'read = ["~/project/**"]\ndeny = ["~/project/private/**"]\n')
        self.config.write_text(self.policy)
        self.config.chmod(0o600)
        self.root = self.home / "project"
        self.root.mkdir()
        (self.root / "private").mkdir()
        (self.root / "private" / "password.txt").write_text("synthetic-private")
        self.testfile = self.root / "test_calc.py"
        self.testfile.write_bytes(b"\xef\xbb\xbfdef test_answer(): assert 1\r\n")
        self.testfile.chmod(0o700)
        (self.root / "alias").symlink_to(self.testfile)
        project = project_scope.Project(permissions.load_policy(), str(self.root)).scan()
        self.session = project_cli.ProjectSession(project, "project", project.policy,
                                                 SimpleNamespace(details=False), lambda _: "yes")
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch("sys.stdin.isatty", return_value=True))
        self.enterContext(patch("sys.stdout.isatty", return_value=True))
        self.runner = self.enterContext(patch.object(task_sandbox, "run_copy", return_value={
            "passed": True, "output": "SYNTHETIC-LOCAL-ONLY", "exit_code": 0}))

    def test_test_recipe_copies_exact_authorized_bytes_and_only_remembers_the_outcome(self):
        status = self.session.test_project("pytest")
        self.assertIn("pytest passed", status)
        self.assertNotIn("SYNTHETIC-LOCAL-ONLY", status)
        self.assertIn("SYNTHETIC-LOCAL-ONLY", self.output.getvalue())
        files, recipe = self.runner.call_args.args
        self.assertEqual(recipe, "pytest")
        self.assertEqual(self.runner.call_args.kwargs["executable"], ["test_calc.py"])
        self.assertEqual(files, {"test_calc.py": self.testfile.read_bytes().decode()})

    def test_owner_decline_policy_revocation_and_concurrent_edit_block_the_runner(self):
        def revoke(_prompt):
            self.config.write_text(self.policy + "# revoked\n")
            return "yes"

        def change(_prompt):
            self.testfile.write_bytes(b"def test_new(): pass\n")
            return "yes"

        for reader in (lambda _: "no", change, revoke):
            with self.subTest(reader=reader):
                self.session.reader = reader
                self.assertIsNone(self.session.test_project("python-unittest"))
                self.runner.assert_not_called()

    def test_equal_size_rewrite_with_mtime_restored_during_consent_denies_execution(self):
        def edit(_prompt):
            before = self.testfile.stat()
            self.testfile.write_bytes(self.testfile.read_bytes().replace(b"assert 1", b"assert 0"))
            os.utime(self.testfile, ns=(before.st_atime_ns, before.st_mtime_ns))
            return "yes"
        self.session.reader = edit
        self.assertIsNone(self.session.test_project("npm-test"))
        self.runner.assert_not_called()

    def test_unsupported_or_incomplete_copies_never_run_a_smaller_suite(self):
        (self.root / "unsupported").write_bytes(b"\xff\xff")
        self.assertIsNone(self.session.test_project("pytest"))
        self.runner.assert_not_called()
        (self.root / "unsupported").unlink()
        (self.root / "large").write_bytes(b"a" * 65537)
        self.assertIsNone(self.session.test_project("pytest"))
        self.runner.assert_not_called()

    def test_model_cannot_select_program_options_or_files_for_a_project_suite(self):
        for recipe in ("pytest", "python-unittest", "go-test", "npm-test"):
            self.assertEqual(project_step.parse_decision(json.dumps({"action": "test", "recipe": recipe}))["recipe"], recipe)
        for other in ({"recipe": "pytest -k pass"}, {"recipe": "pytest", "argv": ["sh"]}, {"recipe": "test.sh"}):
            with self.subTest(other=other), self.assertRaises(ValueError):
                project_step.parse_decision(json.dumps({"action": "test", **other}))


class CgroupPreflightTests(unittest.TestCase):
    def test_scope_name_membership_and_effective_controller_limits_must_all_match(self):
        unit = task_cgroup.scope_name()
        membership = "0::/app.slice/" + unit
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope = root / "app.slice" / unit
            scope.mkdir(parents=True)
            controls = {"memory.max": str(task_cgroup.MEMORY), "memory.swap.max": "0",
                        "pids.max": str(task_cgroup.PIDS), "cpu.max": "200000 100000"}
            for key, limit in controls.items():
                (scope / key).write_text(limit)
            task_cgroup.verify_limits(unit, membership, root)
            for key, limit in (("memory.max", "max"), ("pids.max", "10000"),
                               ("memory.swap.max", "1"), ("cpu.max", "max 100000")):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    (scope / key).write_text(limit)
                    task_cgroup.verify_limits(unit, membership, root)
                (scope / key).write_text(controls[key])
            for outside in ("0::/", membership + "/child", membership.replace("app.slice/", "../")):
                with self.assertRaises(ValueError):
                    task_cgroup.verify_limits(unit, outside, root)
            (scope / "pids.max").unlink()
            with self.assertRaises(OSError):
                task_cgroup.verify_limits(unit, membership, root)

    def test_failed_scope_never_falls_back_to_launching_the_suite_directly(self):
        from unittest.mock import Mock
        provisional = Mock(return_value=(1, b"", b"scope unavailable"))
        with patch.object(os, "getuid", return_value=1000), patch.object(os, "geteuid", return_value=1000), \
                patch.object(task_sandbox, "bounded_process", provisional), patch.object(task_cgroup, "cleanup") as end:
            result = task_sandbox.run_copy({"go.mod": "module example.invalid/m\n"}, "go-test")
        self.assertFalse(result["passed"])
        argv = provisional.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/systemd-run")
        self.assertLess(argv.index("--scope"), argv.index("/usr/bin/bwrap"))
        self.assertEqual(argv[-1], "go-test")
        self.assertEqual(provisional.call_count, 1)
        end.assert_called_once()
