"""One yes for one conversation: bound to the captured permissions and recipient, kept in memory only."""
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_desktop import permissions, question_consent
from ai_desktop.question_consent import ConversationConsent


def snapshot(**access):
    return SimpleNamespace(access={"files": [], "systemd": [], "weather": False, **access},
                           sources={"claude": True})


class ConversationConsentTests(unittest.TestCase):
    def test_nothing_is_covered_before_the_yes(self):
        self.assertFalse(ConversationConsent().covers(snapshot()))

    def test_the_same_permissions_are_covered_after_the_yes(self):
        consent = ConversationConsent()
        consent.grant(snapshot())
        self.assertTrue(consent.covers(snapshot()))

    def test_changed_permissions_are_not_covered(self):
        consent = ConversationConsent()
        consent.grant(snapshot())
        self.assertFalse(consent.covers(snapshot(weather=True)))
        self.assertFalse(consent.covers(snapshot(files=[{"id": "f0", "pattern": "~/**"}])))

    def test_changed_sources_are_not_covered(self):
        consent = ConversationConsent()
        consent.grant(snapshot())
        self.assertFalse(consent.covers(SimpleNamespace(access=snapshot().access, sources={"claude": True, "web": True})))

    def test_another_recipient_is_not_covered(self):
        consent = ConversationConsent()
        with patch.object(question_consent, "current_execution", return_value={"provider": "claude"}):
            consent.grant(snapshot())
        with patch.object(question_consent, "current_execution", return_value={"provider": "codex"}):
            self.assertFalse(consent.covers(snapshot()))

    def test_any_edit_of_the_policy_file_needs_a_new_yes_even_when_the_advertised_access_is_the_same(self):
        base = 'version = 2\n[network]\nclaude = true\n[files]\nread = ["~/data/**"]\ndeny = %s\n%s'
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        with tempfile.TemporaryDirectory(prefix="ai-consent-", dir=real_home) as home, \
                patch.object(permissions, "_identity", return_value=(os.getuid(), home)):
            config = Path(home, ".config/ai-desktop/permissions.toml")
            config.parent.mkdir(parents=True, mode=0o700)
            (Path(home) / "data").mkdir()

            def captured(text):
                config.write_text(text)
                config.chmod(0o600)
                policy = permissions.load_policy()
                return SimpleNamespace(policy=policy, access=policy.access(), sources=policy.sources())
            first = captured(base % ('["~/data/secret/"]', ""))
            consent = ConversationConsent()
            consent.grant(first)
            self.assertTrue(consent.covers(captured(base % ('["~/data/secret/"]', ""))))
            for label, text in (("deny removed", base % ("[]", "")),
                                ("limit raised", base % ('["~/data/secret/"]', 'read_files = 128\n')),
                                ("process sharing", base % ('["~/data/secret/"]', '[commands]\nshare_processes = true\n')),
                                ("comment", base % ('["~/data/secret/"]', "") + "# x\n")):
                with self.subTest(label):
                    changed = captured(text)
                    self.assertEqual(changed.access, first.access)        # the old check saw no difference here
                    self.assertFalse(consent.covers(changed))

    def test_a_new_conversation_starts_without_a_yes(self):
        ConversationConsent().grant(snapshot())
        self.assertFalse(ConversationConsent().covers(snapshot()))


if __name__ == "__main__":
    unittest.main()
