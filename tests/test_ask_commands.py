"""Controls typed at the `ask` prompt: local, never sent, and the conversation survives them."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
import contextlib
import io
import unittest
from unittest.mock import patch

from ai_desktop import analysis_contract, ask_commands as commands, agent_profiles as profiles, conversation_state, execution, question_input

BASE = {"provider": "claude", "model": "default", "effort": "default", "instructions": "", "profile": "default", "access": "changes"}


class FakeStore:
    def __init__(self, config):
        self.config = config

    def load(self):
        return type("Snapshot", (), {"config": self.config})()


class Conversation:
    def __init__(self):
        self.bound = []

    def rebind(self, selected):
        self.bound.append(selected)


class ControlsTests(unittest.TestCase):
    def setUp(self):
        config = profiles.defaults().put("default", profiles.Profile(skills=("a",)))
        self.enterContext(patch.object(profiles, "Store", return_value=FakeStore(config)))
        self.enterContext(patch.object(profiles, "_skill", side_effect=lambda name: "tresc " + name))
        self.enterContext(patch("ai_desktop.skills.names", return_value=("a", "b")))
        self.enterContext(execution.using(BASE))
        self.controls = commands.Controls()

    def run_line(self, line, conversation=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = self.controls.handle(line, conversation)
        return result, out.getvalue()

    def test_a_path_or_a_sentence_is_a_question_and_a_lone_unknown_word_is_explained(self):
        for line in ("/etc/fstab co to jest?", "co robi /etc/fstab", "/usr/bin/ls", " zwykle pytanie", "  "):
            self.assertIs(self.run_line(line)[0], False, line)
        result, output = self.run_line("/etc")
        self.assertIs(result, True)
        self.assertIn("Nieznane polecenie", output)
        self.assertIn("spacji", output)

    def test_help_lists_every_command_and_aliases_reach_the_same_action(self):
        output = self.run_line("/pomoc")[1]
        for name, _ in commands.COMMANDS:
            self.assertIn(name, output)
        self.assertEqual(self.run_line("/help")[1], output)
        for line in ("/koniec", "/exit", "/q"):
            self.assertEqual(self.run_line(line)[0], commands.EXIT)

    def test_model_and_effort_change_the_next_question_and_say_consent_is_asked_again(self):
        conversation = Conversation()
        result, output = self.run_line("/model haiku", conversation)
        self.assertIs(result, True)
        self.assertEqual(execution.current()["model"], "haiku")
        self.assertIn("poprosi o zgodę", output)
        self.assertEqual(conversation.bound[-1]["model"], "haiku")
        self.run_line("/effort high", conversation)
        self.assertEqual(execution.current()["effort"], "high")

    def test_an_invalid_model_changes_nothing_and_binds_nothing(self):
        conversation, before = Conversation(), execution.current()
        output = self.run_line("/model nie-ma-takiego", conversation)[1]
        self.assertEqual(execution.current(), before)
        self.assertEqual(conversation.bound, [])
        self.assertNotIn("poprosi o zgodę", output)

    def test_skills_toggle_for_this_conversation_without_touching_the_saved_agent(self):
        output = self.run_line("/skills b")[1]
        self.assertIn("a, b", output)
        self.assertIn("Skill a:\ntresc a", execution.current()["instructions"])
        self.assertIn("Skill b:\ntresc b", execution.current()["instructions"])
        self.run_line("/skills a")
        self.assertNotIn("Skill a:", execution.current()["instructions"])
        self.assertEqual(profiles.Store().load().config.profiles["default"].skills, ("a",))
        self.assertEqual(execution.current()["access"], "changes")      # never a wider permission

    def test_an_unknown_skill_or_one_that_does_not_fit_changes_nothing(self):
        before = execution.current()
        self.assertIn("Nie ma Skilla", self.run_line("/skills zzz")[1])
        with patch.object(profiles, "instructions_for", side_effect=profiles.ProfileError("x")):
            self.assertIn("Nie zmieniono", self.run_line("/skills b")[1])
        self.assertEqual(execution.current(), before)

    def test_a_saved_conversation_keeps_its_agent(self):
        before = execution.current()
        output = self.run_line("/agent reviewer", Conversation())[1]
        self.assertEqual(execution.current(), before)
        self.assertIn("związana", output)


class LongConversationTests(unittest.TestCase):
    def test_a_long_message_is_kept_for_follow_ups_and_older_turns_leave_first(self):
        talk = conversation_state.Conversation()
        pasted = "linia logu\n" * 9000                                          # about 100 KiB
        talk.finish("krótkie", "odpowiedź", None, ())
        talk.finish(pasted, "odpowiedź", None, ())
        self.assertEqual(talk.history()[-1]["question"], pasted)
        self.assertEqual(execution.validate_history(talk.history()), talk.history())
        talk.finish(pasted, "odpowiedź", None, ())
        kept = talk.history()
        self.assertEqual(len(kept), 1)                                            # the newest stays, the older left
        self.assertLessEqual(sum(len(t["question"].encode()) for t in kept), execution.HISTORY_TOTAL_BYTES)

    def test_the_wire_accepts_a_long_ask_message_and_material_keeps_its_short_limit(self):
        text = "x" * analysis_contract.MAX_MESSAGE_BYTES
        self.assertEqual(analysis_contract.validate_question(text, analysis_contract.MAX_MESSAGE_BYTES), text)
        with self.assertRaises(ValueError):
            analysis_contract.validate_question(text)
        with self.assertRaises(ValueError):
            analysis_contract.validate_question(text + "x", analysis_contract.MAX_MESSAGE_BYTES)


class SuggestionTests(unittest.TestCase):
    def session(self):
        session = question_input.PromptSession()
        session.commands = commands.COMMANDS
        return session

    def test_only_one_word_starting_with_a_slash_opens_the_list(self):
        session = self.session()
        self.assertEqual([i[0] for i in question_input._matches(session, "/mo")], ["/model"])
        self.assertEqual(len(question_input._matches(session, "/")), len(commands.COMMANDS))
        for text in ("", "pytanie", "/etc/fstab", "/model haiku", "/a\nb"):
            self.assertEqual(question_input._matches(session, text), (), text)
        self.assertEqual(question_input._matches(None, "/"), ())
        self.assertEqual(question_input._matches(question_input.PromptSession(), "/"), ())


if __name__ == "__main__":
    unittest.main()
