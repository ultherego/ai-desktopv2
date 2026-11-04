"""Saved-conversation menu contracts using real private records, no AI or OAuth."""
import contextlib
import io
from pathlib import Path
import os
import pwd
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_desktop import conversation_sessions as sessions
from ai_desktop import agent_profiles, execution
from ai_desktop import session_cli
from ai_desktop.terminal_menu import MenuInputError


class SessionMenuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aid-session-menu-", dir=pwd.getpwuid(os.geteuid()).pw_dir)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = sessions.Store(home=self.home)

    def conversation(self, question="Pytanie", answer="Odpowiedź", *, selected=None):
        value = self.store.create("ask", execution_config=selected)
        value.append(question, answer)
        value.close()
        return value.session_id

    def path(self, ident):
        return self.home / ".local/state/ai-desktop/conversations" / (ident + ".json")

    def run_menu(self, choices, *, argv=None, tty=False, dispatch=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(session_cli, "choose", side_effect=choices) as choose, \
                patch.object(session_cli.sys.stdin, "isatty", return_value=tty), \
                patch.object(session_cli.sys.stdout, "isatty", return_value=tty):
            result = session_cli.main(["menu"] if argv is None else argv, store=self.store, dispatch=dispatch)
        return result, output.getvalue(), choose

    def test_empty_interactive_default_keeps_opt_in_message_and_creates_no_files(self):
        result, output, choose = self.run_menu([], argv=[], tty=True)
        self.assertEqual(result, 0)
        self.assertIn("--session new", output)
        choose.assert_not_called()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_nonterminal_default_is_a_local_list_without_menu_or_dispatch(self):
        ident = self.conversation()
        dispatch = Mock()
        result, output, choose = self.run_menu([], argv=[], dispatch=dispatch)
        self.assertEqual(result, 0)
        self.assertIn(ident, output)
        choose.assert_not_called()
        dispatch.assert_not_called()

    def test_cancel_and_back_do_not_change_record_or_resume(self):
        ident = self.conversation()
        original = self.path(ident).read_bytes()
        dispatch = Mock()
        for choices in ([None], [0, None, None], [0, 3, 2]):
            with self.subTest(choices=choices):
                result, _, _ = self.run_menu(choices, dispatch=dispatch)
                self.assertEqual(result, 0)
                self.assertEqual(self.path(ident).read_bytes(), original)
        dispatch.assert_not_called()

    def test_interactive_default_opens_the_menu_for_existing_records(self):
        self.conversation()
        result, _, choose = self.run_menu([None], argv=[], tty=True)
        self.assertEqual(result, 0)
        self.assertIn("Zapisane rozmowy", choose.call_args.args[0])

    def test_preview_is_bounded_and_does_not_dispatch_or_mutate(self):
        ident = self.conversation("Q" * 2000, "A" * 8000)
        original = self.path(ident).read_bytes()
        dispatch = Mock()
        result, output, _ = self.run_menu([0, 0, None, None], dispatch=dispatch)
        self.assertEqual(result, 0)
        self.assertIn("[skrócono]", output)
        self.assertIn("sessions show " + ident, output)
        self.assertLess(len(output), 2500)
        self.assertEqual(self.path(ident).read_bytes(), original)
        dispatch.assert_not_called()

    def test_resume_uses_existing_fresh_dispatch_arguments(self):
        ident = self.conversation()
        dispatch = Mock(return_value=7)
        result, _, _ = self.run_menu([0, 1], dispatch=dispatch)
        self.assertEqual(result, 7)
        dispatch.assert_called_once_with(["ask", "--session", ident])
        self.assertTrue(self.path(ident).is_file())

    def test_saved_service_labels_ignore_current_service_in_list_menu_and_preview(self):
        for provider in ("claude", "codex"):
            selected = {"provider": provider, "model": "default", "effort": "high",
                        "profile": "reviewer", "instructions": "", "access": "read-only"}
            ambient = dict(selected, provider="codex" if provider == "claude" else "claude")
            ident = self.conversation(selected=selected)
            original = self.path(ident).read_bytes()
            name = "Codex" if provider == "codex" else "Claude"
            with self.subTest(provider=provider), execution.using(ambient):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(session_cli.main(["list"], store=self.store), 0)
                line = next(line for line in output.getvalue().splitlines() if ident in line)
                self.assertIn(name, line)
                self.assertIn("reviewer", line)
                result, _, choose = self.run_menu([None])
                self.assertEqual(result, 0)
                item = next(item for item in choose.call_args.args[1] if ident in item)
                self.assertIn(name, item)
                with contextlib.redirect_stdout(io.StringIO()) as shown:
                    self.assertEqual(session_cli.main(["show", ident], store=self.store), 0)
                self.assertIn("Rozmowa %s · ask · %s" % (ident, name), shown.getvalue())
            self.assertEqual(self.path(ident).read_bytes(), original)

    def test_legacy_saved_service_is_claude_even_when_current_service_is_codex(self):
        ident = self.conversation()
        ambient = {"provider": "codex", "model": "default", "effort": "default",
                   "profile": "default", "instructions": "", "access": "changes"}
        with execution.using(ambient):
            result, _, choose = self.run_menu([None])
        self.assertEqual(result, 0)
        item = next(item for item in choose.call_args.args[1] if ident in item)
        self.assertIn("Claude", item)
        self.assertNotIn("Codex", item)

    def test_service_switch_keeps_existing_resume_binding_guard(self):
        profiles = agent_profiles.Store(home=self.home)
        profiles.mutate(lambda config: config.with_provider("codex"))
        selected = agent_profiles.resolve(store=profiles)
        ident = self.conversation(selected=selected)
        original = self.path(ident).read_bytes()
        profiles.mutate(lambda config: config.with_provider("claude"))
        def dispatch(arguments):
            self.assertEqual(arguments, ["ask", "--session", ident, "--agent", "default",
                                         "--model", "default", "--effort", "default"])
            fresh = agent_profiles.resolve(store=profiles)
            with self.assertRaisesRegex(sessions.SessionError, "session-binding"):
                self.store.open(ident, "ask", execution_config=fresh)
            return 2
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(session_cli.main(["resume", ident], store=self.store, dispatch=dispatch), 2)
        self.assertEqual(self.path(ident).read_bytes(), original)

    def test_active_record_refuses_menu_resume_before_dispatch(self):
        value = self.store.create("ask")
        self.addCleanup(value.close)
        dispatch = Mock()
        result, output, _ = self.run_menu([0, 1], dispatch=dispatch)
        self.assertEqual(result, 2)
        self.assertIn("aktywna", output)
        dispatch.assert_not_called()

    def test_explicit_delete_action_removes_only_selected_record(self):
        ident = self.conversation()
        dispatch = Mock()
        result, output, choose = self.run_menu([0, 2], dispatch=dispatch)
        self.assertEqual(result, 0)
        self.assertIn("Usunięto zapis", output)
        self.assertEqual(self.store.list(), [])
        self.assertEqual(choose.call_count, 2)
        self.assertIn(ident, choose.call_args.args[0])
        self.assertIn("Usuń zapis tej rozmowy", choose.call_args.args[1])
        dispatch.assert_not_called()

    def test_changed_record_after_review_is_not_deleted(self):
        ident = self.conversation()
        original = self.store.read(ident)
        def choices(title, options):
            if title.startswith("Zapisane"):
                return 0
            current = self.store.open(ident, "ask")
            try:
                current.append("Nowe pytanie", "Równoległa odpowiedź")
            finally:
                current.close()
            return 2
        result, output, _ = self.run_menu(choices)
        self.assertEqual(result, 2)
        self.assertIn("zmieniła się", output)
        after = self.store.read(ident)
        self.assertGreater(after["revision"], original["revision"])
        self.assertEqual(after["history"][-1]["answer"], "Równoległa odpowiedź")

    def test_removal_compares_reviewed_snapshot_inside_the_store_lock(self):
        ident = self.conversation()
        reviewed = self.store.read(ident)
        updated = self.store.open(ident, "ask")
        updated.append("Później", "Nowa treść")
        updated.close()
        with self.assertRaisesRegex(sessions.SessionError, "session-conflict"):
            self.store.remove(ident, expected=reviewed)
        self.assertEqual(self.store.read(ident)["history"][-1]["answer"], "Nowa treść")
        # The original explicit CLI route retains its established semantics.
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(session_cli.main(["remove", ident], store=self.store), 0)

    def test_pagination_preserves_all_record_choices_and_stays_within_menu_limit(self):
        for number in range(19):
            self.conversation("Pytanie " + str(number))
        seen = []
        def choices(title, options):
            self.assertLessEqual(len(options), 12)
            seen.extend(option.split(" · ")[0] for option in options if " · " in option)
            return options.index("Następna strona") if "Następna strona" in options else None
        result, _, choose = self.run_menu(choices)
        self.assertEqual(result, 0)
        self.assertEqual(choose.call_count, 3)
        self.assertEqual(set(seen), {value["id"] for value in self.store.list()})

    def test_invalid_selection_or_terminal_failure_never_dispatches(self):
        ident = self.conversation()
        original = self.path(ident).read_bytes()
        for choices in ([True], [99], MenuInputError()):
            dispatch = Mock()
            result, _, _ = self.run_menu(choices, dispatch=dispatch)
            self.assertEqual(result, 2)
            self.assertEqual(self.path(ident).read_bytes(), original)
            dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
