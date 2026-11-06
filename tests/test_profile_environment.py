"""The ambient-profile runner preserves real resolution, guards and results."""
from contextlib import redirect_stdout
from dataclasses import replace
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import agent_profiles as profiles, execution, run_controls
from ai_desktop.analysis_contract import AnalysisDenied
import profile_environment_check as check


class ProfileEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aid-profile-env-test-", dir=Path.home())
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.store = profiles.Store(self.home)
        self.store.mutate(lambda _: check.ambient_config(), expected=self.store.load())

    def test_default_store_uses_private_fixture_but_explicit_home_is_unchanged(self):
        explicit = self.home / "explicit"
        explicit.mkdir(mode=0o700)
        with check.ambient_store(self.home):
            self.assertEqual(profiles.Store().home, self.home)
            self.assertEqual(profiles.Store(home=None).home, self.home)
            self.assertEqual(profiles.Store(explicit).home, explicit)
            self.assertEqual(profiles.Store(home=explicit).home, explicit)
            self.assertEqual(profiles.resolve()["profile"], "ambient-reviewer")
            self.assertEqual(profiles.resolve(store=profiles.Store(explicit))["provider"], "claude")

    def test_canary_keeps_real_readonly_and_identity_guards(self):
        expected = profiles.resolve(store=self.store)
        before = check.snapshot(self.home)
        with check.ambient_store(self.home):
            check.check_canary(expected)
            with execution.using(profiles.resolve()), self.assertRaises(AnalysisDenied):
                run_controls.require_changes()
            actual_uid = os.getuid()
            with patch.object(profiles.os, "geteuid", return_value=actual_uid + 1), \
                    self.assertRaises(profiles.ProfileError):
                profiles.Store()
        self.assertEqual(check.snapshot(self.home), before)

    def run_small_suite(self, action):
        case = unittest.FunctionTestCase(action)
        calls = []
        class Loader:
            def discover(self, *args, **kwargs):
                calls.append((args, kwargs))
                return unittest.TestSuite([case])
        with redirect_stdout(io.StringIO()) as output:
            result = check.run(loader=Loader(), runner=unittest.TextTestRunner(stream=io.StringIO()),
                               parent=self.home)
        self.assertEqual(calls, [((str(check.ROOT / "tests"),), {"pattern": "test_*.py"})])
        return result, output.getvalue()

    def test_runner_uses_real_readonly_profile_and_leaves_ambient_tree_unchanged(self):
        before = check.snapshot(self.home)
        def action():
            selected = profiles.resolve()
            self.assertEqual(selected["provider"], "codex")
            self.assertEqual(selected["access"], "read-only")
            self.assertEqual(selected["effort"], "xhigh")
            self.assertIn("- code-review:", selected["instructions"])
            self.assertNotIn("Skill code-review:", selected["instructions"])
        code, output = self.run_small_suite(action)
        self.assertEqual(code, 0)
        self.assertIn("PROFILE ENVIRONMENT: PASS", output)
        # The nested fixture is deleted after its unchanged-tree check.
        self.assertEqual(check.snapshot(self.home), before)

    def test_runner_propagates_test_failure_without_filtering_it(self):
        def action():
            self.fail("synthetic failure must remain a failure")
        code, output = self.run_small_suite(action)
        self.assertEqual(code, 1)
        self.assertIn("PROFILE ENVIRONMENT: FAIL unit suite", output)
        self.assertNotIn("PROFILE ENVIRONMENT: PASS", output)

    def test_mutated_ambient_configuration_is_rejected_even_if_test_passes(self):
        def action():
            store = profiles.Store()
            before = store.load()
            store.mutate(lambda value: value.put(value.active, replace(value.profiles[value.active],
                instructions="Unexpected fixture mutation.")), expected=before)
        with self.assertRaisesRegex(ValueError, "ambient-config-changed"):
            self.run_small_suite(action)

    def test_snapshot_refuses_link_without_following_its_target(self):
        (self.home / "link").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError, "fixture-type"):
            check.snapshot(self.home)

    def test_snapshot_with_root_metadata_detects_create_then_unlink(self):
        before = check.snapshot(self.home, include_root=True)
        temporary = self.home / "unexpected"
        temporary.write_text("synthetic")
        temporary.unlink()
        self.assertNotEqual(check.snapshot(self.home, include_root=True), before)

    def test_canary_refuses_a_disabled_readonly_guard(self):
        expected = profiles.resolve(store=self.store)
        with check.ambient_store(self.home), patch.object(run_controls, "require_changes"), \
                self.assertRaisesRegex(ValueError, "ambient-readonly"):
            check.check_canary(expected)

    def test_missing_opt_in_stops_before_any_fixture_or_suite(self):
        with patch.object(check, "run", side_effect=AssertionError("no fixture")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(check.main([]), 2)
        self.assertIn("REFUSED", output.getvalue())
