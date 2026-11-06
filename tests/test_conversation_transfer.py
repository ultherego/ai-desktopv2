"""Explicit conversation recipient changes, using real temporary policy, account choices and session records."""
from contextlib import nullcontext, redirect_stdout
import io
import json
import os
from pathlib import Path
import pwd
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import agent_profiles, ask_cli, ask_commands, assistant_flow, conversation_sessions
from ai_desktop import conversation_state, execution, permissions
from ai_desktop.account_store import AccountStore
from ai_desktop.question_consent import ConversationConsent


class TransferTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(dir=pwd.getpwuid(os.geteuid()).pw_dir))
        self.home = Path(directory)
        self.store = conversation_sessions.Store(self.home)
        self.profiles = agent_profiles.Store(self.home)
        self.accounts = AccountStore(self.home)
        self.enterContext(patch.object(agent_profiles, "Store", return_value=self.profiles))
        self.enterContext(patch.object(permissions, "_identity", return_value=(os.getuid(), directory)))
        self.config = self.home / ".config/ai-desktop/permissions.toml"
        self.config.parent.mkdir(parents=True, mode=0o700)
        self.policy()
        self.out = self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(sys.stdin, "isatty", return_value=True))
        self.enterContext(patch.object(sys.stdout, "isatty", return_value=True))
        self.enterContext(execution.using(agent_profiles.resolve()))
        self.controls = ask_commands.Controls()
        self.consent = ConversationConsent()
        self.consent.grant(assistant_flow.capture_access())
        self.talk = conversation_state.Conversation()
        self.talk.finish("Remember my question", "Prior private result", None, [str(self.home / "private")])
        self.talk.finish("Continue", "Dependent answer", None, ())

    def policy(self, *, codex=True):
        body = ('version = 2\n[network]\nclaude = true\ncodex = %s\nlocal = true\n'
                '[files]\nread = []\ndeny = []\n' % ("true" if codex else "false"))
        self.config.write_text(body)
        self.config.chmod(0o600)

    def switch(self, text, reply, saved=None):
        self.assertTrue(self.controls.handle(text, saved, talk=self.talk, consent=self.consent, reader=reply))

    def test_confirmed_transfer_saves_the_same_thread_and_rechecks_sources_before_sharing(self):
        self.accounts.mutate(lambda accounts: accounts.add("codex", "work").select("codex", "work"))
        with self.store.create("ask", execution_config=execution.current()) as saved:
            saved.save_state(conversation_state.export(self.talk))
            replies = []
            self.switch("/provider codex", lambda prompt: replies.append(prompt) or "yes", saved)
            self.assertEqual(len(replies), 1)
            self.assertIn("account: work", self.out.getvalue())
            self.assertEqual(execution.current()["account"], "work")
            self.assertEqual(execution.current()["provider"], "codex")
            self.assertTrue(self.consent.covers(assistant_flow.capture_access()))
            self.assertNotIn("Prior private result", str(self.talk.history()))
            self.assertIn("Remember my question", str(self.talk.history()))
            saved_id = saved.session_id
        self.assertEqual(self.profiles.load().config.selected_provider, "claude")
        self.assertNotIn("Prior private result", str(self.store.read(saved_id)))
        self.assertIn("codex", self.store.resume_args(saved_id))
        with self.store.open(saved_id, "ask", execution_config=execution.current()):
            pass
        self.accounts.mutate(lambda accounts: accounts.select("codex", "default"))
        with self.assertRaisesRegex(conversation_sessions.SessionError, "session-binding"):
            self.store.open(saved_id, "ask", execution_config=agent_profiles.resolve(provider="codex"))

    def test_refusal_keeps_original_recipient_memory_saved_revision_and_grant(self):
        before = execution.current()
        with self.store.create("ask", execution_config=before) as saved:
            saved.save_state(conversation_state.export(self.talk))
            record = saved.record
            self.switch("/agent codex", lambda _: "no", saved)
            self.assertEqual(saved.record, record)
        self.assertEqual(execution.current(), before)
        self.assertTrue(self.consent.covers(assistant_flow.capture_access()))
        self.assertIn("Prior private result", str(self.talk.history()))

    def test_disabled_provider_is_refused_without_asking(self):
        self.policy(codex=False)
        self.switch("/provider codex", lambda _: self.fail("asked for a forbidden destination"))
        self.assertEqual(execution.current()["provider"], "claude")

    def test_replaced_policy_or_account_during_prompt_cancels_the_transfer(self):
        self.accounts.mutate(lambda accounts: accounts.add("codex", "personal"))
        def different_account(_):
            self.accounts.mutate(lambda accounts: accounts.select("codex", "personal"))
            return "yes"
        self.switch("/provider codex", different_account)
        self.assertEqual(execution.current()["provider"], "claude")
        def revoke(_):
            self.policy(codex=False)
            return "yes"
        self.switch("/provider codex", revoke)
        self.assertEqual(execution.current()["provider"], "claude")
        self.assertIn("Prior private result", str(self.talk.history()))

    def test_active_thread_cannot_be_rebound_by_a_second_session_open(self):
        with self.store.create("ask", execution_config=execution.current()) as saved:
            record = saved.record
            with self.assertRaisesRegex(conversation_sessions.SessionError, "session-active"):
                self.store.open(saved.session_id, "ask", execution_config={**execution.current(), "model": "haiku"})
            self.assertEqual(self.store.read(saved.session_id), record)

    def test_local_recipient_identifies_its_loopback_port_and_never_uses_a_cloud_account(self):
        self.switch("/agent local", lambda _: "yes")
        self.assertEqual(execution.current()["provider"], "local")
        self.assertIn("127.0.0.1:8080", self.out.getvalue())
        with self.store.create("ask", execution_config=execution.current()) as saved:
            session_id = saved.session_id
        with self.assertRaisesRegex(conversation_sessions.SessionError, "session-binding"):
            self.store.open(session_id, "ask", execution_config={**execution.current(), "local_port": 18080})

    def test_first_command_after_resume_transfers_once_then_only_the_approved_context_reaches_new_recipient(self):
        with self.store.create("ask", execution_config=execution.current()) as original:
            original.save_state(conversation_state.export(self.talk))
            session_id = original.session_id
        prompts, packets = [], []
        replies = iter(["/provider codex", "yes", "What was my question?", ""])
        def read(prompt):
            prompts.append(prompt)
            return next(replies)
        def send(approval):
            packets.append(json.loads(approval.payload))
            return json.dumps({"action": "answer", "text": "Received"})
        with patch.object(conversation_sessions, "Store", return_value=self.store), \
                patch.object(ask_cli, "os", wraps=os) as cli_os, patch.object(ask_cli, "Presence"), \
                patch.object(ask_cli, "installation_ready"), patch.object(ask_cli, "managed_session", return_value=nullcontext()), \
                patch.object(ask_cli, "send_once", side_effect=send):
            cli_os.getuid.return_value = cli_os.geteuid.return_value = 1000
            self.assertEqual(ask_cli.main(["--session", session_id], reader=read), 0, self.out.getvalue())
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0]["execution"]["provider"], "codex")
        self.assertIn("Remember my question", str(packets[0]["conversation"]))
        self.assertNotIn("Prior private result", str(packets))
        self.assertEqual(len([prompt for prompt in prompts if "[y/n" in prompt]), 1)
        self.assertEqual(self.store.read(session_id)["execution"]["provider"], "codex")

    def test_view_displays_only_the_sanitized_context_and_still_requires_an_answer(self):
        replies = iter(["view", "no"])
        self.switch("/provider codex", lambda _: next(replies))
        self.assertIn("Remember my question", self.out.getvalue())
        self.assertNotIn("Prior private result", self.out.getvalue())
        self.assertIn("Prior private result", str(self.talk.history()))
        self.assertEqual(execution.current()["provider"], "claude")
