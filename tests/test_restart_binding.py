"""E5 restart acceptance helper: synthetic HOME, fake identity, simulated restart."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import task_workspace as w
from ai_desktop.analysis_contract import canonical
from scripts import restart_binding as rb

FS = {"kind": "btrfs", "fsid": "a" * 32, "tree": 257, "uuid": "b" * 32}


class RestartBindingTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.geteuid(), 0)
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ai-rb-home-")))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}))
        # The owner's own files and older private journals must never be touched.
        (self.home / "hello.py").write_text("print('mine')\n")
        (self.home / w.STATE).mkdir(mode=0o700)
        (self.home / w.STATE / ("a" * 64 + ".json")).write_text("{legacy schema 2 journal}")
        self.untouched = rb.tree(self.home)
        self.fs = copy.deepcopy(FS)
        self.enterContext(patch.object(w.fs_identity, "identity", side_effect=lambda fd: copy.deepcopy(self.fs)))
        self.boot = "boot-before"
        self.enterContext(patch.object(rb, "boot_id", side_effect=lambda: self.boot))
        self.base = self.home / rb.NAME
        self.ws = self.base / "ws"

    def run_action(self, action):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rb.main([action, "--base", str(self.base)])
        return code, out.getvalue()

    def prepared(self):
        code, output = self.run_action("prepare")
        self.assertEqual(code, 0, output)
        evidence = json.loads((self.base / "evidence.json").read_text())
        return evidence, self.ws / w.STATE / evidence["journal"]

    def restart(self):
        self.boot = "boot-after"

    def as_if_dev_was(self, evidence, journal, old):
        """Rewrite the recorded runtime device as it was before the 'remount'."""
        value = json.loads(journal.read_text())
        current = value["before"]["root"][0]

        def walk(node):
            if isinstance(node, dict):
                for key, item in node.items():
                    if key in ("root", "parent", "inode") and isinstance(item, list) and item[0] == current:
                        item[0] = old
                    else:
                        walk(item)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
        walk(value)
        journal.write_text(canonical(value))
        evidence["dev"] = old
        evidence["journal_sha256"] = rb.sha(journal.read_bytes())
        (self.base / "evidence.json").write_text(json.dumps(evidence))

    def assert_home_untouched(self):
        after = {k: v for k, v in rb.tree(self.home).items() if not k.startswith(rb.NAME)}
        self.assertEqual(after, self.untouched)

    def test_prepare_records_a_new_schema_3_task_and_refuses_repetition(self):
        evidence, journal = self.prepared()
        value = json.loads(journal.read_text())
        self.assertEqual((value["schema"], value["status"], value["before"]["fs"]), (3, "applied", FS))
        self.assertEqual((self.ws / rb.TARGET).read_text(), rb.CHANGED)
        self.assertEqual(oct((self.ws / rb.TARGET).stat().st_mode & 0o777), oct(rb.MODE))
        self.assertEqual(evidence["boot_id"], "boot-before")
        before = rb.tree(self.base)
        code, output = self.run_action("prepare")
        self.assertEqual(code, 2)
        self.assertEqual(rb.tree(self.base), before)
        self.assert_home_untouched()

    def test_prepare_without_durable_identity_creates_nothing(self):
        self.fs = None
        code, output = self.run_action("prepare")
        self.assertEqual(code, 2)
        self.assertFalse(self.base.exists())
        self.assert_home_untouched()

    def test_base_other_than_the_fixed_directory_is_refused(self):
        for path in (self.home, self.home / "other", self.home / "x" / rb.NAME):
            with self.assertRaises(SystemExit):
                rb.main(["prepare", "--base", str(path)])
        self.assert_home_untouched()

    def test_verify_before_restart_changes_nothing(self):
        self.prepared()
        before = rb.tree(self.base)
        code, output = self.run_action("verify")
        self.assertEqual(code, 3)
        self.assertIn("RESULT=NO_RESTART", output)
        self.assertEqual(rb.tree(self.base), before)

    def test_restart_without_device_change_is_not_reported_as_rebound(self):
        self.prepared()
        self.restart()
        code, output = self.run_action("verify")
        self.assertEqual(code, 0, output)
        self.assertIn("RESULT=RESTART_CHECKED_REBOUND_NOT_OBSERVED", output)
        self.assertNotIn("REBOUND_OBSERVED\n", output.replace("NOT_OBSERVED", ""))
        self.assertEqual((self.ws / rb.TARGET).read_text(), rb.ORIGINAL)
        self.assertEqual(oct((self.ws / rb.TARGET).stat().st_mode & 0o777), oct(rb.MODE))
        self.assert_home_untouched()

    def test_restart_with_device_change_is_rebound_and_rolls_back_exactly(self):
        evidence, journal = self.prepared()
        self.restart()
        self.as_if_dev_was(evidence, journal, os.stat(self.ws).st_dev ^ 0x55)
        code, output = self.run_action("verify")
        self.assertEqual(code, 0, output)
        self.assertIn("RESULT=REBOUND_OBSERVED", output)
        self.assertEqual((self.ws / rb.TARGET).read_text(), rb.ORIGINAL)
        self.assertEqual(oct((self.ws / rb.TARGET).stat().st_mode & 0o777), oct(rb.MODE))
        self.assertEqual(json.loads(journal.read_text())["status"], "rolled-back")
        self.assert_home_untouched()

    def test_other_filesystem_identity_fails_without_rollback(self):
        _, journal = self.prepared()
        self.restart()
        self.fs = dict(FS, tree=258)
        saved = journal.read_bytes()
        code, output = self.run_action("verify")
        self.assertEqual(code, 1)
        self.assertEqual((self.ws / rb.TARGET).read_text(), rb.CHANGED)
        self.assertEqual(journal.read_bytes(), saved)

    def test_user_change_or_journal_change_fails_and_keeps_files(self):
        evidence, journal = self.prepared()
        self.restart()
        (self.ws / rb.TARGET).write_text("USER EDIT\n")
        self.assertEqual(self.run_action("verify")[0], 1)
        self.assertEqual((self.ws / rb.TARGET).read_text(), "USER EDIT\n")
        (self.ws / rb.TARGET).write_text(rb.CHANGED)
        os.chmod(self.ws / rb.TARGET, rb.MODE)
        journal.write_text(journal.read_text() + "\n")
        self.assertEqual(self.run_action("verify")[0], 1)
        self.assertEqual((self.ws / rb.TARGET).read_text(), rb.CHANGED)

    def test_invalid_evidence_fails_closed(self):
        self.prepared()
        self.restart()
        path = self.base / "evidence.json"
        value = json.loads(path.read_text())
        for mutate in (lambda v: v.update(extra=1), lambda v: v.update(base="/elsewhere"),
                       lambda v: v.pop("plan"), lambda v: v.update(version=2)):
            fresh = copy.deepcopy(value)
            mutate(fresh)
            path.write_text(json.dumps(fresh))
            self.assertEqual(self.run_action("verify")[0], 1)
            self.assertEqual((self.ws / rb.TARGET).read_text(), rb.CHANGED)
        path.write_text("not json")
        self.assertEqual(self.run_action("verify")[0], 1)

    def test_cleanup_requires_a_completed_rollback_and_removes_only_the_base(self):
        self.prepared()
        code, output = self.run_action("cleanup")
        self.assertEqual(code, 2)
        self.assertTrue(self.base.exists())
        self.restart()
        self.assertEqual(self.run_action("verify")[0], 0)
        self.assertEqual(self.run_action("cleanup")[0], 0)
        self.assertFalse(self.base.exists())
        self.assert_home_untouched()

    def test_symlinked_base_is_refused(self):
        other = self.home / "elsewhere"
        other.mkdir()
        self.base.symlink_to(other)
        for action in ("prepare", "verify", "cleanup"):
            with self.assertRaises(SystemExit):
                rb.main([action, "--base", str(self.base)])
        self.assertEqual(list(other.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
