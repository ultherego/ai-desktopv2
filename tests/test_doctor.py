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
        self.config = SimpleNamespace(selected_provider="claude")
        self.enterContext(patch("ai_desktop.agent_profiles.Store", return_value=SimpleNamespace(
            load=lambda: SimpleNamespace(config=self.config))))
        self.enterContext(patch.object(doctor.system_clients, "inspect", side_effect=lambda provider:
            SimpleNamespace(source="system", provider=provider, status="missing", detail="missing")))

    def test_only_shared_system_discovery_counts_for_each_provider(self):
        for present in ((), ("claude",), ("codex",), ("claude", "codex")):
            with self.subTest(present=present), tempfile.TemporaryDirectory() as directory:
                skills, profile, _ = self.skill_tree(Path(directory))
                # Leftover package-private vendor files must never become a fallback.
                (Path(directory) / "claude-code").write_bytes(b"synthetic old client")
                (Path(directory) / "codex").write_bytes(b"synthetic old client")
                def result(provider):
                    return SimpleNamespace(source="system", provider=provider,
                        status="present" if provider in present else "missing",
                        detail="vendor-unverified" if provider in present else "missing",
                        path="/private-not-for-report/" + provider, identity="private-inode")
                with patch.object(doctor.system_clients, "inspect", side_effect=result) as inspect, \
                        patch.object(doctor.skills, "ROOT", skills), patch.object(doctor, "PROFILE", profile), \
                        patch.object(doctor.shutil, "which", return_value="/usr/bin/tool"), \
                        patch.object(doctor.sys, "platform", "linux"), \
                        patch.object(doctor.sys, "version_info", (3, 12)), \
                        patch("ai_desktop.launcher.claude_binary", side_effect=AssertionError("no legacy fallback")):
                    report = doctor.inspect()
                self.assertEqual([call.args for call in inspect.call_args_list], [("claude",), ("codex",)])
                self.assertEqual(report["runtime_client_source"], "independent")
                self.assertNotIn("bundled_clients_present", report)
                self.assertTrue(report["offline_prerequisites_present"])
                self.assertFalse(report["diagnose_prerequisites_present"])
                for provider, item in report["system_clients"].items():
                    self.assertEqual(item["metadata_trusted"], provider in present)
                    self.assertEqual(item["runtime_candidate"], provider in present)
                    self.assertEqual(item["layout"], "native-elf" if provider in present else "unverified")
                    self.assertFalse(item["version_checked"])
                    self.assertFalse(item["protocol_checked"])
                self.assertNotIn("private-not-for-report", json.dumps(report))
                self.assertNotIn("private-inode", json.dumps(report))

    def test_unsafe_unsupported_and_changing_clients_remain_inactive_without_false_trust(self):
        for status, detail, message in (("unsafe", "owner", "unsafe file metadata"),
                                       ("unsupported", "not-native-elf", "unsupported layout"),
                                       ("changed", "metadata-changed", "changed during inspection"),
                                       ("unavailable", "metadata-unavailable", "metadata unavailable")):
            with self.subTest(status=status), patch.object(doctor.system_clients, "inspect",
                    return_value=SimpleNamespace(source="system", status=status, detail=detail)):
                report = doctor.inspect()
            for item in report["system_clients"].values():
                self.assertEqual((item["status"], item["detail"]), (status, detail))
                self.assertFalse(item["metadata_trusted"])
                self.assertFalse(item["runtime_candidate"])
                self.assertEqual(doctor.system_client_summary(item), message + "; unavailable for execution.")

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
                    patch.object(doctor.system_clients, "inspect", return_value=SimpleNamespace(source="system", status="missing", detail="missing")), \
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

    def test_registered_runtime_metadata_is_neutral_and_does_not_inspect_its_source(self):
        for state in ("present", "unsafe", "unsupported", "changed", "unavailable"):
            with self.subTest(state=state), patch.object(doctor.system_clients, "inspect",
                    return_value=SimpleNamespace(source="registered", status=state,
                                                 detail="synthetic-metadata")), \
                    patch("subprocess.Popen", side_effect=AssertionError("metadata only")), \
                    patch("subprocess.run", side_effect=AssertionError("metadata only")):
                report = doctor.inspect()
            for item in report["system_clients"].values():
                self.assertEqual(item["source"], "registered")
                self.assertEqual(item["runtime_candidate"], state == "present")
                self.assertFalse(item["version_checked"])
                self.assertFalse(item["protocol_checked"])
            self.assertNotIn("compatible", json.dumps(report))

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
                patch.object(doctor.system_clients, "inspect", return_value=SimpleNamespace(source="system", status="present", detail="vendor-unverified")),
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
                patch.object(doctor.system_clients, "inspect", return_value=SimpleNamespace(source="system", status="present", detail="vendor-unverified")),
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
                patch.object(doctor.system_clients, "inspect", return_value=SimpleNamespace(source="system", status="present", detail="vendor-unverified")),
                patch.object(doctor.shutil, "which", return_value="/usr/bin/tool"),
                patch.object(doctor.sys, "platform", "linux"),
                patch("subprocess.run") as run,
            ):
                report = doctor.inspect()
        self.assertTrue(report["skill_profile_matches"])
        self.assertTrue(report["system_clients"]["claude"]["runtime_candidate"])
        self.assertFalse(report["diagnose_prerequisites_present"])
        run.assert_not_called()

    def test_missing_claude_is_not_fatal_or_reported_as_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            skills, profile, _ = self.skill_tree(Path(directory))
            with (
                patch.object(doctor.skills, "ROOT", skills),
                patch.object(doctor, "PROFILE", profile),
                patch.object(doctor.system_clients, "inspect", return_value=SimpleNamespace(source="system", status="missing", detail="missing")),
                patch.object(doctor.shutil, "which", return_value="/usr/bin/tool"),
                patch.object(doctor.sys, "platform", "linux"),
            ):
                report = doctor.inspect()
        self.assertTrue(report["offline_prerequisites_present"])
        self.assertFalse(report["diagnose_prerequisites_present"])
        self.assertFalse(report["system_clients"]["claude"]["runtime_candidate"])

    def test_modified_installed_skill_is_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            skills, profile, destination = self.skill_tree(Path(directory))
            destination.write_text("bad", encoding="utf-8")
            with (
                patch.object(doctor.skills, "ROOT", skills),
                patch.object(doctor, "PROFILE", profile),
                patch.object(doctor.system_clients, "inspect", return_value=SimpleNamespace(source="system", status="present", detail="vendor-unverified")),
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
                patch.object(doctor.system_clients, "inspect", return_value=SimpleNamespace(source="system", status="present", detail="vendor-unverified")),
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
                patch.object(doctor.system_clients, "inspect", return_value=SimpleNamespace(source="system", status="present", detail="vendor-unverified")),
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
                     "isolated_account_metadata_valid": True,
                     "system_clients": {"claude": {"status": "missing", "source": "system"}, "codex": {"status": "present", "source": "registered"}}}
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
        self.assertIn("Runtime sources: system packages or registered copies (ai-desktop clients list).", text)
        self.assertIn("Isolated account metadata: valid.", text)
        self.assertIn("Claude (system): missing; unavailable for execution.", text)
        self.assertIn("Codex (registered): trusted file metadata, native ELF; compatibility unchecked.", text)
        self.assertIn("Compatibility: ai-desktop doctor --clients (selected service, offline).", text)
        self.assertIn("Login and AI operation: not checked.", text)
        self.assertIn("ai-desktop doctor --v", text)
        for technical in ("DOCTOR: PASS", "diagnose_prerequisites_present", "{", "READINESS"):
            self.assertNotIn(technical, text)

    def test_missing_metadata_is_reported_honestly_and_completed_check_still_returns_zero(self):
        mock_data = {"offline_prerequisites_present": False, "diagnose_prerequisites_present": False,
                     "isolated_account_metadata_valid": False}
        with patch.object(doctor, "inspect", return_value=mock_data), \
                patch.object(doctor.os, "geteuid", return_value=1000), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(doctor.main([]), 0)
        text = out.getvalue()
        self.assertEqual(len(text.splitlines()), 8)
        self.assertIn("Offline requirements (metadata): incomplete.", text)
        self.assertIn("Runtime sources: system packages or registered copies (ai-desktop clients list).", text)
        self.assertIn("Isolated account metadata: unconfirmed.", text)
        self.assertIn("Login and AI operation: not checked.", text)
        self.assertNotIn("PASS", text)

    def test_verbose_aliases_preserve_complete_metadata_json(self):
        mock_data = {"offline_prerequisites_present": False, "diagnose_prerequisites_present": False,
                     "isolated_account_metadata_valid": False,
                     "tools_available": {"bwrap": False}}
        for flag in ("--v", "--verbose", "-v", "--details"):
            with self.subTest(flag=flag), patch.object(doctor, "inspect", return_value=mock_data) as inspect, \
                    patch.object(doctor.os, "geteuid", return_value=1000), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(doctor.main([flag]), 0)
            inspect.assert_called_once_with()
            text = out.getvalue()
            self.assertTrue(text.startswith("=== AI DESKTOP READINESS (READ-ONLY) ===\n"))
            result = "DOCTOR: PASS (checks completed; login and AI not checked)\n"
            self.assertTrue(text.endswith(result))
            self.assertEqual(json.loads(text.split("\n", 1)[1][:-len(result)]), mock_data)

    def test_help_and_invalid_arguments_do_not_even_inspect_metadata(self):
        for arguments, expected in ((["--help"], 0), (["-h"], 0), (["--execute"], 2),
                                    (["--v", "extra"], 2), (["extra"], 2), (["--clients", "--clients"], 2), (["--v", "-v"], 2)):
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
            for flag in ("--v", "--verbose", "-v", "--details", "--clients"):
                self.assertEqual(doctor.main([flag]), 2)
        inspect.assert_not_called()

    def test_explicit_probe_checks_only_the_selected_provider_and_reports_test_coverage(self):
        for provider, tested, version in (("claude", True, "2.1.312"), ("claude", False, "2.1.318"),
                                         ("codex", True, "0.166.0"), ("codex", False, "0.167.0")):
            self.config.selected_provider = provider
            outcome = {"provider": provider, "status": "compatible-tested" if tested else "compatible-untested",
                       "version": version, "version_checked": True, "protocol_checked": True, "tested": tested}
            with self.subTest(provider=provider, tested=tested), \
                    patch.object(doctor.os, "geteuid", return_value=1000), \
                    patch.object(doctor, "inspect", return_value={"system_clients": {}}), \
                    patch("ai_desktop.client_compat.inspect", return_value=outcome) as probe, \
                    patch("subprocess.Popen", side_effect=AssertionError("only explicit probe owns execution")), \
                    patch("socket.socket", side_effect=AssertionError("doctor must not connect")), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(doctor.main(["--clients"]), 0)
            probe.assert_called_once_with(provider)
            self.assertIn("Selected " + provider.capitalize() + " " + version + ": " + outcome["status"], output.getvalue())
            self.assertIn("Login and AI operation: not checked.", output.getvalue())

    def test_probe_refusals_return_nonzero_without_becoming_live_readiness(self):
        for reason in ("client-missing", "client-unsafe", "client-layout", "client-changed",
                       "client-unavailable", "client-version", "client-protocol", "timeout", "sandbox"):
            outcome = {"provider": "claude", "status": reason, "version": None,
                       "version_checked": False, "protocol_checked": False, "tested": False}
            with self.subTest(reason=reason), patch.object(doctor.os, "geteuid", return_value=1000), \
                    patch.object(doctor, "inspect", return_value={"diagnose_prerequisites_present": False}), \
                    patch("ai_desktop.client_compat.inspect", return_value=outcome), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(doctor.main(["--clients", "--v"]), 2)
            text = output.getvalue()
            self.assertIn('"status": "' + reason + '"', text)
            self.assertIn('"diagnose_prerequisites_present": false', text)
            self.assertIn("DOCTOR: REFUSED", text)
            self.assertNotIn("DOCTOR: PASS", text)

    def test_verbose_probe_preserves_the_shared_result_without_inventing_installed_version(self):
        outcome = {"provider": "claude", "status": "compatible-untested", "version": "2.1.318",
                   "version_checked": True, "protocol_checked": True, "tested": False}
        with patch.object(doctor.os, "geteuid", return_value=1000), \
                patch.object(doctor, "inspect", return_value={"runtime_client_source": "independent"}), \
                patch("ai_desktop.client_compat.inspect", return_value=outcome), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(doctor.main(["--v", "--clients"]), 0)
        lines = output.getvalue().splitlines()
        report = json.loads("\n".join(lines[1:-1]))
        self.assertEqual(report["client_compatibility"], outcome)
        self.assertNotIn("2.1.312", output.getvalue())

    def test_metadata_default_never_calls_the_explicit_probe(self):
        with patch.object(doctor.os, "geteuid", return_value=1000), \
                patch.object(doctor, "inspect", return_value={}), \
                patch("ai_desktop.client_compat.inspect", side_effect=AssertionError("explicit flag required")) as probe, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(doctor.main([]), 0)
            self.assertEqual(doctor.main(["--v"]), 0)
        probe.assert_not_called()

    def test_local_selection_does_not_require_or_probe_any_cloud_client(self):
        self.config.selected_provider = "local"
        with patch.object(doctor.os, "geteuid", return_value=1000), \
                patch.object(doctor, "inspect", side_effect=AssertionError("cloud metadata is irrelevant")), \
                patch("ai_desktop.permissions.load_policy", return_value=None), \
                patch("ai_desktop.client_compat.inspect", side_effect=AssertionError("no cloud probe")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(doctor.main(["--clients", "--v"]), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["provider"], "local")
        self.assertFalse(data["server_checked"])

    def test_missing_selection_does_not_choose_a_cloud_provider_for_the_user(self):
        self.config.selected_provider = None
        with patch.object(doctor.os, "geteuid", return_value=1000), \
                patch.object(doctor, "inspect", return_value={}), \
                patch("ai_desktop.client_compat.inspect", side_effect=AssertionError("choose provider first")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(doctor.main(["--clients"]), 2)
        self.assertIn("choose a service", output.getvalue())


if __name__ == "__main__":
    unittest.main()
