"""Regression tests for the centralized verification runner (no external services)."""
import contextlib
import io
from pathlib import Path
import subprocess
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts import verify


class VerificationRunnerTests(unittest.TestCase):
    def test_ci_steps_never_launch_ai_or_bubblewrap(self):
        self.assertEqual(verify.STEPS["ci"], ("syntax", "unit", "cli", "skills", "doctor", "compare", "compare-summary", "snapshot", "compare-exit", "validate-report", "offline-pipeline", "stdin-pipeline", "snapshot-save", "e1-boundary", "e2-contract", "e2-broker", "e2-egress", "e3-e4-contracts", "e5-e6-contracts", "e7-install", "e7-host-cycle"))
        for step in verify.STEPS["ci"]:
            argv = verify.command_for(step)
            if argv is not None:
                self.assertNotIn("ask", argv)
                self.assertNotIn("executor", argv)
                self.assertNotIn("agent", argv)

    def test_compare_has_explicit_offline_step(self):
        self.assertEqual(
            verify.command_for("compare", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "test_compare.py", "-v"],
        )

    def test_compare_summary_has_explicit_offline_step(self):
        self.assertEqual(
            verify.command_for("compare-summary", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "test_compare_summary.py", "-v"],
        )

    def test_snapshot_has_explicit_unit_test_step(self):
        self.assertEqual(
            verify.command_for("snapshot", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "test_snapshot.py", "-v"],
        )

    def test_compare_exit_is_offline_unit_step(self):
        self.assertEqual(
            verify.command_for("compare-exit", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "test_compare_exit.py", "-v"],
        )

    def test_validate_report_is_offline_step(self):
        self.assertEqual(
            verify.command_for("validate-report", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "test_validate_snapshot.py", "-v"],
        )

    def test_offline_pipeline_has_explicit_ci_stage(self):
        self.assertEqual(
            verify.command_for("offline-pipeline", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "test_offline_pipeline.py", "-v"],
        )

    def test_stdin_pipeline_is_an_explicit_offline_ci_step(self):
        self.assertEqual(
            verify.command_for("stdin-pipeline", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "test_stdin_pipeline.py", "-v"],
        )

    def test_snapshot_save_is_explicit_ci_stage(self):
        self.assertEqual(
            verify.command_for("snapshot-save", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "test_private_snapshot_save.py", "-v"],
        )

    def test_e1_boundaries_have_explicit_offline_stage(self):
        self.assertEqual(
            verify.command_for("e1-boundary", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "test_security_boundary_e1.py", "-v"],
        )

    def test_e2_contract_has_offline_ci_step(self):
        self.assertEqual(
            verify.command_for("e2-contract", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "test_e2_*boundary*.py", "-v"],
        )
        self.assertEqual(
            verify.command_for("e2-host-probe", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "host_e2_*checks.py", "-v"],
        )
        self.assertNotIn("e2-host-probe", verify.STEPS["ci"])
        self.assertIn("e2-host-probe", verify.STEPS["local"])

    def test_e2_broker_policy_is_offline_ci_step(self):
        self.assertEqual(
            verify.command_for("e2-broker", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "test_e2_broker.py", "-v"],
        )
        self.assertIn("e2-broker", verify.STEPS["ci"])

    def test_e2_egress_policy_is_an_offline_ci_step(self):
        self.assertEqual(
            verify.command_for("e2-egress", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "test_e2_egress.py", "-v"],
        )
        self.assertIn("e2-egress", verify.STEPS["ci"])

    def test_e3_e4_contracts_run_offline_in_ci(self):
        self.assertEqual(
            verify.command_for("e3-e4-contracts", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "test_e3_e4*.py", "-v"],
        )
        self.assertIn("e3-e4-contracts", verify.STEPS["ci"])

    def test_btrfs_binding_host_gate_is_local_only(self):
        self.assertEqual(
            verify.command_for("e5-btrfs-binding", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "host_e5_btrfs_binding.py", "-v"],
        )
        self.assertIn("e5-btrfs-binding", verify.STEPS["local"])
        self.assertNotIn("e5-btrfs-binding", verify.STEPS["ci"])

    def test_e5_e6_contracts_and_host_cycle_are_separated(self):
        self.assertEqual(
            verify.command_for("e5-e6-contracts", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "test_e5_*.py", "-v"],
        )
        self.assertEqual(
            verify.command_for("e5-host-cycle", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "host_e5_workspace_checks.py", "-v"],
        )
        self.assertNotIn("e5-host-cycle", verify.STEPS["ci"])
        self.assertIn("e5-host-cycle", verify.STEPS["local"])

    def test_e7_process_lifecycle_gate_is_offline_in_ci_and_local(self):
        self.assertIn("e7-host-cycle", verify.STEPS["ci"])
        self.assertIn("e7-host-cycle", verify.STEPS["local"])
        self.assertIn("e7-host-cycle", verify.STEPS["ai"])
        self.assertEqual(verify.command_for("e7-host-cycle", python="python3"),
            ["python3", "-B", "-m", "unittest", "discover", "-s", "tests",
             "-p", "host_e7_lifecycle_checks.py", "-v"])

    def test_e7_fresh_prefix_installer_is_offline_ci_step(self):
        self.assertEqual(
            verify.command_for("e7-install", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "unittest", "discover", "-s",
             "tests", "-p", "test_e7_*.py", "-v"],
        )
        self.assertIn("e7-install", verify.STEPS["ci"])

    def test_client_compat_is_separate_explicit_opt_in_not_ci_or_local(self):
        self.assertEqual(verify.STEPS["compat"], ("e2-claude-compat",))
        for mode in ("ci", "local", "ai"):
            self.assertNotIn("e2-claude-compat", verify.STEPS[mode])
        with self.assertRaises(ValueError):
            verify.run("compat")
        with self.assertRaises(ValueError):
            verify.run("ci", compat_consent=True)
        self.assertEqual(verify.command_for("e2-claude-compat", python="python3"),
            ["python3", "-B", "-m", "ai_desktop.e2_client_compat", "self-test", "--allow-claude-compat"])
        self.assertGreater(verify.TIMEOUTS["e2-claude-compat"], 55 * 12)

    def test_client_compat_failure_logs_only_fixed_phase_metadata(self):
        for output, shown in (("COMPAT: REFUSED phase=reply", True),
                              ("COMPAT: REFUSED phase=SECRET", False),
                              ("PRIVATE NATIVE LOG", False)):
            messages = []
            with patch.object(verify.os, "geteuid", return_value=1000), \
                 patch.object(verify, "execute", return_value=(
                    {"step": "e2-claude-compat", "passed": False, "exit_code": 1, "seconds": 0.01}, output)):
                verify.run("compat", compat_consent=True, writer=messages.append)
            self.assertEqual(output in messages, shown)

    def test_compat_success_requires_full_pinned_failure_and_recovery_matrix(self):
        good = ("COMPAT: PASS version=2.1.312 isolated-loopback reply=PASS denial=PASS rate-limit=PASS"
                " unavailable=PASS disconnect=PASS timeout-stop=PASS recovery=5/5 real_auth=false upstream=false")
        for output, shown in ((good, True), (good.replace("2.1.312", "2.1.313"), False),
                              (good.replace("recovery=5/5", "recovery=4/5"), False),
                              (good.replace(" disconnect=PASS", ""), False),
                              (good + "\nPRIVATE LOG", False)):
            messages = []
            with patch.object(verify.os, "geteuid", return_value=1000), \
                    patch.object(verify, "execute", return_value=(
                        {"step": "e2-claude-compat", "passed": True, "exit_code": 0, "seconds": 0.01}, output)):
                verify.run("compat", compat_consent=True, writer=messages.append)
            self.assertEqual(output in messages, shown)

    def test_compat_new_failure_and_recovery_phases_preserve_only_finite_diagnostics(self):
        from ai_desktop.e2_compat_fixture import SCENARIOS
        for phase in SCENARIOS:
            for status in ("none", "other", "401", "429", "500", "502", "503", "504", "529"):
                output = ("COMPAT: REFUSED phase=" + phase + " reason=client-denial exit=1 requests=1 messages=1"
                          " input_messages=1 text_blocks=1 marker=1 exact=1 result_type=1 success_subtype=1"
                          " is_error=1 api_status=" + status)
                messages = []
                with patch.object(verify.os, "geteuid", return_value=1000), \
                        patch.object(verify, "execute", return_value=(
                            {"step": "e2-claude-compat", "passed": False, "exit_code": 1, "seconds": 0.01}, output)):
                    result = verify.run("compat", compat_consent=True, writer=messages.append)
                self.assertIn(output, messages)
                self.assertFalse(result[0]["passed"])

    def test_compat_failure_details_use_fixed_reason_vocabulary_and_no_raw_logs(self):
        from ai_desktop.e2_compat_fixture import REASONS
        self.assertEqual(verify.COMPAT_FAILURE_REASONS, REASONS)
        good = ("COMPAT: REFUSED phase=reply reason=request-content exit=1 requests=2 messages=0"
                " input_messages=1 text_blocks=2 marker=1 exact=0 result_type=0 success_subtype=0 is_error=0 api_status=none")
        for output, shown in ((good, True), (good.replace("request-content", "PRIVATE"), False),
                              (good + "\nPRIVATE STDERR", False),
                              (good.replace("api_status=none", "api_status=403"), False),
                              (good.replace("api_status=none", "api_status=PRIVATE"), False),
                              (good.replace("result_type=0", "result_type=True"), False),
                              (good.replace("request-content", "private-unlisted"), False)):
            messages = []
            with patch.object(verify.os, "geteuid", return_value=1000), \
                 patch.object(verify, "execute", return_value=(
                    {"step": "e2-claude-compat", "passed": False, "exit_code": 1, "seconds": 0.01}, output)):
                result = verify.run("compat", compat_consent=True, writer=messages.append)
            self.assertFalse(result[0]["passed"])
            self.assertEqual(output in messages, shown)

    def test_ai_is_opt_in(self):
        with self.assertRaises(ValueError):
            verify.run("ai", writer=lambda _: None)
        with self.assertRaises(ValueError):
            verify.run("other", writer=lambda _: None)

    def test_cli_is_allowlisted(self):
        self.assertIsNone(verify.command_for("syntax"))
        with self.assertRaises(ValueError):
            verify.command_for("shell")
        self.assertEqual(
            verify.command_for("controller-ask", python="/usr/bin/python3"),
            ["/usr/bin/python3", "-B", "-m", "ai_desktop", "controller", "ask"],
        )

    def test_successful_steps_report_metadata_only(self):
        successful = subprocess.CompletedProcess([], 0, "PRIVATE-DATA", "")
        fake = lambda *args, **kwargs: successful
        result, output = verify.execute("cli", runner=fake)
        self.assertTrue(result["passed"])
        self.assertNotIn("PRIVATE-DATA", repr(result))
        self.assertEqual(output, "PRIVATE-DATA")

    def test_failed_step_hides_output_in_local_mode(self):
        failed = subprocess.CompletedProcess([], 1, "SECRET-IN-STDOUT", "SECRET-IN-STDERR")
        messages = []
        with patch.object(verify.os, "geteuid", return_value=1000), patch.object(verify, "execute", return_value=(
            {"step": "unit", "passed": False, "exit_code": 1, "seconds": 0.01},
            failed.stdout + failed.stderr,
        )):
            checks = verify.run("local", fail_fast=True, writer=messages.append)
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0]["passed"])
        self.assertNotIn("SECRET", "\n".join(messages))

    def test_fail_fast_stops_after_first_failure(self):
        messages = []
        with patch.object(verify, "execute", return_value=(
            {"step": "syntax", "passed": False, "exit_code": 1, "seconds": 0},
            "SyntaxError",
        )) as execute:
            checks = verify.run("ci", fail_fast=True, writer=messages.append)
        self.assertEqual(len(checks), 1)
        execute.assert_called_once()

    def test_timeout_has_fixed_error_type_not_raw_output(self):
        with patch.object(verify, "command_for", return_value=["python", "script.py"]):
            result, output = verify.execute(
                "cli",
                runner=lambda *a, **kw: (_ for _ in ()).throw(
                    subprocess.TimeoutExpired("python", 5)
                ),
            )
        self.assertFalse(result["passed"])
        self.assertEqual(output, "TimeoutExpired")

    def test_syntax_check_parses_project(self):
        code, message = verify.syntax_check()
        self.assertEqual(code, 0, message)
        self.assertIn("Parsed", message)

    def test_main_default_is_ci_only(self):
        calls = []
        with patch.object(verify, "run", return_value=[
            {"step": step, "passed": True, "exit_code": 0, "seconds": 0}
            for step in verify.STEPS["ci"]
        ]) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(verify.main([]), 0)
        self.assertEqual(run.call_args.args[0], "ci")
        self.assertFalse(run.call_args.kwargs["ai_consent"])

    def test_main_json_contains_no_command_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "verification.json"
            with patch.object(verify, "run", return_value=[
                {"step": step, "passed": True, "exit_code": 0, "seconds": 0.01}
                for step in verify.STEPS["ci"]
            ]):
                self.assertEqual(verify.main(["--mode", "ci", "--json", str(result_path)]), 0)
            contents = result_path.read_text(encoding="utf-8")
            self.assertIn('"passed": true', contents)
            self.assertNotIn("stdout", contents)
            self.assertNotIn("stderr", contents)

    def test_result_file_is_private_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "private.json"
            with patch.object(verify, "run", return_value=[
                {"step": step, "passed": True, "exit_code": 0, "seconds": 0.01}
                for step in verify.STEPS["ci"]
            ]):
                self.assertEqual(verify.main(["--json", str(dest)]), 0)
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)

    def test_main_refuses_existing_regular_file_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "existing.json"
            dest.write_text("preserve important content", encoding="utf-8")
            with patch.object(verify, "run", return_value=[
                {"step": step, "passed": True, "exit_code": 0, "seconds": 0.01}
                for step in verify.STEPS["ci"]
            ]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(verify.main(["--json", str(dest)]), 2)
            self.assertEqual(dest.read_text(encoding="utf-8"), "preserve important content")

    def test_missing_parent_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "nested" / "not-present"
            dest = parent / "verify.json"
            with patch.object(verify, "run", return_value=[
                {"step": step, "passed": True, "exit_code": 0, "seconds": 0.01}
                for step in verify.STEPS["ci"]
            ]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(verify.main(["--json", str(dest)]), 2)
            self.assertFalse(parent.exists())

    def test_second_write_refused_and_first_result_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "result.json"
            with patch.object(verify, "run", return_value=[
                {"step": step, "passed": True, "exit_code": 0, "seconds": 0.01}
                for step in verify.STEPS["ci"]
            ]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(verify.main(["--json", str(dest)]), 0)
                    first = dest.read_bytes()
                    self.assertEqual(verify.main(["--json", str(dest)]), 2)
            self.assertEqual(dest.read_bytes(), first)

    def test_main_cannot_overwrite_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "file.json"
            target = Path(directory) / "target.json"
            target.write_text("important", encoding="utf-8")
            dest.symlink_to(target)
            with patch.object(verify, "run", return_value=[
                {"step": step, "passed": True, "exit_code": 0, "seconds": 0.01}
                for step in verify.STEPS["ci"]
            ]):
                self.assertEqual(verify.main(["--json", str(dest)]), 2)
            self.assertEqual(target.read_text(encoding="utf-8"), "important")


if __name__ == "__main__":
    unittest.main()

