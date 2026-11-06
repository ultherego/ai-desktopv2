"""End-to-end through the real components: project loop -> consent chain -> edit pipeline -> journal -> rollback / trash / restore.

Only the model transport, the A+ session and the isolated test sandbox are replaced. Everything else (policy, scope
mapping, QuestionConsent, ask_edit.prepare, the task executor and its journal, workspace_trash, task_cli) is real, on a
synthetic project under a temporary HOME.
"""
import contextlib
from contextlib import nullcontext
import io
import json
import os
from unittest.mock import patch

from ai_desktop import project_cli as pcli, task_cli, task_workspace as tw, workspace_trash
from ai_desktop.terminal_consent import PROMPTS
from tests.test_project_cli import Base, script

SANDBOX_OK = {"passed": True, "output": ""}


class Indicator:
    def __init__(self, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def phase(self, name): pass


class ChainBase(Base):
    write = '["~/proj/**"]'

    def setUp(self):
        super().setUp()
        self.config.write_text('version = 2\n[files]\nread = ["~/proj/**"]\nwrite = %s\ndeny = ["~/proj/secret/**"]\n'
                               '[network]\nclaude = true\n' % self.write)
        self.config.chmod(0o600)
        self.workspace = str(self.proj if "proj" in self.write else self.home)
        self.prefix = "" if "proj" in self.write else "proj/"
        self.transported = []

    def transport(self, content_for):
        """A model stand-in for the workspace request: it answers with new content for every writable file."""
        def send(approval):
            payload = json.loads(approval.payload)
            self.transported.append(payload)
            files = [{"path": f["path"], "content": content_for(f["path"], f["content"])}
                     for f in payload["task"]["files"] if f["writable"]]
            return json.dumps({"summary": "Zmiana testowa.", "files": files})
        return send

    def session(self, commands, answers, send):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(pcli.ask_cli, "managed_session", lambda **k: nullcontext()))
        stack.enter_context(patch.object(pcli.ask_cli, "send_once", send))
        stack.enter_context(patch.object(pcli.ask_cli, "installation_ready"))
        stack.enter_context(patch.object(pcli.ask_cli, "WaitingIndicator", Indicator))
        stack.enter_context(patch.object(task_cli.task_sandbox, "run", return_value=SANDBOX_OK))
        with stack:
            self.run_cli(commands + ["/exit"], answers)

    def cli(self, *argv, answer="yes"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(task_cli.sys.stdin, "isatty", return_value=True), \
                patch.object(task_cli.sys.stdout, "isatty", return_value=True):
            code = task_cli.main(list(argv), reader=lambda prompt: answer)
        return code, out.getvalue()


class ChainTests:
    def test_a_file_changed_after_analysis_is_not_silently_sent_as_an_already_read_file(self):
        target = self.proj / "src/util.py"
        actions = script({"action": "read", "path": "src/util.py", "ranges": [{"first": 1, "last": 2}]},
                         {"action": "edit", "files": ["src/util.py"], "create": [], "check": "text"})

        def model(payload):
            answer = actions(payload)
            if json.loads(answer)["action"] == "edit":
                target.write_text("new private version\n")
            return answer

        self.model = model
        self.session(["zmień util.py"], ["yes"], self.transport(lambda path, old: old + "new\n"))
        self.assertEqual(self.transported, [])
        self.assertEqual(target.read_text(), "new private version\n")
        self.assertIn("The file changed, so the task was stopped.", self.output)
        self.assertNotIn(PROMPTS["project-edit"], self.prompts)  # stop, do not nag for another yes

    def test_a_file_changed_during_the_extra_transfer_question_never_reaches_the_edit_transport(self):
        target = self.proj / "docs/readme.txt"
        self.model = script({"action": "edit", "files": ["docs/readme.txt"], "create": [], "check": "text"})
        real = pcli.confirm

        def confirm(action, **kwargs):
            answer = real(action, **kwargs)
            if action == "project-edit" and answer:
                target.write_text("changed while the question was displayed\n")
            return answer

        with patch.object(pcli, "confirm", confirm):
            self.session(["zmień readme"], ["yes", "yes"], self.transport(lambda path, old: old + "new\n"))
        self.assertEqual(self.transported, [])
        self.assertIn("The file changed, so the task was stopped.", self.output)
        self.assertEqual(target.read_text(), "changed while the question was displayed\n")

    def test_changed_deletion_consent_refuses_and_partial_recovery_is_visible_in_status(self):
        a, b = self.proj / "docs/readme.txt", self.proj / "src/util.py"
        self.model = script({"action": "edit", "files": [], "create": [], "delete": ["docs/readme.txt"], "check": "text"})
        real_confirm = pcli.confirm

        def confirm(action, **kwargs):
            answer = real_confirm(action, **kwargs)
            if action == "project-delete" and answer:
                a.write_text("changed during deletion consent\n")
            return answer

        with patch.object(pcli, "confirm", confirm):
            self.session(["usuń readme"], ["yes", "yes"], self.transport(lambda path, old: old))
        self.assertTrue(a.exists())
        self.assertIn("Nothing was deleted", self.output)
        self.assertEqual(workspace_trash.recovery_records(self.workspace), [])
        self.model = script({"action": "edit", "files": [], "create": [],
                             "delete": ["docs/readme.txt", "src/util.py"], "check": "text"})
        real_save, calls = tw._save, []

        def save(*args, **kwargs):
            result = real_save(*args, **kwargs)
            calls.append(1)
            if len(calls) == 2:
                a.write_text("concurrent replacement\n")
                b.write_text("concurrent edit\n")
            return result

        with patch.object(tw, "_save", save):
            self.session(["usuń oba"], ["yes", "yes"], self.transport(lambda path, old: old))
        self.assertNotIn("Nothing was deleted", self.output)
        self.assertIn("Deletion interrupted", self.output)
        record, = workspace_trash.recovery_records(self.workspace)
        self.assertIn(record["plan"], self.output)
        code, output = self.cli("status", "--workspace", self.workspace)
        self.assertEqual(code, 0, output)
        self.assertIn(record["plan"], output)
        self.assertIn("task restore", output)
        self.assertEqual((a.read_text(), b.read_text()), ("concurrent replacement\n", "concurrent edit\n"))

    def test_create_modify_rollback_delete_restore_through_the_real_pipeline(self):
        target = self.proj / "docs/new.txt"
        # 1. create (project consent, then the write consent)
        self.model = script({"action": "edit", "files": [], "create": ["docs/new.txt"], "check": "text"})
        self.session(["stwórz docs/new.txt"], ["yes", "yes"], self.transport(lambda path, old: "pierwsza wersja\n"))
        self.assertEqual(target.read_text(), "pierwsza wersja\n")
        self.assertIn("Changes saved.", self.output)
        self.assertIn("--workspace %s" % self.workspace, self.output)
        self.assertEqual(self.transported[0]["task"]["files"][0]["path"], self.prefix + "docs/new.txt")
        ((plan, status),) = tw.status(self.workspace)
        self.assertEqual(status, "applied")
        # 2. roll it back with the real command: the new file disappears
        code, text = self.cli("rollback", "--workspace", self.workspace, "--plan", plan)
        self.assertEqual(code, 0, text)
        self.assertFalse(target.exists())
        # 3. create again, then modify it (the file was read whole in the same task, so no second question)
        self.model = script({"action": "edit", "files": [], "create": ["docs/new.txt"], "check": "text"})
        self.session(["stwórz ponownie"], ["yes", "yes"], self.transport(lambda path, old: "wersja 1\n"))
        self.model = script({"action": "read", "path": "docs/new.txt", "ranges": [{"first": 1, "last": 5}]},
                            {"action": "edit", "files": ["docs/new.txt"], "create": [], "check": "text"})
        target.chmod(0o640)
        self.session(["dopisz linię"], ["yes", "yes"], self.transport(lambda path, old: old + "wersja 2\n"))
        self.assertEqual(target.read_text(), "wersja 1\nwersja 2\n")
        self.assertNotIn(PROMPTS["project-edit"], self.prompts)
        # 4. delete it: it moves to the trash, other task edits in the same workspace keep working afterwards
        self.model = script({"action": "edit", "files": [], "create": [], "delete": ["docs/new.txt"], "check": "text"})
        self.session(["usuń docs/new.txt"], ["yes", "yes"], self.transport(lambda path, old: old))
        self.assertFalse(target.exists())
        trash_plan = [l for l in self.output.splitlines() if l.startswith("Restore:")][0].rsplit(" ", 1)[1]
        other = self.proj / "docs/other.txt"
        self.model = script({"action": "edit", "files": [], "create": ["docs/other.txt"], "check": "text"})
        self.session(["stwórz other"], ["yes", "yes"], self.transport(lambda path, old: "inny\n"))
        self.assertEqual(other.read_text(), "inny\n")                       # the trash entry did not block the edit
        # 5. restore the deleted file with the real command: same bytes and mode as before the deletion
        code, text = self.cli("restore", "--workspace", self.workspace, "--plan", trash_plan)
        self.assertEqual(code, 0, text)
        self.assertEqual(target.read_text(), "wersja 1\nwersja 2\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_restore_never_overwrites_a_file_created_in_the_meantime_and_status_lists_every_journal(self):
        target = self.proj / "docs/readme.txt"
        self.model = script({"action": "edit", "files": [], "create": [], "delete": ["docs/readme.txt"], "check": "text"})
        self.session(["usuń readme"], ["yes", "yes"], self.transport(lambda path, old: old))
        plan = [l for l in self.output.splitlines() if l.startswith("Restore:")][0].rsplit(" ", 1)[1]
        target.write_text("nowy plik o tej samej nazwie\n")
        code, text = self.cli("restore", "--workspace", self.workspace, "--plan", plan)
        self.assertEqual(code, 2)
        self.assertIn("Conflict", text)
        self.assertEqual(target.read_text(), "nowy plik o tej samej nazwie\n")
        code, text = self.cli("status", "--workspace", self.workspace)
        self.assertEqual(code, 0, text)                                       # status reads fine with a trash entry present

    def test_a_change_the_user_declines_at_the_diff_writes_nothing_and_leaves_no_blocking_journal(self):
        self.model = script({"action": "edit", "files": [], "create": ["docs/never.txt"], "check": "text"})
        self.session(["stwórz never"], ["yes", "no"], self.transport(lambda path, old: "x\n"))
        self.assertFalse((self.proj / "docs/never.txt").exists())
        self.assertIn("Save cancelled. The project is unchanged.", self.output)
        self.model = script({"action": "edit", "files": [], "create": ["docs/later.txt"], "check": "text"})
        self.session(["stwórz later"], ["yes", "yes"], self.transport(lambda path, old: "y\n"))
        self.assertEqual((self.proj / "docs/later.txt").read_text(), "y\n")


class ProjectScopeChainTests(ChainTests, ChainBase):
    def test_edit_carries_exact_authorized_project_history_and_observations(self):
        marker = "synthetic-semicolon-choice"
        self.model = script({"action": "answer", "text": marker, "sources": []},
                            {"action": "read", "path": "src/util.py", "ranges": [{"first": 1, "last": 2}]},
                            {"action": "edit", "files": ["src/util.py"], "create": [], "check": "text"})
        self.session(["Which delimiter?", "Apply it."], ["yes", "yes", "no"],
                     self.transport(lambda path, old: old + "# selected delimiter\n"))
        edited, = self.transported
        context = edited["task"]["project_context"]
        self.assertEqual(edited["task"]["instruction"], "Apply it.")
        self.assertEqual(context, self.payloads()[-1]["project"])
        self.assertEqual(context["history"][-1]["answer"], marker)
        self.assertIn("return 1", str(context["observations"]))
        self.assertNotIn("TOP-SECRET", json.dumps(edited))
        self.assertEqual((self.proj / "src/util.py").read_text(), "def helper():\n    return 1\n")

    def test_new_policy_between_the_planner_and_edit_cannot_authorize_old_observations(self):
        def model(_payload):
            self.config.write_text(self.config.read_text() + "# a different policy, same project scopes\n")
            return json.dumps({"action": "edit", "files": [], "create": ["docs/new.txt"], "check": "text"})
        self.model = model
        self.session(["Use our earlier findings."], ["yes"], self.transport(lambda path, old: "new"))
        self.assertEqual(self.transported, [])
        self.assertFalse((self.proj / "docs/new.txt").exists())

    write = '["~/proj/**"]'


class HomeWideScopeChainTests(ChainTests, ChainBase):
    """`write = ["~/**"]` (as in the owner's policy): the workspace is the whole HOME, paths are prefixed with proj/."""
    write = '["~/**"]'


class UnsafeWorkspaceTests(ChainBase):
    write = '["~/proj/**"]'

    def test_a_write_root_with_group_write_permission_is_named_instead_of_failing_silently(self):
        self.proj.chmod(0o770)
        self.model = script({"action": "edit", "files": [], "create": ["docs/x.txt"], "check": "text"})
        self.session(["stwórz x"], ["yes"], self.transport(lambda path, old: "x\n"))
        self.assertRegex(self.output, r"Refused\s+The write scope in permissions.toml does not point to a safe working directory")
        self.assertFalse((self.proj / "docs/x.txt").exists())
