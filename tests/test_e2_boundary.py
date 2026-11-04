"""E2 isolation boundary contract: no provider requests, credentials or host writes."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import e2_boundary, e2_probe
from ai_desktop.report_policy import serialize_report
from test_report_policy import report_fixture


class E2BoundaryTests(unittest.TestCase):
    def test_fixed_sandbox_command_has_no_writable_host_mounts_or_network(self):
        cmd = e2_boundary.build_command(
            e2_boundary.SOURCE, "--probe", "/home/hostuser", "net:[42]"
        )
        for flag in ("--unshare-user", "--unshare-pid", "--unshare-net",
                     "--unshare-ipc", "--unshare-uts", "--new-session",
                     "--clearenv", "--die-with-parent"):
            self.assertIn(flag, cmd)
        self.assertNotIn("--bind", cmd)
        self.assertNotIn("--dev-bind", cmd)
        self.assertNotIn("--share-net", cmd)
        self.assertNotIn("--cap-add", cmd)
        self.assertEqual(cmd[-1], "--probe")
        mounts = [(cmd[i+1], cmd[i+2]) for i, v in enumerate(cmd[:-2])
                  if v == "--ro-bind"]
        self.assertEqual(len(mounts), len(e2_boundary.FILES) + 1)
        self.assertEqual(mounts[0], ("/usr", "/usr"))
        self.assertTrue(all(s.startswith(str(e2_boundary.SOURCE)) for s, _ in mounts[1:]))
        self.assertNotIn("/home/hostuser", [src for src, _ in mounts])
        self.assertNotIn("CLAUDE_CONFIG_DIR", cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", cmd)
        self.assertEqual(cmd[cmd.index("--setenv") + 1], "PATH")

    def test_cannot_launch_arbitrary_action_or_external_code(self):
        for action in ("--shell", "--exec", "--network", "--ask", "-p", "bash", ""):
            with self.subTest(action=action), self.assertRaises(ValueError):
                e2_boundary.build_command(e2_boundary.SOURCE, action, "/h", "net:[1]")
        with tempfile.TemporaryDirectory() as path:
            with self.assertRaises(ValueError):
                e2_boundary.build_command(Path(path), "--probe", "/h", "net:[1]")

    def test_symlink_source_rejected_before_process_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "code"
            link.symlink_to(e2_boundary.SOURCE, target_is_directory=True)
            with self.assertRaises(ValueError):
                e2_boundary.build_command(link, "--probe", "/h", "net:[1]")

    def test_probe_accepts_only_exact_verified_result(self):
        def fake_run(argv, **kwargs):
            self.assertIn("--unshare-net", argv)
            self.assertIsNone(kwargs["input"])
            return subprocess.CompletedProcess(argv, 0, '{"probe": "PASS"}', "")
        with patch.object(e2_boundary.os, "geteuid", return_value=1000), \
             patch.object(e2_boundary.shutil, "which", return_value="/usr/bin/bwrap"), \
             patch.object(e2_boundary.os, "readlink", return_value="net:[123]"):
            self.assertEqual(e2_boundary.execute("--probe", runner=fake_run),
                             {"probe": "PASS"})

    def test_probe_rejects_fake_or_extended_response(self):
        for result in ('{"probe": "FAIL"}', '{"probe":"PASS","oauth":"SECRET"}',
                       "not json", '{"probe":"PASS"}' + "x" * 2048):
            with self.subTest(result=result[:30]), \
                 patch.object(e2_boundary.os, "geteuid", return_value=1000), \
                 patch.object(e2_boundary.shutil, "which", return_value="/usr/bin/bwrap"), \
                 patch.object(e2_boundary.os, "readlink", return_value="net:[123]"):
                fake = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, result, "")
                with self.assertRaises(RuntimeError):
                    e2_boundary.execute("--probe", runner=fake)

    def test_inspect_valid_report_exposes_hash_and_count_only(self):
        report = report_fixture()
        expected = serialize_report(report)
        def fake_run(cmd, **kwargs):
            self.assertEqual(kwargs["input"], expected)
            result = {"sha256": hashlib.sha256(expected.encode()).hexdigest(),
                      "field_count": 8}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(result), "")
        with patch.object(e2_boundary.os, "geteuid", return_value=1000), \
             patch.object(e2_boundary.shutil, "which", return_value="/usr/bin/bwrap"), \
             patch.object(e2_boundary.os, "readlink", return_value="net:[123]"):
            result = e2_boundary.execute("--inspect", report=report, runner=fake_run)
        self.assertNotIn("shell", result)
        self.assertEqual(result["field_count"], 8)

    def test_invalid_report_rejected_before_process(self):
        with patch.object(e2_boundary.os, "geteuid", return_value=1000), \
             patch.object(e2_boundary.shutil, "which", return_value="/usr/bin/bwrap"), \
             patch.object(e2_boundary.os, "readlink", return_value="net:[123]"):
            with self.assertRaises(RuntimeError):
                e2_boundary.execute("--inspect", {"private_token": "SECRET"},
                                    runner=lambda *a, **kw: self.fail("must not run"))

    def test_root_missing_bwrap_and_timeout_fail_closed(self):
        with patch.object(e2_boundary.os, "geteuid", return_value=0):
            with self.assertRaises(RuntimeError):
                e2_boundary.execute("--probe", runner=lambda *a, **kw: self.fail("root"))
        with patch.object(e2_boundary.os, "geteuid", return_value=1000), \
             patch.object(e2_boundary.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError):
                e2_boundary.execute("--probe", runner=lambda *a, **kw: self.fail("bwrap"))
        with patch.object(e2_boundary.os, "geteuid", return_value=1000), \
             patch.object(e2_boundary.shutil, "which", return_value="/usr/bin/bwrap"), \
             patch.object(e2_boundary.os, "readlink", return_value="net:[123]"):
            with self.assertRaises(RuntimeError):
                e2_boundary.execute("--probe", runner=lambda *a, **kw: (_ for _ in ()).throw(
                    subprocess.TimeoutExpired("bwrap", 20)
                ))

    def test_child_inspect_rejects_duplicate_fields_without_leak(self):
        data = b'{"kernel":"ONE","kernel":"TWO","token":"PRIVATE"}'
        with patch.object(e2_probe.sys, "stdin") as stdin, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            stdin.buffer.read.return_value = data
            self.assertEqual(e2_probe.main(["--inspect"]), 1)
        self.assertEqual(output.getvalue().strip(), '{"error":"rejected"}')

    def test_child_inspect_accepts_valid_allowlisted_payload(self):
        raw = serialize_report(report_fixture()).encode()
        with patch.object(e2_probe.sys, "stdin") as stdin, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            stdin.buffer.read.return_value = raw
            self.assertEqual(e2_probe.main(["--inspect"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {
            "sha256": hashlib.sha256(raw).hexdigest(), "field_count": 8})

    def test_command_only_offers_synthetic_test(self):
        with patch.object(e2_boundary, "execute", return_value={"probe":"PASS"}) as run, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(e2_boundary.main(["self-test"]), 0)
        run.assert_called_once_with("--probe")
        self.assertIn("no Claude or OAuth", output.getvalue())
        with patch.object(e2_boundary, "execute") as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(e2_boundary.main(["ask"]), 2)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
