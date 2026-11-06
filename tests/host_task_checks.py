"""Real kernel checks; no AI/OAuth, with writes only to disposable synthetic files."""
import contextlib
import json
import os
from pathlib import Path
import pty
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import task_cli, task_contract, task_sandbox, task_workspace
from ai_desktop.task_plan import TaskPlan
from ai_desktop.terminal_consent import PROMPTS
from tests.profile_fixture import DEFAULT_PROFILE


class TaskKernelTests(unittest.TestCase):
    def check(self, files, check="python-unittest"):
        source = {"instruction": "synthetic", "files": [{"path": p, "content": None, "writable": True} for p in files]}
        return task_sandbox.run(source, [{"path": p, "content": c} for p, c in files.items()], check)

    def test_real_declared_task_scope_refusal_write_and_separate_rollback(self):
        self.assertNotEqual(os.geteuid(), 0, "Run host acceptance as the ordinary desktop user")
        self.assertEqual(os.getuid(), os.geteuid())
        original = b"def add(a, b):\n    return a - b\n"
        changed = b"def add(a, b):\n    return a + b\n"
        test_bytes = (b"import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n"
                      b" def test_add(self): self.assertEqual(add(2, 3), 5)\n")
        with tempfile.TemporaryDirectory(prefix="ai-task-consent-", dir=Path.home()) as directory:
            root = Path(directory)
            target, context = root / "calc.py", root / "test_calc.py"
            target.write_bytes(original)
            target.chmod(0o640)
            context.write_bytes(test_bytes)
            context.chmod(0o600)
            run = ["run", "Synthetic addition fix", "--workspace", directory,
                   "--file", "calc.py", "--context", "test_calc.py", "--test", "python-unittest"]
            proposal = json.dumps({"summary": "Synthetic addition fix", "files": [
                {"path": "calc.py", "content": changed.decode("utf-8")}]})
            # Exercise the real terminal guard using a PTY. The explicit reader
            # supplies only the declared decisions, never an implicit extra yes.
            master, slave = pty.openpty()
            with contextlib.ExitStack() as stack:
                stack.callback(os.close, master)
                terminal_out = stack.enter_context(os.fdopen(slave, "w", encoding="utf-8", buffering=1))
                terminal_in = stack.enter_context(os.fdopen(os.dup(slave), "r", encoding="utf-8"))
                os.set_blocking(master, False)
                stack.enter_context(patch.object(task_cli.sys, "stdin", terminal_in))
                stack.enter_context(contextlib.redirect_stdout(terminal_out))
                stack.enter_context(patch("ai_desktop.agent_profiles.resolve",
                                          side_effect=lambda *a, **kw: dict(DEFAULT_PROFILE)))
                stack.enter_context(patch("ai_desktop.agent_profiles.Store",
                                          side_effect=AssertionError("Host fixture must not read private settings")))
                stack.enter_context(patch.object(task_cli, "installation_ready"))
                runtime = stack.enter_context(patch.object(task_cli, "managed_session",
                                                           return_value=contextlib.nullcontext()))
                model = stack.enter_context(patch.object(task_cli, "send_once", return_value=proposal))
                # Observe calls while executing the actual sandbox and its kernel guards.
                sandbox = stack.enter_context(patch.object(task_sandbox, "run", wraps=task_sandbox.run))

                def drain():
                    terminal_out.flush()
                    data = bytearray()
                    while True:
                        try:
                            data.extend(os.read(master, 4096))
                        except BlockingIOError:
                            return data.decode("utf-8").replace("\r\n", "\n")

                def invoke(arguments, decisions):
                    expected = iter(decisions)
                    prompts, chunks = [], []
                    def reader(prompt):
                        chunks.append(drain())
                        wanted, answer = next(expected)
                        self.assertEqual(prompt, PROMPTS[wanted])
                        prompts.append(prompt)
                        terminal_out.write(prompt)
                        return answer
                    code = task_cli.main(arguments, reader=reader)
                    chunks.append(drain())
                    self.assertEqual(list(expected), [], "A required decision was not requested")
                    return code, "".join(chunks), prompts

                code, output, prompts = invoke(run, [("task-scope", "no")])
                self.assertEqual(code, 2, output)
                self.assertEqual(prompts, [PROMPTS["task-scope"]])
                model.assert_not_called()
                runtime.assert_not_called()
                sandbox.assert_not_called()
                self.assertEqual(target.read_bytes(), original)
                self.assertFalse((root / task_workspace.STATE).exists())

                code, output, prompts = invoke(run, [("task-scope", "yes"), ("write", "yes")])
                self.assertEqual(code, 0, output)
                self.assertEqual(prompts, [PROMPTS["task-scope"], PROMPTS["write"]])
                self.assertLess(output.index("Declared test: python-unittest"), output.index(PROMPTS["task-scope"]))
                self.assertLess(output.index("Test on a copy: passed (python-unittest)"), output.index("```diff\n"))
                self.assertLess(output.index("```diff\n"), output.index(PROMPTS["write"]))
                self.assertEqual(target.read_bytes(), changed)
                self.assertEqual(target.stat().st_mode & 0o777, 0o640)
                self.assertEqual(context.read_bytes(), test_bytes)
                self.assertEqual(context.stat().st_mode & 0o777, 0o600)
                records = task_workspace.recovery_records(directory)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["status"], "applied")

                undo = ["rollback", "--workspace", directory]
                code, output, prompts = invoke(undo, [("rollback", "no")])
                self.assertEqual(code, 2, output)
                self.assertEqual(prompts, [PROMPTS["rollback"]])
                self.assertEqual(target.read_bytes(), changed)
                code, output, prompts = invoke(undo, [("rollback", "yes")])
                self.assertEqual(code, 0, output)
                self.assertEqual(prompts, [PROMPTS["rollback"]])
                self.assertIn("Changes undone.", output)
                self.assertEqual(target.read_bytes(), original)
                self.assertEqual(target.stat().st_mode & 0o777, 0o640)
                self.assertEqual(context.read_bytes(), test_bytes)
                self.assertEqual(task_workspace.recovery_records(directory)[0]["status"], "rolled-back")
                model.assert_called_once()
                runtime.assert_called_once_with(quiet=True)
                sandbox.assert_called_once()
                self.assertEqual(sandbox.call_args.args[2], "python-unittest")

    def test_real_unittest_success_failure_and_empty_refusal(self):
        files = {"calc.py": "def add(a,b): return a+b\n", "test_calc.py": "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n"}
        result = self.check(files)
        self.assertTrue(result["passed"], result)
        files["calc.py"] = "def add(a,b): return a-b\n"
        self.assertFalse(self.check(files)["passed"])
        self.assertFalse(self.check({"calc.py": "x=1\n"})["passed"])

    def test_real_copy_accepts_mib_files_and_many_files_without_truncation(self):
        files = {f"parts/f{n}.txt": "synthetic\n" for n in range(130)}
        files["large.txt"] = "x" * (2 * 1024 * 1024)
        files["test_copy.py"] = (
            "import unittest\nfrom pathlib import Path\nclass T(unittest.TestCase):\n"
            " def test_complete_copy(self):\n"
            "  self.assertEqual(len(list(Path('parts').glob('*.txt'))), 130)\n"
            "  self.assertEqual(Path('large.txt').read_bytes(), b'x' * (2 * 1024 * 1024))\n")
        result = task_sandbox.run_copy(files, "python-unittest")
        self.assertTrue(result["passed"], result)

    def test_real_mib_anchored_edit_check_apply_and_rollback(self):
        original = ("#" + "x" * 1022 + "\n") * 2048 + "VALUE = 1\n"
        with tempfile.TemporaryDirectory(prefix="ai-task-large-", dir=Path.home()) as directory:
            root = Path(directory)
            target = root / "large.py"
            target.write_bytes(original.encode())
            target.chmod(0o640)
            before = task_workspace.snapshot(directory, ["large.py"])
            source = {"instruction": "Synthetic anchored change", "files": [
                {"path": "large.py", "content": original, "writable": True}]}
            proposal = task_contract.proposal(json.dumps({"summary": "Synthetic anchored change", "files": [
                {"path": "large.py", "replacements": [{"old": "VALUE = 1", "new": "VALUE = 2"}]}]}), source)
            result = task_sandbox.run(source, proposal["files"], "python-syntax")
            self.assertTrue(result["passed"], result)
            self.assertEqual(target.read_bytes(), original.encode())
            plan = TaskPlan(before, source, proposal, "python-syntax")
            task_workspace.apply(plan, plan.authorize("SAVE " + plan.sha256))
            self.assertEqual(target.read_bytes(), original.replace("VALUE = 1", "VALUE = 2").encode())
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            self.assertTrue(task_workspace.rollback(directory, plan.sha256))
            self.assertEqual(target.read_bytes(), original.encode())
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_real_pytest_on_an_unchanged_copy_pass_fail_empty_and_denials(self):
        files = {"calc.py": "def add(a, b): return a + b\n",
                 "test_calc.py": "import os, socket, pytest\nfrom calc import add\n"
                                 "def test_add():\n assert add(2, 3) == 5\n"
                                 " with pytest.raises(PermissionError): os.fork()\n"
                                 " with pytest.raises(PermissionError): socket.socket()\n"}
        result = task_sandbox.run_copy(files, "pytest")
        if "pytest is unavailable in the system Python" in result["output"] and os.environ.get("AI_DESKTOP_REQUIRE_PROJECT_SUITES") != "1":
            self.assertFalse(result["passed"])  # optional package missing locally: no result is presented as PASS
            return
        self.assertTrue(result["passed"], result)
        self.assertFalse(task_sandbox.run_copy(dict(files, **{"calc.py": "def add(a, b): return 0\n"}), "pytest")["passed"])
        self.assertFalse(task_sandbox.run_copy({"calc.py": "x = 1\n"}, "pytest")["passed"])
        for recipe, test in (
                ("pytest", 'import pytest\ndef test_it(): pytest.skip("fixture")\n'),
                ("python-unittest", 'import unittest\nclass T(unittest.TestCase):\n @unittest.skip("fixture")\n def test_it(self): pass\n')):
            with self.subTest(recipe=recipe):
                self.assertFalse(task_sandbox.run_copy({"test_it.py": test}, recipe)["passed"])

    def test_real_offline_child_process_suites_pass_fail_and_enforce_the_boundary(self):
        go = {"go.mod": "module example.invalid/synthetic\ngo 1.18\n",
              "calc_test.go": 'package synthetic\nimport ("testing"; "net"; "os"; "os/exec")\n'
                              'func TestBounds(t *testing.T) { if _, e := os.Stat("/run/user"); !os.IsNotExist(e) {t.Fatal(e)}; '
                              'if c, e := net.Listen("tcp", "127.0.0.1:0"); e == nil {c.Close();t.Fatal("socket")}; '
                              'if e := exec.Command("/usr/bin/true").Run(); e != nil {t.Fatal(e)}}\n'}
        node = {"package.json": json.dumps({"scripts": {"test": "./check.sh"}}),
                "check.sh": "#!/bin/sh\nexec node scripts/check.cjs\n",
                "scripts/check.cjs": 'const fs = require("node:fs"); const cp = require("node:child_process");'
                                     'if (fs.existsSync("/run/user") || fs.existsSync("/sys/fs/cgroup")) process.exit(1);'
                                     'cp.execFileSync("/usr/bin/true");\n'}
        for recipe, files, executable in (("go-test", go, ()), ("npm-test", node, ("check.sh",))):
            with self.subTest(recipe=recipe):
                result = task_sandbox.run_copy(files, recipe, executable=executable)
                if (os.environ.get("AI_DESKTOP_REQUIRE_PROJECT_SUITES") != "1" and not result["passed"]
                        and any(message in result["output"] for message in
                                ("Failed to connect", "Test refused: the transient user scope", "fixed test tool is not installed"))):
                    self.assertNotEqual(result["exit_code"], 0)
                    continue
                self.assertTrue(result["passed"], result)
                if recipe == "go-test":
                    files = dict(go, **{"calc_test.go": 'package synthetic\nimport "testing"\nfunc TestWrong(t *testing.T){t.Fatal("failure")}\n'})
                    empty = {"go.mod": go["go.mod"], "calc.go": "package synthetic\n"}
                    self.assertFalse(task_sandbox.run_copy(empty, recipe)["passed"])
                else:
                    files = dict(node, **{"scripts/check.cjs": "process.exit(9);\n"})
                self.assertFalse(task_sandbox.run_copy(files, recipe, executable=executable)["passed"])

    def test_actual_filesystem_network_process_and_namespace_denials(self):
        with tempfile.TemporaryDirectory(prefix="ai-task-sentinel-") as tmp:
            sentinel = Path(tmp) / "secret"
            sentinel.write_text("SYNTHETIC")
            code = '''import unittest, os, socket, ctypes, errno
from pathlib import Path
class T(unittest.TestCase):
 def test_isolation(self):
  self.assertFalse(Path(SENTINEL).exists())
  self.assertFalse(Path('/var/lib/ai-desktop').exists())
  self.assertFalse(Path('/run/user').exists())
  self.assertFalse(Path('/etc/passwd').exists())
  self.assertEqual(os.environ.get('HOME'),'/home/client')
  for family in (socket.AF_UNIX,socket.AF_INET,socket.AF_INET6):
   with self.assertRaises(PermissionError): socket.socket(family)
  with self.assertRaises(PermissionError): os.fork()
  lib=ctypes.CDLL(None,use_errno=True)
  self.assertEqual(lib.unshare(0x10000000),-1)
  self.assertEqual(ctypes.get_errno(),errno.EPERM)
  with self.assertRaises(OSError): Path('/usr/AI_DESKTOP_DENIED').write_text('x')
  with self.assertRaises(OSError): Path('/AI_DESKTOP_DENIED').write_text('x')
  Path('/work/temporary').write_text('tmpfs')
  Path('/tmp/temporary').write_text('tmpfs')
'''.replace("SENTINEL", repr(str(sentinel)))
            result = self.check({"test_isolation.py": code})
            self.assertTrue(result["passed"], result)
            self.assertEqual(sentinel.read_text(), "SYNTHETIC")

    def test_python_json_and_bash_syntax_have_real_checks(self):
        for check, name, good, bad in (("python-syntax", "main.py", "x=1\n", "if ("),
                                      ("json", "data.json", '{"x":1}', '{'),
                                      ("bash-syntax", "script.sh", 'echo "ok"\n', 'if then'),
                                      ("fish-syntax", "script.fish", 'echo "ok"\n', 'if true')):
            with self.subTest(check=check):
                result = self.check({name: good}, check)
                self.assertTrue(result["passed"], result)
                self.assertFalse(self.check({name: bad}, check)["passed"])

    def test_resource_and_output_bounds_refuse(self):
        for body in ("while True: pass", "print('x'*100000)"):
            result = self.check({"test_bad.py": body})
            self.assertFalse(result["passed"], result)


if __name__ == "__main__":
    unittest.main()
