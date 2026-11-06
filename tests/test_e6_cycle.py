"""E6 full task cycle through the public task CLI with a fake model.

The model, the sandbox and the managed session are replaced by mocks, which
also lets every refusal case assert that the model was NEVER started. After
each case the filesystem is compared independently of any printed message.
"""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
import contextlib
import io
import json
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import task_cli as cli
from ai_desktop.terminal_consent import PROMPTS
from scripts import e6_cycle as e6
from tests.profile_fixture import DEFAULT_PROFILE

NEW_CALC = "def add(a, b):\n    \"\"\"Sum.\"\"\"\n    return a + b\n"


def proposal(files):
    return json.dumps({"summary": "synthetic change", "files": files})


class CycleBase(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.geteuid(), 0)
        # This cycle exercises task consent/filesystem transitions, not saved
        # agent selection. A developer's read-only/Codex profile must not alter
        # these scenarios, and no private settings may be read by accident.
        self.enterContext(patch("ai_desktop.agent_profiles.resolve",
                                side_effect=lambda *args, **kwargs: dict(DEFAULT_PROFILE)))
        self.enterContext(patch("ai_desktop.agent_profiles.Store",
                                side_effect=AssertionError("E6 cycle must not read private agent settings")))
        self.temp = tempfile.TemporaryDirectory(prefix="ai-e6-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "ws"
        self.root.mkdir(mode=0o700)
        self.expected = e6.create_project(self.root)
        self.outside = self.base / "outside.txt"
        self.outside.write_text("OUTSIDE\n")

    def run_task(self, args=None, *, answers=("yes", "yes"), response=None, passed=True,
                 before_send=None, on_prompt=None):
        args = args or ["run", "change calc", "--workspace", str(self.root), "--file", "calc.py",
                        "--test", "python-syntax"]
        replies, self.prompts, out = iter(answers), [], io.StringIO()

        def reader(prompt):
            self.prompts.append(prompt)
            if on_prompt:
                on_prompt(prompt)
            answer = next(replies)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        def send(approval):
            if before_send:
                before_send()
            return response if response is not None else proposal(
                [{"path": "calc.py", "content": NEW_CALC}])
        result = {"passed": passed, "output": "synthetic", "check": "python-syntax", "exit_code": 0 if passed else 1}
        with contextlib.redirect_stdout(out), patch.object(cli.sys.stdin, "isatty", return_value=True), \
                patch.object(cli.sys.stdout, "isatty", return_value=True), \
                patch.object(cli, "installation_ready"), \
                patch.object(cli, "managed_session", return_value=contextlib.nullcontext()) as session, \
                patch.object(cli, "send_once", side_effect=send) as model, \
                patch.object(cli.task_sandbox, "run", return_value=result) as sandbox:
            code = cli.main(args, reader=reader)
        self.mocks = (model, session, sandbox)
        return code, out.getvalue()

    def assert_model_never_started(self):
        for mock in self.mocks:
            mock.assert_not_called()

    def assert_unchanged(self):
        self.assertEqual(e6.check_unchanged(self.root, self.expected), [])


class Cycle(CycleBase):
    # --- refusals before any session ---------------------------------------------------
    def test_declined_transfer_starts_nothing_and_changes_nothing(self):
        code, output = self.run_task(answers=("no",))
        self.assertEqual((code, self.prompts), (2, [PROMPTS["task-scope"]]))
        self.assert_model_never_started()
        self.assert_unchanged()

    def test_forbidden_targets_are_refused_before_transfer_prompt_or_session(self):
        (self.root / "link").symlink_to(self.outside)
        home = pwd.getpwuid(os.geteuid()).pw_dir
        cases = [("--file", "../outside.txt"), ("--file", "/etc/passwd"), ("--file", ".git/config"),
                 ("--file", "link"), ("--file", "docs/../calc.py"), ("--file", ".ssh/id"),
                 ("--file", ".ai-desktop-tasks/x.json"), ("--file", "missing/dir/new.py")]
        for flag, value in cases:
            with self.subTest(target=value):
                self.expected = e6.manifest(self.root)
                args = ["run", "x", "--workspace", str(self.root), flag, value, "--test", "text"]
                code, _ = self.run_task(args)
                self.assertEqual((code, self.prompts), (2, []))
                self.assert_model_never_started()
                self.assert_unchanged()
        code, _ = self.run_task(["run", "x", "--workspace", home, "--file", "calc.py", "--test", "text"])
        self.assertEqual((code, self.prompts), (2, []))
        self.assert_model_never_started()

    def test_non_terminal_refuses_before_reading_anything(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(cli.sys.stdin, "isatty", return_value=False), \
                patch.object(cli, "send_once") as model:
            code = cli.main(["run", "x", "--workspace", str(self.root), "--file", "calc.py", "--test", "text"])
        self.assertEqual(code, 2)
        model.assert_not_called()
        self.assert_unchanged()

    # --- cancellation --------------------------------------------------------------------
    def test_cancellation_at_each_prompt_changes_nothing_and_is_not_retried(self):
        for label, answers in (("transfer-eof", (EOFError(),)), ("transfer-ctrl-c", (KeyboardInterrupt(),)),
                               ("write-ctrl-c", ("yes", KeyboardInterrupt())),
                               ("write-eof", ("yes", EOFError()))):
            with self.subTest(case=label):
                code, output = self.run_task(answers=answers)
                self.assertEqual(code, 2, output)
                self.assertIn("Cancelled", output)
                self.assertLessEqual(self.mocks[0].call_count, 1)
                self.assertLessEqual(len(self.prompts), 2)
                self.assert_unchanged()

    # --- refusals around the diff ------------------------------------------------------
    def test_declined_write_at_diff_keeps_everything(self):
        code, output = self.run_task(answers=("yes", "no"))
        self.assertEqual((code, self.prompts), (2, [PROMPTS["task-scope"], PROMPTS["write"]]))
        self.assertIn("```diff", output)
        self.mocks[0].assert_called_once()
        self.assert_unchanged()

    def test_file_changed_after_plan_prepared_is_refused_and_user_edit_survives(self):
        def edit():
            (self.root / "calc.py").write_text("USER EDIT\n")
        for label, kwargs in (("while-model-runs", {"before_send": edit}),
                              ("at-write-prompt", {"on_prompt": lambda p: edit() if p == PROMPTS["write"] else None})):
            with self.subTest(case=label):
                self.setUp()
                code, output = self.run_task(**kwargs)
                self.assertEqual(code, 2, output)
                self.expected["calc.py"] = [self.expected["calc.py"][0], self.expected["calc.py"][1],
                                            e6.sha(b"USER EDIT\n")]
                self.assertEqual((self.root / "calc.py").read_text(), "USER EDIT\n")
                self.assert_unchanged()

    def test_proposal_for_unselected_or_forbidden_paths_is_refused_before_test_and_write(self):
        for path in ("README.txt", "docs/notes.txt", "evil.py", ".ssh/authorized_keys", "../outside.txt",
                     "/etc/passwd", ".ai-desktop-tasks/x.json", "calc.py/../README.txt"):
            with self.subTest(path=path):
                code, output = self.run_task(response=proposal([{"path": path, "content": "x\n"}]),
                                             answers=("yes", "yes"))
                self.assertEqual(code, 2, output)
                self.assertEqual(self.prompts, [PROMPTS["task-scope"]])
                self.mocks[2].assert_not_called()
                self.assert_unchanged()
        self.assertEqual(self.outside.read_text(), "OUTSIDE\n")

    def test_proposal_identical_to_the_file_says_so_and_never_offers_a_write(self):
        same = proposal([{"path": "calc.py", "content": e6.FILES["calc.py"][0]}])
        code, output = self.run_task(response=same, answers=("yes", "yes"))
        self.assertEqual((code, self.prompts), (2, [PROMPTS["task-scope"]]))
        self.assertIn("No change was proposed: the file already has this content. Nothing was saved.", output)
        self.mocks[2].assert_not_called()
        self.assert_unchanged()

    def test_failed_proposal_test_never_offers_a_write(self):
        code, output = self.run_task(passed=False)
        self.assertEqual((code, self.prompts), (2, [PROMPTS["task-scope"]]))
        self.assertIn("failed", output)
        self.assert_unchanged()

    # --- success and rollback ----------------------------------------------------------
    def test_valid_write_is_verified_on_disk_and_rolled_back_exactly(self):
        code, output = self.run_task()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.prompts, [PROMPTS["task-scope"], PROMPTS["write"]])
        self.assertEqual(e6.check_written(self.root, self.expected), [])
        self.assertEqual((self.root / "calc.py").read_text(), NEW_CALC)
        self.assertEqual(oct((self.root / "calc.py").stat().st_mode & 0o777), oct(0o640))
        # The unselected files are byte-identical and keep their modes.
        for name in ("README.txt", "docs/notes.txt"):
            self.assertEqual(e6.manifest(self.root)[name], self.expected[name])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(cli.sys.stdin, "isatty", return_value=True), \
                patch.object(cli.sys.stdout, "isatty", return_value=True):
            rolled = cli.main(["rollback", "--workspace", str(self.root)], reader=lambda prompt: "yes")
        self.assertEqual(rolled, 0, out.getvalue())
        self.assertEqual(e6.check_rolled_back(self.root, self.expected), [])
        self.assertEqual((self.root / "calc.py").read_bytes(), e6.FILES["calc.py"][0].encode())

    def test_declined_rollback_keeps_the_written_state(self):
        self.assertEqual(self.run_task()[0], 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(cli.sys.stdin, "isatty", return_value=True), \
                patch.object(cli.sys.stdout, "isatty", return_value=True):
            self.assertEqual(cli.main(["rollback", "--workspace", str(self.root)], reader=lambda p: "no"), 2)
        self.assertEqual(e6.check_written(self.root, self.expected), [])

    def test_rollback_after_user_edit_is_a_conflict_and_keeps_the_edit(self):
        self.assertEqual(self.run_task()[0], 0)
        (self.root / "calc.py").write_text("USER EDIT\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(cli.sys.stdin, "isatty", return_value=True), \
                patch.object(cli.sys.stdout, "isatty", return_value=True):
            code = cli.main(["rollback", "--workspace", str(self.root)], reader=lambda p: "yes")
        self.assertEqual(code, 2)
        self.assertEqual((self.root / "calc.py").read_text(), "USER EDIT\n")
        self.assertNotEqual(e6.check_rolled_back(self.root, self.expected), [])


class CheckerSelfTests(CycleBase):
    """The independent checks must fail on a wrong state, not only pass on a right one."""

    def written(self):
        self.assertEqual(self.run_task()[0], 0)

    def test_unchanged_check_detects_every_kind_of_drift(self):
        for mutate in (lambda: (self.root / "calc.py").write_text("x\n"),
                       lambda: (self.root / "README.txt").chmod(0o644),
                       lambda: (self.root / "extra").write_text("x"),
                       lambda: (self.root / "docs/notes.txt").unlink(),
                       lambda: (self.root / e6.STATE).mkdir()):
            with self.subTest():
                self.setUp()
                mutate()
                self.assertNotEqual(e6.check_unchanged(self.root, self.expected), [])

    def test_written_check_detects_extra_changes_and_wrong_mode_or_journal(self):
        for mutate in (lambda: (self.root / "README.txt").write_text("x\n"),
                       lambda: (self.root / "calc.py").chmod(0o600),
                       lambda: (self.root / "calc.py").write_text("tampered\n"),
                       lambda: [p.unlink() for p in (self.root / e6.STATE).glob("*.json")]):
            with self.subTest():
                self.setUp()
                self.written()
                self.assertEqual(e6.check_written(self.root, self.expected), [])
                mutate()
                self.assertNotEqual(e6.check_written(self.root, self.expected), [])

    def test_rolled_back_check_detects_wrong_mode_bytes_or_status(self):
        for mutate in (lambda: (self.root / "calc.py").chmod(0o600),
                       lambda: (self.root / "calc.py").write_text("other\n"),
                       lambda: [p.write_text(p.read_text().replace("rolled-back", "applied"))
                                for p in (self.root / e6.STATE).glob("*.json")]):
            with self.subTest():
                self.setUp()
                self.written()
                with contextlib.redirect_stdout(io.StringIO()), \
                        patch.object(cli.sys.stdin, "isatty", return_value=True), \
                        patch.object(cli.sys.stdout, "isatty", return_value=True):
                    self.assertEqual(cli.main(["rollback", "--workspace", str(self.root)], reader=lambda p: "yes"), 0)
                self.assertEqual(e6.check_rolled_back(self.root, self.expected), [])
                mutate()
                self.assertNotEqual(e6.check_rolled_back(self.root, self.expected), [])


if __name__ == "__main__":
    unittest.main()
