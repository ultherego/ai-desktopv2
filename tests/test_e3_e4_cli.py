"""E3/E4 real CLI entry points: temp workspaces, a real pty, no model, no network."""
from pathlib import Path
import contextlib
import io
import json
import os
import pty
import re
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_desktop import approval_ui, controller, workspace_cli
from ai_desktop.cli import main as cli_main
from test_report_policy import report_fixture

REPO = str(Path(__file__).resolve().parent.parent)


def run_cli(args, *, answer=None, tty=True):
    """Run `ai-desktop workspace ...`; with tty the user types `answer`."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    command = [sys.executable, "-B", "-m", "ai_desktop", "workspace", *args]
    if not tty:
        done = subprocess.run(command, cwd=REPO, env=env, capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=60, text=True)
        return done.returncode, done.stdout
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(command, cwd=REPO, env=env, stdin=slave,
                                stdout=subprocess.PIPE, stderr=slave, text=True)
        os.close(slave)
        seen = b""
        deadline = time.monotonic() + 30
        while b"\n> " not in seen and proc.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    seen += os.read(master, 65536)
                except OSError:
                    break
        match = re.search(rb"UTWORZ ([0-9a-f]{64})", seen)
        if answer is not None and match:
            reply = answer.replace("{hash}", match.group(1).decode()) + "\n"
            os.write(master, reply.encode())
        out, _ = proc.communicate(timeout=60)
        return proc.returncode, out
    finally:
        os.close(master)


class WorkspaceCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aid-cli-test-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.ws = base / "ws"
        self.ws.mkdir(mode=0o700)
        (self.ws / "notes").mkdir()
        self.src = base / "source.txt"
        self.src.write_text("HELLO FROM CLI\n")
        self.spec = "notes/new.txt=" + str(self.src)

    def test_plan_is_a_dry_run(self):
        code, out = run_cli(["plan", str(self.ws), "--create", self.spec], tty=False)
        self.assertEqual(code, 0)
        self.assertIn("DRY-RUN", out)
        self.assertIn("+HELLO FROM CLI", out)
        self.assertRegex(out, r"Plan SHA-256: [0-9a-f]{64}")
        self.assertEqual(os.listdir(self.ws), ["notes"])
        self.assertEqual(os.listdir(self.ws / "notes"), [])

    def test_apply_requires_terminal(self):
        code, out = run_cli(["apply", str(self.ws), "--create", self.spec], tty=False)
        self.assertEqual(code, 2)
        self.assertFalse((self.ws / "notes/new.txt").exists())
        self.assertNotIn("HELLO", out)

    def test_wrong_confirmation_writes_nothing(self):
        for answer in ("tak", "UTWORZ {hash} ", "utworz {hash}", "UTWORZ " + "0" * 64):
            with self.subTest(answer=answer):
                code, _ = run_cli(["apply", str(self.ws), "--create", self.spec],
                                  answer=answer)
                self.assertEqual(code, 2)
                self.assertEqual(os.listdir(self.ws / "notes"), [])
                self.assertEqual(list(self.ws.glob(".ai-desktop-*")), [])

    def test_confirmed_apply_status_and_explicit_rollback(self):
        code, out = run_cli(["apply", str(self.ws), "--create", self.spec],
                            answer="UTWORZ {hash}")
        self.assertEqual(code, 0, out)
        target = self.ws / "notes/new.txt"
        self.assertEqual(target.read_text(), "HELLO FROM CLI\n")
        plan = re.search(r"plan=([0-9a-f]{64})", out).group(1)
        code, out = run_cli(["status", str(self.ws)], tty=False)
        self.assertEqual((code, out.strip()), (0, "applied " + plan))
        code, out = run_cli(["recover", str(self.ws)], tty=False)
        self.assertEqual(code, 0)
        self.assertTrue(target.exists())  # completed apply stays without explicit choice
        code, out = run_cli(["recover", str(self.ws), "--rollback", plan], tty=False)
        self.assertEqual((code, "recovered=1" in out), (0, True))
        self.assertFalse(target.exists())
        self.assertEqual(run_cli(["status", str(self.ws)], tty=False)[1].strip(),
                         "rolled_back " + plan)

    def test_second_apply_of_existing_target_is_refused(self):
        (self.ws / "notes/new.txt").write_text("MINE")
        code, _ = run_cli(["apply", str(self.ws), "--create", self.spec],
                          answer="UTWORZ {hash}")
        self.assertEqual(code, 2)
        self.assertEqual((self.ws / "notes/new.txt").read_text(), "MINE")

    def test_unsafe_inputs_are_refused_without_writing(self):
        big = Path(self.temp.name) / "big.txt"
        big.write_text("x" * 5000)
        link = Path(self.temp.name) / "link.txt"
        os.symlink(self.src, link)
        fifo = Path(self.temp.name) / "fifo"
        os.mkfifo(fifo)
        for spec in ("notes/a.txt=" + str(big), "notes/a.txt=" + str(link),
                     "notes/a.txt=" + str(fifo), "notes/a.txt=" + self.temp.name,
                     "../escape.txt=" + str(self.src), "/abs.txt=" + str(self.src),
                     ".ssh/x=" + str(self.src), "notes/a.txt", "=" + str(self.src),
                     "notes/a.txt=/nonexistent"):
            with self.subTest(spec=spec):
                code, out = run_cli(["plan", str(self.ws), "--create", spec], tty=False)
                self.assertEqual(code, 2)
                self.assertIn("REFUSED", out)
        self.assertEqual(os.listdir(self.ws / "notes"), [])

    def test_symlinked_workspace_is_refused(self):
        link = Path(self.temp.name) / "wslink"
        os.symlink(self.ws, link)
        code, _ = run_cli(["plan", str(link), "--create", self.spec], tty=False)
        self.assertEqual(code, 2)

    def test_output_never_contains_secret_source_content_on_refusal(self):
        self.src.write_text("TOP SECRET CONTENT\n")
        code, out = run_cli(["apply", str(self.ws), "--create", self.spec],
                            answer="nie", tty=True)
        self.assertEqual(code, 2)
        self.assertNotIn("TOP SECRET", out)

    def test_top_level_dispatch_and_unknown_action(self):
        with patch("ai_desktop.cli.workspace_cli.main", return_value=0) as run:
            self.assertEqual(cli_main(["workspace", "status", "/x"]), 0)
        run.assert_called_once_with(["status", "/x"])
        self.assertEqual(workspace_cli.main(["exec", "x"]), 2)


class ControllerApproveTests(unittest.TestCase):
    def prepared(self):
        report = report_fixture()
        return report, json.dumps(report), {"signals": [], "unknown": []}

    def test_approve_records_consent_and_sends_nothing(self):
        report = self.prepared()[0]
        auth = type("A", (), {"sha256": "ab" * 32})()
        out = io.StringIO()
        with (patch.object(controller, "prepare", return_value=self.prepared()),
              patch.object(controller.approval_ui, "confirm_transfer",
                           return_value=auth) as confirm,
              patch.object(controller.diagnose, "ask") as ask,
              contextlib.redirect_stdout(out)):
            self.assertEqual(controller.main(["approve"]), 0)
        ask.assert_not_called()
        self.assertEqual(confirm.call_args.args[0], report)
        self.assertIn("nie wyslano", out.getvalue())

    def test_approve_denied_returns_2_and_never_asks(self):
        with (patch.object(controller, "prepare", return_value=self.prepared()),
              patch.object(controller.approval_ui, "confirm_transfer",
                           side_effect=approval_ui.ApprovalDenied("x")),
              patch.object(controller.diagnose, "ask") as ask,
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(controller.main(["approve"]), 2)
        ask.assert_not_called()

    def test_approve_without_terminal_is_denied(self):
        with (patch.object(controller, "prepare", return_value=self.prepared()),
              patch("sys.stdin", io.StringIO("PRZEKAZ x\n")),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(controller.main(["approve"]), 2)


if __name__ == "__main__":
    unittest.main()
