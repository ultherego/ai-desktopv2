"""A declared local copy test cannot expand, survive revocation or be replayed."""
import copy
import unittest
from unittest.mock import Mock, patch

from ai_desktop import execution, task_test_consent
from ai_desktop.analysis_contract import AnalysisDenied, CONSENT_CEILING, consent_seconds
from ai_desktop.task_test_consent import CopyTestConsent


CONFIG = {"provider": "claude", "model": "default", "effort": "default", "instructions": "",
          "profile": "default", "access": "changes"}


class CopyTestConsentTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(execution.using(CONFIG))
        self.scope = {"root": "/synthetic/project", "workspace": "/synthetic/project",
                      "source_digest": "a" * 64, "before_digest": "b" * 64,
                      "files": ["main.py", "test_main.py"], "budget": {"files": 2, "bytes": 4096}}
        self.now = 100.0
        self.guard = Mock(return_value=None)

    def make(self, **kwargs):
        return CopyTestConsent(self.scope, "pytest", guard=kwargs.pop("guard", self.guard),
                               clock=lambda: self.now, **kwargs)

    def denied(self, function, *args):
        with self.assertRaises(AnalysisDenied) as caught:
            function(*args)
        self.assertEqual(caught.exception.reason, "consent")
        return caught.exception

    def test_one_explicit_approval_consumes_exact_scope_once_and_runs_live_guard(self):
        consent = self.make()
        self.guard.assert_not_called()
        consent.approve()
        reordered = dict(reversed(list(self.scope.items())))
        self.assertIsNone(consent.consume(reordered, "pytest"))
        self.assertEqual(self.guard.call_count, 2)
        self.denied(consent.consume, self.scope, "pytest")
        self.denied(consent.approve)
        self.assertEqual(self.guard.call_count, 2)

    def test_unapproved_attempt_is_terminal_even_if_ui_later_calls_approve(self):
        consent = self.make()
        self.denied(consent.consume, self.scope, "pytest")
        self.denied(consent.approve)
        self.guard.assert_not_called()

    def test_second_approval_closes_the_original_grant(self):
        consent = self.make()
        consent.approve()
        self.denied(consent.approve)
        self.denied(consent.consume, self.scope, "pytest")
        self.assertEqual(self.guard.call_count, 1)

    def test_changed_root_workspace_sources_selection_or_budget_cannot_reuse_approval(self):
        changes = {"root": "/synthetic/other", "workspace": "/synthetic/other",
                   "source_digest": "c" * 64, "before_digest": "d" * 64,
                   "files": ["main.py", "unapproved.py"], "budget": {"files": 3, "bytes": 8192}}
        for field, value in changes.items():
            with self.subTest(field=field):
                consent = self.make()
                consent.approve()
                changed = copy.deepcopy(self.scope)
                changed[field] = value
                self.denied(consent.consume, changed, "pytest")
                self.denied(consent.consume, self.scope, "pytest")

    def test_mutating_original_scope_does_not_modify_frozen_approval(self):
        consent = self.make()
        consent.approve()
        self.scope["files"].append("new.py")
        self.denied(consent.consume, self.scope, "pytest")

    def test_another_recipe_is_refused_and_cannot_retry_the_original(self):
        consent = self.make()
        consent.approve()
        self.denied(consent.consume, self.scope, "python-unittest")
        self.denied(consent.consume, self.scope, "pytest")

    def test_check_preserves_one_test_without_renewing_the_task_deadline(self):
        consent = self.make(seconds=300)
        consent.approve()
        self.now += 100
        consent.check(self.scope, "pytest")
        self.now += 100
        consent.check(self.scope, "pytest")
        self.now += 100
        consent.consume(self.scope, "pytest")
        self.denied(consent.check, self.scope, "pytest")
        self.denied(consent.consume, self.scope, "pytest")
        consent = self.make(seconds=300)
        consent.approve()
        self.now += 290
        consent.check(self.scope, "pytest")
        self.now += 11
        self.denied(consent.check, self.scope, "pytest")
        self.now = 0
        self.denied(consent.consume, self.scope, "pytest")

    def test_failed_check_closes_even_an_unapproved_or_otherwise_valid_grant(self):
        consent = self.make()
        self.denied(consent.check, self.scope, "pytest")
        self.denied(consent.approve)
        consent = self.make()
        consent.approve()
        changed = {**self.scope, "root": "/synthetic/other"}
        self.denied(consent.check, changed, "pytest")
        self.denied(consent.consume, self.scope, "pytest")

    def test_custom_duration_is_positive_integer_bounded_by_question_ceiling(self):
        for seconds in (1, CONSENT_CEILING):
            with self.subTest(seconds=seconds):
                consent = self.make(seconds=seconds)
                self.assertEqual(consent.seconds, seconds)
                consent.approve()
                consent.consume(self.scope, "pytest")
        for seconds in (False, True, 0, -1, 180.0, "180", CONSENT_CEILING + 1):
            with self.subTest(seconds=seconds):
                self.denied(lambda: self.make(seconds=seconds))

    def test_changed_execution_before_approval_or_consumption_is_refused(self):
        for field, value in (("provider", "codex"), ("model", "sonnet"),
                             ("effort", "high"), ("profile", "other"),
                             ("access", "read-only"), ("instructions", "changed"),
                             ("account", "other")):
            for approve_first in (False, True):
                with self.subTest(field=field, approve_first=approve_first):
                    consent = self.make()
                    if approve_first:
                        consent.approve()
                    with execution.using({**CONFIG, field: value}):
                        if approve_first:
                            self.denied(consent.consume, self.scope, "pytest")
                        else:
                            self.denied(consent.approve)
                    self.denied(consent.approve)
                    self.denied(consent.consume, self.scope, "pytest")

    def test_ttl_starts_at_yes_uses_selected_effort_and_never_renews(self):
        consent = self.make()
        self.now += 10000
        consent.approve()
        self.assertEqual(consent.seconds, consent_seconds("default"))
        self.now += consent.seconds
        consent.consume(self.scope, "pytest")
        with execution.using({**CONFIG, "effort": "xhigh"}):
            consent = self.make()
            self.assertEqual(consent.seconds, consent_seconds("xhigh"))
            self.assertGreater(consent.seconds, consent_seconds("default"))
            consent.approve()
            self.now += consent.seconds + 0.01
            self.denied(consent.consume, self.scope, "pytest")
            self.now = 0
            self.denied(consent.approve)
            self.denied(consent.consume, self.scope, "pytest")

    def test_revoked_guard_refuses_before_use_without_leaking_its_error(self):
        for error in (ValueError("PRIVATE POLICY"), OSError("PRIVATE SOURCE"),
                      RuntimeError("PRIVATE STATE"), AnalysisDenied("authorization")):
            for approve_first in (False, True):
                with self.subTest(error=type(error), approve_first=approve_first):
                    self.guard.side_effect = None
                    consent = self.make()
                    if approve_first:
                        consent.approve()
                    self.guard.side_effect = error
                    caught = self.denied(consent.consume, self.scope, "pytest") if approve_first else self.denied(consent.approve)
                    self.assertNotIn("PRIVATE", str(caught))
                    self.guard.side_effect = None
                    self.denied(consent.approve)
                    self.denied(consent.consume, self.scope, "pytest")

    def test_expiry_execution_and_scope_are_rechecked_after_live_guard(self):
        for mutate in (lambda: setattr(self, "now", self.now + 20000),
                       lambda: execution.select({**CONFIG, "profile": "changed"}),
                       lambda: self.scope["files"].append("late.py")):
            with self.subTest(mutate=mutate), execution.using(CONFIG):
                self.guard.side_effect = None
                consent = self.make()
                consent.approve()
                self.guard.side_effect = mutate
                self.denied(consent.consume, self.scope, "pytest")
                self.guard.side_effect = None
                self.denied(consent.consume, self.scope, "pytest")

    def test_explicit_false_guard_and_interrupted_guard_leave_no_live_grant(self):
        consent = self.make()
        self.guard.return_value = False
        self.denied(consent.approve)
        self.guard.return_value = None
        self.denied(consent.approve)
        consent = self.make()
        consent.approve()
        self.guard.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            consent.consume(self.scope, "pytest")
        self.guard.side_effect = None
        self.denied(consent.consume, self.scope, "pytest")

    def test_guard_cannot_reopen_grant_closed_during_approval_or_check(self):
        for approved in (False, True):
            self.guard.side_effect = None
            consent = self.make()
            if approved:
                consent.approve()
            self.guard.side_effect = consent.close
            if approved:
                self.denied(consent.check, self.scope, "pytest")
            else:
                self.denied(consent.approve)
            self.guard.side_effect = None
            self.denied(consent.approve)
            self.denied(consent.consume, self.scope, "pytest")

    def test_cannot_use_a_grant_in_another_process_or_after_close(self):
        consent = self.make()
        consent.approve()
        with patch.object(task_test_consent.os, "getpid", return_value=-1):
            self.denied(consent.consume, self.scope, "pytest")
        self.denied(consent.consume, self.scope, "pytest")
        for approved in (False, True):
            consent = self.make()
            if approved:
                consent.approve()
            consent.close()
            consent.close()
            self.denied(consent.approve)
            self.denied(consent.consume, self.scope, "pytest")

    def test_invalid_scope_recipe_or_guard_cannot_create_a_grant(self):
        for scope in ({}, [], True, {1: "ambiguous"}, {"files": ("main.py",)},
                      {"value": float("nan")}, {"value": object()}, {"value": "\ud800"}):
            with self.subTest(scope=repr(scope)):
                self.denied(lambda: CopyTestConsent(scope, "pytest", guard=self.guard))
        for recipe in (True, "", "pytest --upload", "rm -rf"):
            with self.subTest(recipe=recipe):
                self.denied(lambda: CopyTestConsent(self.scope, recipe, guard=self.guard))
        self.denied(lambda: CopyTestConsent(self.scope, "pytest", guard=None))
        self.denied(lambda: CopyTestConsent(self.scope, "pytest", guard=self.guard, clock=False))
        cyclic = {}
        cyclic["cycle"] = cyclic
        self.denied(lambda: CopyTestConsent(cyclic, "pytest", guard=self.guard))


if __name__ == "__main__":
    unittest.main()
