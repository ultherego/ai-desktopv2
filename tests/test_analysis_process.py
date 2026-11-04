"""Real bounded pipes, backpressure, cancellation and descendant cleanup."""
import os
import sys
import time
import unittest
from unittest.mock import patch

from ai_desktop.analysis_contract import AnalysisDenied
from ai_desktop.process_io import bounded_process, MAX_INPUT_BYTES


class AnalysisProcessTests(unittest.TestCase):
    def run_code(self, code, **kwargs):
        return bounded_process([sys.executable, "-I", "-B", "-c", code],
                               env={"PATH": "/usr/bin"}, **kwargs)

    def test_concurrent_input_output_avoids_pipe_backpressure_deadlock(self):
        code, out, err = self.run_code(
            "import sys; sys.stdout.buffer.write(b'x'*60000); sys.stdout.flush(); "
            "data=sys.stdin.buffer.read(); sys.stderr.write(str(len(data)))",
            input_bytes=b"y" * 32000, timeout=3)
        self.assertEqual((code, len(out), err), (0, 60000, b"32000"))

    def test_explicit_large_input_is_complete_with_simultaneous_output(self):
        source = bytes(range(256)) * (MAX_INPUT_BYTES // 256)
        code, out, err = self.run_code(
            "import hashlib,sys; sys.stdout.buffer.write(b'x'*60000); sys.stdout.flush(); "
            "data=sys.stdin.buffer.read(); sys.stderr.write(hashlib.sha256(data).hexdigest())",
            input_bytes=source, input_limit=MAX_INPUT_BYTES, timeout=4)
        import hashlib
        self.assertEqual((code, len(out), err), (0, 60000, hashlib.sha256(source).hexdigest().encode()))

    def test_limit_and_timeout_terminate_process(self):
        for code, kwargs, reason in (
            ("import os;\nwhile True: os.write(2,b'x'*8192)", {"limit": 1000}, "output-limit"),
            ("import time; time.sleep(30)", {"timeout": .1}, "timeout"),
            ("import time; time.sleep(30)", {"cancelled": lambda: True}, "cancelled"),
        ):
            begin = time.monotonic()
            with self.assertRaisesRegex(AnalysisDenied, "reason=" + reason):
                self.run_code(code, **kwargs)
            self.assertLess(time.monotonic() - begin, 3)

    def test_descendant_holding_pipe_does_not_escape_timeout(self):
        begin = time.monotonic()
        with self.assertRaisesRegex(AnalysisDenied, "reason=timeout"):
            self.run_code("import os,time;\nif os.fork()==0: time.sleep(30)\nelse: os._exit(0)", timeout=.2)
        self.assertLess(time.monotonic() - begin, 3)

    def test_invalid_input_never_starts_a_process(self):
        for value in ("text", b"x" * 32769):
            with self.assertRaisesRegex(AnalysisDenied, "reason=input"):
                bounded_process(["/does/not/exist"], env={}, input_bytes=value)

    def test_explicit_input_limits_and_preexisting_cancellation_refuse_before_spawn(self):
        with patch("ai_desktop.process_io.subprocess.Popen") as start:
            for limit, size in ((True, 0), (-1, 0), (MAX_INPUT_BYTES + 1, 0),
                                (32768.0, 0), (640 * 1024, 640 * 1024 + 1)):
                with self.subTest(limit=limit), self.assertRaisesRegex(AnalysisDenied, "reason=input"):
                    bounded_process(["unused"], env={}, input_bytes=b"x" * size, input_limit=limit)
            with self.assertRaisesRegex(AnalysisDenied, "reason=cancelled"):
                bounded_process(["unused"], env={}, cancelled=lambda: True)
            start.assert_not_called()

    def test_cancellation_after_process_closes_all_output_still_terminates(self):
        begin = time.monotonic()
        with self.assertRaisesRegex(AnalysisDenied, "reason=cancelled"):
            self.run_code("import os,time; os.close(1); os.close(2); time.sleep(30)",
                          cancelled=lambda: time.monotonic() - begin > .2, timeout=5)
        self.assertLess(time.monotonic() - begin, 2)
