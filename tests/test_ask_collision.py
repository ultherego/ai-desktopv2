"""Collision dialogue with synthetic transports and real temporary target files."""
from contextlib import nullcontext, redirect_stdout
import io
import json
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_desktop import ask_cli, ask_edit, ask_followup, assistant_flow, permissions, task_cli, task_workspace
from ai_desktop.analysis_contract import AnalysisDenied, MAX_MESSAGE_BYTES
from ai_desktop.question_input import FOLLOWUP_PROMPT, QuestionInputError
from ai_desktop.terminal_consent import PROMPTS
from tests.profile_fixture import DEFAULT_PROFILE

OLD = '# synthetic original\nprint("old value")\n'
NEW = 'print("hello world")\n'
QUESTION = 'Utwórz hello.py wypisujący hello world.'


DONE = {'action': 'answer', 'text': 'Gotowe.'}


class FollowupTextTests(unittest.TestCase):
    def test_free_text_preserves_context_but_is_not_a_write_approval(self):
        out = io.StringIO()
        with redirect_stdout(out):
            result = ask_followup.clarify(QUESTION, 'hello.py', workspace='/home/tester', reader=lambda _: 'zachowaj stary, utwórz hello2.py')
        self.assertIn(QUESTION, result)
        self.assertIn('hello2.py', result)
        self.assertIn('Edytować, stworzyć nowy, anulować?', out.getvalue())
        self.assertNotIn(OLD, result)
        self.assertLessEqual(len(result.encode()), MAX_MESSAGE_BYTES)

    def test_short_dialogue_has_exact_lines_and_blank_separators(self):
        out = io.StringIO()
        def reader(prompt):
            self.assertEqual(prompt, "> ")
            print(prompt + "edytuj istniejący")
            return "edytuj istniejący"
        with redirect_stdout(out):
            revised = ask_followup.clarify(QUESTION, "hello.py", workspace="/home/tester", reader=reader)
        self.assertEqual(out.getvalue(),
            '\nPlik "hello.py" istnieje w "/home/tester".\n'
            'Edytować, stworzyć nowy, anulować?\n'
            '> edytuj istniejący\n\n'
            'Przygotuję nowy plan według tej odpowiedzi.\n')
        self.assertIn(QUESTION, revised)
        self.assertIn('"hello.py"', revised)
        self.assertNotIn('/home/tester', revised)

    def test_nested_location_is_display_only_and_terminal_safe(self):
        location = '/home/tester/zażółć "quoted"\u2028line'
        out = io.StringIO()
        with redirect_stdout(out):
            revised = ask_followup.clarify(QUESTION, "scripts/hello.py", workspace=location,
                                          reader=lambda _: "podmień")
        shown = json.dumps(location + '/scripts', ensure_ascii=False).replace("\u2028", "\\u2028")
        self.assertTrue(out.getvalue().startswith('\nPlik "hello.py" istnieje w ' + shown + '.\n'))
        self.assertNotIn("\u2028", out.getvalue())
        self.assertNotIn(location, revised)
        self.assertIn('"scripts/hello.py"', revised)

    def test_invalid_location_refuses_before_prompt_or_output(self):
        for workspace in (None, "", "relative", "/home/../target", "/home//target", "/home/x\n", "/home/\x1b[31m"):
            out, reader = io.StringIO(), Mock()
            with self.subTest(workspace=workspace), redirect_stdout(out), self.assertRaises(ValueError):
                ask_followup.clarify(QUESTION, "hello.py", workspace=workspace, reader=reader)
            reader.assert_not_called()
            self.assertEqual(out.getvalue(), "")

    def test_eof_and_interrupt_do_not_form_or_announce_another_plan(self):
        for error in (EOFError(), KeyboardInterrupt()):
            out, reader = io.StringIO(), Mock(side_effect=error)
            with self.subTest(error=type(error)), redirect_stdout(out), self.assertRaises(type(error)):
                ask_followup.clarify(QUESTION, "hello.py", workspace="/home/tester", reader=reader)
            reader.assert_called_once_with("> ")
            self.assertNotIn("Przygotuję nowy plan", out.getvalue())

    def test_cancel_and_empty_never_form_a_new_question(self):
        with redirect_stdout(io.StringIO()):
            for answer in ('anuluj', ' NIE ', 'stop', 'rezygnuję', '', '  '):
                self.assertIsNone(ask_followup.clarify(QUESTION, 'hello.py', workspace='/home/tester', reader=lambda _, value=answer: value))

    def test_input_and_context_budgets_are_fail_closed(self):
        with redirect_stdout(io.StringIO()):
            for reply in ('x' * MAX_MESSAGE_BYTES, '\x1b[31m', None):
                with self.subTest(reply=str(reply)[:10]), self.assertRaises((QuestionInputError, ValueError)):
                    ask_followup.clarify(QUESTION, 'hello.py', workspace='/home/tester', reader=lambda _: reply)
            reader = Mock()
            with self.assertRaises(QuestionInputError):
                ask_followup.clarify('x' * MAX_MESSAGE_BYTES, 'hello.py', workspace='/home/tester', reader=reader)
            reader.assert_not_called()

    def test_noncollision_error_is_not_retried_or_turned_into_a_prompt(self):
        for error in (ask_edit.EditDenied('input'), AnalysisDenied('cleanup'), AnalysisDenied('client')):
            with patch.object(assistant_flow, '_run_once', side_effect=error) as run, \
                    patch.object(ask_followup, 'clarify') as follow:
                with self.assertRaises(AnalysisDenied):
                    assistant_flow.run(QUESTION, None, Mock(), Mock())
            run.assert_called_once(); follow.assert_not_called()

    def test_repeated_collisions_have_finite_explicit_turns(self):
        with patch.object(assistant_flow, '_run_once', side_effect=ask_edit.CreateCollision('hello.py', workspace='/home/tester')) as run, \
                patch.object(ask_followup, 'clarify', return_value='nowa instrukcja') as follow, \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(assistant_flow.Declined):
                assistant_flow.run(QUESTION, None, Mock(), Mock())
        self.assertEqual(run.call_count, 3)
        self.assertEqual(follow.call_count, 2)

    def test_exception_cannot_carry_unvalidated_names(self):
        for path in ('../file', '/etc/file', 'hello\n.py', '.ai-desktop-tasks/x'):
            with self.subTest(path=path), self.assertRaises((ValueError, AnalysisDenied)):
                ask_edit.CreateCollision(path, workspace='/home/tester')


@unittest.skipIf(os.geteuid() == 0, 'real owner filesystem tests require a non-root runner')
class CollisionFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("ai_desktop.agent_profiles.resolve", return_value=DEFAULT_PROFILE))
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix='ai-collision-', dir=pwd.getpwuid(os.getuid()).pw_dir))
        self.home = Path(self.temp)
        self.home.chmod(0o700)
        self.target = self.home / 'hello.py'
        self.target.write_text(OLD); self.target.chmod(0o600)
        self.config = self.home / '.config/ai-desktop/permissions.toml'
        self.config.parent.mkdir(parents=True, mode=0o700)
        self.config.write_text('version=2\n[network]\nclaude=true\n[files]\nread=["~/**"]\nwrite=["~/**"]\ndeny=[]\n')
        self.config.chmod(0o600)
        self.enterContext(patch.object(permissions, '_home', return_value=str(self.home)))
        self.out = io.StringIO(); self.out.isatty = lambda: True
        self.enterContext(redirect_stdout(self.out))
        self.enterContext(patch.object(ask_cli.sys.stdin, 'isatty', return_value=True))
        self.enterContext(patch.object(ask_cli, 'installation_ready'))
        self.enterContext(patch.object(ask_cli, 'managed_session', side_effect=lambda **_: nullcontext()))
        self.enterContext(patch.object(task_cli.task_sandbox, 'run', return_value={'passed': True, 'output': 'synthetic test', 'check': 'python-syntax'}))
        self.payloads = []

    def run_flow(self, choice='podmień', target='hello.py', write='tak', *, mutation=None, deny_read=False, details=False, echo=False, refused=False):
        if deny_read:
            self.config.write_text(self.config.read_text().replace('read=["~/**"]', 'read=[]'))
        # A declined turn returns to the prompt (the conversation stays open), so each script ends with the empty line.
        script = [QUESTION, 'tak', choice]
        if choice != 'anuluj' and not deny_read and not refused:
            script.append(write)           # a refused plan ends the turn before any write question
        if mutation and refused:
            script.append('tak')           # an edited permissions.toml asks for the conversation's consent again
        replies = iter(script + [''])
        def reader(prompt):
            value = next(replies)
            if echo:
                print(prompt + value)
            if prompt == FOLLOWUP_PROMPT and mutation:
                mutation()
            return value
        def send(approval):
            data = json.loads(approval.payload)
            self.payloads.append(data)
            if data['mode'] == 'assistant':
                if any(p['mode'] == 'workspace' for p in self.payloads):
                    return json.dumps(DONE)        # after an applied change the model says the task is finished
                if len(self.payloads) == 1:
                    plan = {'action': 'edit', 'write': 'w0', 'files': [], 'create': ['hello.py'], 'check': 'python-syntax'}
                elif target == 'hello.py':
                    plan = {'action': 'edit', 'write': 'w0', 'files': [{'id': 'f0', 'path': target}], 'create': [], 'check': 'python-syntax'}
                else:
                    plan = {'action': 'edit', 'write': 'w0', 'files': [], 'create': [target], 'check': 'python-syntax'}
                return json.dumps(plan)
            return json.dumps({'summary': 'Plan zmiany syntetycznej.', 'files': [{'path': target, 'content': NEW}]})
        with patch.object(ask_cli, 'send_once', side_effect=send) as sent:
            code = ask_cli.main(['--v'] if details else [], reader=reader)
        return code, sent

    def test_replace_uses_new_transfer_consent_and_separate_write_approval_then_rollback(self):
        code, sent = self.run_flow()
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertEqual(sent.call_count, 4)
        self.assertEqual([p['mode'] for p in self.payloads], ['assistant', 'assistant', 'workspace', 'assistant'])
        self.assertNotIn(OLD, json.dumps(self.payloads[:2]))
        self.assertEqual(self.payloads[2]['task']['files'][0]['content'], OLD)
        self.assertIn('podmień', self.payloads[1]['question'])
        self.assertEqual(self.payloads[2]['task']['instruction'], self.payloads[1]['question'])
        self.assertEqual(self.target.read_text(), NEW)
        records = task_workspace.recovery_records(str(self.home))
        self.assertEqual(len(records), 1)
        self.assertTrue(task_workspace.rollback(str(self.home), records[0]['plan']))
        self.assertEqual(self.target.read_text(), OLD)

    def test_device_drift_history_does_not_hide_collision_or_authorize_write(self):
        from ai_desktop.task_plan import TaskPlan
        before = task_workspace.snapshot(str(self.home), ['hello.py'])
        source = {'instruction': 'synthetic', 'files': [
            {'path': 'hello.py', 'content': OLD, 'writable': True}]}
        old_plan = TaskPlan(before, source, {'summary': 'old history', 'files': [
            {'path': 'hello.py', 'content': OLD + '\n'}]}, 'python-syntax')
        task_workspace.apply(old_plan, old_plan.authorize('ZAPISZ ' + old_plan.sha256))
        journal = self.home / task_workspace.STATE / (old_plan.sha256 + '.json')
        value = json.loads(journal.read_text())
        value['before']['root'][0] ^= 1
        # Legacy history (schema 2) cannot prove its origin; schema 3 on Btrfs would rebind.
        value['schema'] = 2
        del value['before']['fs']
        journal.write_text(json.dumps(value))
        original = journal.read_bytes()
        # Local, independent edit: the new proposal must use this fresh value.
        self.target.write_text(OLD)
        code, sent = self.run_flow(write='nie')
        self.assertEqual(code, 2, self.out.getvalue())
        self.assertEqual(sent.call_count, 3)
        self.assertIn('istnieje w', self.out.getvalue())
        self.assertNotIn(task_workspace.DETACHED_HISTORY_HINT, self.out.getvalue())
        self.assertEqual(self.target.read_text(), OLD)
        self.assertEqual(journal.read_bytes(), original)
        self.assertEqual(len(task_workspace.recovery_records(str(self.home))), 1)
        self.payloads.clear()
        self.out.seek(0); self.out.truncate(0)
        code, sent = self.run_flow(write='nie', details=True)
        self.assertEqual(code, 2)
        self.assertEqual(sent.call_count, 3)
        self.assertIn(task_workspace.DETACHED_HISTORY_HINT, self.out.getvalue())
        self.assertEqual(self.target.read_text(), OLD)
        self.assertEqual(journal.read_bytes(), original)

    def test_full_collision_dialogue_spacing_keeps_write_refusal(self):
        code, sent = self.run_flow('edytuj istniejący', write='nie', echo=True)
        self.assertEqual(code, 2)
        self.assertEqual(sent.call_count, 3)
        output = self.out.getvalue()
        self.assertIn('Czekam na odpowiedź od Claude… Ctrl+C przerywa oczekiwanie.\n\n'
                      'Plik "hello.py" istnieje w ' + json.dumps(str(self.home)) + '.\n'
                      'Edytować, stworzyć nowy, anulować?\n> edytuj istniejący\n\n'
                      'Przygotuję nowy plan według tej odpowiedzi.\n', output)
        self.assertIn('Czekam na odpowiedź od Claude… Ctrl+C przerywa oczekiwanie.\n\nPropozycja:\n', output)
        self.assertIn('\n\n=== Zmiany do zapisania ===\n', output)
        self.assertIn('\n```\n\n' + PROMPTS['write'] + 'nie\n', output)
        for unwanted in ('Co mam zrobić zamiast tego?', 'Możesz odpowiedzieć', 'To wybór dalszego planu',
                         task_workspace.DETACHED_HISTORY_HINT):
            self.assertNotIn(unwanted, output)
        self.assertEqual(self.target.read_text(), OLD)
        self.assertNotIn(str(self.home), self.payloads[1]['question'])
        self.assertFalse((self.home / task_workspace.STATE).exists())

    def test_new_name_does_not_read_or_overwrite_existing_file(self):
        with patch.object(permissions, 'read_selected', wraps=permissions.read_selected) as read:
            code, sent = self.run_flow('zachowaj stary i utwórz hello2.py', target='hello2.py')
        self.assertEqual(code, 0, self.out.getvalue()); read.assert_not_called()
        self.assertEqual(sent.call_count, 4)
        self.assertEqual(self.target.read_text(), OLD)
        self.assertEqual((self.home/'hello2.py').read_text(), NEW)
        self.assertNotIn(OLD, json.dumps(self.payloads))

    def test_cancel_after_collision_leaves_original_and_history_untouched(self):
        code, sent = self.run_flow('anuluj')
        self.assertEqual(code, 2); self.assertEqual(sent.call_count, 1)
        self.assertEqual(self.target.read_text(), OLD)
        self.assertFalse((self.home/task_workspace.STATE).exists())

    def edit_after_discovery(self, write):
        plans = [{'action': 'list', 'files': ['f0']},
                 {'action': 'edit', 'write': 'w0', 'files': [{'id': 'f0', 'path': 'hello.py'}], 'create': [],
                  'check': 'python-syntax'}]
        replies = iter([QUESTION, 'tak', write, ''])

        def send(approval):
            data = json.loads(approval.payload)
            self.payloads.append(data)
            if data['mode'] == 'assistant':
                return json.dumps(plans.pop(0) if plans else DONE)
            return json.dumps({'summary': 'Plan zmiany syntetycznej.', 'files': [{'path': 'hello.py', 'content': NEW}]})
        with patch.object(ask_cli, 'send_once', side_effect=send):
            return ask_cli.main([], reader=lambda prompt: next(replies))

    def test_edit_after_discovery_shows_the_diff_and_writes_only_after_its_own_yes(self):
        self.assertEqual(self.edit_after_discovery('tak'), 0, self.out.getvalue())
        self.assertEqual([p['mode'] for p in self.payloads], ['assistant', 'assistant', 'workspace', 'assistant'])
        self.assertIn('discovery', self.payloads[1])
        self.assertIn('=== Zmiany do zapisania ===', self.out.getvalue())
        self.assertEqual(self.target.read_text(), NEW)

    def test_edit_after_discovery_declined_at_the_diff_leaves_the_file(self):
        self.assertEqual(self.edit_after_discovery('nie'), 2, self.out.getvalue())
        self.assertEqual(self.target.read_text(), OLD)
        self.assertFalse((self.home / task_workspace.STATE).exists())

    def test_edit_after_discovery_still_respects_the_deny_list(self):
        self.config.write_text(self.config.read_text().replace('deny=[]', 'deny=["~/hello.py"]'))
        self.edit_after_discovery('')                    # the turn is refused; the empty line ends the conversation
        self.assertEqual(self.target.read_text(), OLD)
        self.assertNotIn(OLD, json.dumps(self.payloads))

    def steps_flow(self, writes, answers):
        """The model proposes one new file after another; `writes` is how many it wants."""
        names = ['p%d.py' % i for i in range(writes)]
        questions, proposals = [], list(names)
        replies = iter([QUESTION, 'tak'] + answers + [''])

        def send(approval):
            data = json.loads(approval.payload)
            self.payloads.append(data)
            if data['mode'] == 'workspace':
                return json.dumps({'summary': 'Krok.', 'files': [{'path': data['task']['files'][0]['path'], 'content': NEW}]})
            questions.append(data['question'])
            if not proposals:
                return json.dumps(DONE)
            return json.dumps({'action': 'edit', 'write': 'w0', 'files': [], 'create': [proposals.pop(0)],
                               'check': 'python-syntax'})
        with patch.object(ask_cli, 'send_once', side_effect=send):
            return ask_cli.main([], reader=lambda prompt: next(replies)), questions

    def test_one_question_makes_several_changes_each_with_its_own_yes(self):
        code, questions = self.steps_flow(3, ['tak', 'tak', 'tak'])
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertEqual(self.out.getvalue().count('=== Zmiany do zapisania ==='), 3)
        for index in range(3):
            self.assertEqual((self.home / ('p%d.py' % index)).read_text(), NEW)
        self.assertEqual(questions[0], QUESTION)
        for question in questions[1:]:
            self.assertEqual(question, ask_cli.CONTINUE_PREFIX + QUESTION)   # the task stays, the prefix never stacks
        self.assertIn('Gotowe.', self.out.getvalue())

    def test_declining_one_step_ends_the_task_and_returns_to_the_user(self):
        code, questions = self.steps_flow(3, ['tak', 'nie'])
        self.assertEqual(code, 2)
        self.assertTrue((self.home / 'p0.py').exists())
        self.assertFalse((self.home / 'p1.py').exists())
        self.assertFalse((self.home / 'p2.py').exists())
        self.assertEqual(len(questions), 2)

    def test_the_automatic_steps_of_one_question_are_bounded(self):
        code, questions = self.steps_flow(ask_cli.MAX_AUTO_STEPS + 3, ['tak'] * (ask_cli.MAX_AUTO_STEPS + 1))
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertEqual(len(questions), ask_cli.MAX_AUTO_STEPS + 1)
        self.assertIn('Limit %d kolejnych kroków' % ask_cli.MAX_AUTO_STEPS, self.out.getvalue())
        self.assertFalse((self.home / ('p%d.py' % (ask_cli.MAX_AUTO_STEPS + 1))).exists())

    def test_a_long_task_still_fits_the_question_limit_after_the_prefix(self):
        text = 'ż' * (ask_cli.MAX_MESSAGE_BYTES // 2)
        following = ask_cli._continuation(text)
        ask_cli.validate_question(following, ask_cli.MAX_MESSAGE_BYTES)
        self.assertEqual(ask_cli._continuation(following), following)

    def delete_flow(self, answers, plan=None):
        replies = iter([QUESTION, 'tak'] + answers + [''])
        plan = plan or {'action': 'delete', 'write': 'w0', 'paths': ['hello.py']}

        def send(approval):
            self.payloads.append(json.loads(approval.payload))
            return json.dumps(DONE if len(self.payloads) > 1 else plan)
        with patch.object(ask_cli, 'send_once', side_effect=send) as sent:
            return ask_cli.main([], reader=lambda prompt: next(replies)), sent

    def test_delete_moves_the_file_to_a_restorable_trash_after_its_own_yes(self):
        code, sent = self.delete_flow(['tak'])
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertEqual(sent.call_count, 2)                # the plan, then the model's closing answer
        self.assertNotIn(OLD, json.dumps(self.payloads))
        self.assertFalse(self.target.exists())
        command = next(line for line in self.out.getvalue().splitlines() if line.startswith('Przywrócenie: '))
        plan = command.split('--plan ')[1].strip()
        from ai_desktop import workspace_trash
        workspace_trash.restore(str(self.home), plan)
        self.assertEqual(self.target.read_text(), OLD)

    def test_delete_declined_leaves_the_file(self):
        code, _ = self.delete_flow(['nie'])
        self.assertEqual(code, 2)
        self.assertEqual(self.target.read_text(), OLD)
        self.assertIn('Nic nie usunięto', self.out.getvalue())

    def test_delete_refuses_a_denied_file_without_asking(self):
        self.config.write_text(self.config.read_text().replace('deny=[]', 'deny=["~/hello.py"]'))
        code, _ = self.delete_flow([])
        self.assertEqual(code, 2)
        self.assertEqual(self.target.read_text(), OLD)
        self.assertIn('Nie zapisano zmian', self.out.getvalue())

    def test_delete_refuses_a_missing_file_a_directory_and_a_symlink(self):
        (self.home / 'folder').mkdir()
        (self.home / 'link').symlink_to(self.target)
        for name in ('nie-ma.py', 'folder', 'link'):
            with self.subTest(name=name):
                self.payloads.clear()
                code, _ = self.delete_flow([], {'action': 'delete', 'write': 'w0', 'paths': [name]})
                self.assertEqual(code, 2)
                self.assertIn('Nie zapisano zmian', self.out.getvalue())
        self.assertEqual(self.target.read_text(), OLD)
        self.assertTrue((self.home / 'link').is_symlink())

    def test_delete_needs_a_write_scope(self):
        self.config.write_text(self.config.read_text().replace('write=["~/**"]', 'write=[]'))
        code, _ = self.delete_flow([])
        self.assertEqual(code, 2)
        self.assertEqual(self.target.read_text(), OLD)

    def test_a_write_scope_without_a_directory_is_not_offered_to_the_model(self):
        self.config.write_text(self.config.read_text().replace('write=["~/**"]', 'write=["~/brak/","~/**"]'))
        from ai_desktop import assistant_flow
        self.assertEqual([item['id'] for item in assistant_flow.capture_access().access['write']], ['w1'])
        self.config.write_text(self.config.read_text().replace('write=["~/brak/","~/**"]', 'write=["~/brak/"]'))
        self.assertNotIn('write', assistant_flow.capture_access().access)

    def test_replace_reply_does_not_substitute_for_final_write_consent(self):
        code, sent = self.run_flow(write='nie')
        self.assertEqual(code, 2); self.assertEqual(sent.call_count, 3)
        self.assertEqual(self.target.read_text(), OLD)
        self.assertFalse((self.home/task_workspace.STATE).exists())

    def test_fresh_followup_policy_deny_prevents_second_file_read_and_write(self):
        def revoke():
            self.config.write_text(self.config.read_text().replace('deny=[]', 'deny=["~/hello.py"]'))
        with patch.object(permissions, 'read_selected', wraps=permissions.read_selected) as read:
            code, sent = self.run_flow(mutation=revoke, refused=True)
        self.assertEqual(code, 2); self.assertEqual(sent.call_count, 2)
        read.assert_not_called(); self.assertEqual(self.target.read_text(), OLD)

    def test_write_grant_does_not_create_read_permission(self):
        code, sent = self.run_flow(deny_read=True)
        self.assertEqual(code, 2); self.assertEqual(sent.call_count, 2)
        self.assertEqual(self.target.read_text(), OLD)
        self.assertNotIn(OLD, json.dumps(self.payloads))

    def test_collision_never_reads_regular_symlink_directory_or_fifo_contents(self):
        for name, make in (('regular', lambda p: p.write_text('PRIVATE')),
                           ('symlink', lambda p: p.symlink_to(self.target)),
                           ('directory', lambda p: p.mkdir()),
                           ('pipe', lambda p: os.mkfifo(p))):
            p = self.home/name; make(p)
            with self.subTest(name=name), patch.object(task_workspace, 'read_file', side_effect=AssertionError('unexpected read')):
                with self.assertRaises(task_workspace.NewTargetExists) as caught:
                    task_workspace.snapshot(str(self.home), [name], must_be_absent=[name])
            self.assertEqual(caught.exception.path, name)
        self.assertEqual(self.target.read_text(), OLD)

    def test_symlink_is_not_followed_when_user_requests_replacement(self):
        self.target.unlink(); self.target.symlink_to(self.home/'elsewhere')
        (self.home/'elsewhere').write_text(OLD)
        code, sent = self.run_flow(refused=True)
        self.assertEqual(code, 2); self.assertEqual(sent.call_count, 2)
        self.assertTrue(self.target.is_symlink())
        self.assertEqual((self.home/'elsewhere').read_text(), OLD)


if __name__ == '__main__':
    unittest.main()
