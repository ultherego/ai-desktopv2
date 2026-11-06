"""Pure scoped approval, expiry and replay checks; no source or native I/O."""
import copy
import unittest
from unittest.mock import Mock, patch

from ai_desktop.analysis_contract import AnalysisConsent, AnalysisDenied, Approval
from ai_desktop.assistant_flow import AccessSnapshot
from ai_desktop.question_consent import QuestionConsent
from ai_desktop.permissions import Limits


QUESTION = "Sprawdź syntetyczną konfigurację"
FILE = "/synthetic/config.txt"


class PlanReadyTests(unittest.TestCase):
    """Only a caller that validated its own edit plan may start at the workspace phase; nothing else changes."""

    def make(self, **kwargs):
        policy = Mock(spec=("recheck", "select"))
        snapshot = AccessSnapshot(policy, {"claude": True, "systemd": [], "weather": False},
                                  {"files": [], "systemd": [], "weather": False})
        return QuestionConsent(QUESTION, snapshot, **kwargs)

    def test_default_starts_with_the_planner_phase_and_only_true_enables_the_shortcut(self):
        self.assertEqual(self.make()._phase, "first")
        for value in (False, None, 1, "yes", [True]):
            self.assertEqual(self.make(plan_ready=value)._phase, "first", value)
        self.assertEqual(self.make(plan_ready=True)._phase, "context")


class QuestionConsentTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        for target in ("builtins.open", "os.open", "subprocess.Popen"):
            self.enterContext(patch(target, side_effect=AssertionError("unexpected I/O")))

    def fixture(self, *, approved=True, web=False):
        policy = Mock(spec=("recheck", "select", "limits"))
        policy.limits.return_value = Limits()

        def select(paths, limit=None):
            if tuple(paths) != (FILE,):
                raise ValueError("files-denied")
            return tuple(paths)

        policy.select.side_effect = select
        snapshot = AccessSnapshot(policy,
            {"claude": True, "systemd": ["system"], "weather": False},
            {"files": [{"id": "f0", "pattern": "/synthetic/*"}],
             "systemd": ["system"], "weather": False})
        if web:
            snapshot.access["web"] = True
            snapshot.sources["web"] = True
        permit = QuestionConsent(QUESTION, snapshot, clock=lambda: self.now[0])
        if approved:
            permit.approve()
        return permit, snapshot, policy

    def gate(self, *, question=QUESTION, context=None, access=None, web=False):
        return AnalysisConsent(None, question=question, context=context, access=access, web=web,
                               clock=lambda: self.now[0])

    def transfer(self, permit, *, question=QUESTION, context=None, access=None, web=False):
        gate = self.gate(question=question, context=context, access=access, web=web)
        permit.claim(gate, question, context=context, access=access, web=web)
        approval = permit.authorize(gate, question, context=context, access=access, web=web)
        self.assertIs(type(approval), Approval)
        self.assertEqual((approval.payload, approval.sha256), (gate.payload, gate.sha256))
        return gate

    def test_the_window_slides_with_each_step_but_never_past_the_question_ceiling(self):
        from ai_desktop.analysis_contract import CONSENT_CEILING, TTL
        permit, _, _ = self.fixture()
        self.now[0] += TTL - 1
        permit.renew()                                       # an explicit step of this question: a fresh window
        self.now[0] += TTL - 1
        permit.check()                                       # would have expired without the renewal
        for _ in range(100):                                 # however many steps, the ceiling is absolute
            self.now[0] += TTL - 1
            try:
                permit.renew()
                permit.check()
            except AnalysisDenied:
                break
        self.assertLessEqual(self.now[0] - 100.0, CONSENT_CEILING + TTL)
        with self.assertRaises(AnalysisDenied):
            permit.check()

    def test_an_expired_closed_or_unapproved_question_is_never_revived(self):
        from ai_desktop.analysis_contract import TTL
        permit, _, _ = self.fixture()
        self.now[0] += TTL + 1
        with self.assertRaises(AnalysisDenied):
            permit.check()                                   # expiry is noticed and closes the question
        permit.renew()
        self.assert_closed(permit)
        fresh, _, _ = self.fixture(approved=False)
        fresh.renew()
        with self.assertRaises(AnalysisDenied):
            fresh.check()

    def test_slow_reasoning_gets_a_longer_window_and_the_default_keeps_180_seconds(self):
        from ai_desktop.analysis_contract import TTL, consent_seconds
        self.assertEqual((TTL, consent_seconds(None), consent_seconds("high")), (180, 180, 180))
        self.assertEqual(consent_seconds("max"), consent_seconds("xhigh"))
        self.assertGreater(consent_seconds("max"), 180)
        self.assertGreater(consent_seconds("max"), 900)              # a slow model is not mistaken for a lapsed consent

    def assert_closed(self, permit):
        with self.assertRaises(AnalysisDenied):
            permit.check()
        with self.assertRaises(AnalysisDenied):
            permit.approve()

    def test_explicit_approval_is_required_before_any_claim(self):
        permit, _, _ = self.fixture(approved=False)
        with self.assertRaises(AnalysisDenied):
            permit.check()
        permit.approve()
        permit.check()
        unapproved, _, _ = self.fixture(approved=False)
        with self.assertRaises(AnalysisDenied):
            unapproved.claim(self.gate(context=[]), QUESTION, context=[])
        self.assert_closed(unapproved)

    def test_planner_then_context_allow_two_distinct_payloads_but_no_third(self):
        permit, snapshot, policy = self.fixture()
        planner = self.transfer(permit, access=snapshot.access)
        context = [{"kind": "files", "files": [{"path": FILE, "content": "synthetic\n"}]}]
        answer = self.transfer(permit, context=context)
        self.assertNotEqual(planner.sha256, answer.sha256)
        self.assertNotEqual(planner.nonce, answer.nonce)
        policy.select.assert_called_with((FILE,), limit=Limits().read_files)
        with self.assertRaises(AnalysisDenied):
            permit.claim(self.gate(context=[]), QUESTION, context=[])
        self.assert_closed(permit)

    def test_direct_context_consumes_the_only_transfer(self):
        permit, snapshot, _ = self.fixture()
        self.transfer(permit, context=[])
        with self.assertRaises(AnalysisDenied):
            permit.claim(self.gate(access=snapshot.access), QUESTION, access=snapshot.access)
        self.assert_closed(permit)

    def test_web_is_only_second_transfer_after_planner_and_has_no_refill(self):
        permit, snapshot, policy = self.fixture(web=True)
        planner = self.transfer(permit, access=snapshot.access)
        answer = self.transfer(permit, web=True)
        self.assertNotEqual(planner.sha256, answer.sha256)
        policy.select.assert_not_called()
        for values in (dict(web=True), dict(context=[]), dict(access=snapshot.access)):
            with self.subTest(values=values), self.assertRaises(AnalysisDenied):
                permit.claim(self.gate(**values), QUESTION, **values)
        self.assert_closed(permit)

    def test_web_requires_explicit_captured_permission_and_planning_phase(self):
        for case in ("absent", "first", "after-direct-answer"):
            permit, snapshot, _ = self.fixture(web=case != "absent")
            if case != "first":
                self.transfer(permit, context=[] if case == "after-direct-answer" else None,
                              access=None if case == "after-direct-answer" else snapshot.access)
            with self.subTest(case=case), self.assertRaises(AnalysisDenied):
                permit.claim(self.gate(web=True), QUESTION, web=True)
            self.assert_closed(permit)
        # Even forged availability cannot give an unconfigured question web access.
        for access, source in ((False, True), (True, False), (False, False)):
            _, snapshot, _ = self.fixture()
            snapshot.access["web"] = access
            snapshot.sources["web"] = source
            permit = QuestionConsent(QUESTION, snapshot)
            permit.approve()
            with self.subTest(access=access, source=source), self.assertRaises(AnalysisDenied):
                permit.require_web()
        snapshot.access["web"] = True
        object.__setattr__(snapshot, "policy", None)
        snapshot.sources["web"] = True
        with patch.object(AccessSnapshot, "require_claude"):
            permit = QuestionConsent(QUESTION, snapshot)
            permit.approve()
            with self.assertRaises(AnalysisDenied):
                permit.require_web()

    def test_web_question_local_payload_or_claim_mode_cannot_change(self):
        for extra in (dict(context=[]), dict(access={}), dict(task={}), dict(write_grant=object()),
                      dict(web=1), dict(question="different question")):
            permit, snapshot, _ = self.fixture(web=True)
            self.transfer(permit, access=snapshot.access)
            values = dict(question=QUESTION, web=True)
            values.update(extra)
            with self.subTest(extra=extra), self.assertRaises(AnalysisDenied):
                permit.claim(self.gate(web=True), **values)
            self.assert_closed(permit)
        permit, snapshot, _ = self.fixture(web=True)
        self.transfer(permit, access=snapshot.access)
        gate = self.gate(web=True)
        permit.claim(gate, QUESTION, web=True)
        with self.assertRaises(AnalysisDenied):
            permit.authorize(gate, QUESTION, context=[])
        self.assert_closed(permit)

    def test_web_rechecks_revocation_and_original_deadline_before_send(self):
        for case in ("policy", "access", "source", "expiry"):
            permit, snapshot, policy = self.fixture(web=True)
            self.transfer(permit, access=snapshot.access)
            gate = self.gate(web=True)
            permit.claim(gate, QUESTION, web=True)
            if case == "policy":
                policy.recheck.side_effect = ValueError("policy-changed")
            elif case == "access":
                snapshot.access["web"] = False
            elif case == "source":
                snapshot.sources["web"] = False
            else:
                self.now[0] = 281
            with self.subTest(case=case), self.assertRaises(AnalysisDenied):
                permit.authorize(gate, QUESTION, web=True)
            self.assert_closed(permit)

    def test_pending_claim_cannot_be_replaced_by_a_new_nonce_or_replayed(self):
        for replacement in (False, True):
            with self.subTest(new_gate=replacement):
                permit, snapshot, _ = self.fixture()
                first = self.gate(access=snapshot.access)
                permit.claim(first, QUESTION, access=snapshot.access)
                second = self.gate(access=snapshot.access) if replacement else first
                with self.assertRaises(AnalysisDenied):
                    permit.claim(second, QUESTION, access=snapshot.access)
                self.assert_closed(permit)

    def test_authorize_requires_the_reserved_gate_and_cannot_be_replayed(self):
        for mode in ("unclaimed", "foreign", "replay"):
            with self.subTest(mode=mode):
                permit, snapshot, _ = self.fixture()
                gate = self.gate(access=snapshot.access)
                if mode != "unclaimed":
                    permit.claim(gate, QUESTION, access=snapshot.access)
                if mode == "replay":
                    permit.authorize(gate, QUESTION, access=snapshot.access)
                if mode == "foreign":
                    gate = self.gate(access=snapshot.access)
                with self.assertRaises(AnalysisDenied):
                    permit.authorize(gate, QUESTION, access=snapshot.access)
                self.assert_closed(permit)

    def test_wrong_question_access_or_gate_cannot_claim_authority(self):
        for mode in ("question", "access", "gate"):
            with self.subTest(mode=mode):
                permit, snapshot, _ = self.fixture()
                question = "Inne pytanie" if mode == "question" else QUESTION
                access = copy.deepcopy(snapshot.access)
                if mode == "access":
                    access["weather"] = True
                gate = object() if mode == "gate" else self.gate(question=question, access=access)
                with self.assertRaises(AnalysisDenied):
                    permit.claim(gate, question, access=access)
                self.assert_closed(permit)

    def test_payload_mutation_after_claim_refuses_without_retry(self):
        permit, snapshot, _ = self.fixture()
        gate = self.gate(access=snapshot.access)
        permit.claim(gate, QUESTION, access=snapshot.access)
        gate.payload += " "
        with self.assertRaises(AnalysisDenied):
            permit.authorize(gate, QUESTION, access=snapshot.access)
        with self.assertRaises(AnalysisDenied):
            permit.claim(self.gate(access=snapshot.access), QUESTION, access=snapshot.access)
        self.assert_closed(permit)

    def test_access_sources_and_policy_identity_are_bound_for_the_question(self):
        for mode in ("access", "sources", "policy"):
            with self.subTest(mode=mode):
                permit, snapshot, policy = self.fixture()
                if mode == "access":
                    snapshot.access["files"][0]["pattern"] = "/another/*"
                elif mode == "sources":
                    snapshot.sources["claude"] = False
                else:
                    object.__setattr__(snapshot, "policy", Mock())
                with self.assertRaises(AnalysisDenied):
                    permit.check()
                # Restoring metadata never reopens a failed decision.
                snapshot.access["files"][0]["pattern"] = "/synthetic/*"
                snapshot.sources["claude"] = True
                object.__setattr__(snapshot, "policy", policy)
                self.assert_closed(permit)

    def test_a_claim_inside_the_live_window_renews_it_but_a_claim_after_expiry_cannot_revive_it(self):
        permit, snapshot, _ = self.fixture()
        self.now[0] = 200.0
        self.transfer(permit, access=snapshot.access)
        self.now[0] = 279.0                                   # still inside the first window (100 + 180)
        gate = self.gate(context=[])
        permit.claim(gate, QUESTION, context=[])              # renewed: the next step has a whole window of its own
        self.now[0] = 400.0
        permit.authorize(gate, QUESTION, context=[])
        late, _, _ = self.fixture()
        self.now[0] += 181.0                                  # the window ran out before anything claimed it
        with self.assertRaises(AnalysisDenied):
            late.claim(self.gate(context=[]), QUESTION, context=[])
        self.assert_closed(late)

    def test_the_time_the_user_spends_answering_is_not_counted(self):
        from ai_desktop.analysis_contract import TTL
        permit, _, _ = self.fixture()
        with permit.waiting():
            self.now[0] += 900                                # a long think at a prompt (or a sudo password)
        permit.check()
        self.now[0] += TTL - 1
        permit.check()                                        # only machine time is limited
        self.now[0] += 2
        with self.assertRaises(AnalysisDenied):
            permit.check()

    def test_the_window_starts_at_the_users_yes_not_at_the_creation_of_the_question(self):
        from ai_desktop.analysis_contract import TTL
        permit, _, _ = self.fixture(approved=False)
        self.now[0] += 1000                                   # the user hesitated before answering the first prompt
        permit.approve()
        self.now[0] += TTL - 1
        permit.check()

    def test_deadline_is_rechecked_after_policy_validation(self):
        permit, _, policy = self.fixture()
        policy.recheck.side_effect = lambda: self.now.__setitem__(0, 281.0)
        with self.assertRaises(AnalysisDenied):
            permit.check()
        self.assert_closed(permit)

    def test_policy_revocation_between_claim_and_authorize_closes_the_question(self):
        permit, snapshot, policy = self.fixture()
        gate = self.gate(access=snapshot.access)
        permit.claim(gate, QUESTION, access=snapshot.access)
        policy.recheck.side_effect = ValueError("policy-changed")
        with self.assertRaises(AnalysisDenied):
            permit.authorize(gate, QUESTION, access=snapshot.access)
        policy.recheck.side_effect = None
        self.assert_closed(permit)

    def test_each_source_must_belong_to_the_captured_permissions(self):
        for mode in ("weather", "systemd", "foreign-policy", "unknown-file", "duplicate-file"):
            with self.subTest(mode=mode):
                permit, _, policy = self.fixture()
                permit.require_systemd("system")
                permit.require_files(policy, ["f0"])
                with self.assertRaises(AnalysisDenied):
                    if mode == "weather":
                        permit.require_weather()
                    elif mode == "systemd":
                        permit.require_systemd("user")
                    elif mode == "foreign-policy":
                        permit.require_files(Mock(), ["f0"])
                    elif mode == "unknown-file":
                        permit.require_files(policy, ["f1"])
                    else:
                        permit.require_files(policy, ["f0", "f0"])
                policy.select.assert_not_called()
                self.assert_closed(permit)

    def test_payload_cannot_include_a_file_outside_the_captured_policy(self):
        permit, _, policy = self.fixture()
        context = [{"kind": "files", "files": [{"path": "/another/file.txt", "content": "synthetic"}]}]
        with self.assertRaises(ValueError):
            permit.claim(self.gate(context=context), QUESTION, context=context)
        policy.select.assert_called_once_with(("/another/file.txt",), limit=Limits().read_files)
        self.assert_closed(permit)

    def test_distinct_selectors_share_one_captured_scope_without_expanding_authority(self):
        permit, _, policy = self.fixture()
        permit.require_files(policy, [{"id": "f0", "path": "config.txt"},
                                     {"id": "f0", "path": "parts/**"}])
        permit.check()
        policy.select.assert_not_called()
        # The final payload still checks exact paths against the same policy.
        outside = [{"kind": "files", "files": [{"path": "/outside/file.txt", "content": "synthetic"}]}]
        with self.assertRaises(ValueError):
            permit.claim(self.gate(context=outside), QUESTION, context=outside)
        self.assert_closed(permit)

    def test_narrow_selector_unknown_scope_duplicate_or_invalid_path_closes_question(self):
        selections = [[{"id": "f1", "path": "config.txt"}], [{"id": "f0", "path": "../outside"}],
                      [{"id": "f0", "path": "/outside"}], [{"id": "f0", "path": "x", "write": True}],
                      [{"id": "f0", "path": "config.txt"}] * 2,
                      [{"id": "f0", "path": f"file{i}.txt"} for i in range(129)]]
        for selected in selections:
            permit, _, policy = self.fixture()
            with self.subTest(selected=selected), self.assertRaises(AnalysisDenied):
                permit.require_files(policy, selected)
            policy.select.assert_not_called()
            self.assert_closed(permit)
        permit, _, policy = self.fixture()
        with self.assertRaises(AnalysisDenied):
            permit.require_files(Mock(), [{"id": "f0", "path": "config.txt"}])
        policy.select.assert_not_called()
        self.assert_closed(permit)

    def test_second_approval_or_explicit_close_cannot_restart_the_question(self):
        for mode in ("approved-again", "closed"):
            with self.subTest(mode=mode):
                permit, _, _ = self.fixture()
                if mode == "approved-again":
                    with self.assertRaises(AnalysisDenied):
                        permit.approve()
                else:
                    permit.close()
                self.assert_closed(permit)


if __name__ == "__main__":
    unittest.main()
