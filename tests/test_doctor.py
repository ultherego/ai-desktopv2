import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import doctor


class DoctorTests(unittest.TestCase):
    def setUp(self):
        # This report must not depend on whether the package is installed on the machine running the tests.
        self.enterContext(patch.object(doctor, "INSTALLED", Path("/nonexistent/ai-desktop")))

    def test_an_installed_package_checks_only_its_own_client(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(doctor, "INSTALLED", Path(temp)), \
                patch.object(doctor.launcher, "claude_binary", return_value=Path("/usr/bin/claude")):
            self.assertFalse(doctor.inspect()["claude_binary_valid"])     # another client on PATH does not count
            (Path(temp) / "claude-code").write_bytes(b"x")
            self.assertTrue(doctor.inspect()["claude_binary_valid"])

    def skill_tree(self, root):
        skills = root / "skills"
        source = skills / doctor.SKILL / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_text(
            "---\nname: cachyos-diagnostics\ndescription: Local system checks\n---\n"
            "Follow read-only checks.\n",
            encoding="utf-8",
        )
        profile = root / "profile"
        destination = profile / "skills" / doctor.SKILL / "SKILL.md"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(source.read_bytes())
        return skills, profile, destination

    def test_fully_present_prerequisites(self):
        with tempfile.TemporaryDirectory() as directory:
            skills, profile, _ = self.skill_tree(Path(directory))
            with (
                patch.object(doctor.skills, "ROOT", skills),
                patch.object(doctor, "PROFILE", profile),
                patch.object(doctor.launcher, "claude_binary", return_value=Path("/opt/claude")),
                patch.object(doctor.shutil, "which", return_value="/usr/bin/tool"),
                patch.object(doctor.sys, "platform", "linux"),
            ):
                report = doctor.inspect()
        self.assertTrue(report["offline_prerequisites_present"])
        self.assertFalse(report["diagnose_prerequisites_present"])
        self.assertTrue(report["skill_profile_matches"])
        self.assertEqual(set(report["tools_available"]), set(doctor.TOOLS))
        self.assertNotIn(str(profile), json.dumps(report))
        self.assertNotIn("/opt/claude", json.dumps(report))

    def test_separate_account_metadata_does_not_unlock_claude(self):
        with tempfile.TemporaryDirectory() as directory:
            skills, profile, _ = self.skill_tree(Path(directory))
            with (
                patch.object(doctor.skills, "ROOT", skills),
                patch.object(doctor, "PROFILE", profile),
                patch.object(doctor.launcher, "claude_binary", return_value=Path("/opt/claude")),
                patch.object(doctor.shutil, "which", return_value="/usr/bin/tool"),
                patch.object(doctor.sys, "platform", "linux"),
                patch.object(doctor.e2_account, "account_ready", return_value=True) as checked,
                patch("subprocess.run") as run,
            ):
                report = doctor.inspect()
        checked.assert_called_once_with()
        self.assertTrue(report["isolated_account_metadata_valid"])
        self.assertFalse(report["diagnose_prerequisites_present"])
        self.assertNotIn("oauth", json.dumps(report).lower())
        run.assert_not_called()

    def test_complete_legacy_profile_never_unblocks_unapproved_claude(self):
        with tempfile.TemporaryDirectory() as directory:
            skills, profile, _ = self.skill_tree(Path(directory))
            with (
                patch.object(doctor.skills, "ROOT", skills),
                patch.object(doctor, "PROFILE", profile),
                patch.object(doctor.launcher, "claude_binary", return_value=Path("/opt/claude")),
                patch.object(doctor.shutil, "which", return_value="/usr/bin/tool"),
                patch.object(doctor.sys, "platform", "linux"),
                patch("subprocess.run") as run,
            ):
                report = doctor.inspect()
        self.assertTrue(report["skill_profile_matches"])
        self.assertTrue(report["claude_binary_valid"])
        self.assertFalse(report["diagnose_prerequisites_present"])
        run.assert_not_called()

    def test_missing_claude_is_not_fatal_or_reported_as_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            skills, profile, _ = self.skill_tree(Path(directory))
            with (
                patch.object(doctor.skills, "ROOT", skills),
                patch.object(doctor, "PROFILE", profile),
                patch.object(doctor.launcher, "claude_binary", side_effect=FileNotFoundError),
                patch.object(doctor.shutil, "which", return_value="/usr/bin/tool"),
                patch.object(doctor.sys, "platform", "linux"),
            ):
                report = doctor.inspect()
        self.assertTrue(report["offline_prerequisites_present"])
        self.assertFalse(report["diagnose_prerequisites_present"])
        self.assertFalse(report["claude_binary_valid"])

    def test_modified_installed_skill_is_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            skills, profile, destination = self.skill_tree(Path(directory))
            destination.write_text("bad", encoding="utf-8")
            with (
                patch.object(doctor.skills, "ROOT", skills),
                patch.object(doctor, "PROFILE", profile),
                patch.object(doctor.launcher, "claude_binary", return_value=Path("/opt/claude")),
                patch.object(doctor.shutil, "which", return_value="/usr/bin/tool"),
                patch.object(doctor.sys, "platform", "linux"),
            ):
                report = doctor.inspect()
        self.assertFalse(report["skill_profile_matches"])
        self.assertFalse(report["diagnose_prerequisites_present"])

    def test_profile_symlink_disallowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skills, profile, _ = self.skill_tree(root)
            linked = root / "linked-profile"
            profile.rename(linked)
            profile.symlink_to(linked, target_is_directory=True)
            with (
                patch.object(doctor.skills, "ROOT", skills),
                patch.object(doctor, "PROFILE", profile),
                patch.object(doctor.launcher, "claude_binary", return_value=Path("/opt/claude")),
                patch.object(doctor.shutil, "which", return_value="/usr/bin/tool"),
                patch.object(doctor.sys, "platform", "linux"),
            ):
                report = doctor.inspect()
        self.assertFalse(report["claude_profile_directory"])
        self.assertFalse(report["diagnose_prerequisites_present"])

    def test_no_bwrap_does_not_claim_offline_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            skills, profile, _ = self.skill_tree(Path(directory))
            with (
                patch.object(doctor.skills, "ROOT", skills),
                patch.object(doctor, "PROFILE", profile),
                patch.object(doctor.launcher, "claude_binary", return_value=Path("/opt/claude")),
                patch.object(doctor.shutil, "which",
                             side_effect=lambda tool: None if tool == "bwrap" else "/usr/bin/tool"),
                patch.object(doctor.sys, "platform", "linux"),
            ):
                report = doctor.inspect()
        self.assertFalse(report["offline_prerequisites_present"])

    def test_doctor_does_not_spawn_claude_or_shell(self):
        mock_data = {"offline_prerequisites_present": False,
                     "diagnose_prerequisites_present": False}
        with (
            patch.object(doctor, "inspect", return_value=mock_data) as inspect,
            patch.object(doctor.os, "geteuid", return_value=1000),
            patch("subprocess.run") as run,
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(doctor.main(["--v"]), 0)
        inspect.assert_called_once_with()
        run.assert_not_called()
        self.assertIn('"offline_prerequisites_present": false', out.getvalue())

    def test_extra_arguments_disallowed(self):
        with (
            patch.object(doctor, "inspect") as inspect,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(doctor.main(["--execute"]), 2)
        inspect.assert_not_called()

    def test_default_is_short_and_does_not_turn_metadata_into_a_live_ai_claim(self):
        mock_data = {"offline_prerequisites_present": True, "diagnose_prerequisites_present": False,
                     "claude_binary_valid": True, "isolated_account_metadata_valid": True}
        with patch.object(doctor, "inspect", return_value=mock_data) as inspect, \
                patch.object(doctor.os, "geteuid", return_value=1000), \
                patch("subprocess.run") as run, patch("subprocess.Popen") as popen, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(doctor.main([]), 0)
        inspect.assert_called_once_with()
        run.assert_not_called()
        popen.assert_not_called()
        text = out.getvalue()
        self.assertEqual(len(text.splitlines()), 4)
        self.assertIn("Offline requirements (metadata): met.", text)
        self.assertIn("Claude client: found; isolated account metadata: valid.", text)
        self.assertIn("Login and AI operation: not checked.", text)
        self.assertIn("ai-desktop doctor --v", text)
        for technical in ("DOCTOR: PASS", "diagnose_prerequisites_present", "{", "READINESS"):
            self.assertNotIn(technical, text)

    def test_missing_metadata_is_reported_honestly_and_completed_check_still_returns_zero(self):
        mock_data = {"offline_prerequisites_present": False, "diagnose_prerequisites_present": False,
                     "claude_binary_valid": False, "isolated_account_metadata_valid": False}
        with patch.object(doctor, "inspect", return_value=mock_data), \
                patch.object(doctor.os, "geteuid", return_value=1000), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(doctor.main([]), 0)
        text = out.getvalue()
        self.assertEqual(len(text.splitlines()), 4)
        self.assertIn("Offline requirements (metadata): incomplete.", text)
        self.assertIn("Claude client: missing; isolated account metadata: unconfirmed.", text)
        self.assertIn("Login and AI operation: not checked.", text)
        self.assertNotIn("PASS", text)

    def test_verbose_aliases_preserve_complete_metadata_json_and_legacy_result(self):
        mock_data = {"offline_prerequisites_present": False, "diagnose_prerequisites_present": False,
                     "claude_binary_valid": True, "isolated_account_metadata_valid": False,
                     "tools_available": {"bwrap": False}}
        for flag in ("--v", "--verbose", "-v", "--details"):
            with self.subTest(flag=flag), patch.object(doctor, "inspect", return_value=mock_data) as inspect, \
                    patch.object(doctor.os, "geteuid", return_value=1000), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(doctor.main([flag]), 0)
            inspect.assert_called_once_with()
            text = out.getvalue()
            self.assertTrue(text.startswith("=== AI DESKTOP READINESS (READ-ONLY) ===\n"))
            result = "DOCTOR: PASS (requirements checked; AI not started)\n"
            self.assertTrue(text.endswith(result))
            self.assertEqual(json.loads(text.split("\n", 1)[1][:-len(result)]), mock_data)

    def test_help_and_invalid_arguments_do_not_even_inspect_metadata(self):
        for arguments, expected in ((["--help"], 0), (["-h"], 0), (["--execute"], 2),
                                    (["--v", "extra"], 2), (["extra"], 2)):
            with self.subTest(arguments=arguments), patch.object(doctor, "inspect") as inspect, \
                    patch.object(doctor.os, "geteuid", side_effect=AssertionError("identity must not be probed")), \
                    patch("subprocess.run") as run, patch("subprocess.Popen") as popen, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(doctor.main(arguments), expected)
            inspect.assert_not_called()
            run.assert_not_called()
            popen.assert_not_called()
            self.assertIn("Usage: ai-desktop doctor", out.getvalue())

    def test_root_disallowed(self):
        with (
            patch.object(doctor.os, "geteuid", return_value=0),
            patch.object(doctor, "inspect") as inspect,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(doctor.main([]), 2)
            for flag in ("--v", "--verbose", "-v", "--details"):
                self.assertEqual(doctor.main([flag]), 2)
        inspect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
