"""One damaged saved conversation must not break the others; /rejestr must show files handed over for a change."""
import contextlib
import io
import os
from pathlib import Path
import pwd
import tempfile
import unittest

from ai_desktop import conversation_sessions as cs, project_cli


class DamagedRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix="ai-sessions-", dir=pwd.getpwuid(os.geteuid()).pw_dir))
        home = Path(self.temp).resolve()
        home.chmod(0o700)
        self.store = cs.Store(home)
        self.root = self.store.path
        for path in (home / ".local", home / ".local/state", home / ".local/state/ai-desktop", self.root):
            path.mkdir(mode=0o700)
        self.good = "a" * 24
        self.bad = "b" * 24
        (self.root / (self.bad + ".json")).write_text("{uszkodzony")
        os.chmod(self.root / (self.bad + ".json"), 0o600)

    def test_a_damaged_record_is_reported_and_does_not_hide_the_list(self):
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.store.damaged(), [self.bad])

    def test_a_readable_record_is_not_removed_as_damaged(self):
        value = self.store.create("ask", "", "", None)
        ident = value.session_id
        value.close()
        with self.assertRaises(cs.SessionError):
            self.store.remove_damaged(ident)
        self.assertEqual([item["id"] for item in self.store.list()], [ident])

    def test_a_damaged_record_can_be_removed(self):
        self.store.remove_damaged(self.bad)
        self.assertEqual(self.store.damaged(), [])
        self.assertFalse((self.root / (self.bad + ".json")).exists())

    def test_a_symlink_is_removed_itself_and_its_target_stays(self):
        target = Path(self.temp) / "target"
        target.write_text("zostaje")
        link = self.root / (self.good + ".json")
        link.symlink_to(target)
        self.assertIn(self.good, self.store.damaged())
        self.store.remove_damaged(self.good)
        self.assertFalse(link.is_symlink())
        self.assertEqual(target.read_text(), "zostaje")


class RegisterTests(unittest.TestCase):
    def session(self):
        value = project_cli.ProjectSession.__new__(project_cli.ProjectSession)
        value.tasks, value.edit_transfers = [], []
        return value

    def output(self, value):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            value.ledger()
        return out.getvalue()

    def test_nothing_sent_says_so(self):
        self.assertIn("Nic nie zostało przekazane", self.output(self.session()))

    def test_an_edit_transfer_is_listed_and_does_not_claim_nothing_was_sent(self):
        value = self.session()
        value.edit_transfers.append({"files": ["a.py", "b.py"], "bytes": 12, "state": "odpowiedź otrzymana"})
        text = self.output(value)
        self.assertNotIn("Nic nie zostało", text)
        self.assertIn("2 plików, 12 B", text)
        self.assertIn("a.py", text)


if __name__ == "__main__":
    unittest.main()
