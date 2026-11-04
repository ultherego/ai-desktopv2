"""One yes for one conversation: bound to the captured permissions and recipient, kept in memory only."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_desktop import question_consent
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

    def test_a_new_conversation_starts_without_a_yes(self):
        ConversationConsent().grant(snapshot())
        self.assertFalse(ConversationConsent().covers(snapshot()))


if __name__ == "__main__":
    unittest.main()
