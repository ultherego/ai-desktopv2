"""Recipient presentation follows frozen task data, without account/client IO."""
from contextlib import ExitStack, redirect_stdout
import io
import os
import threading
import unittest
from unittest.mock import patch

from ai_desktop import execution, run_controls, terminal_consent, terminal_progress


def selected(provider):
    return {"provider": provider, "model": "default", "effort": "high", "instructions": "",
            "profile": "chosen-profile", "access": "read-only"}


class RecipientPromptTests(unittest.TestCase):
    def test_explicit_snapshot_and_legacy_none_override_another_ambient_profile(self):
        for ambient, frozen, expected in (("claude", selected("codex"), "Codex"),
                                          ("codex", selected("claude"), "Claude"),
                                          ("codex", None, "Claude")):
            with self.subTest(ambient=ambient, expected=expected), execution.using(selected(ambient)):
                self.assertEqual(run_controls.recipient(config=frozen), expected)
                self.assertIn(expected, run_controls.label(config=frozen))
                self.assertIn("od " + expected + "…", run_controls.wait_message(config=frozen))
        self.assertIsNone(execution.current())

    def test_recipient_snapshot_copies_and_never_reads_accounts_or_native_clients(self):
        config = selected("codex")
        with patch("builtins.open", side_effect=AssertionError("file")), \
                patch("os.open", side_effect=AssertionError("file")), \
                patch("socket.socket", side_effect=AssertionError("network")), \
                patch("subprocess.run", side_effect=AssertionError("process")), \
                patch("subprocess.Popen", side_effect=AssertionError("process")):
            frozen = run_controls.recipient_config(config)
            config["provider"] = "claude"
            self.assertEqual(run_controls.recipient(config=frozen), "Codex")
            self.assertEqual(run_controls.recipient(config=None), "Claude")
            self.assertIn("ChatGPT / Codex", run_controls.failure_hint("auth-required", "unused", config=frozen))
            self.assertEqual(run_controls.failure_hint("runtime", "Claude runtime", config=None), "Claude runtime")

    def test_invalid_execution_cannot_supply_a_recipient_or_start_a_consent_read(self):
        for config in ({"provider": "codex"}, {**selected("codex"), "provider": "Codex\x1b[2J"},
                       {**selected("codex"), "effort": "max"}, True):
            with self.subTest(config=config), \
                    patch.object(terminal_consent.sys.stdin, "isatty", return_value=True), \
                    patch.object(terminal_consent.sys.stdout, "isatty", return_value=True), \
                    patch.object(terminal_consent, "fresh_input") as read:
                with self.assertRaises(ValueError):
                    run_controls.wait_message(config=config)
                with self.assertRaises(ValueError):
                    terminal_consent.confirm("transfer", config=config)
            read.assert_not_called()

    def test_all_egress_prompts_name_the_frozen_recipient_and_local_decisions_stay_local(self):
        ai_actions = {"ask-question", "ask-transfer", "transfer", "material-transfer", "material-whole",
                      "material-task", "project-task", "project-continue", "project-edit"}
        for provider, expected, other in (("claude", "Claude", "Codex"), ("codex", "Codex", "Claude")):
            for action in terminal_consent.PROMPTS:
                prompts = []
                with self.subTest(provider=provider, action=action), execution.using(selected(other.lower())), \
                        patch.object(terminal_consent.sys.stdin, "isatty", return_value=True), \
                        patch.object(terminal_consent.sys.stdout, "isatty", return_value=True):
                    self.assertFalse(terminal_consent.confirm(
                        action, reader=lambda prompt: prompts.append(prompt) or "n", config=selected(provider)))
                self.assertEqual(len(prompts), 1)
                if action in ai_actions:
                    self.assertIn(expected, prompts[0])
                    self.assertNotIn(other, prompts[0])
                else:
                    self.assertEqual(prompts[0], terminal_consent.PROMPTS[action])
                    self.assertNotIn("Claude", prompts[0])
                    self.assertNotIn("Codex", prompts[0])

    def test_reprompt_keeps_one_recipient_if_controller_context_or_source_dictionary_changes(self):
        for explicit in (False, True):
            source, prompts, replies = selected("codex"), [], iter(("?", "", "t"))
            def read(prompt):
                prompts.append(prompt)
                execution.select(selected("claude"))
                source["provider"] = "claude"
                return next(replies)
            with self.subTest(explicit=explicit), redirect_stdout(io.StringIO()), \
                    execution.using(selected("codex")), \
                    patch.object(terminal_consent.sys.stdin, "isatty", return_value=True), \
                    patch.object(terminal_consent.sys.stdout, "isatty", return_value=True):
                kwargs = {"config": source} if explicit else {}
                self.assertTrue(terminal_consent.confirm("transfer", reader=read, **kwargs))
            self.assertEqual(len(prompts), 3)
            self.assertTrue(all("Codex" in prompt and "Claude" not in prompt for prompt in prompts))

    def test_nonterminal_never_reads_or_displays_a_provider_confirmation(self):
        with patch.object(terminal_consent.sys.stdin, "isatty", return_value=False), \
                patch.object(terminal_consent, "fresh_input") as read, \
                redirect_stdout(io.StringIO()) as output:
            self.assertFalse(terminal_consent.confirm("transfer", config=selected("codex")))
        read.assert_not_called()
        self.assertEqual(output.getvalue(), "")


class ProviderAnimationTests(unittest.TestCase):
    def test_real_background_thread_uses_constructor_recipient_with_no_ambient_execution(self):
        painted = threading.Event()
        child_contexts = []
        parent = threading.get_ident()
        class Output(io.StringIO):
            def isatty(self):
                return True
            def fileno(self):
                return 1
            def write(self, value):
                result = super().write(value)
                if threading.get_ident() != parent:
                    child_contexts.append(execution.current())
                    painted.set()
                return result
        out = Output()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"TERM": "xterm"}, clear=True))
            stack.enter_context(patch.object(terminal_progress.os, "isatty", return_value=True))
            stack.enter_context(patch.object(terminal_progress.os, "get_terminal_size",
                                           return_value=os.terminal_size((100, 24))))
            with execution.using(selected("codex")):
                indicator = terminal_progress.WaitingIndicator(stream=out)
            self.assertIsNone(execution.current())
            # No live ContextVar lookup is permitted during phases/animation.
            with patch.object(terminal_progress, "recipient", side_effect=AssertionError("late recipient lookup")):
                with indicator:
                    indicator.phase("task")
                    self.assertTrue(painted.wait(3), "animation thread did not draw")
                    indicator.phase("question")
            self.assertTrue(child_contexts)
            self.assertTrue(all(value is None for value in child_contexts))
            self.assertIn("Codex pracuje", out.getvalue())
            self.assertIn("Codex: Opracowywanie odpowiedzi", out.getvalue())
            self.assertNotIn("Claude", out.getvalue())
            self.assertFalse(indicator._thread.is_alive())
            self.assertTrue(out.getvalue().endswith(terminal_progress.CLEAR_LINE))

    def test_explicit_legacy_recipient_is_not_replaced_by_ambient_codex(self):
        class Output(io.StringIO):
            def isatty(self): return True
            def fileno(self): return 1
        out = Output()
        with execution.using(selected("codex")), \
                patch.dict(os.environ, {"TERM": "xterm"}, clear=True), \
                patch.object(terminal_progress.os, "isatty", return_value=True), \
                patch.object(terminal_progress.os, "get_terminal_size", return_value=os.terminal_size((80, 24))), \
                patch.object(terminal_progress.threading, "Thread"):
            with terminal_progress.WaitingIndicator(stream=out, config=None) as indicator:
                indicator.phase("task")
        self.assertIn("Claude pracuje", out.getvalue())
        self.assertNotIn("Codex", out.getvalue())


if __name__ == "__main__":
    unittest.main()
