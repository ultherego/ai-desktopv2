"""Host test diagnostics contract: only fixed denial labels, never worker logs.

No sudo, pacman write, UID switch, OAuth, socket listener or real worker.
"""
import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import host_e2_real_uid_once as host


class HostUIDStageTests(unittest.TestCase):
    def test_worker_keeps_controlling_tty_for_sudo_timestamp(self):
        """No setsid: sudo -n should reuse the operator's sudo -v tty cache."""
        child = object()
        with patch.object(host.subprocess, "Popen", return_value=child) as launch:
            self.assertIs(host.start_worker("f" * 32, 1000), child)
        launch.assert_called_once()
        args, kwargs = launch.call_args
        self.assertEqual(args[0], [
            "/usr/bin/timeout", "--signal=TERM", "--kill-after=2s", "12s",
            "/usr/bin/sudo", "-n", "-u", "ai-desktop", "--",
            "/usr/bin/ai-desktop", "worker-once", "f" * 32, "1000",
        ])
        self.assertNotIn("start_new_session", kwargs)
        self.assertNotIn("preexec_fn", kwargs)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["stdout"], host.subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], host.subprocess.PIPE)

    def test_refusal_requires_explicit_flag_without_privileged_calls(self):
        with patch.object(host, "command") as cmd, patch.object(
                host, "start_worker") as spawn:
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                self.assertEqual(host.main([]), 2)
            cmd.assert_not_called()
            spawn.assert_not_called()
            self.assertEqual(
                stream.getvalue(),
                "HOST CROSS UID: REFUSED (explicit consent flag required)\n",
            )

    def test_only_allowlisted_failure_stages_are_exposed(self):
        self.assertEqual(host.safe_stage("preflight-sudo"), "preflight-sudo")
        self.assertEqual(host.safe_stage("positive-send"), "positive-send")
        for value in ("my-private-data", "", None, True, 1, "../token"):
            self.assertEqual(host.safe_stage(value), "boundary")

    def test_sudo_denial_classified_before_any_worker_launch(self):
        fake_uid = 1700
        fake_worker = 947
        metadata = SimpleNamespace(st_uid=0, st_mode=0o100755)
        with (patch.object(host.os, "geteuid", return_value=fake_uid),
              patch.object(host.e2_account, "account_ready", return_value=True),
              patch.object(host.pwd, "getpwnam",
                           return_value=SimpleNamespace(pw_uid=fake_worker)),
              patch.object(host.os, "lstat", return_value=metadata),
              patch.object(host, "command", side_effect=[
                  (0, host.EXPECTED_PACKAGE), (1, ""),
              ]),
              patch.object(host, "start_worker") as launcher,
              contextlib.redirect_stdout(io.StringIO()) as stream):
            self.assertEqual(host.main(["--allow-real-uid-test"]), 2)
        launcher.assert_not_called()
        self.assertEqual(
            stream.getvalue(), "HOST CROSS UID: REFUSED stage=preflight-sudo\n")

    def test_ready_categorizes_worker_refusal_eof_and_invalid_reply(self):
        for raw, expected in (
            ("WORKER: REFUSED\n", "worker-refused-before-bind"),
            ("", "worker-exited-before-bind"),
            ("SENSITIVE: fake output\n", "worker-unexpected-ready"),
        ):
            task = SimpleNamespace(stdout=io.StringIO(raw))
            with (self.subTest(raw=raw),
                  patch.object(host.select, "select",
                               return_value=([task.stdout], [], []))):
                with self.assertRaises(host.HostRefusal) as denied:
                    host.ready(task)
                self.assertEqual(str(denied.exception), expected)

    def test_ready_timeout_has_fixed_classification(self):
        task = SimpleNamespace(stdout=io.StringIO())
        with patch.object(host.select, "select", return_value=([], [], [])):
            with self.assertRaises(host.HostRefusal) as denied:
                host.ready(task)
        self.assertEqual(str(denied.exception), "ready-timeout")

    def test_finish_does_not_echo_worker_stderr_or_payload(self):
        bad = SimpleNamespace(
            communicate=Mock(return_value=("UNTRUSTED", "SECRET WORKER LOG")),
            returncode=2,
        )
        with self.assertRaises(host.HostRefusal) as denied:
            host.finish(bad, 0, "WORKER: RECEIVED sha256=xxx")
        self.assertEqual(str(denied.exception), "worker-exit")


if __name__ == "__main__":
    unittest.main()
