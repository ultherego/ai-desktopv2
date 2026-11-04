"""Directory aliases remain bound through collection and final transfer consent."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import pwd
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import execution, file_context, permissions
from ai_desktop.analysis_contract import AnalysisConsent, AnalysisDenied, Approval
from ai_desktop.assistant_flow import AccessSnapshot
from ai_desktop.question_consent import QuestionConsent


QUESTION = "Sprawdź syntetyczne ustawienia monitora"


class DirectoryAliasConsentTests(unittest.TestCase):
    def setUp(self):
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="aid-alias-consent-", dir=real_home)))
        self.uid = os.getuid()
        self.enterContext(patch.object(permissions, "_identity", return_value=(self.uid, str(self.home))))
        self.enterContext(execution.using(None))
        self.config = self.home / ".config/ai-desktop/permissions.toml"
        self.config.parent.mkdir(parents=True, mode=0o700)
        self.config.write_text("version = 2\n[network]\nclaude = true\n[files]\nread = "
                               + json.dumps(["~/.config/**"])
                               + "\ndeny = " + json.dumps(["~/.config/ai-desktop/"]) + "\n")
        self.config.chmod(0o600)
        self.target = self.home / ".config/dms"
        self.target.mkdir(mode=0o700)
        self.file = self.target / "settings.json"
        self.file.write_text('{"monitor":"DP-1"}\n')
        self.alias = self.home / ".config/DankMaterialShell"
        self.alias.symlink_to("dms")
        self.policy = permissions.load_policy()

    def permit(self, *, approved=True):
        snapshot = AccessSnapshot(self.policy, self.policy.sources(), self.policy.access())
        permit = QuestionConsent(QUESTION, snapshot)
        if approved:
            permit.approve()
        return permit

    def selection(self, policy=None):
        return (self.policy if policy is None else policy).expand_selected(((0, "DankMaterialShell/**"),))

    def collect(self, permit):
        args = SimpleNamespace(details=False, preview=False, no_context=False)
        with redirect_stdout(io.StringIO()):
            item = file_context.collect(self.policy, [{"id": "f0", "path": "DankMaterialShell/**"}],
                                        args, question_consent=permit)
        return [item]

    def claimed(self):
        permit = self.permit()
        context = self.collect(permit)
        gate = AnalysisConsent(None, question=QUESTION, context=context)
        permit.claim(gate, QUESTION, context=context)
        return permit, gate, context

    def assert_closed(self, permit):
        with self.assertRaises(AnalysisDenied) as denied:
            permit.check()
        self.assertEqual(denied.exception.reason, "consent")

    def test_collector_binds_the_alias_and_an_unchanged_snapshot_authorizes_physical_context(self):
        permit, gate, context = self.claimed()
        self.assertEqual(len(permit._file_reads), 1)
        self.assertEqual(context[0]["files"], [{"path": str(self.file), "content": '{"monitor":"DP-1"}\n'}])
        approval = permit.authorize(gate, QUESTION, context=context)
        self.assertIs(type(approval), Approval)
        self.assertEqual((approval.payload, approval.sha256), (gate.payload, gate.sha256))

    def test_alias_retargeted_after_collection_and_claim_is_refused_before_gate_authorization(self):
        permit, gate, context = self.claimed()
        other = self.home / ".config/other"
        other.mkdir(mode=0o700)
        (other / "settings.json").write_text("SYNTHETIC REPLACEMENT")
        self.alias.unlink()
        self.alias.symlink_to("other")
        with patch.object(gate, "authorize", wraps=gate.authorize) as authorize:
            with self.assertRaises(AnalysisDenied) as denied:
                permit.authorize(gate, QUESTION, context=context)
            self.assertEqual(denied.exception.reason, "consent")
            authorize.assert_not_called()
        self.assertFalse(gate._used)
        self.assert_closed(permit)

    def test_recreated_alias_with_the_same_target_is_also_a_version_change(self):
        permit, gate, context = self.claimed()
        self.alias.unlink()
        self.alias.symlink_to("dms")
        with self.assertRaises(AnalysisDenied) as denied:
            permit.authorize(gate, QUESTION, context=context)
        self.assertEqual(denied.exception.reason, "consent")
        self.assertFalse(gate._used)
        self.assert_closed(permit)

    def test_replacing_the_target_directory_after_claim_prevents_transfer_of_the_old_context(self):
        permit, gate, context = self.claimed()
        self.target.rename(self.target.with_name("previous-dms"))
        self.target.mkdir(mode=0o700)
        (self.target / "settings.json").write_text("SYNTHETIC REPLACEMENT")
        with self.assertRaises(AnalysisDenied) as denied:
            permit.authorize(gate, QUESTION, context=context)
        self.assertEqual(denied.exception.reason, "consent")
        self.assertFalse(gate._used)
        self.assert_closed(permit)

    def test_a_stale_selection_is_rejected_at_binding_before_a_file_read(self):
        permit, selected = self.permit(), self.selection()
        self.alias.unlink()
        self.alias.symlink_to("dms")
        with patch.object(permissions, "_read", wraps=permissions._read) as read:
            with self.assertRaises(AnalysisDenied):
                permit.bind_file_selection(self.policy, selected)
        self.assertTrue(all(str(call.args[0]) == str(self.config) for call in read.call_args_list))
        self.assert_closed(permit)

    def test_binding_requires_approval_the_original_policy_and_exact_snapshot_type(self):
        selected = self.selection()
        unapproved = self.permit(approved=False)
        with self.assertRaises(AnalysisDenied):
            unapproved.bind_file_selection(self.policy, selected)
        self.assertEqual(unapproved._file_reads, ())
        other_policy = permissions.load_policy()  # Same bytes do not establish object identity.
        for policy, value in ((other_policy, selected),
                              (self.policy, self.selection(other_policy)),
                              (self.policy, object()),
                              (self.policy, replace(selected, aliases=()))):
            with self.subTest(policy_is_original=policy is self.policy, value_type=type(value).__name__):
                permit = self.permit()
                with self.assertRaises(AnalysisDenied):
                    permit.bind_file_selection(policy, value)
                self.assert_closed(permit)

    def test_ordinary_paths_do_not_add_a_guard_or_grant_authority_to_an_unallowed_context(self):
        permit = self.permit()
        permit.bind_file_selection(self.policy, (str(self.file),))
        self.assertEqual(permit._file_reads, ())
        context = [{"kind": "files", "files": [{"path": "/synthetic/outside.txt", "content": "synthetic"}]}]
        gate = AnalysisConsent(None, question=QUESTION, context=context)
        with self.assertRaises((AnalysisDenied, ValueError)):
            permit.claim(gate, QUESTION, context=context)
        self.assertFalse(gate._used)
        self.assert_closed(permit)

    def test_alias_bindings_are_bounded_and_do_not_refill_the_question(self):
        permit, selected = self.permit(), self.selection()
        for _ in range(8):
            permit.bind_file_selection(self.policy, selected)
        self.assertEqual(len(permit._file_reads), 8)
        with self.assertRaises(AnalysisDenied):
            permit.bind_file_selection(self.policy, selected)
        self.assertEqual(len(permit._file_reads), 8)
        self.assert_closed(permit)
