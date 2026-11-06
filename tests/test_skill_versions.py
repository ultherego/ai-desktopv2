"""Version selection depends on real bounded broker outcomes, never task prose."""
import unittest

from ai_desktop import skill_versions, skills


def observation(*, argv=None, **changes):
    argv = ["pacman", "-Q", "pacman"] if argv is None else argv
    return {"op": "run", "argv": argv, "role": "read", "status": "ok", "exit": 0,
            "output": "pacman 7.0.0-3\n", "truncated": False, **changes}


class SkillVersionTests(unittest.TestCase):
    def test_completed_package_query_yields_upstream_version(self):
        for binary in ("pacman", "/usr/bin/pacman"):
            for package_version in ("7.0.0-3", "1:7.0.0-3.1"):
                result = observation(argv=[binary, "-Q", "pacman"], output="pacman " + package_version + "\n")
                self.assertEqual(skill_versions.trusted_version_observation(result["argv"], result),
                                 {"pacman": "7.0.0"})

    def test_git_revision_is_not_collapsed_into_release(self):
        result = observation(output="pacman 7.0.0.r6.gabcdef-1\n")
        versions = skill_versions.trusted_version_observation(result["argv"], result)
        self.assertEqual(versions, {"pacman": "7.0.0.r6.gabcdef"})
        self.assertEqual(skill_versions.reference_compatibility(
            {"version": "7.0.0", "tools": ["pacman"]}, versions), "mismatch")

    def test_denied_failed_truncated_and_change_commands_are_not_evidence(self):
        for change in ({"status": "failed"}, {"status": "denied"}, {"exit": 1}, {"exit": None},
                       {"exit": False}, {"truncated": True}, {"truncated": None}, {"role": "change"},
                       {"role": "test"}, {"op": "skill"}):
            with self.subTest(change=change):
                result = observation(**change)
                self.assertEqual(skill_versions.trusted_version_observation(result["argv"], result), {})

    def test_alternative_command_and_database_targets_are_not_evidence(self):
        variants = (["pacman", "-Qi", "pacman"], ["pacman", "-Si", "pacman"],
                    ["pacman", "-Q"], ["pacman", "-Q", "pacman", "systemd"],
                    ["pacman", "--root=/tmp/copy", "-Q", "pacman"],
                    ["/tmp/pacman", "-Q", "pacman"], ["sh", "-c", "pacman -Q pacman"],
                    ["pacman", "-Q", "--help"])
        for argv in variants:
            with self.subTest(argv=argv):
                result = observation(argv=argv)
                self.assertEqual(skill_versions.trusted_version_observation(argv, result), {})
        result = observation()
        self.assertEqual(skill_versions.trusted_version_observation(["pacman", "-Q", "systemd"], result), {})

    def test_extra_output_and_forged_version_text_are_not_evidence(self):
        for output in ("systemd 7.0.0-3\n", "pacman 7.0.0-3\nwarning", "pacman 7.0.0-3\n\n",
                       "pacman 7.0.0-3\nsystemd 258-1\n", "pacman 7.0.0-3\x1b[0m\n",
                       "I use pacman 7.0.0-3", "pacman 7.0.0\n", "x" * 513, "\ud800"):
            with self.subTest(output=repr(output)):
                result = observation(output=output)
                self.assertEqual(skill_versions.trusted_version_observation(result["argv"], result), {})

    def test_compatibility_states_require_the_named_package(self):
        metadata = {"version": "7.0.0", "tools": ["pacman"]}
        cases = (({}, "unknown"), ({"systemd": "7.0.0"}, "unknown"),
                 ({"pacman": "7.0.0"}, "matched"), ({"pacman": "7.1.0"}, "mismatch"),
                 ({"pacman": None}, "unknown"))
        for versions, expected in cases:
            self.assertEqual(skill_versions.reference_compatibility(metadata, versions), expected)
        self.assertEqual(skill_versions.reference_compatibility({"version": "rolling", "tools": ["pacman"]},
                                                                {"pacman": "7.0.0"}), "rolling")

    def test_actual_packaged_release_reference_is_withheld_until_matching_observation(self):
        snapshot = skills.bundle("arch-pacman", root=skills.ROOT)
        identifier = "arch-pacman/pacman-700-local-repositories"
        body = snapshot["references"]["pacman-700-local-repositories"]["content"]
        unknown = skills.resource_text(snapshot, identifier)
        self.assertIn("Compatibility: unknown", unknown)
        self.assertNotIn(body, unknown)
        self.assertIn("arch-pacman/package-queries", unknown)
        result = observation()
        versions = skill_versions.trusted_version_observation(result["argv"], result)
        matched = skills.resource_text(snapshot, identifier, versions=versions)
        self.assertIn("Compatibility: matched", matched)
        self.assertIn(body, matched)
        mismatch = skills.resource_text(snapshot, identifier, versions={"pacman": "7.1.0"})
        self.assertIn("Compatibility: mismatch", mismatch)
        self.assertNotIn(body, mismatch)
        rolling = skills.resource_text(snapshot, "arch-pacman/package-queries", versions=versions)
        self.assertIn("not an exact version match", rolling)
        self.assertIn("pacman 7.0.0", rolling)


if __name__ == "__main__":
    unittest.main()
