"""E7 release gates on synthetic data: client/worker version skew is refused, an interrupted upgrade is recovered by
reinstalling the same artifact, the hook never mutates data on upgrade/removal, and no official client binary is tracked."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

from ai_desktop import analysis_contract as ac
from ai_desktop import material_whole as whole
from ai_desktop import materials

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "packaging/arch/ai-desktop.install"
PKGBUILD = ROOT / "packaging/arch/PKGBUILD"


def material_request():
    plan = whole.WholePlan(materials.import_bytes(b"alpha\nbeta\n" * 40, "file"))
    return ac.application_input(None, "Pytanie?", material=plan.map_input(0, 1, 4096))


class VersionSkewTests(unittest.TestCase):
    """The worker re-derives the expected request from ITS OWN code, so a client of another version is refused (fail closed)."""

    def test_every_binding_field_of_a_material_request_must_match_the_workers_own_code(self):
        raw = material_request()
        ac.parse_request(raw.encode())                                            # same version on both sides: accepted
        for key in ("client_context", "system_prompt", "client_version", "policy", "schema"):
            changed = json.loads(raw)
            changed[key] = "other" if key != "schema" else 99
            with self.subTest(key=key), self.assertRaises(ac.AnalysisDenied):
                ac.parse_request(ac.canonical(changed).encode())
        extra = json.loads(raw)
        extra["new_field_from_a_newer_client"] = 1
        with self.assertRaises(ac.AnalysisDenied):
            ac.parse_request(ac.canonical(extra).encode())

    def test_a_client_and_a_worker_built_from_different_contracts_refuse_each_other_both_ways(self):
        for name in ("SYSTEM_PROMPT", "POLICY", "CLIENT_VERSION"):
            with self.subTest(name=name):
                with patch.object(ac, name, getattr(ac, name) + " v-next"):        # an upgraded client talking to an old worker
                    newer = material_request()
                with self.assertRaises(ac.AnalysisDenied):
                    ac.parse_request(newer.encode())
                old = material_request()
                with patch.object(ac, name, getattr(ac, name) + " v-next"):        # an old client talking to an upgraded worker
                    with self.assertRaises(ac.AnalysisDenied):
                        ac.parse_request(old.encode())

    def test_a_half_updated_pair_is_refused_until_both_sides_are_the_same_version_again(self):
        """Protocol property only. Recovery of a real half-updated installation is exercised in test_e7_artifact_and_torn_update."""
        with patch.object(ac, "SYSTEM_PROMPT", ac.SYSTEM_PROMPT + " half-written"):
            torn = material_request()                                              # the client is already the new version ...
        with self.assertRaises(ac.AnalysisDenied):                                 # ... the worker is still the old one
            ac.parse_request(torn.encode())
        ac.parse_request(material_request().encode())                              # the same package everywhere again: works

    def test_a_response_from_a_newer_worker_with_an_unknown_field_is_refused_by_an_older_client(self):
        sha = "a" * 64
        value = json.loads(ac.result_packet(sha, text="Odpowiedź.", mode="material"))
        value["future_field"] = 1
        with self.assertRaises(ac.AnalysisDenied):
            ac.read_result(ac.canonical(value).encode(), sha, mode="material")


class HookRetentionTests(unittest.TestCase):
    def body(self, name):
        text = HOOK.read_text()
        match = re.search(r"^%s\(\) \{\n(.*?)^\}" % name, text, re.S | re.M)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def test_upgrade_and_removal_validate_or_do_nothing_and_never_mutate_account_profile_config_or_logs(self):
        for name in ("post_upgrade", "post_remove"):
            body = re.sub(r"#.*", "", self.body(name))
            for command in ("chown", "chmod", "rm ", "rmdir", "mv ", "cp ", "userdel", "usermod", "groupdel", "systemd-sysusers",
                            "systemd-tmpfiles", "systemctl", "sudo", "tee ", ">", "sed -i", "truncate", "pacman"):
                with self.subTest(hook=name, command=command):
                    self.assertNotIn(command, body)

    def test_removal_is_a_noop_that_documents_the_retention(self):
        self.assertEqual(re.sub(r"#.*", "", self.body("post_remove")).strip(), ":")
        self.assertIn("preserve the UID and profile", HOOK.read_text())

    def test_the_package_installs_nothing_under_var_lib_or_a_home_directory(self):
        package = PKGBUILD.read_text()
        for forbidden in ("${pkgdir}/var", "${pkgdir}/home", "${pkgdir}/root", "${pkgdir}/etc/ai-desktop", "/.config"):
            self.assertNotIn(forbidden, package)


class NoBundledClientTests(unittest.TestCase):
    def test_the_package_has_no_remote_sources_and_legacy_staging_stays_untracked(self):
        package = PKGBUILD.read_text()
        self.assertIn("source=()", package)
        self.assertIn("sha256sums=()", package)
        ignored = subprocess.run(["git", "check-ignore", "packaging/arch/claude-code"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(ignored.returncode, 0)                                   # ignored, so it cannot be committed by accident
        self.assertNotIn("_claude_code_sha256", package)
        self.assertNotIn("AI_DESKTOP_CHECK_CLIENTS", package)
        for artifact in ("packaging/arch/*.pkg.tar.*",):
            self.assertIn(artifact, (ROOT / ".gitignore").read_text())

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_no_tracked_file_is_an_executable_binary(self):
        listing = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.split(b"\0")
        elf = [name.decode() for name in listing if name and (ROOT / name.decode()).is_file()
               and (ROOT / name.decode()).read_bytes()[:4] == b"\x7fELF"]
        self.assertEqual(elf, [])                                                 # no official client (or any binary) in the repo


if __name__ == "__main__":
    unittest.main()
