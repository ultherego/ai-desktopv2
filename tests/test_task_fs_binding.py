"""E5 durable binding of task history to a filesystem/subvolume identity.

Real private temporary files; the Btrfs identity is replaced by a fake so the
contract is exercised on any filesystem. Drift of the anonymous device number
is simulated by editing the journal. Real Btrfs ioctls: host_e5_btrfs_binding.py.
"""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import fs_identity, task_cli, task_workspace as w
from ai_desktop.analysis_contract import canonical
from ai_desktop.task_plan import TaskPlan
from ai_desktop.terminal_consent import PROMPTS

FS = {"kind": "btrfs", "fsid": "a" * 32, "tree": 257, "uuid": "b" * 32}
OTHER_SUBVOLUME = dict(FS, tree=258, uuid="c" * 32)
OTHER_FILESYSTEM = dict(FS, fsid="d" * 32)


class Base(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.geteuid(), 0, "run filesystem regressions as a regular user")
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ai-fsbind-")))
        self.identity = copy.deepcopy(FS)
        patcher = patch.object(w.fs_identity, "identity", side_effect=lambda fd: copy.deepcopy(self.identity))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.target = self.root / "one.txt"
        self.target.write_text("ORIGINAL\n")
        self.target.chmod(0o640)
        self.plan = self.apply("CHANGED\n")
        self.journal = self.root / w.STATE / (self.plan.sha256 + ".json")
        self.original = self.load()

    def apply(self, content):
        before = w.snapshot(str(self.root), ["one.txt"])
        source = {"instruction": "synthetic", "files": [
            {"path": "one.txt", "content": before["files"]["one.txt"]["file"]["content"], "writable": True}]}
        plan = TaskPlan(before, source, {"summary": "synthetic", "files": [
            {"path": "one.txt", "content": content}]}, "text")
        w.apply(plan, plan.authorize("SAVE " + plan.sha256))
        return plan

    def load(self):
        return json.loads(self.journal.read_text())

    def store(self, value):
        self.journal.write_text(canonical(value))

    def drift(self, *, fs="keep", status=None):
        """Simulate a remount: every runtime [dev, ino] gets another device."""
        value = self.load()
        old = value["before"]["root"][0]

        def walk(node):
            if isinstance(node, dict):
                for key, item in node.items():
                    if key in ("root", "parent", "inode") and isinstance(item, list) and item[0] == old:
                        item[0] = old ^ 0x55
                    else:
                        walk(item)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
        walk(value)
        if fs != "keep":
            value["before"]["fs"] = fs
        if status:
            value["status"] = status
        self.store(value)
        return value

    def state(self):
        info = self.target.stat()
        return self.target.read_bytes(), stat.S_IMODE(info.st_mode)


class FsBindingTests(Base):
    def test_new_journal_records_durable_identity_and_plan_binds_it(self):
        value = self.load()
        self.assertEqual((value["schema"], value["before"]["fs"]), (3, FS))
        before = w.snapshot(str(self.root), ["one.txt"])
        self.assertEqual(before["fs"], FS)
        source = {"instruction": "s", "files": [{"path": "one.txt", "content": "CHANGED\n", "writable": True}]}
        proposal = {"summary": "s", "files": [{"path": "one.txt", "content": "X\n"}]}
        first = TaskPlan(before, source, proposal, "text")
        self.identity = OTHER_SUBVOLUME
        second = TaskPlan(w.snapshot(str(self.root), ["one.txt"]), source, proposal, "text")
        self.assertEqual(first.data()["before"]["fs"], FS)
        self.assertNotEqual(first.sha256, second.sha256)

    def test_apply_refuses_when_identity_changes_after_approval(self):
        before = w.snapshot(str(self.root), ["one.txt"])
        source = {"instruction": "s", "files": [{"path": "one.txt", "content": "CHANGED\n", "writable": True}]}
        plan = TaskPlan(before, source, {"summary": "s", "files": [{"path": "one.txt", "content": "N\n"}]}, "text")
        approval = plan.authorize("SAVE " + plan.sha256)
        self.identity = OTHER_SUBVOLUME
        with self.assertRaises(ValueError):
            w.apply(plan, approval)
        self.assertEqual(self.target.read_text(), "CHANGED\n")

    def test_unchanged_history_needs_no_rebinding(self):
        record = w.recovery_records(str(self.root))[0]
        self.assertEqual(record["status"], "applied")
        self.assertNotIn("rebound", record)

    def test_device_drift_with_same_subvolume_is_rebound_and_rolls_back(self):
        self.drift()
        record = w.recovery_records(str(self.root))[0]
        self.assertEqual((record["status"], record["rebound"]), ("applied", True))
        self.assertEqual(record["files"], [{"path": "one.txt", "action": "restore"}])
        self.assertEqual(w.status(str(self.root)), [(self.plan.sha256, "applied")])
        self.assertTrue(w.rollback(str(self.root), self.plan.sha256,
                                   expected_journal=record["journal_sha256"]))
        self.assertEqual(self.state(), (b"ORIGINAL\n", 0o640))
        value = self.load()
        self.assertEqual(value["status"], "rolled-back")
        self.assertEqual(value["before"]["root"][0], os.stat(self.root).st_dev)
        self.assertEqual(w.recovery_records(str(self.root))[0]["status"], "rolled-back")

    def test_rebound_history_blocks_new_plans_only_while_unfinished(self):
        self.drift(status="prepared")
        with self.assertRaises(ValueError):
            w._can_apply(str(self.root))
        self.assertTrue(w.rollback(str(self.root), self.plan.sha256))
        self.assertEqual(self.state(), (b"ORIGINAL\n", 0o640))
        self.assertEqual(w._can_apply(str(self.root)), 0)

    def test_other_subvolume_or_snapshot_is_detached_even_if_device_is_reused(self):
        for label, other in (("subvolume", OTHER_SUBVOLUME), ("filesystem", OTHER_FILESYSTEM), ("not-btrfs", None)):
            for drifted in (True, False):
                with self.subTest(other=label, device_drift=drifted):
                    self.store(copy.deepcopy(self.original))
                    if drifted:
                        self.drift()
                    self.identity = other
                    saved = self.journal.read_bytes()
                    record = w.recovery_records(str(self.root))[0]
                    self.assertEqual((record["status"], record["files"]), ("detached", []))
                    self.assertNotIn("rebound", record)
                    with self.assertRaisesRegex(ValueError, "root"):
                        w.rollback(str(self.root), self.plan.sha256)
                    self.assertEqual(self.journal.read_bytes(), saved)
                    self.assertEqual(self.state(), (b"CHANGED\n", 0o640))
                    self.identity = copy.deepcopy(FS)

    def test_unfinished_history_of_another_subvolume_blocks_instead_of_hiding(self):
        self.drift(status="prepared")
        self.identity = OTHER_SUBVOLUME
        with self.assertRaises(ValueError):
            w.recovery_records(str(self.root))
        with self.assertRaises(ValueError):
            w._can_apply(str(self.root))

    def test_copied_directory_with_other_root_inode_is_refused(self):
        value = self.load()
        value["before"]["root"][1] += 1
        self.store(value)
        with self.assertRaises(ValueError):
            w.recovery_records(str(self.root))
        with self.assertRaises(ValueError):
            w.rollback(str(self.root), self.plan.sha256)

    def test_other_workspace_path_is_refused(self):
        value = self.load()
        value["before"]["workspace"] = str(self.root) + "-copy"
        self.store(value)
        with self.assertRaises(ValueError):
            w.recovery_records(str(self.root))

    def test_later_user_edit_is_a_conflict_after_rebinding(self):
        self.drift()
        self.target.write_text("USER EDIT\n")
        self.assertFalse(w.rollback(str(self.root), self.plan.sha256))
        self.assertEqual(self.target.read_text(), "USER EDIT\n")
        self.assertEqual(self.load()["status"], "conflict")

    def test_same_bytes_and_mode_in_a_different_file_object_roll_back(self):
        # An editor saving by rename or an earlier rollback gives a new inode; nothing newer can be lost.
        self.drift()
        with open(self.target, "rb"):  # keep the old inode number from being reused
            self.target.unlink()
            self.target.write_text("CHANGED\n")
            self.target.chmod(0o640)
            self.assertTrue(w.rollback(str(self.root), self.plan.sha256))
        self.assertNotEqual(self.state(), (b"CHANGED\n", 0o640))

    def test_same_bytes_with_another_mode_stay_a_conflict(self):
        self.drift()
        self.target.chmod(0o600)
        before = self.state()
        self.assertFalse(w.rollback(str(self.root), self.plan.sha256))
        self.assertEqual(self.state(), before)

    def test_symlink_and_fifo_in_place_of_target_are_not_followed_or_blocking(self):
        for make in ("symlink", "fifo"):
            with self.subTest(make=make):
                self.setUp()
                self.drift()
                outside = self.root / "outside.txt"
                outside.write_text("CHANGED\n")
                self.target.unlink()
                if make == "symlink":
                    os.symlink(outside, self.target)
                else:
                    os.mkfifo(self.target)
                self.assertFalse(w.rollback(str(self.root), self.plan.sha256))
                self.assertTrue(self.target.is_symlink() or stat.S_ISFIFO(self.target.lstat().st_mode))
                self.assertEqual(outside.read_text(), "CHANGED\n")

    def test_device_numbers_of_other_devices_are_not_adopted_by_rebase(self):
        value = self.load()
        old = value["before"]["root"][0]
        value["before"]["files"]["one.txt"]["parent"][0] = old ^ 0x1234   # e.g. nested subvolume
        w._rebase(value, 777)
        self.assertEqual(value["before"]["root"][0], 777)
        self.assertEqual(value["before"]["files"]["one.txt"]["parent"][0], old ^ 0x1234)
        self.assertEqual(value["before"]["files"]["one.txt"]["file"]["inode"][0], 777)

    def test_interrupted_rollback_after_rebinding_can_be_retried(self):
        self.drift()
        real = os.replace
        calls = []

        def fail_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("synthetic failure before publishing the undo")
            return real(*args, **kwargs)
        with patch.object(w.os, "replace", side_effect=fail_once):
            self.assertFalse(w.rollback(str(self.root), self.plan.sha256))
        self.assertEqual(self.load()["status"], "conflict")
        self.assertEqual(self.target.read_text(), "CHANGED\n")


class PublicCliTests(Base):
    """The owner's actual entry points: task status and task rollback."""

    def run_cli(self, action, answers=()):
        out, replies, prompts = io.StringIO(), iter(answers), []

        def reader(prompt):
            prompts.append(prompt)
            return next(replies)
        with contextlib.redirect_stdout(out), \
                patch.object(task_cli.sys.stdin, "isatty", return_value=True), \
                patch.object(task_cli.sys.stdout, "isatty", return_value=True):
            code = task_cli.main([action, "--workspace", str(self.root)], reader=reader)
        return code, out.getvalue(), prompts

    def test_status_after_device_drift_is_a_normal_stored_task(self):
        self.drift()
        code, output, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("State: saved", output)
        self.assertIn("Restore original: one.txt", output)
        self.assertNotIn(w.DETACHED_HISTORY_HINT, output)
        self.assertNotIn("unlinked", output)

    def test_rollback_after_device_drift_restores_exact_bytes_and_mode(self):
        self.drift()
        code, output, prompts = self.run_cli("rollback", ["YES"])
        self.assertEqual((code, prompts), (0, [PROMPTS["rollback"]]), output)
        self.assertIn("Changes undone.", output)
        self.assertEqual(self.state(), (b"ORIGINAL\n", 0o640))
        self.assertEqual(self.load()["status"], "rolled-back")

    def test_declined_rollback_after_device_drift_changes_nothing(self):
        self.drift()
        saved = self.journal.read_bytes()
        code, output, _ = self.run_cli("rollback", ["no"])
        self.assertEqual(code, 2)
        self.assertEqual(self.state(), (b"CHANGED\n", 0o640))
        self.assertEqual(self.journal.read_bytes(), saved)

    def test_foreign_subvolume_history_is_reported_and_never_offered_for_rollback(self):
        self.drift()
        self.identity = OTHER_SUBVOLUME
        saved = self.journal.read_bytes()
        code, output, prompts = self.run_cli("rollback")
        self.assertEqual((code, prompts), (2, []))
        self.assertIn(w.DETACHED_HISTORY_HINT, output)
        self.assertEqual(self.state(), (b"CHANGED\n", 0o640))
        self.assertEqual(self.journal.read_bytes(), saved)
        code, output, _ = self.run_cli("status")
        self.assertIn("unlinked history", output)


class JournalSchemaTests(Base):
    def refused(self, mutate):
        value = self.load()
        mutate(value)
        self.store(value)
        with self.assertRaises(ValueError):
            w.recovery_records(str(self.root))

    def test_malformed_durable_identity_is_refused(self):
        bad = [
            lambda v: v["before"].pop("fs"),
            lambda v: v["before"].update(fs={"kind": "btrfs"}),
            lambda v: v["before"].update(fs=dict(FS, kind="ext4")),
            lambda v: v["before"].update(fs=dict(FS, fsid="A" * 32)),
            lambda v: v["before"].update(fs=dict(FS, fsid="0" * 32)),
            lambda v: v["before"].update(fs=dict(FS, tree=0)),
            lambda v: v["before"].update(fs=dict(FS, tree=True)),
            lambda v: v["before"].update(fs=dict(FS, tree=2 ** 64)),
            lambda v: v["before"].update(fs=dict(FS, uuid="b" * 31)),
            lambda v: v["before"].update(fs=dict(FS, extra=1)),
            lambda v: v["before"].update(fs="btrfs"),
            lambda v: v.update(schema=2),   # schema 2 must not carry the new key
            lambda v: v.update(schema=4),
        ]
        for index, mutate in enumerate(bad):
            with self.subTest(index=index):
                self.refused(mutate)
                self.store(copy.deepcopy(self.original))

    def test_unsupported_filesystem_records_null_and_keeps_strict_device_binding(self):
        self.identity = None
        second = self.apply("SECOND\n")
        journal = self.root / w.STATE / (second.sha256 + ".json")
        self.assertIsNone(json.loads(journal.read_text())["before"]["fs"])
        value = json.loads(journal.read_text())
        old = value["before"]["root"][0]
        value["before"]["root"][0] = old ^ 1
        journal.write_text(canonical(value))
        records = {r["plan"]: r for r in w.recovery_records(str(self.root))}
        self.assertEqual(records[second.sha256]["status"], "detached")


class IdentityModuleTests(unittest.TestCase):
    def fake_ioctl(self, fsid=b"\x11" * 16, tree=257, uuid=b"\x22" * 16):
        import struct

        def ioctl(fd, request, buffer, mutate):
            if request == fs_identity.FS_INFO:
                buffer[16:32] = fsid
            elif request == fs_identity.SUBVOL_INFO:
                struct.pack_into("<Q", buffer, 0, tree)
                buffer[296:312] = uuid
            else:
                raise OSError("unexpected request")
        return ioctl

    def test_request_numbers_match_the_documented_btrfs_ioctls(self):
        self.assertEqual(fs_identity.FS_INFO, 0x8400941F)
        self.assertEqual(fs_identity.SUBVOL_INFO, 0x81F8943C)

    def test_parses_fixed_offsets(self):
        with patch.object(fs_identity, "_is_btrfs", return_value=True), \
                patch.object(fs_identity.fcntl, "ioctl", side_effect=self.fake_ioctl()):
            self.assertEqual(fs_identity.identity(0), {
                "kind": "btrfs", "fsid": "11" * 16, "tree": 257, "uuid": "22" * 16})

    def test_unknown_or_failing_sources_give_none(self):
        with patch.object(fs_identity, "_is_btrfs", return_value=False):
            self.assertIsNone(fs_identity.identity(0))
        with patch.object(fs_identity, "_is_btrfs", return_value=True):
            for fake in (self.fake_ioctl(fsid=b"\0" * 16), self.fake_ioctl(tree=0)):
                with patch.object(fs_identity.fcntl, "ioctl", side_effect=fake):
                    self.assertIsNone(fs_identity.identity(0))
            with patch.object(fs_identity.fcntl, "ioctl", side_effect=OSError(25, "not a btrfs")):
                self.assertIsNone(fs_identity.identity(0))

    def test_top_level_subvolume_without_uuid_is_still_identified(self):
        with patch.object(fs_identity, "_is_btrfs", return_value=True), \
                patch.object(fs_identity.fcntl, "ioctl", side_effect=self.fake_ioctl(tree=5, uuid=b"\0" * 16)):
            result = fs_identity.identity(0)
        self.assertEqual((result["tree"], result["uuid"]), (5, "0" * 32))
        self.assertTrue(fs_identity.valid(result))

    def test_non_btrfs_directory_has_no_identity(self):
        with tempfile.TemporaryDirectory(prefix="ai-fsid-") as path:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                result = fs_identity.identity(fd)
            finally:
                os.close(fd)
        self.assertTrue(result is None or fs_identity.valid(result))

    def test_valid_accepts_only_exact_shape(self):
        self.assertTrue(fs_identity.valid(None))
        self.assertTrue(fs_identity.valid(FS))
        for bad in ({}, [], "x", dict(FS, kind="zfs"), dict(FS, tree=-1), dict(FS, fsid="g" * 32)):
            self.assertFalse(fs_identity.valid(bad))


if __name__ == "__main__":
    unittest.main()
