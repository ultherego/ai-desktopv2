"""Plain task refusals and what a finished change looks like when the thread is read back."""
import io
import os
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import Mock, patch

os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")

from ai_desktop import ask_cli as ask, ask_edit, conversation_state, task_cli
from tests.test_automatic_conversation_save import PROFILE, FakeStore


class TaskRefusalTests(unittest.TestCase):
    def test_a_known_reason_says_what_to_do(self):
        for code, words in (("tty", "interactive terminal"), ("recovery required", "task status"),
                            ("workspace changed", "Nothing was saved"), ("text", "line endings")):
            with self.subTest(code=code):
                self.assertIn(words, task_cli.refusal_text(task_cli.refusal_reason(ValueError(code))))

    def test_a_journal_with_wrong_permissions_names_the_directory(self):
        text = task_cli.refusal_text("journal directory", "/home/example/project")
        self.assertIn(".ai-desktop-tasks", text)
        self.assertIn("/home/example/project", text)

    def test_an_unknown_reason_is_named_instead_of_hidden(self):
        self.assertEqual(task_cli.refusal_reason(ValueError("stage collision")), "stage collision")
        self.assertIn("reason: stage collision", task_cli.refusal_text("stage collision"))

    def test_free_text_and_other_errors_never_reach_the_message(self):
        for exc, expected in ((ValueError("/home/example/secret path: \x1b[2J"), "ValueError"),
                              (PermissionError(13, "denied", "/home/example/x"), "PermissionError"),
                              (KeyError("x"), "KeyError")):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(task_cli.refusal_reason(exc), expected)


class ChangeInTheThreadTests(unittest.TestCase):
    def test_a_saved_change_is_read_back_as_what_the_controller_recorded(self):
        answers = iter(["write", "Done."])

        def flow(*unused_args, **unused_kwargs):
            if next(answers) == "write":
                conversation_state.note_action("Change applied through ask (test text passed, can be undone): "
                                               "SYNTHETIC greeting [a.py]")
                return ask_edit.CompletedTask(0)
            return "Done."

        store = FakeStore()
        output = io.StringIO()
        output.isatty = lambda: True
        with ExitStack() as stack:
            stack.enter_context(patch("ai_desktop.agent_profiles.resolve", return_value=PROFILE))
            stack.enter_context(patch.object(ask.os, "geteuid", return_value=1000))
            stack.enter_context(patch.object(ask.os, "getuid", return_value=1000))
            stack.enter_context(patch.object(ask.sys.stdin, "isatty", return_value=True))
            stack.enter_context(patch.object(ask, "Presence", return_value=Mock()))
            stack.enter_context(patch.object(ask, "_choose_thread", return_value=("new", None)))
            stack.enter_context(patch.object(ask, "_read_message", side_effect=["Create a.py", ""]))
            stack.enter_context(patch.object(ask.assistant_flow, "run", side_effect=flow))
            stack.enter_context(patch("ai_desktop.conversation_sessions.Store", return_value=store))
            stack.enter_context(redirect_stdout(output))
            self.assertEqual(ask.main([], reader=input), 0, output.getvalue())
        thread = conversation_state.load(store.conversation.saved).transcript
        self.assertIn("SYNTHETIC greeting [a.py]", thread[0]["answer"])
        self.assertNotIn("Change task finished", thread[0]["answer"])
        self.assertTrue(thread[1]["automatic"])


if __name__ == "__main__":
    unittest.main()
