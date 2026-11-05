"""Device drift: terminal history is inert; fresh writes and rollback stay bound.

Real private temporary files, no model/host configuration. The journal device
numbers are deliberately changed to simulate loss of the old runtime identity;
this does NOT assert a real Btrfs remount or establish a persistent identity.
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import task_cli, task_workspace as w
from ai_desktop.analysis_contract import canonical
from ai_desktop.task_plan import TaskPlan


class DetachedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.geteuid(), 0, 'run filesystem regressions as a regular user')
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='ai-history-')))
        self.target = self.root / 'hello.py'
        self.target.write_text('original\n')
        self.target.chmod(0o600)
        self.plan = self.make_plan('first\n')
        w.apply(self.plan, self.plan.authorize('SAVE ' + self.plan.sha256))
        self.journal = self.root / w.STATE / (self.plan.sha256 + '.json')

    def make_plan(self, content):
        before = w.snapshot(str(self.root), ['hello.py'])
        source = {'instruction': 'synthetic change', 'files': [
            {'path': 'hello.py', 'content': before['files']['hello.py']['file']['content'], 'writable': True}]}
        return TaskPlan(before, source, {'summary': 'synthetic', 'files': [
            {'path': 'hello.py', 'content': content}]}, 'text')

    def detach(self, *, status=None):
        value = json.loads(self.journal.read_text())
        old_device = os.stat(self.root).st_dev ^ 1         # always a real mismatch, however often this is called
        def walk(node):
            if isinstance(node, dict):
                for key, item in node.items():
                    if key in ('root', 'parent', 'inode') and isinstance(item, list):
                        item[0] = old_device
                    else:
                        walk(item)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
        walk(value)
        if status is not None:
            value['status'] = status
        # Legacy history (schema 2, no durable identity) is what stays detached.
        value['schema'] = 2
        value['before'].pop('fs', None)
        self.journal.write_text(canonical(value))
        return value

    def preserved(self):
        return (self.journal.read_bytes(), self.journal.stat().st_mtime_ns,
                self.journal.stat().st_ino, self.target.read_bytes(), self.target.stat().st_ino)

    def test_completed_device_drift_is_reported_without_mutation_or_target_read(self):
        self.detach()
        before = self.preserved()
        # _state_dir reads its fixed .gitignore only, never a target file here.
        original = w.read_file
        names = []
        def observe(fd, name):
            names.append(name)
            return original(fd, name)
        with patch.object(w, 'read_file', side_effect=observe):
            records = w.recovery_records(str(self.root))
            count = w._can_apply(str(self.root))
        self.assertEqual(count, 1)
        self.assertEqual(records[0]['status'], 'detached')
        self.assertEqual(records[0]['files'], [])
        self.assertEqual(w.status(str(self.root)), [(self.plan.sha256, 'detached')])
        self.assertEqual(set(names), {'.gitignore'})
        self.assertEqual(self.preserved(), before)
        self.assertEqual(json.loads(self.journal.read_text())['status'], 'applied')

    def test_new_plan_uses_current_data_and_rollback_never_rewrites_old_history(self):
        self.detach()
        original_journal = self.journal.read_bytes()
        self.target.write_text('independent user edit\n')
        second = self.make_plan('second\n')
        w.apply(second, second.authorize('SAVE ' + second.sha256))
        records = {r['plan']: r for r in w.recovery_records(str(self.root))}
        self.assertEqual(records[self.plan.sha256]['status'], 'detached')
        self.assertEqual(records[second.sha256]['status'], 'applied')
        self.assertTrue(w.rollback(str(self.root), second.sha256))
        self.assertEqual(self.target.read_text(), 'independent user edit\n')
        self.assertEqual(self.journal.read_bytes(), original_journal)

    def test_direct_rollback_of_detached_record_refuses_before_any_write(self):
        self.detach()
        before = self.preserved()
        with patch.object(w, '_save') as save, patch.object(w, '_write') as write, \
                patch.object(w.os, 'replace') as replace, patch.object(w.os, 'unlink') as unlink:
            with self.assertRaises(ValueError):
                w.rollback(str(self.root), self.plan.sha256)
        for call in (save, write, replace, unlink):
            call.assert_not_called()
        self.assertEqual(self.preserved(), before)

    def test_unfinished_records_block_even_with_device_only_difference(self):
        for state in ('prepared', 'conflict'):
            self.detach(status=state)
            before = self.preserved()
            with self.subTest(state=state), self.assertRaises(ValueError):
                w._can_apply(str(self.root))
            self.assertEqual(self.preserved(), before)

    def test_path_or_inode_mismatch_still_blocks(self):
        original = self.detach()
        for field in ('path', 'inode'):
            value = json.loads(canonical(original))
            if field == 'path':
                value['before']['workspace'] += '/different'
            else:
                value['before']['root'][1] += 1
            self.journal.write_text(canonical(value))
            with self.subTest(field=field), self.assertRaises(ValueError):
                w._can_apply(str(self.root))

    def test_inconsistent_applied_record_is_not_detached(self):
        original = self.detach()
        for field in ('published', 'after', 'undo'):
            value = json.loads(canonical(original))
            entry = value['entries'][0]
            entry[field] = {'published': False, 'after': None,
                            'undo': value['before']['files']['hello.py']['file']}[field]
            self.journal.write_text(canonical(value))
            with self.subTest(field=field), self.assertRaises(ValueError):
                w._can_apply(str(self.root))

    def test_legacy_missing_undo_stage_tracking_is_not_silently_accepted(self):
        value = self.detach()
        value['schema'] = 1
        for entry in value['entries']:
            del entry['undo_temp']
        self.journal.write_text(canonical(value))
        with self.assertRaises(ValueError):
            w._can_apply(str(self.root))

    def test_any_residual_stage_blocks_without_reading_or_removing_it(self):
        value = self.detach()
        for key in ('temp', 'undo_temp'):
            stage = self.root / value['entries'][0][key]
            for kind in ('file', 'symlink', 'fifo', 'directory'):
                with self.subTest(key=key, kind=kind):
                    if kind == 'file': stage.write_text('PRIVATE STAGE')
                    elif kind == 'symlink': stage.symlink_to(self.target)
                    elif kind == 'fifo': os.mkfifo(stage)
                    else: stage.mkdir()
                    with self.assertRaises(ValueError):
                        w._can_apply(str(self.root))
                    self.assertTrue(stage.exists())
                    stage.rmdir() if kind == 'directory' else stage.unlink()

    def test_parent_symlink_and_missing_parent_refuse(self):
        value = self.detach()
        for kind in ('symlink', 'missing'):
            other = json.loads(canonical(value))
            saved = other['before']['files'].pop('hello.py')
            other['before']['files']['parent/hello.py'] = saved
            other['entries'][0]['path'] = 'parent/hello.py'
            self.journal.write_text(canonical(other))
            link = self.root / 'parent'
            if kind == 'symlink': link.symlink_to(self.root, target_is_directory=True)
            with self.subTest(kind=kind), self.assertRaises((OSError, ValueError, RuntimeError)):
                w._can_apply(str(self.root))
            if link.is_symlink(): link.unlink()

    def test_rolled_back_terminal_history_is_readonly(self):
        self.assertTrue(w.rollback(str(self.root), self.plan.sha256))
        self.detach()
        before = self.preserved()
        self.assertEqual(w._can_apply(str(self.root)), 1)
        self.assertEqual(self.preserved(), before)
        self.assertEqual(json.loads(self.journal.read_text())['status'], 'rolled-back')

    def test_detached_records_still_consume_history_capacity(self):
        with patch.object(w, 'MAX_JOURNALS', 1):
            self.detach()
            with self.assertRaises(w.JournalLimit):
                w._can_apply(str(self.root))

    def test_status_has_no_restore_action_and_rollback_ui_never_prompts(self):
        self.detach()
        records = w.recovery_records(str(self.root))
        for details in (False, True):
            out = io.StringIO()
            with redirect_stdout(out):
                task_cli.show_record(records[0], details=details)
            self.assertNotIn('Restore', out.getvalue())
            self.assertNotIn('Delete', out.getvalue())
            for plan in (None, self.plan.sha256):
                reader = Mock(side_effect=AssertionError('unexpected approval'))
                with redirect_stdout(io.StringIO()), patch.object(w, 'rollback') as undo:
                    code = task_cli.rollback_task(SimpleNamespace(plan=plan, details=details,
                            workspace=str(self.root)), records, reader)
                self.assertEqual(code, 2)
                reader.assert_not_called(); undo.assert_not_called()

    def test_same_binding_record_and_new_write_guards_are_unchanged(self):
        self.assertEqual(w._can_apply(str(self.root)), 0)
        self.assertEqual(w.recovery_records(str(self.root))[0]['status'], 'applied')
        self.detach()
        second = self.make_plan('next\n')
        self.target.write_text('changed after preview\n')
        before = self.preserved()
        with self.assertRaises(ValueError):
            w.apply(second, second.authorize('SAVE ' + second.sha256))
        self.assertEqual(self.preserved(), before)
