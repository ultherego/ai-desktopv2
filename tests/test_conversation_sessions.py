"""Real private files and leases; no login, AI process or network."""
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import pwd
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import conversation_sessions as sessions, session_cli


CONFIG = {"provider": "claude", "model": "sonnet", "effort": "medium", "instructions": "Rzeczowo.",
          "profile": "reviewer", "access": "read-only"}


class ConversationTests(unittest.TestCase):
    def setUp(self):
        actual = pwd.getpwuid(os.geteuid()).pw_dir
        self.temporary = self.enterContext(tempfile.TemporaryDirectory(prefix="aid-conversations-", dir=actual))
        self.home = Path(self.temporary)
        self.store = sessions.Store(self.home)

    def conversation(self, kind="material", config=CONFIG):
        value = self.store.create(kind, str(self.home / "material.txt") if kind != "ask" else "",
                                  "a" * 64 if kind != "ask" else "", config)
        self.addCleanup(value.close)
        return value

    def path(self, conversation):
        return self.store.path / (conversation.session_id + ".json")

    def assertRefused(self, function, code=None):
        with self.assertRaises((sessions.SessionError, OSError)) as caught:
            function()
        if code:
            self.assertEqual(str(caught.exception), code)

    def test_no_implicit_persistence_or_creation_for_read_only_list(self):
        self.assertEqual(self.store.list(), [])
        self.assertFalse((self.home / ".local").exists())
        self.assertIsNone(sessions.load_session(SimpleNamespace(), "ask", store=self.store))
        self.assertFalse((self.home / ".local").exists())
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(session_cli.main([], store=self.store), 0)
        self.assertIn("--session new", output.getvalue())
        self.assertFalse((self.home / ".local").exists())

    def test_create_append_close_and_resume_are_real_private_files(self):
        value = self.conversation()
        self.assertRegex(value.session_id, r"\A[a-f0-9]{24}\Z")
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path(value).stat().st_mode), 0o600)
        self.assertEqual(self.path(value).stat().st_nlink, 1)
        value.append("Co to?", "Odpowiedź.")
        self.assertEqual(value.history, [{"question": "Co to?", "answer": "Odpowiedź."}])
        value.close()
        resumed = self.store.open(value.session_id, "material", str(self.home / "material.txt"), "a" * 64, CONFIG)
        self.addCleanup(resumed.close)
        self.assertEqual(resumed.history, value.history)
        self.assertEqual(resumed.record["revision"], 1)

    def test_history_retains_four_turns_and_shortens_answers_visibly(self):
        value = self.conversation()
        for number in range(6):
            value.append("Pytanie %d" % number, "ą" * 5000)
        record = self.store.read(value.session_id)
        self.assertEqual(len(record["history"]), 4)
        self.assertEqual(record["history"][0]["question"], "Pytanie 2")
        self.assertIn("[skrócono]", record["history"][0]["answer"])
        self.assertLessEqual(len(record["history"][0]["answer"].encode()), 8192 + 64)
        self.assertLessEqual(len(value.history[0]["answer"].encode()), 1536 + 64)
        altered = value.history
        altered[0]["answer"] = "Podmiana"
        self.assertNotEqual(value.history[0]["answer"], "Podmiana")

    def test_current_binding_and_complete_execution_must_match(self):
        value = self.conversation()
        value.close()
        for kind, path, digest, config in (
                ("project", str(self.home / "material.txt"), "a" * 64, CONFIG),
                ("material", str(self.home / "other.txt"), "a" * 64, CONFIG),
                ("material", str(self.home / "material.txt"), "b" * 64, CONFIG),
                ("material", str(self.home / "material.txt"), "a" * 64, {**CONFIG, "access": "changes"})):
            self.assertRefused(lambda: self.store.open(value.session_id, kind, path, digest, config), "session-binding")
        # A changed model or Skill set of the same agent keeps the conversation and is recorded.
        changed = {**CONFIG, "instructions": "Inne Skille.", "model": "haiku"}
        with self.store.open(value.session_id, "material", str(self.home / "material.txt"), "a" * 64, changed) as reopened:
            self.assertEqual(reopened.record["execution"], changed)
        # No serialized grants/permissions or native session identifiers exist.
        self.assertEqual(set(value.record), sessions._KEYS)
        self.assertNotIn("grant", self.path(value).read_text())

    def test_active_lease_refuses_second_open_and_removal(self):
        value = self.conversation()
        self.assertTrue(self.store.active(value.session_id))
        self.assertRefused(lambda: self.store.open(value.session_id, "material", str(self.home / "material.txt"),
                                                 "a" * 64, CONFIG), "session-active")
        self.assertRefused(lambda: self.store.remove(value.session_id), "session-active")
        self.assertTrue(self.path(value).is_file())
        value.close()
        self.assertFalse(self.store.active(value.session_id))
        self.store.remove(value.session_id)
        self.assertFalse(self.path(value).exists())
        self.assertEqual(self.store.list(), [])
        self.assertRefused(lambda: value.append("Pytanie", "Odpowiedź"), "session-closed")

    def test_revision_comparison_prevents_lost_cooperating_update(self):
        value = self.conversation()
        first = value.record
        second = copy.deepcopy(first)
        first["history"] = [{"question": "Pierwszy", "answer": "Wynik"}]
        self.store.update(first, 0)
        second["history"] = [{"question": "Drugi", "answer": "Wynik"}]
        self.assertRefused(lambda: self.store.update(second, 0), "session-conflict")
        self.assertEqual(self.store.read(value.session_id)["history"], first["history"])

    def test_immutable_metadata_cannot_be_replaced_by_update(self):
        value = self.conversation()
        changed = value.record
        changed["binding"] = "b" * 64
        self.assertRefused(lambda: self.store.update(changed, 0), "session-binding")
        self.assertEqual(self.store.read(value.session_id)["binding"], "a" * 64)

    def test_refuses_symlink_hardlink_and_public_record(self):
        for attack in ("symlink", "hardlink", "mode"):
            with self.subTest(attack=attack):
                value = self.conversation()
                value.close()
                path = self.path(value)
                if attack == "symlink":
                    target = self.home / "elsewhere"
                    target.write_text(path.read_text())
                    target.chmod(0o600)
                    path.unlink()
                    path.symlink_to(target)
                elif attack == "hardlink":
                    os.link(path, self.home / "another-link")
                else:
                    path.chmod(0o644)
                self.assertRefused(lambda: self.store.read(value.session_id))
                path.unlink()
                if attack == "hardlink":
                    (self.home / "another-link").unlink()

    def test_symlink_or_writable_ancestor_is_not_treated_as_missing(self):
        outside = self.home / "outside"
        outside.mkdir(mode=0o700)
        (self.home / ".local").symlink_to(outside, target_is_directory=True)
        self.assertRefused(self.store.list)
        (self.home / ".local").unlink()
        (self.home / ".local").mkdir(mode=0o777)
        (self.home / ".local").chmod(0o777)
        self.assertRefused(self.store.list, "session-directory")
        self.assertRefused(lambda: self.conversation(), "session-directory")

    def test_bounded_read_and_closed_schema_reject_tampered_record(self):
        value = self.conversation()
        path = self.path(value)
        raw = path.read_bytes()
        for broken in (b"x" * (sessions.MAX_FILE + 1), b'{"schema":1,"schema":1}',
                       json.dumps({**value.record, "grant": "forged"}).encode(),
                       json.dumps({**value.record, "path": 123}).encode(),
                       json.dumps({**value.record, "revision": True}).encode()):
            path.write_bytes(broken)
            self.assertRefused(lambda: self.store.read(value.session_id))
        path.write_bytes(raw)

    def test_fifo_is_refused_without_blocking(self):
        value = self.conversation()
        path = self.path(value)
        path.unlink()
        os.mkfifo(path, 0o600)
        self.assertRefused(lambda: self.store.read(value.session_id), "session-file")

    def test_read_detects_modification_during_read(self):
        value = self.conversation()
        original_read = os.read
        def racing_read(fd, size):
            raw = original_read(fd, size)
            with self.path(value).open("ab") as file:
                file.write(b" ")
            return raw
        with patch.object(sessions.os, "read", side_effect=racing_read):
            self.assertRefused(lambda: self.store.read(value.session_id), "session-conflict")

    def test_no_replace_publication_preserves_concurrent_destination(self):
        value = self.conversation()
        before = self.path(value).read_bytes()
        # Publishing a second initial record to an occupied ID never replaces
        # it, including a target created after the ID was chosen.
        with self.store._root() as root:
            self.assertRefused(lambda: self.store._publish(root, value.record))
        self.assertEqual(self.path(value).read_bytes(), before)
        self.assertFalse(any(name.startswith(".new-") for name in os.listdir(self.store.path)))

    def test_invalid_binding_text_controls_and_ids_never_persist(self):
        for path in ("relative", str(self.home / "../other"), "/line\nname"):
            self.assertRefused(lambda: self.store.create("material", path, "a" * 64, CONFIG))
        value = self.conversation()
        before = self.path(value).read_bytes()
        for question, answer in (("q\x1b[31m", "a"), ("q", "a\u202e"), ("x" * (128 * 1024 + 1), "a")):
            self.assertRefused(lambda: value.append(question, answer))
            self.assertEqual(self.path(value).read_bytes(), before)
        for ident in ("../escape", "x" * 24, "a" * 25):
            self.assertRefused(lambda: self.store.read(ident), "session-id")

    def test_session_opt_in_hook_and_resume_arguments_do_not_carry_authority(self):
        value = sessions.load_session(SimpleNamespace(session="new"), "material", str(self.home / "material.txt"),
                                      "a" * 64, execution=CONFIG, store=self.store)
        self.addCleanup(value.close)
        value.close()
        args = self.store.resume_args(value.session_id)
        self.assertEqual(args, ["material", str(self.home / "material.txt"), "--session", value.session_id,
                                "--agent", "reviewer", "--model", "sonnet", "--effort", "medium"])
        self.assertNotIn("read-only", args)
        self.assertNotIn("instructions", args)
        resumed = sessions.load_session(SimpleNamespace(session=value.session_id), "material", str(self.home / "material.txt"),
                                        "a" * 64, execution=CONFIG, store=self.store)
        resumed.close()

    def test_cli_show_list_resume_remove_are_local_and_concise(self):
        value = self.conversation("ask")
        value.append("Moje pytanie", "Moja odpowiedź")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(session_cli.main(["list"], store=self.store), 0)
            self.assertEqual(session_cli.main(["show", value.session_id], store=self.store), 0)
        self.assertIn("[aktywna]", output.getvalue())
        self.assertIn("Moje pytanie", output.getvalue())
        dispatch = unittest.mock.Mock(return_value=7)
        self.assertEqual(session_cli.main(["resume", value.session_id], store=self.store, dispatch=dispatch), 7)
        dispatch.assert_called_once_with(self.store.resume_args(value.session_id))
        value.close()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(session_cli.main(["remove", value.session_id], store=self.store), 0)
        self.assertEqual(self.store.list(), [])

    def test_session_limit_is_enforced_before_publication(self):
        value = self.conversation()
        with patch.object(sessions, "MAX_SESSIONS", 1):
            self.assertRefused(lambda: self.conversation(), "session-limit")
        self.assertEqual(len(self.store.list()), 1)


if __name__ == "__main__":
    unittest.main()
