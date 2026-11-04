import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import executor, worker
from test_report_policy import report_fixture


class WorkerTests(unittest.TestCase):
    def test_plain_report_has_no_warnings(self):
        report = report_fixture()
        self.assertEqual(worker.evaluate(report), {"signals": [], "unknown": []})

    def test_flags_are_predefined_and_redacted(self):
        report = report_fixture()
        report["systemd_health"] = {"state": "degraded", "failed_units": 2}
        report["btrfs_health"]["free_percent"] = 8
        result = worker.evaluate(report)
        self.assertEqual(result["signals"], [
            "systemd_degraded", "failed_units_present",
            "btrfs_low_space",
        ])
        self.assertNotIn("private", json.dumps(result))

    def test_rejects_unexpected_fields_before_processing(self):
        report = report_fixture()
        report["secret"] = "SENSITIVE"
        with self.assertRaises(ValueError):
            worker.evaluate(report)

    def test_check_rejects_oversized_stdin(self):
        with patch("sys.stdin", io.StringIO("X" * 4097)):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(worker.main(["--check"]), 1)
        self.assertEqual(out.getvalue().strip(), '{"error":"rejected"}')

    def test_check_rejects_secret_without_echoing(self):
        with patch("sys.stdin", io.StringIO('{"password":"MY-SECRET"}')):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(worker.main(["--check"]), 1)
        self.assertNotIn("MY-SECRET", out.getvalue())

    def test_check_valid_report(self):
        with patch("sys.stdin", io.StringIO(json.dumps(report_fixture()))):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(worker.main(["--check"]), 0)
        obj = json.loads(out.getvalue())
        self.assertEqual(obj["signals"], [])

    def test_probe_requires_parent_namespace_metadata(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(worker.probe())


class ExecutorTests(unittest.TestCase):
    def test_mounts_only_fixed_python_files_readonly(self):
        cmd = executor.build_command(
            executor.PACKAGE_ROOT,
            "--check",
            Path("/home/private-user"),
            "net:[1]",
        )
        for flag in ("--unshare-user", "--unshare-pid", "--unshare-net",
                     "--unshare-ipc", "--unshare-uts", "--clearenv",
                     "--new-session", "--die-with-parent"):
            self.assertIn(flag, cmd)
        self.assertNotIn("--bind", cmd)
        self.assertNotIn("--share-net", cmd)
        self.assertNotIn("/home/private-user", [
            cmd[i + 1] for i, value in enumerate(cmd)
            if value == "--ro-bind"
        ])
        mounts = [
            cmd[i:i + 3] for i, flag in enumerate(cmd) if flag == "--ro-bind"
        ]
        self.assertEqual(len(mounts), 1 + len(executor.WORKER_FILES))
        self.assertIn(
            ["--ro-bind", str(executor.PACKAGE_ROOT / "worker.py"),
             "/opt/ai_desktop/worker.py"],
            mounts,
        )
        self.assertIn("--tmpfs", cmd)
        self.assertEqual(cmd[-1], "--check")

    def test_refuses_unapproved_action(self):
        with self.assertRaises(ValueError):
            executor.build_command(
                executor.PACKAGE_ROOT, "--shell", Path("/home/user"), "net:[1]"
            )

    def test_refuses_symlinked_module(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in executor.WORKER_FILES:
                (base / name).write_text("x", encoding="utf-8")
            (base / "worker.py").unlink()
            (base / "worker.py").symlink_to(base / "__init__.py")
            with self.assertRaises(ValueError):
                executor.build_command(base, "--check", "/home/user", "net:[1]")

    def test_bwrap_invocation_never_uses_shell(self):
        done = subprocess.CompletedProcess([], 0, '{"probe":"PASS"}', "")
        with patch.object(executor.shutil, "which", return_value="/usr/bin/bwrap"):
            with patch.object(executor.os, "readlink", return_value="net:[1]"):
                with patch.object(executor.subprocess, "run", return_value=done) as run:
                    self.assertEqual(executor._run("--probe"), {"probe": "PASS"})
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertEqual(run.call_args.kwargs["timeout"], 20)
        self.assertEqual(run.call_args.kwargs["input"], None)

    def test_executor_inspect_rejects_unvalidated_data(self):
        with patch("ai_desktop.diagnose.collect", return_value={"secret": "abc"}):
            with patch.object(executor, "_run") as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(executor.main(["inspect"]), 1)
                run.assert_not_called()

    def test_executor_inspect_no_ai_needed(self):
        with patch("ai_desktop.diagnose.collect", return_value=report_fixture()):
            with patch.object(executor, "_run", return_value={
                "signals": ["btrfs_low_space"], "unknown": [],
            }) as run:
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(executor.main(["inspect"]), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], "--check")
        self.assertIn("btrfs_low_space", out.getvalue())
        self.assertNotIn("7.3.0-4-cachyos", out.getvalue())

    def test_executor_rejects_unexpected_worker_output(self):
        with patch("ai_desktop.diagnose.collect", return_value=report_fixture()):
            with patch.object(executor, "_run", return_value={
                "signals": ["arbitrary_command"], "unknown": [],
            }):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(executor.main(["inspect"]), 1)

    def test_executor_probe(self):
        with patch.object(executor, "_run", return_value={"probe": "PASS"}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(executor.main(["self-test"]), 0)
            self.assertIn("PASS", out.getvalue())

    def test_executor_fails_closed_if_bwrap_missing(self):
        with patch.object(executor.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError):
                executor._run("--probe")


if __name__ == "__main__":
    unittest.main()
