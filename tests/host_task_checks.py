"""Real kernel test executor checks. Synthetic data only, no AI/OAuth or host writes."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from ai_desktop import task_sandbox


class TaskKernelTests(unittest.TestCase):
    def check(self, files, check="python-unittest"):
        source = {"instruction": "synthetic", "files": [{"path": p, "content": None, "writable": True} for p in files]}
        return task_sandbox.run(source, [{"path": p, "content": c} for p, c in files.items()], check)

    def test_real_unittest_success_failure_and_empty_refusal(self):
        files = {"calc.py": "def add(a,b): return a+b\n", "test_calc.py": "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n"}
        result = self.check(files)
        self.assertTrue(result["passed"], result)
        files["calc.py"] = "def add(a,b): return a-b\n"
        self.assertFalse(self.check(files)["passed"])
        self.assertFalse(self.check({"calc.py": "x=1\n"})["passed"])

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
