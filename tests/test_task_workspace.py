"""Real temp files, stale data, restart recovery and deliberate policy refusals."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import task_contract as contract, task_workspace as workspace
from ai_desktop.analysis_contract import AnalysisConsent, AnalysisDenied, canonical, parse_request
from ai_desktop.task_plan import TaskPlan, Authorization
from ai_desktop.workspace_executor import ExecutionDenied


class TaskWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-task-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "calc.py").write_text("old\n")
        (self.root / "calc.py").chmod(0o640)

    def plan(self, names=None, clock=None):
        before = workspace.snapshot(str(self.root), names or ["calc.py", "new.txt"])
        source = {"instruction": "change", "files": [
            {"path": p, "content": f["file"]["content"] if f["file"] else None, "writable": True}
            for p, f in before["files"].items()]}
        proposed = {"summary": "change", "files": [{"path": p, "content": "new\n"} for p in before["files"]]}
        plan = TaskPlan(before, source, proposed, "python-syntax", **({"clock": clock} if clock else {}))
        return plan

    def apply(self, plan):
        workspace.apply(plan, plan.authorize("SAVE " + plan.sha256))

    def windows_plan(self, data):
        (self.root / "data.csv").write_bytes(data)
        before = workspace.snapshot(str(self.root), ["data.csv"])
        source = contract.request({"instruction": "change", "files": [
            {"path": "data.csv", "content": contract.plain_text(before["files"]["data.csv"]["file"]["content"]),
             "writable": True}]})
        return source, TaskPlan(before, source, {"summary": "change", "files": [
            {"path": "data.csv", "content": "a;b\n1;2\n"}]}, "text")

    def test_two_mib_anchored_edit_keeps_format_and_survives_durable_undo(self):
        original = b"\xef\xbb\xbf" + b"# filler\r\n" * 210000 + b"VALUE = 1\r\n"
        target = self.root / "large.py"
        target.write_bytes(original)
        before = workspace.snapshot(str(self.root), ["large.py"])
        source = {"instruction": "Update VALUE only", "files": [{"path": "large.py", "writable": True,
                  "content": contract.plain_text(original.decode("utf-8"))}]}
        response = {"summary": "Update VALUE", "files": [{"path": "large.py", "replacements": [
            {"old": "VALUE = 1", "new": "VALUE = 2"}]}]}
        plan = TaskPlan(before, source, response, "python-syntax")
        self.assertGreater(len(source["files"][0]["content"].encode()), 1024 * 1024)
        self.assertLess(len(canonical(response)), 256)
        self.assertIn("+VALUE = 2", plan.diff())
        self.apply(plan)
        self.assertEqual(target.read_bytes(), original.replace(b"VALUE = 1", b"VALUE = 2"))
        self.assertEqual(workspace.status(str(self.root)), [(plan.sha256, "applied")])
        self.assertTrue(workspace.rollback(str(self.root), plan.sha256))
        self.assertEqual(target.read_bytes(), original)

    def test_an_edit_keeps_the_bom_and_windows_line_ends_and_undo_restores_the_exact_bytes(self):
        original = b"\xef\xbb\xbfa,b\r\n1,2\r\n"
        source, plan = self.windows_plan(original)
        self.assertEqual(source["files"][0]["content"], "a,b\n1,2\n")     # no BOM or CR reaches the model
        difference = plan.diff()
        self.assertNotIn("\r", difference.replace("\\r", ""))
        self.assertIn("-a,b\n", difference)
        self.assertIn("+a;b\n", difference)
        self.apply(plan)
        self.assertEqual((self.root / "data.csv").read_bytes(), b"\xef\xbb\xbfa;b\r\n1;2\r\n")
        self.assertTrue(workspace.rollback(str(self.root), plan.sha256))
        self.assertEqual((self.root / "data.csv").read_bytes(), original)

    def test_line_ends_alone_are_kept_without_adding_a_bom_and_the_other_way_round(self):
        for original, expected in ((b"a,b\r\n1,2\r\n", b"a;b\r\n1;2\r\n"), (b"\xef\xbb\xbfa,b\n1,2\n", b"\xef\xbb\xbfa;b\n1;2\n")):
            with self.subTest(original=original):
                _, plan = self.windows_plan(original)
                self.apply(plan)
                self.assertEqual((self.root / "data.csv").read_bytes(), expected)
                self.assertTrue(workspace.rollback(str(self.root), plan.sha256))

    def test_a_file_whose_bytes_an_edit_could_not_give_back_is_refused(self):
        for data in (b"a\r\nb\n", b"a\rb\n", b"a\r\r\nb\r\n", b"a\xef\xbb\xbfb\n", b"\xef\xbb\xbf\xef\xbb\xbfa\n", b"a\x00\n"):
            with self.subTest(data=data):
                (self.root / "data.csv").write_bytes(data)
                with self.assertRaises(ValueError):
                    workspace.snapshot(str(self.root), ["data.csv"])

    def test_the_model_cannot_send_windows_line_ends_or_a_bom(self):
        source, _ = self.windows_plan(b"a,b\r\n")
        for content in ("a;b\r\n", "\ufeffa;b\n"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                contract.proposal(canonical({"summary": "x", "files": [{"path": "data.csv", "content": content}]}), source)

    def test_a_format_that_no_longer_fits_the_limit_is_refused_before_any_write(self):
        body = "a\n" * (contract.MAX_FILE // 2)
        (self.root / "data.csv").write_bytes(b"a\r\nb\r\n")
        before = workspace.snapshot(str(self.root), ["data.csv"])
        source = {"instruction": "change", "files": [{"path": "data.csv", "content": "a\nb\n", "writable": True}]}
        plan = TaskPlan(before, source, {"summary": "x", "files": [{"path": "data.csv", "content": body}]}, "text")
        with self.assertRaises(ValueError):
            self.apply(plan)
        self.assertEqual((self.root / "data.csv").read_bytes(), b"a\r\nb\r\n")

    def test_crlf_expansion_total_refuses_before_journal_or_target_writes(self):
        for name in ("a.txt", "b.txt"):
            (self.root / name).write_bytes(b"a\r\n")
        with patch.object(contract, "MAX_TOTAL", 10):
            before = workspace.snapshot(str(self.root), ["a.txt", "b.txt"])
            source = {"instruction": "change", "files": [
                {"path": name, "content": "a\n", "writable": True} for name in before["files"]]}
            plan = TaskPlan(before, source, {"summary": "change", "files": [
                {"path": name, "content": "a\nb\n"} for name in before["files"]]}, "text")
            with patch.object(workspace, "_write") as write, patch.object(workspace, "_save") as save:
                with self.assertRaisesRegex(ValueError, "formatted total"):
                    self.apply(plan)
                write.assert_not_called()
                save.assert_not_called()
        for name in ("a.txt", "b.txt"):
            self.assertEqual((self.root / name).read_bytes(), b"a\r\n")
        self.assertFalse((self.root / workspace.STATE).exists())

    def test_apply_existing_create_new_and_idempotent_durable_rollback(self):
        plan = self.plan(); self.apply(plan)
        self.assertEqual((self.root / "calc.py").read_text(), "new\n")
        self.assertEqual((self.root / "calc.py").stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.root / "new.txt").stat().st_mode & 0o777, 0o600)
        self.assertEqual(workspace.status(str(self.root)), [(plan.sha256, "applied")])
        code = "from ai_desktop.task_workspace import rollback; import sys; sys.exit(0 if rollback(sys.argv[1],sys.argv[2]) else 2)"
        done = subprocess.run([sys.executable, "-B", "-c", code, str(self.root), plan.sha256], capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.root / "calc.py").read_text(), "old\n")
        self.assertFalse((self.root / "new.txt").exists())
        self.assertTrue(workspace.rollback(str(self.root), plan.sha256))

    def test_recovery_metadata_is_readonly_private_and_binds_rollback(self):
        self.assertEqual(workspace.recovery_records(str(self.root)), [])
        self.assertFalse((self.root / workspace.STATE).exists())
        plan = self.plan(); self.apply(plan)
        journal = self.root / workspace.STATE / (plan.sha256 + ".json")
        raw, modified = journal.read_bytes(), journal.stat().st_mtime_ns
        expected = {"plan": plan.sha256, "status": "applied",
                    "files": [{"path": "calc.py", "action": "restore"},
                              {"path": "new.txt", "action": "remove"}],
                    "journal_sha256": workspace.sha(canonical(json.loads(raw)).encode("utf-8"))}
        records = workspace.recovery_records(str(self.root))
        self.assertAlmostEqual(records[0].pop("modified"), modified / 1e9, places=3)   # float rounding differs per platform
        self.assertEqual(records, [expected])
        self.assertEqual((journal.read_bytes(), journal.stat().st_mtime_ns), (raw, modified))
        self.assertEqual((self.root / "calc.py").read_text(), "new\n")
        self.assertTrue(workspace.rollback(str(self.root), plan.sha256,
                                           expected_journal=expected["journal_sha256"]))
        self.assertEqual((self.root / "calc.py").read_text(), "old\n")
        self.assertFalse((self.root / "new.txt").exists())
        records = workspace.recovery_records(str(self.root))
        self.assertEqual(records[0]["status"], "rolled-back")
        self.assertNotEqual(records[0]["journal_sha256"], expected["journal_sha256"])

    def test_changed_journal_after_selection_is_rechecked_under_lock_before_writes(self):
        plan = self.plan(); self.apply(plan)
        record = workspace.recovery_records(str(self.root))[0]
        journal = self.root / workspace.STATE / (plan.sha256 + ".json")
        real_lock = workspace.fcntl.flock
        def change_after_lock(fd, operation):
            real_lock(fd, operation)
            value = json.loads(journal.read_text())
            value["status"] = "prepared"
            journal.write_text(canonical(value))
        with patch.object(workspace.fcntl, "flock", side_effect=change_after_lock), \
                patch.object(workspace, "_write") as write, patch.object(workspace, "_save") as save, \
                patch.object(workspace.os, "unlink") as unlink, patch.object(workspace.os, "replace") as replace:
            with self.assertRaisesRegex(ValueError, "journal changed"):
                workspace.rollback(str(self.root), plan.sha256, expected_journal=record["journal_sha256"])
            for mutation in (write, save, unlink, replace):
                mutation.assert_not_called()
        self.assertEqual((self.root / "calc.py").read_text(), "new\n")
        self.assertEqual((self.root / "new.txt").read_text(), "new\n")
        self.assertEqual(json.loads(journal.read_text())["status"], "prepared")

    def test_recovery_rejects_invalid_journal_and_wrong_root_without_mutation(self):
        plan = self.plan(); self.apply(plan)
        journal = self.root / workspace.STATE / (plan.sha256 + ".json")
        original = json.loads(journal.read_text())
        for mutation in ("path", "root", "workspace", "content"):
            value = json.loads(canonical(original))
            if mutation == "path": value["entries"][0]["path"] = "../calc.py"
            elif mutation == "root": value["before"]["root"][1] += 1
            elif mutation == "workspace": value["before"]["workspace"] += "/other"
            else: value["before"]["files"]["calc.py"]["file"]["content"] = "tampered"
            raw = canonical(value)
            journal.write_text(raw)
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError): workspace.recovery_records(str(self.root))
                with self.assertRaises(ValueError):
                    workspace.rollback(str(self.root), plan.sha256,
                                       expected_journal=workspace.sha(raw.encode("utf-8")))
                self.assertEqual(journal.read_text(), raw)
                self.assertEqual((self.root / "calc.py").read_text(), "new\n")
                self.assertEqual((self.root / "new.txt").read_text(), "new\n")

    def test_recovery_order_and_canonical_digest_are_stable(self):
        first = self.plan(); self.apply(first)
        first_record = workspace.recovery_records(str(self.root))[0]
        journal = self.root / workspace.STATE / (first.sha256 + ".json")
        journal.write_text(json.dumps(json.loads(journal.read_text()), indent=2))
        again = workspace.recovery_records(str(self.root))
        for record in (first_record, again[0]):
            self.assertIsInstance(record.pop("modified"), float)  # the file time may change; the digest must not
        self.assertEqual(again, [first_record])
        (self.root / "other.py").write_text("other\n")
        second = self.plan(names=["other.py"]); self.apply(second)
        self.assertEqual([r["plan"] for r in workspace.recovery_records(str(self.root))],
                         sorted([first.sha256, second.sha256]))

    def test_invalid_journal_approval_refuses_before_reading_workspace(self):
        for digest in ("", "a" * 63, "g" * 64, 1, [], True):
            with self.subTest(digest=digest), patch.object(workspace, "_root") as root:
                with self.assertRaisesRegex(ValueError, "journal approval"):
                    workspace.rollback(str(self.root), "a" * 64, expected_journal=digest)
                root.assert_not_called()

    def test_stale_file_replaced_inode_symlink_fifo_and_hardlink_refuse(self):
        plan = self.plan(); (self.root / "calc.py").write_text("user changed")
        with self.assertRaises(ValueError): self.apply(plan)
        self.assertFalse((self.root / "new.txt").exists())
        for variant in ("symlink", "fifo", "hardlink", "writable"):
            path = self.root / "other.py"
            if variant == "symlink": path.symlink_to(self.root / "calc.py")
            elif variant == "fifo": os.mkfifo(path)
            elif variant == "hardlink": os.link(self.root / "calc.py", path)
            else: path.write_text("x"); path.chmod(0o666)
            with self.subTest(variant=variant), self.assertRaises((OSError, ValueError)):
                workspace.snapshot(str(self.root), [path.name])
            path.unlink()

    def test_rollback_does_not_overwrite_user_changes_and_retry_is_possible(self):
        plan = self.plan(); self.apply(plan)
        (self.root / "calc.py").write_text("user edit\n")
        self.assertFalse(workspace.rollback(str(self.root), plan.sha256))
        self.assertEqual((self.root / "calc.py").read_text(), "user edit\n")
        self.assertFalse((self.root / "new.txt").exists())
        self.assertEqual(workspace.status(str(self.root)), [(plan.sha256, "conflict")])
        (self.root / "calc.py").write_text("new\n")
        self.assertTrue(workspace.rollback(str(self.root), plan.sha256))

    def test_process_death_after_publish_before_journal_ack_is_recoverable(self):
        script = '''
import os, sys
from ai_desktop import task_workspace as w
from ai_desktop.task_plan import TaskPlan
root=sys.argv[1]
before=w.snapshot(root,['calc.py','new.txt'])
source={'instruction':'x','files':[{'path':p,'content':f['file']['content'] if f['file'] else None,'writable':True} for p,f in before['files'].items()]}
plan=TaskPlan(before,source,{'summary':'x','files':[{'path':p,'content':'new\\n'} for p in before['files']]},'python-syntax')
real=w.rename_exchange
def crash(src_dir,src,dst_dir,dst):
    real(src_dir,src,dst_dir,dst)
    if dst=='calc.py': os._exit(77)      # killed right after the swap: the original still sits under the stage name
w.rename_exchange=crash
w.apply(plan,plan.authorize('SAVE '+plan.sha256))
'''
        done = subprocess.run([sys.executable, "-B", "-c", script, str(self.root)], capture_output=True)
        self.assertEqual(done.returncode, 77, done.stderr)
        records = workspace.status(str(self.root))
        self.assertEqual(records[0][1], "prepared")
        self.assertTrue(workspace.rollback(str(self.root), records[0][0]))
        self.assertEqual((self.root / "calc.py").read_text(), "old\n")
        self.assertFalse((self.root / "new.txt").exists())

    def test_plan_refuses_forged_expired_and_replayed_approval(self):
        now = [0]; plan = self.plan(clock=lambda: now[0])
        with self.assertRaises(ValueError):
            workspace.apply(plan, Authorization(plan.sha256, "fake"))
        plan = self.plan(clock=lambda: now[0]); auth = plan.authorize("SAVE " + plan.sha256)
        now[0] = 181
        with self.assertRaises(ValueError): workspace.apply(plan, auth)
        plan = self.plan(); auth = plan.authorize("SAVE " + plan.sha256)
        workspace.apply(plan, auth)
        with self.assertRaises(ValueError): workspace.apply(plan, auth)

    def test_wire_task_strict_paths_and_readonly_selection(self):
        source = {"instruction": "fix", "files": [{"path": "calc.py", "content": "x", "writable": True},
            {"path": "test_calc.py", "content": "test", "writable": False}]}
        gate = AnalysisConsent(None, task=source)
        raw = gate.authorize(None, task=source, sha256=gate.sha256, nonce=gate.nonce).payload.encode()
        self.assertEqual(json.loads(parse_request(raw)[0])["task"], source)
        for path in ("../x", "/tmp/x", "x/.git/config", ".env", "backup/x", "test_calc.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                contract.proposal(canonical({"summary": "x", "files": [{"path": path, "content": "new"}]}), source)
        for raw in ('{"summary":"x","files":[],"command":"sh"}', '{"summary":"x","summary":"y","files":[]}'):
            with self.assertRaises(ValueError): contract.proposal(raw, source)
        source["instruction"] = "changed"
        with self.assertRaises(AnalysisDenied):
            gate.check(None, task=source, sha256=gate.sha256, nonce=gate.nonce)

    def test_tampered_journal_cannot_follow_symlink_or_escape(self):
        plan = self.plan(); self.apply(plan)
        journal = self.root / workspace.STATE / (plan.sha256 + ".json")
        value = json.loads(journal.read_text()); value["entries"][0]["path"] = "../calc.py"
        journal.write_text(canonical(value))
        with self.assertRaises(ValueError): workspace.rollback(str(self.root), plan.sha256)
        self.assertEqual((self.root / "calc.py").read_text(), "new\n")

    def test_diff_binds_exact_newline_and_context_mutation(self):
        plan = self.plan()
        self.assertIn('New content as JSON: "new\\n"', plan.diff())
        data = plan.data(); data["proposal"]["files"][0]["content"] = "evil"
        self.apply(plan)
        self.assertEqual((self.root / "calc.py").read_text(), "new\n")

    def test_private_undo_directory_is_ignored_by_git(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        plan = self.plan(); self.apply(plan)
        relative = workspace.STATE + "/" + plan.sha256 + ".json"
        result = subprocess.run(["git", "-C", str(self.root), "check-ignore", "--", relative], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.root / workspace.STATE / ".gitignore").write_text("wrong")
        with self.assertRaises(ValueError): workspace.status(str(self.root))

    def test_editor_write_after_the_last_check_is_kept_not_overwritten(self):
        """The window the old os.replace left open: the editor saves after the final comparison, before the publish."""
        for how in ("in place", "by rename"):
            with self.subTest(how):
                target = self.root / "calc.py"
                target.write_text("old\n")
                target.chmod(0o640)
                plan = self.plan(names=["calc.py"])
                real = workspace.rename_exchange

                def editor_then_swap(src_dir, src, dst_dir, dst, how=how):
                    if dst == "calc.py":
                        if how == "in place":
                            target.write_text("editor version\n")
                        else:
                            other = self.root / "editor.tmp"
                            other.write_text("editor version\n")
                            other.chmod(0o640)
                            os.replace(other, target)
                    return real(src_dir, src, dst_dir, dst)
                with patch.object(workspace, "rename_exchange", side_effect=editor_then_swap), \
                        self.assertRaises(ValueError):
                    self.apply(plan)
                self.assertEqual(target.read_text(), "editor version\n")       # their save survives
                self.assertEqual(list(self.root.glob(".ai-task-*")), [])                # our stage is gone
                self.assertFalse(workspace.rollback(str(self.root), plan.sha256))       # a later edit is never undone
                self.assertEqual(target.read_text(), "editor version\n")
                for journal in (self.root / workspace.STATE).glob("*.json"):
                    journal.unlink()

    def test_crash_between_the_swap_and_removing_the_old_file_rolls_back_cleanly(self):
        plan = self.plan(names=["calc.py"])
        with patch.object(workspace.os, "unlink", side_effect=OSError("killed")), self.assertRaises(OSError):
            self.apply(plan)
        self.assertEqual((self.root / "calc.py").read_text(), "new\n")            # published
        self.assertEqual(len(list(self.root.glob(".ai-task-*"))), 1)             # the displaced original is still there
        self.assertTrue(workspace.rollback(str(self.root), plan.sha256))
        self.assertEqual((self.root / "calc.py").read_text(), "old\n")
        self.assertEqual(list(self.root.glob(".ai-task-*")), [])

    def editor(self, target, how):
        if how == "in place":
            target.write_text("editor version\n")
        else:
            other = self.root / "editor.tmp"
            other.write_text("editor version\n")
            other.chmod(target.stat().st_mode & 0o777 if target.exists() else 0o600)
            os.replace(other, target)

    def test_undoing_an_edit_keeps_a_save_made_after_the_last_comparison(self):
        for how in ("in place", "by rename"):
            with self.subTest(how):
                target = self.root / "calc.py"
                target.write_text("old\n")
                target.chmod(0o640)
                plan = self.plan(names=["calc.py"]); self.apply(plan)
                real = workspace.rename_exchange

                def editor_then_swap(src_dir, src, dst_dir, dst, how=how):
                    if dst == "calc.py":
                        self.editor(target, how)
                    return real(src_dir, src, dst_dir, dst)
                with patch.object(workspace, "rename_exchange", side_effect=editor_then_swap):
                    self.assertFalse(workspace.rollback(str(self.root), plan.sha256))
                self.assertEqual(target.read_text(), "editor version\n")
                self.assertEqual(workspace.status(str(self.root))[0][1], "conflict")
                for journal in (self.root / workspace.STATE).glob("*.json"):
                    journal.unlink()
                for stage in self.root.glob(".ai-task-*"):
                    stage.unlink()

    def test_undoing_a_new_file_keeps_a_save_made_after_the_last_comparison(self):
        for how in ("in place", "by rename"):
            with self.subTest(how):
                target = self.root / "new.txt"
                target.unlink(missing_ok=True)
                plan = self.plan(names=["new.txt"]); self.apply(plan)
                real = workspace.rename_noreplace

                def editor_then_move(src_dir, src, dst_dir, dst, how=how):
                    if src == "new.txt":
                        self.editor(target, how)
                    return real(src_dir, src, dst_dir, dst)
                with patch.object(workspace, "rename_noreplace", side_effect=editor_then_move):
                    self.assertFalse(workspace.rollback(str(self.root), plan.sha256))
                self.assertEqual(target.read_text(), "editor version\n")
                self.assertEqual(workspace.status(str(self.root))[0][1], "conflict")
                for journal in (self.root / workspace.STATE).glob("*.json"):
                    journal.unlink()
                for stage in self.root.glob(".ai-task-*"):
                    stage.unlink()

    def test_a_crash_inside_an_undo_is_finished_by_the_next_attempt(self):
        for name, content in (("calc.py", "old\n"), ("new.txt", None)):
            with self.subTest(name):
                target = self.root / name
                if content is not None:
                    target.write_text(content)
                else:
                    target.unlink(missing_ok=True)
                plan = self.plan(names=[name]); self.apply(plan)
                with patch.object(workspace.os, "unlink", side_effect=OSError("killed")):
                    self.assertFalse(workspace.rollback(str(self.root), plan.sha256))
                self.assertTrue(workspace.rollback(str(self.root), plan.sha256))
                self.assertEqual(target.read_text() if content is not None else target.exists(), content if content is not None else False)
                self.assertEqual(list(self.root.glob(".ai-task-*")), [])
                for journal in (self.root / workspace.STATE).glob("*.json"):
                    journal.unlink()

    def test_write_failure_keeps_original_and_unknown_partial_file_is_not_deleted(self):
        plan = self.plan()
        write = workspace.os.write
        def interrupted(fd, data):
            if data == b"new\n":
                write(fd, data[:1])
                raise OSError("disk failure")
            return write(fd, data)
        with patch.object(workspace.os, "write", side_effect=interrupted), self.assertRaises(OSError):
            self.apply(plan)
        self.assertEqual((self.root / "calc.py").read_text(), "old\n")
        self.assertFalse(workspace.rollback(str(self.root), plan.sha256))
        self.assertEqual(workspace.status(str(self.root))[0][1], "conflict")
        self.assertEqual(len(list(self.root.glob(".ai-task-*"))), 1)


@unittest.skipIf(os.geteuid() == 0, "filesystem executor requires a real non-root UID")
class TaskHomeWorkspaceTests(unittest.TestCase):
    """Non-root executor tests: passwd HOME is a fresh disposable directory."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-task-owned-home-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        home = patch.object(workspace.pwd, "getpwuid",
                            return_value=SimpleNamespace(pw_dir=str(self.root)))
        home.start()
        self.addCleanup(home.stop)

    def test_explicit_home_dotfile_edits_and_scoped_creation_roll_back(self):
        names = (".config/niri/config.kdl", ".ssh/config", ".local/share/note.txt",
                 ".env", "secrets/token.txt", "backup/copy.txt")
        originals = {}
        for index, name in enumerate(names):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            originals[name] = f"SYNTHETIC ORIGINAL {index}\n"
            target.write_text(originals[name])
            target.chmod(0o640)
        (self.root / "test").mkdir()
        (self.root / "unselected.txt").write_text("SYNTHETIC UNSELECTED\n")
        created = "test/hello.py"
        before = workspace.snapshot(str(self.root), [*names, created], must_be_absent=[created])
        self.assertIsNone(before["files"][created]["file"])
        source = {"instruction": "Change exactly the selected synthetic files", "files": [
            {"path": name, "content": originals.get(name), "writable": True}
            for name in [*names, created]]}
        replacement = {"summary": "Synthetic change", "files": [
            {"path": name, "content": "SYNTHETIC REPLACEMENT\n"} for name in names] + [
            {"path": created, "content": "print('hello')\n"}]}
        plan = TaskPlan(before, source, replacement, "text")
        workspace.apply(plan, plan.authorize("SAVE " + plan.sha256))
        for name in names:
            self.assertEqual((self.root / name).read_text(), "SYNTHETIC REPLACEMENT\n")
        self.assertEqual((self.root / created).read_text(), "print('hello')\n")
        self.assertEqual((self.root / "unselected.txt").read_text(), "SYNTHETIC UNSELECTED\n")
        self.assertEqual(workspace.status(str(self.root)), [(plan.sha256, "applied")])
        self.assertTrue(workspace.rollback(str(self.root), plan.sha256))
        for name in names:
            self.assertEqual((self.root / name).read_text(), originals[name])
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o640)
        self.assertFalse((self.root / created).exists())
        self.assertTrue((self.root / workspace.STATE).is_dir())
        self.assertEqual(workspace.status(str(self.root)), [(plan.sha256, "rolled-back")])

    def test_home_selection_cannot_read_or_edit_private_journal_or_stages(self):
        for name in (".ai-desktop-tasks/record.json", ".AI-DESKTOP-TASKS/record.json",
                     ".ai-task-1234", "test/.AI-TASK-1234"):
            with self.subTest(name=name), patch.object(workspace, "read_file") as read:
                with self.assertRaises(ValueError):
                    workspace.snapshot(str(self.root), [name])
                read.assert_not_called()
        self.assertFalse((self.root / workspace.STATE).exists())

    def test_home_targets_and_workspace_components_never_follow_symlinks(self):
        (self.root / "test").mkdir()
        (self.root / "test" / "original.txt").write_text("SYNTHETIC UNCHANGED\n")
        (self.root / "alias").symlink_to(self.root / "test", target_is_directory=True)
        (self.root / "test" / "link.txt").symlink_to(self.root / "test" / "original.txt")
        for root, name in ((self.root, "alias/original.txt"),
                           (self.root / "alias", "original.txt"),
                           (self.root, "test/link.txt")):
            with self.subTest(root=root, name=name), self.assertRaises((OSError, ValueError, ExecutionDenied)):
                workspace.snapshot(str(root), [name])
        with self.assertRaises(ValueError), patch.object(workspace, "read_file") as read:
            workspace.snapshot(str(self.root), ["test/link.txt"], must_be_absent=["test/link.txt"])
        read.assert_not_called()
        self.assertEqual((self.root / "test" / "original.txt").read_text(), "SYNTHETIC UNCHANGED\n")
        self.assertFalse((self.root / workspace.STATE).exists())

    def test_home_does_not_allow_writable_parents_or_missing_creation_directories(self):
        (self.root / "test").mkdir(mode=0o700)
        (self.root / "test").chmod(0o777)
        with patch.object(workspace, "read_file") as read:
            with self.assertRaises(ExecutionDenied):
                workspace.snapshot(str(self.root), ["test/hello.py"], must_be_absent=["test/hello.py"])
            with self.assertRaises(OSError):
                workspace.snapshot(str(self.root), ["missing/hello.py"], must_be_absent=["missing/hello.py"])
            read.assert_not_called()
        self.assertFalse((self.root / "test" / "hello.py").exists())
        self.assertFalse((self.root / "missing").exists())
