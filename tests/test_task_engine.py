"""Task limits, safe config roots, guarded mutation and recoverable journals."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import task_contract as contract, task_workspace as workspace
from ai_desktop import task_sandbox, task_child, workspace_executor
from ai_desktop.analysis_contract import canonical
from ai_desktop.task_plan import TaskPlan


def plan_for(root, names, contents):
    before = workspace.snapshot(str(root), names)
    source = {"instruction": "Synthetic edit", "files": [
        {"path": name, "content": item["file"]["content"] if item["file"] else None, "writable": True}
        for name, item in before["files"].items()]}
    proposed = {"summary": "Synthetic proposal", "files": [
        {"path": name, "content": content} for name, content in zip(names, contents)]}
    return TaskPlan(before, source, proposed, "text")


class TaskCopyTests(unittest.TestCase):
    def test_maximum_source_plus_proposal_reaches_bounded_sandbox_without_truncation(self):
        source = {"instruction": "Synthetic", "files": [
            {"path": "context1.txt", "content": "\\" * 65536, "writable": False},
            {"path": "context2.txt", "content": "\\" * 65536, "writable": False},
            {"path": "new1.txt", "content": None, "writable": True},
            {"path": "new2.txt", "content": None, "writable": True}]}
        replacements = [{"path": "new1.txt", "content": "\\" * 65536},
                        {"path": "new2.txt", "content": "\\" * 65536}]
        with patch.object(task_sandbox.os, "geteuid", return_value=1000), \
                patch.object(task_sandbox.os, "getuid", return_value=1000), \
                patch.object(task_sandbox, "command", return_value=["synthetic"]), \
                patch.object(task_sandbox, "bounded_process", return_value=(0, b"integrity", b"")) as process:
            result = task_sandbox.run(source, replacements, "text")
        self.assertTrue(result["passed"])
        payload = process.call_args.kwargs["input_bytes"]
        self.assertGreater(len(payload), 512 * 1024)
        self.assertLessEqual(len(payload), 640 * 1024)
        self.assertEqual(process.call_args.kwargs["input_limit"], 640 * 1024)
        self.assertEqual(sum(len(item["content"].encode()) for item in json.loads(payload)["files"]), 256 * 1024)

    def test_text_copy_preserves_utf8_backslashes_and_missing_final_newline(self):
        content = "ą\\\t\n" * 7000 + "END"
        data = {"files": [{"path": "niri/config.kdl", "content": content}]}
        with tempfile.TemporaryDirectory() as directory:
            task_child.materialize(data, directory)
            self.assertEqual((Path(directory) / "niri/config.kdl").read_bytes(), content.encode("utf-8"))
            with self.assertRaises(FileExistsError):
                task_child.materialize(data, directory)

    def test_toml_check_uses_parser_and_does_not_execute_strings(self):
        task_child.syntax_check([{"path": "app.toml", "content": 'value = "$(do-not-execute)"\n'}], "toml")
        with self.assertRaises(ValueError):
            task_child.syntax_check([{"path": "app.toml", "content": "value = ["}], "toml")

    def test_journal_preflight_reserves_after_and_undo_states(self):
        content = "\\" * 65536
        original = {"inode": [1, 2], "mode": 0o600, "sha256": workspace.sha(content.encode()), "content": content}
        before = {"workspace": "/tmp/synthetic", "root": [1, 1], "files": {
            name: {"parent": [1, 1], "file": original} for name in ("a.txt", "b.txt")}}
        entries = [{"path": name, "after": None, "undo": None, "published": False,
                    "temp": ".ai-task-" + str(index) * 24, "undo_temp": ".ai-task-" + str(index + 2) * 24}
                   for index, name in enumerate(before["files"])]
        value = {"schema": 2, "plan": "a" * 64, "before": before, "entries": entries, "status": "prepared"}
        replacements = [{"path": name, "content": "\t" * 65536} for name in before["files"]]
        workspace._journal_capacity(value, replacements)
        # An initially small journal must not be accepted if it cannot grow
        # enough to record both published replacements and restoration inodes.
        with patch.object(workspace, "MAX_JOURNAL", len(canonical(value).encode()) + 1024):
            with self.assertRaises(workspace.JournalLimit):
                workspace._journal_capacity(value, replacements)


@unittest.skipIf(os.geteuid() == 0, "filesystem executor requires a real non-root UID")
class TaskFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-task-engine-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / ".config" / "niri"
        self.root.mkdir(parents=True, mode=0o700)
        self.enterContext(patch.object(workspace.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.home))))

    def test_config_root_large_edit_new_file_and_rollback_keep_legacy_root_guard(self):
        original = "a" * 64000
        (self.root / "config.kdl").write_text(original)
        plan = plan_for(self.root, ["config.kdl", "new.kdl"], ["b" * 65536, "ą" * 32768])
        workspace.apply(plan, plan.authorize("ZAPISZ " + plan.sha256))
        self.assertEqual((self.root / "config.kdl").read_text(), "b" * 65536)
        self.assertEqual((self.root / "new.kdl").read_text(), "ą" * 32768)
        self.assertTrue(workspace.rollback(str(self.root), plan.sha256))
        self.assertEqual((self.root / "config.kdl").read_text(), original)
        self.assertFalse((self.root / "new.kdl").exists())
        with self.assertRaises(workspace_executor.ExecutionDenied):
            workspace_executor._root(str(self.root))

    def test_guard_refusal_after_lock_creates_no_journal_or_target(self):
        (self.root / "config.kdl").write_text("old")
        plan = plan_for(self.root, ["config.kdl", "new.kdl"], ["new", "created"])
        locked = []
        real_lock = workspace.fcntl.flock
        def lock(fd, operation):
            real_lock(fd, operation)
            locked.append(True)
        def denied():
            self.assertEqual(locked, [True])
            self.assertEqual((self.root / "config.kdl").read_text(), "old")
            raise ValueError("policy revoked")
        with patch.object(workspace.fcntl, "flock", side_effect=lock), \
                patch.object(workspace, "_write") as write, patch.object(workspace, "_save") as save:
            with self.assertRaisesRegex(ValueError, "policy revoked"):
                workspace.apply(plan, plan.authorize("ZAPISZ " + plan.sha256), write_guard=denied)
            write.assert_not_called()
            save.assert_not_called()
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["config.kdl"])

    def test_journal_capacity_refusal_precedes_any_mutation(self):
        (self.root / "config.kdl").write_text("old")
        plan = plan_for(self.root, ["config.kdl", "new.kdl"], ["new", "created"])
        with patch.object(workspace, "MAX_JOURNAL", 1), patch.object(workspace, "_write") as write:
            with self.assertRaises(workspace.JournalLimit):
                workspace.apply(plan, plan.authorize("ZAPISZ " + plan.sha256))
            write.assert_not_called()
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["config.kdl"])

    def test_new_selection_and_recheck_refuse_appeared_file_without_reading_it(self):
        before = workspace.snapshot(str(self.root), ["new.kdl"], must_be_absent=["new.kdl"])
        (self.root / "new.kdl").write_text("unapproved private data")
        with patch.object(workspace, "read_file") as read:
            with self.assertRaises(ValueError):
                workspace.snapshot(str(self.root), ["new.kdl"], must_be_absent=["new.kdl"])
            with self.assertRaises(ValueError):
                workspace.unchanged(before)
            read.assert_not_called()

    def test_appeared_new_target_after_guard_is_not_read_or_overwritten(self):
        plan = plan_for(self.root, ["new.kdl"], ["created"])
        real_read = workspace.read_file
        def read(parent, name):
            self.assertNotEqual(name, "new.kdl")
            return real_read(parent, name)
        with patch.object(workspace, "read_file", side_effect=read):
            with self.assertRaises(ValueError):
                workspace.apply(plan, plan.authorize("ZAPISZ " + plan.sha256),
                    write_guard=lambda: (self.root / "new.kdl").write_text("private concurrent edit"))
        self.assertEqual((self.root / "new.kdl").read_text(), "private concurrent edit")

    def test_root_and_parent_boundaries_and_no_new_directory_creation(self):
        for allowed in (self.home, self.home / ".ssh", self.home / ".config" / "ai-desktop",
                        *(self.home / name for name in ("secrets", "oauth", "backup", "backups", ".local", ".git"))):
            allowed.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.subTest(allowed=allowed):
                root = workspace._root(str(allowed))
                try:
                    self.assertEqual(os.fstat(root).st_ino, allowed.stat().st_ino)
                finally:
                    os.close(root)
        for blocked in (Path("/etc"), Path("/tmp"), self.home.parent,
                        self.home / ".ai-desktop-tasks", self.home / ".ai-task-synthetic"):
            with self.subTest(path=blocked), self.assertRaises(workspace_executor.ExecutionDenied):
                workspace._root(str(blocked))
        with self.assertRaises(workspace_executor.ExecutionDenied):
            workspace_executor._root(str(self.home))
        with self.assertRaises(OSError):
            workspace.snapshot(str(self.root), ["missing/new.txt"])
        self.assertFalse((self.root / "missing").exists())
        (self.home / "alias").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(workspace_executor.ExecutionDenied):
            workspace._root(str(self.home / "alias"))
