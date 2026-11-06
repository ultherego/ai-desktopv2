"""Finite read-stage authority; synthetic snapshots, no client or filesystem access."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import project_task_consent as consent
from ai_desktop.material_grant import GrantDenied
from tests.test_material_cli import Clock


class ProjectTaskConsentTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.execution = {"provider": "claude", "account": "default"}
        self.enterContext(patch.object(consent, "current_execution", side_effect=lambda: self.execution))
        self.policy = SimpleNamespace(fingerprint=Mock(return_value="policy"), recheck=Mock())
        self.project = SimpleNamespace(project_id="0" * 16, root="/project", policy=self.policy,
            manifest_sha256=Mock(return_value="1" * 64),
            files=(SimpleNamespace(path="sample.py", identity=(1, 2, 3, 4)),),
            stats={"scanned": 1, "indexed": 1}, complete=True, reason=None, scan=Mock())
        self.scope = consent.ProjectTaskConsent(self.project, "Inspect the project", clock=self.clock)

    def denied(self, method, code):
        with self.assertRaises(GrantDenied) as caught:
            method(self.project)
        self.assertEqual(caught.exception.code, code)

    def test_unapproved_closed_or_twice_approved_scope_never_issues_a_stage(self):
        self.denied(self.scope.next_stage, "GRANT_INVALID")
        self.scope.approve(self.project)
        self.denied(self.scope.approve, "GRANT_INVALID")
        self.denied(self.scope.next_stage, "GRANT_INVALID")
        fresh = consent.ProjectTaskConsent(self.project, "Inspect", clock=self.clock)
        fresh.close()
        self.denied(fresh.approve, "GRANT_INVALID")

    def test_one_approval_issues_only_the_declared_number_of_individually_bounded_stages(self):
        self.scope.approve(self.project)
        grants = [self.scope.next_stage(self.project) for _ in range(consent.MAX_STAGES)]
        self.assertEqual(sum(g.max_requests for g in grants), consent.TASK_REQUESTS)
        self.assertEqual(sum(g.max_bytes for g in grants), consent.TASK_BYTES)
        for grant in grants:
            self.assertEqual((grant.material_id, grant.material_sha256), ("0" * 16, "1" * 64))
            self.assertEqual((grant.max_requests, grant.max_bytes), (consent.MAX_STEPS, consent.TOTAL_BYTES))
            self.assertFalse(grant.internet)
            self.assertFalse(grant.write)
        self.denied(self.scope.next_stage, "BUDGET_EXHAUSTED")
        self.denied(self.scope.next_stage, "GRANT_INVALID")

    def test_task_deadline_starts_at_yes_and_later_stages_cannot_extend_it(self):
        self.clock.now += 10000
        self.scope.approve(self.project)
        deadline = self.clock() + consent.TASK_SECONDS
        first = self.scope.next_stage(self.project)
        self.assertEqual(first.deadline, self.clock() + consent.MAX_DEADLINE_SECONDS)
        self.clock.now = deadline - 1
        last = self.scope.next_stage(self.project)
        self.assertEqual(last.deadline, deadline)
        self.clock.now = deadline + 1
        self.denied(self.scope.check, "GRANT_EXPIRED")
        self.denied(self.scope.next_stage, "GRANT_INVALID")

    def test_changed_snapshot_refuses_even_when_legacy_manifest_is_unchanged(self):
        mutations = (
            lambda: setattr(self.project, "root", "/different"),
            lambda: setattr(self.project, "files", (SimpleNamespace(path="sample.py", identity=(1, 2, 3, 5)),)),
            lambda: setattr(self.project, "files", ()),
            lambda: setattr(self.project, "complete", False),
            lambda: self.project.stats.update(scanned=2),
            lambda: setattr(self.project.manifest_sha256, "return_value", "2" * 64),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                scope = consent.ProjectTaskConsent(self.project, "Inspect", clock=self.clock)
                scope.approve(self.project)
                mutate()
                self.denied(scope.next_stage, "SOURCE_CHANGED")
                self.denied(scope.next_stage, "GRANT_INVALID")

    def test_policy_identity_fingerprint_or_recheck_failure_refuses(self):
        for change in ("identity", "fingerprint", "recheck"):
            with self.subTest(change=change):
                scope = consent.ProjectTaskConsent(self.project, "Inspect", clock=self.clock)
                scope.approve(self.project)
                if change == "identity":
                    self.project.policy = SimpleNamespace(fingerprint=Mock(return_value="policy"), recheck=Mock())
                elif change == "fingerprint":
                    self.project.policy.fingerprint.return_value = "new-policy"
                else:
                    self.project.policy.recheck.side_effect = ValueError("changed")
                self.denied(scope.next_stage, "GRANT_INVALID")

    def test_changed_recipient_before_yes_or_between_stages_is_not_authorized(self):
        self.execution = {"provider": "codex", "account": "other"}
        self.denied(self.scope.approve, "GRANT_INVALID")
        scope = consent.ProjectTaskConsent(self.project, "Inspect", clock=self.clock)
        scope.approve(self.project)
        scope.next_stage(self.project)
        self.execution = {"provider": "codex", "account": "different"}
        self.denied(scope.next_stage, "GRANT_INVALID")

    def test_another_project_object_cannot_use_the_scope_even_with_identical_metadata(self):
        self.scope.approve(self.project)
        other = SimpleNamespace(**vars(self.project))
        with self.assertRaises(GrantDenied) as caught:
            self.scope.next_stage(other)
        self.assertEqual(caught.exception.code, "GRANT_INVALID")

    def test_live_rescan_detects_changes_not_yet_present_in_cached_entries(self):
        self.scope.approve(self.project)
        self.project.scan.reset_mock()
        self.scope.check(self.project)
        self.project.scan.assert_called_once_with()
        self.project.scan.side_effect = lambda: setattr(self.project, "files", ())
        self.denied(self.scope.check, "SOURCE_CHANGED")

    def test_an_inherited_scope_refuses_in_a_forked_process(self):
        self.scope.approve(self.project)
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            try:
                self.scope.next_stage(self.project)
                os.write(write_fd, b"allowed")
            except GrantDenied as exc:
                os.write(write_fd, exc.code.encode("ascii"))
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        try:
            result = os.read(read_fd, 128)
        finally:
            os.close(read_fd)
            _, status = os.waitpid(child, 0)
        self.assertEqual(status, 0)
        self.assertEqual(result, b"GRANT_INVALID")
        self.scope.next_stage(self.project)  # The parent's own live grant remains usable.


if __name__ == "__main__":
    unittest.main()
