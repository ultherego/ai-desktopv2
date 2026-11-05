"""Real process-death recovery on fresh workspaces; run as the normal CI UID."""
import contextlib
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import task_workspace as workspace
from ai_desktop.analysis_contract import canonical
from ai_desktop.task_plan import TaskPlan


CRASH = r'''
import json, os, sys
from pathlib import Path
from ai_desktop import task_workspace as w
root, plan, phase = sys.argv[1:]
value = json.loads((Path(root) / w.STATE / (plan + '.json')).read_text())
stage = value['entries'][0]['undo_temp']
real_write, real_save, real_exchange, real_oswrite = w._write, w._save, w.rename_exchange, w.os.write
def write(parent, name, data, mode=0o600):
    if name == stage and phase == 'before-write': os._exit(77)
    result = real_write(parent, name, data, mode)
    if name == stage and phase == 'after-write': os._exit(77)
    return result
def save(fd, name, value, **kwargs):
    real_save(fd, name, value, **kwargs)
    if phase == 'after-identity' and value['entries'][0]['undo'] is not None: os._exit(77)
def exchange(src_dir, src, dst_dir, dst):
    real_exchange(src_dir, src, dst_dir, dst)
    if dst == 'one.txt' and phase == 'after-rename': os._exit(77)
def oswrite(fd, data):
    if phase == 'partial-write' and data == b'ORIGINAL\n':
        real_oswrite(fd, data[:2])
        os._exit(77)
    return real_oswrite(fd, data)
w._write, w._save, w.rename_exchange, w.os.write = write, save, exchange, oswrite
w.rollback(root, plan)
'''


class TaskRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-task-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def plan(self, root, names, content="CHANGED\n"):
        before = workspace.snapshot(str(root), names)
        source = {"instruction": "synthetic", "files": [
            {"path": name, "content": item["file"]["content"], "writable": True}
            for name, item in before["files"].items()]}
        return TaskPlan(before, source, {"summary": "synthetic", "files": [
            {"path": name, "content": content} for name in names]}, "python-syntax")

    def applied(self, label="case", names=("one.txt",)):
        root = self.root / label
        root.mkdir(mode=0o700)
        for name in names:
            (root / name).write_text("ORIGINAL\n")
            (root / name).chmod(0o640)
        plan = self.plan(root, names)
        workspace.apply(plan, plan.authorize("SAVE " + plan.sha256))
        journal = root / workspace.STATE / (plan.sha256 + ".json")
        return root, plan, journal

    def crash(self, root, plan, phase):
        done = subprocess.run([sys.executable, "-B", "-c", CRASH, str(root), plan.sha256, phase],
                              capture_output=True, timeout=15)
        self.assertEqual(done.returncode, 77, done.stderr.decode(errors="replace"))

    def legacy(self, journal):
        value = json.loads(journal.read_text())
        value["schema"] = 1
        del value["before"]["fs"]  # schema 1 predates the durable filesystem identity
        for entry in value["entries"]:
            del entry["undo_temp"]
        journal.write_text(canonical(value))
        return value

    def test_undo_crash_cutpoints_have_durable_names_and_truthful_retry(self):
        for phase in ("before-write", "partial-write", "after-write", "after-identity", "after-rename"):
            with self.subTest(phase=phase):
                root, plan, journal = self.applied(phase)
                value = json.loads(journal.read_text())
                self.assertEqual(value["schema"], 3)
                stage = root / value["entries"][0]["undo_temp"]
                self.crash(root, plan, phase)
                value = json.loads(journal.read_text())
                self.assertEqual(value["entries"][0]["undo_temp"], stage.name)
                retry = workspace.rollback(str(root), plan.sha256)
                unknown = phase in ("partial-write", "after-write")
                self.assertEqual(retry, not unknown)
                if unknown:
                    self.assertEqual(workspace.status(str(root)), [(plan.sha256, "conflict")])
                    self.assertEqual((root / "one.txt").read_text(), "CHANGED\n")
                    self.assertTrue(stage.exists())
                    self.assertEqual(stage.read_bytes(), b"OR" if phase == "partial-write" else b"ORIGINAL\n")
                    self.assertFalse(workspace.rollback(str(root), plan.sha256))
                    self.assertTrue(stage.exists())
                else:
                    self.assertEqual((root / "one.txt").read_text(), "ORIGINAL\n")
                    self.assertEqual(workspace.status(str(root)), [(plan.sha256, "rolled-back")])
                    self.assertEqual(list(root.glob(".ai-task-*")), [])
                    self.assertTrue(workspace.rollback(str(root), plan.sha256))

    def test_recorded_undo_stage_replacement_is_preserved_and_never_published(self):
        root, plan, journal = self.applied()
        self.crash(root, plan, "after-identity")
        value = json.loads(journal.read_text())
        stage = root / value["entries"][0]["undo_temp"]
        foreign = root / "foreign.txt"
        foreign.write_text("ORIGINAL\n")
        foreign.chmod(0o640)
        foreign_inode = foreign.stat().st_ino
        os.replace(foreign, stage)
        self.assertFalse(workspace.rollback(str(root), plan.sha256))
        self.assertEqual(stage.stat().st_ino, foreign_inode)
        self.assertEqual(stage.read_text(), "ORIGINAL\n")
        self.assertEqual((root / "one.txt").read_text(), "CHANGED\n")

    def test_unknown_undo_name_collision_is_not_overwritten_or_deleted(self):
        root, plan, journal = self.applied()
        stage = root / json.loads(journal.read_text())["entries"][0]["undo_temp"]
        stage.write_text("FOREIGN\n")
        self.assertFalse(workspace.rollback(str(root), plan.sha256))
        self.assertEqual(stage.read_text(), "FOREIGN\n")
        self.assertEqual((root / "one.txt").read_text(), "CHANGED\n")
        self.assertIsNone(json.loads(journal.read_text())["entries"][0]["undo"])

    def test_clean_legacy_migrates_after_approved_digest_check_and_rolls_back(self):
        root, plan, journal = self.applied()
        self.legacy(journal)
        record = workspace.recovery_records(str(root))[0]
        with patch.object(workspace, "_upgrade_legacy") as upgrade:
            with self.assertRaisesRegex(ValueError, "journal changed"):
                workspace.rollback(str(root), plan.sha256, expected_journal="0" * 64)
            upgrade.assert_not_called()
        self.assertTrue(workspace.rollback(str(root), plan.sha256, expected_journal=record["journal_sha256"]))
        self.assertEqual(json.loads(journal.read_text())["schema"], 2)
        self.assertEqual((root / "one.txt").read_text(), "ORIGINAL\n")
        self.assertEqual(list(root.glob(".ai-task-*")), [])

    def test_legacy_unknown_stage_is_not_read_adopted_or_deleted_and_never_passes(self):
        root, plan, journal = self.applied()
        self.legacy(journal)
        orphan = root / (".ai-task-" + "f" * 24)
        orphan.write_text("SYNTHETIC PRIVATE ORIGINAL\n")
        with patch.object(workspace, "read_file", wraps=workspace.read_file) as read:
            self.assertFalse(workspace.rollback(str(root), plan.sha256))
            self.assertNotIn(orphan.name, [call.args[1] for call in read.call_args_list])
        self.assertFalse(workspace.rollback(str(root), plan.sha256))
        self.assertEqual(json.loads(journal.read_text())["schema"], 1)
        self.assertEqual(workspace.status(str(root)), [(plan.sha256, "conflict")])
        self.assertEqual(orphan.read_text(), "SYNTHETIC PRIVATE ORIGINAL\n")
        self.assertEqual((root / "one.txt").read_text(), "CHANGED\n")

    def test_failed_legacy_name_allocation_keeps_journal_valid_and_unchanged_targets(self):
        root, plan, journal = self.applied(names=("one.txt", "two.txt"))
        self.legacy(journal)
        with patch.object(workspace, "_stage_name", side_effect=[".ai-task-" + "e" * 24, ValueError("collision")]):
            self.assertFalse(workspace.rollback(str(root), plan.sha256))
        value = json.loads(journal.read_text())
        self.assertEqual(value["schema"], 1)
        self.assertTrue(all("undo_temp" not in entry for entry in value["entries"]))
        self.assertEqual(workspace.status(str(root)), [(plan.sha256, "conflict")])
        self.assertEqual((root / "one.txt").read_text(), "CHANGED\n")
        self.assertEqual((root / "two.txt").read_text(), "CHANGED\n")
        self.assertEqual(list(root.glob(".ai-task-*")), [])

    def test_legacy_directory_name_scan_is_bounded(self):
        root, plan, journal = self.applied()
        self.legacy(journal)
        names = iter([SimpleNamespace(name="ordinary-file")] * (workspace.MAX_LEGACY_PARENT_ENTRIES + 1))
        with patch.object(workspace.os, "scandir", return_value=contextlib.nullcontext(names)):
            self.assertFalse(workspace.rollback(str(root), plan.sha256))
        self.assertEqual(json.loads(journal.read_text())["schema"], 1)
        self.assertEqual((root / "one.txt").read_text(), "CHANGED\n")

    def test_full_history_refuses_129th_journal_and_preserves_status_and_rollback(self):
        root, plan, journal = self.applied()
        value = json.loads(journal.read_text())
        for index in range(workspace.MAX_JOURNALS - 1):
            historical = dict(value, plan=f"{index:064x}")
            destination = journal.parent / (historical["plan"] + ".json")
            destination.write_text(canonical(historical))
            destination.chmod(0o600)
        self.assertEqual(len(workspace.recovery_records(str(root))), 128)
        candidate = self.plan(root, ("one.txt",), content="NEXT\n")
        with patch.object(workspace, "_save") as save, self.assertRaises(workspace.JournalLimit):
            workspace.apply(candidate, candidate.authorize("SAVE " + candidate.sha256))
        save.assert_not_called()
        self.assertEqual((root / "one.txt").read_text(), "CHANGED\n")
        self.assertEqual(len(workspace.recovery_records(str(root))), 128)
        self.assertTrue(workspace.rollback(str(root), plan.sha256))
        self.assertEqual((root / "one.txt").read_text(), "ORIGINAL\n")

    def test_history_capacity_is_rechecked_under_lock_before_new_journal(self):
        root, _, _ = self.applied()
        plan = self.plan(root, ("one.txt",), content="NEXT\n")
        with patch.object(workspace, "recovery_records", side_effect=[[], [{"status": "applied"}] * 128]), \
                patch.object(workspace, "_save") as save, self.assertRaises(workspace.JournalLimit):
            workspace.apply(plan, plan.authorize("SAVE " + plan.sha256))
        save.assert_not_called()
        self.assertEqual((root / "one.txt").read_text(), "CHANGED\n")


class TaskJournalRuleTests(unittest.TestCase):
    """Pure checks can run even where the filesystem executor rejects root."""
    def test_history_limit_and_recovery_state_are_separate_refusals(self):
        for records, error in (([{"status": "applied"}] * 128, workspace.JournalLimit),
                               ([{"status": "prepared"}], ValueError), ([{"status": "conflict"}], ValueError)):
            with patch.object(workspace, "recovery_records", return_value=records), self.assertRaises(error):
                workspace._can_apply("unused")
        with patch.object(workspace, "recovery_records", return_value=[{"status": "rolled-back"}] * 127):
            workspace._can_apply("unused")

    def test_stage_names_never_reuse_a_known_name_and_collision_attempts_are_bounded(self):
        taken = {".ai-task-" + "a" * 24}
        with patch.object(workspace.secrets, "token_hex", side_effect=["a" * 24, "b" * 24]):
            self.assertEqual(workspace._stage_name(taken), ".ai-task-" + "b" * 24)
        with patch.object(workspace.secrets, "token_hex", return_value="a" * 24) as token:
            with self.assertRaises(ValueError):
                workspace._stage_name(taken)
            self.assertEqual(token.call_count, 8)


if __name__ == "__main__":
    unittest.main()
