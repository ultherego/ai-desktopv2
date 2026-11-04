"""E5 history binding against the REAL Btrfs identity of the owner's HOME.

Read-only ioctls plus one private temporary directory in HOME that is removed
afterwards. No subvolume, snapshot, mount or privileged operation is created;
remount-driven device drift is simulated by editing the temporary journal, so
this does NOT replace an observation after a real reboot (see docs/KNOWN_LIMITATIONS.md).
"""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

from ai_desktop import fs_identity, task_workspace as w
from ai_desktop.analysis_contract import canonical
from ai_desktop.task_plan import TaskPlan


def real_fs(path):
    return subprocess.run(["stat", "-f", "-c", "%T", str(path)], capture_output=True,
                          text=True, check=True).stdout.strip()


class RealBtrfsBindingTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.geteuid(), 0, "run as the regular owner")
        home = Path.home()
        self.assertEqual(real_fs(home), "btrfs", "ten test wymaga HOME na Btrfs")
        self.root = Path(tempfile.mkdtemp(prefix="ai-btrfs-bind-", dir=home))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.root.chmod(0o700)
        self.target = self.root / "one.txt"
        self.target.write_text("ORIGINAL\n")
        self.target.chmod(0o640)
        before = w.snapshot(str(self.root), ["one.txt"])
        source = {"instruction": "synthetic", "files": [
            {"path": "one.txt", "content": "ORIGINAL\n", "writable": True}]}
        self.plan = TaskPlan(before, source, {"summary": "synthetic", "files": [
            {"path": "one.txt", "content": "CHANGED\n"}]}, "text")
        w.apply(self.plan, self.plan.authorize("ZAPISZ " + self.plan.sha256))
        self.journal = self.root / w.STATE / (self.plan.sha256 + ".json")

    def drifted(self, mutate=None):
        value = json.loads(self.journal.read_text())
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
        if mutate:
            mutate(value)
        self.journal.write_text(canonical(value))

    def test_real_identity_is_valid_stable_and_recorded(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            first, second = fs_identity.identity(fd), fs_identity.identity(fd)
        finally:
            os.close(fd)
        self.assertIsNotNone(first)
        self.assertTrue(fs_identity.valid(first))
        self.assertEqual(first, second)
        value = json.loads(self.journal.read_text())
        self.assertEqual((value["schema"], value["before"]["fs"]), (3, first))

    def test_remount_style_device_drift_is_rebound_and_rolled_back_exactly(self):
        self.drifted()
        record = w.recovery_records(str(self.root))[0]
        self.assertEqual((record["status"], record["rebound"]), ("applied", True))
        self.assertTrue(w.rollback(str(self.root), self.plan.sha256,
                                   expected_journal=record["journal_sha256"]))
        self.assertEqual(self.target.read_bytes(), b"ORIGINAL\n")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o640)
        self.assertEqual(w.recovery_records(str(self.root))[0]["status"], "rolled-back")

    def test_journal_of_another_subvolume_or_snapshot_is_never_adopted(self):
        def other(value):
            fs = value["before"]["fs"]
            fs["tree"] += 1
            fs["uuid"] = "e" * 32
        self.drifted(other)
        saved = self.journal.read_bytes()
        self.assertEqual(w.recovery_records(str(self.root))[0]["status"], "detached")
        with self.assertRaises(ValueError):
            w.rollback(str(self.root), self.plan.sha256)
        self.assertEqual(self.journal.read_bytes(), saved)
        self.assertEqual(self.target.read_bytes(), b"CHANGED\n")


if __name__ == "__main__":
    unittest.main()
