import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import doctor


class DoctorTests(unittest.TestCase):
    def setUp(self):
        # This report must not depend on whether the package is installed on the machine running the tests.
        self.enterContext(patch.object(doctor, "INSTALLED", Path("/nonexistent/ai-desktop")))
        self.enterContext(patch.object(doctor.system_clients, "inspect", side_effect=lambda provider:
            SimpleNamespace(provider=provider, status="missing", detail="missing")))

    def test_an_installed_package_checks_only_its_own_client(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(doctor, "INSTALLED", Path(temp)), \
                patch.object(doctor.launcher, "claude_binary", return_value=Path("/usr/bin/claude")):
            self.assertFalse(doctor.inspect()["claude_binary_valid"])     # another client on PATH does not count
            (Path(temp) / "claude-code").write_bytes(b"x")
            self.assertTrue(doctor.inspect()["claude_binary_valid"])

    def test_system_client_discovery_never_satisfies_or_changes_bundled_runtime_requirements(self):
        for present in ((), ("claude",), ("codex",), ("claude", "codex")):
            with self.subTest(present=present), tempfile.TemporaryDirectory() as directory:
                skills, profile, _ = self.skill_tree(Path(directory))
                def result(provider):
                    return SimpleNamespace(provider=provider,
                        status="present" if provider in present else "missing",
                        detail="vendor-unverified" if provider in present else "missing",
                        path="/private-not-for-report/" + provider, identity="private-inode")
                with patch.object(doctor.system_clients, "inspect", side_effect=result) as inspect, \
                        patch.object(doctor.skills, "ROOT", skills), patch.object(doctor, "PROFILE", profile), \
                        patch.object(doctor, "INSTALLED", Path(directory) / "package"), \
                        patch.object(doctor.launcher, "claude_binary", side_effect=FileNotFoundError), \
                        patch.object(doctor.shutil, "which", return_value="/usr/bin/tool"), \
                        patch.object(doctor.sys, "platform", "linux"), \
                        patch.object(doctor.sys, "version_info", (3, 12)):
                    report = doctor.inspect()
                self.assertEqual([call.args for call in inspect.call_args_list], [("claude",), ("codex",)])
                self.assertEqual(report["runtime_client_source"], "bundled-package")
                self.assertEqual(report["bundled_clients_present"], {"claude": False, "codex": False})
                self.assertFalse(report["claude_binary_valid"])
                self.assertTrue(report["offline_prerequisites_present"])
                self.assertFalse(report["diagnose_prerequisites_present"])
                for provider, item in report["system_clients"].items():
                    self.assertEqual(item["metadata_trusted"], provider in present)
                    self.assertEqual(item["layout"], "native-elf" if provider in present else "unverified")
                    self.assertFalse(item["active"])
                    self.assertFalse(item["version_checked"])
                    self.assertFalse(item["protocol_checked"])
                self.assertNotIn("private-not-for-report", json.dumps(report))
                self.assertNotIn("private-inode", json.dumps(report))

    def test_each_bundled_client_is_reported_independently(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(doctor, "INSTALLED", Path(directory)):
            for present in ((), ("claude",), ("codex",), ("claude", "codex")):
                with self.subTest(present=present):
                    for provider, name in (("claude", "claude-code"), ("codex", "codex")):
                        path = Path(directory) / name
                        if provider in present:
                            path.write_bytes(b"synthetic presence only")
                        else:
                            path.unlink(missing_ok=True)
                    self.assertEqual(doctor.inspect()["bundled_clients_present"],
                                     {provider: provider in present for provider in ("claude", "codex")})

    def test_unsafe_unsupported_and_changing_clients_remain_inactive_without_false_trust(self):
        for status, detail, message in (("unsafe", "owner", "unsafe file metadata"),
                                       ("unsupported", "not-native-elf", "unsupported layout"),
                                       ("changed", "metadata-changed", "changed during inspection"),
                                       ("unavailable", "metadata-unavailable", "metadata unavailable")):
            with self.subTest(status=status), patch.object(doctor.system_clients, "inspect",
                    return_value=SimpleNamespace(status=status, detail=detail)):
                report = doctor.inspect()
            for item in report["system_clients"].values():
                self.assertEqual((item["status"], item["detail"]), (status, detail))
                self.assertFalse(item["metadata_trusted"])
                self.assertFalse(item["active"])
                self.assertEqual(doctor.system_client_summary(item), message + "; not active.")

    def test_system_discovery_does_not_start_clients_network_or_read_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skills, profile, _ = self.skill_tree(root)
            for name in ("auth.json", ".credentials.json"):
                (profile / name).write_text("SYNTHETIC-PRIVATE-CREDENTIAL")
            read_bytes = Path.read_bytes
            reads = []
            def only_skills(path):
                self.assertEqual(path.name, "SKILL.md")
                reads.append(path)
                return read_bytes(path)
            with patch.object(doctor.skills, "ROOT", skills), patch.object(doctor, "PROFILE", profile), \
                    patch.object(doctor.launcher, "claude_binary", side_effect=FileNotFoundError), \
                    patch.object(Path, "read_bytes", only_skills), \
                    patch("subprocess.run", side_effect=AssertionError("must not execute clients")) as run, \
                    patch("subprocess.Popen", side_effect=AssertionError("must not spawn clients")) as popen, \
                    patch("socket.socket", side_effect=AssertionError("must not connect")) as network:
                report = doctor.inspect()
            self.assertTrue(reads)
            self.assertNotIn("SYNTHETIC-PRIVATE-CREDENTIAL", json.dumps(report))
            run.assert_not_called()
            popen.assert_not_called()
            network.assert_not_called()

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
                     "claude_binary_valid": True, "isolated_account_metadata_valid": True,
                     "bundled_clients_present": {"claude": True, "codex": False},
                     "system_clients": {"claude": {"status": "missing"}, "codex": {"status": "present"}}}
        with patch.object(doctor, "inspect", return_value=mock_data) as inspect, \
                patch.object(doctor.os, "geteuid", return_value=1000), \
                patch("subprocess.run") as run, patch("subprocess.Popen") as popen, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(doctor.main([]), 0)
        inspect.assert_called_once_with()
        run.assert_not_called()
        popen.assert_not_called()
        text = out.getvalue()
        self.assertEqual(len(text.splitlines()), 8)
        self.assertIn("Offline requirements (metadata): met.", text)
        self.assertIn("Runtime source: bundled package clients (Claude found; Codex missing).", text)
        self.assertIn("Isolated account metadata: valid.", text)
        self.assertIn("System Claude: missing; not active.", text)
        self.assertIn("System Codex: trusted file metadata, native ELF; not active.", text)
        self.assertIn("vendor, version and protocol: not verified; runtime migration is pending.", text)
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
        self.assertEqual(len(text.splitlines()), 8)
        self.assertIn("Offline requirements (metadata): incomplete.", text)
        self.assertIn("Runtime source: bundled package clients (Claude missing; Codex missing).", text)
        self.assertIn("Isolated account metadata: unconfirmed.", text)
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
