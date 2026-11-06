"""Descriptor forwarding is explicit, bounded and does not broaden defaults."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop.analysis_contract import AnalysisDenied
from ai_desktop.process_io import bounded_process


class ProcessDescriptorTests(unittest.TestCase):
    def test_only_explicit_descriptor_is_inherited(self):
        with tempfile.TemporaryFile() as included, tempfile.TemporaryFile() as excluded:
            included.write(b"payload")
            included.seek(0)
            os.set_inheritable(excluded.fileno(), True)
            script = ('import os; print(os.read(%d, 7).decode());\n'
                      'try: os.fstat(%d)\n'
                      'except OSError: print("closed")\n'
                      'else: raise SystemExit(2)') % (included.fileno(), excluded.fileno())
            result = bounded_process([sys.executable, "-I", "-B", "-c", script], env={},
                pass_fds=(included.fileno(),), timeout=5, limit=1024)
            self.assertEqual(result, (0, b"payload\nclosed\n", b""))

    def test_invalid_descriptors_are_rejected_before_launch(self):
        with tempfile.TemporaryFile() as stream:
            fd = stream.fileno()
            invalid = (None, [], [fd], (True,), (0,), (-1,), ("3",), (fd, fd), (999999,), tuple(range(3, 4100)))
            with patch("ai_desktop.process_io.subprocess.Popen") as launch:
                for descriptors in invalid:
                    with self.subTest(descriptors=repr(descriptors)[:40]), self.assertRaises(AnalysisDenied):
                        bounded_process(["unused"], env={}, pass_fds=descriptors)
                launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
