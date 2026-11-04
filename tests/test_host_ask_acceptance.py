"""The native ask harness refuses ordinary CI and never repairs the host."""
import contextlib
import io
import unittest
from unittest.mock import patch

import host_ask_acceptance as host
from ai_desktop import systemd_context


class HostAskGuards(unittest.TestCase):
    def test_flags_root_and_tty_refuse_before_any_host_or_provider(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(host.prior, "preflight") as ready, \
                patch.object(host.prior, "run") as run, patch.object(host, "reject_question") as reject, \
                patch.object(systemd_context, "collect") as inventory:
            for args in ([], [host.WEATHER_FLAG], [host.FLAG, "unexpected"], [host.WEATHER_FLAG, host.FLAG]):
                self.assertEqual(host.main(args), 2)
            for uid, tty in ((0, True), (1000, False)):
                with patch.object(host.os, "geteuid", return_value=uid), patch.object(host.os, "isatty", return_value=tty):
                    self.assertEqual(host.main([host.FLAG]), 2)
            ready.assert_not_called()
            run.assert_not_called()
            reject.assert_not_called()
            inventory.assert_not_called()

    def test_preflight_or_native_failure_is_redacted_without_login_or_repair(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, patch.object(host.os, "geteuid", return_value=1000), \
                patch.object(host.os, "isatty", return_value=True), \
                patch.object(host.prior, "preflight", return_value=(1000, ["socket", "service"])), \
                patch.object(host, "reject_question"), patch.object(host.prior, "check_stopped") as stopped, \
                patch.object(host.prior, "run", side_effect=ValueError("PRIVATE-NATIVE-ERROR")) as run:
            self.assertEqual(host.main([host.FLAG, host.WEATHER_FLAG]), 2)
        self.assertNotIn("PRIVATE", out.getvalue())
        self.assertNotIn("HOST ASK: PASS", out.getvalue())
        self.assertEqual(stopped.call_count, 2)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[:2], ("/usr/bin/ai-desktop", "ask"))

    def test_public_systemd_failure_prevents_success_without_hidden_collection(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, patch.object(host.os, "geteuid", return_value=1000), \
                patch.object(host.os, "isatty", return_value=True), \
                patch.object(host.prior, "preflight", return_value=(1000, ["socket", "service"])), \
                patch.object(host, "reject_question"), patch.object(host.prior, "check_stopped"), \
                patch.object(systemd_context, "collect") as inventory, \
                patch.object(host.prior, "run", side_effect=[None, ValueError("PRIVATE")]) as run:
            self.assertEqual(host.main([host.FLAG, host.WEATHER_FLAG]), 2)
        inventory.assert_not_called()
        self.assertEqual(run.call_count, 2)
        self.assertIn("FAIL stage=systemd", out.getvalue())
        self.assertNotIn("HOST ASK: PASS", out.getvalue())
        self.assertNotIn("PRIVATE", out.getvalue())

    def test_weather_is_separate_opt_in_and_cleanup_is_required(self):
        for weather, cleanup_failure in ((False, False), (True, False), (True, True)):
            with self.subTest(weather=weather, cleanup_failure=cleanup_failure), \
                    contextlib.redirect_stdout(io.StringIO()) as out, \
                    patch.object(host.os, "geteuid", return_value=1000), patch.object(host.os, "isatty", return_value=True), \
                    patch.object(host.prior, "preflight", return_value=(1000, ["socket", "service"])), \
                    patch.object(systemd_context, "collect") as inventory, \
                    patch.object(host, "reject_question"), patch.object(host.prior, "run") as run, \
                    patch.object(host.prior, "check_stopped",
                                 side_effect=[None, None, None, None, ValueError("PRIVATE")] if cleanup_failure else None):
                args = [host.FLAG] + ([host.WEATHER_FLAG] if weather else [])
                self.assertEqual(host.main(args), 2 if cleanup_failure else 0)
            inventory.assert_not_called()
            self.assertEqual(run.call_count, 3 if weather else 2)
            self.assertEqual(any("--weather" in call.args for call in run.call_args_list), weather)
            self.assertFalse(any("login" in call.args or "stop" in call.args for call in run.call_args_list))
            self.assertNotIn("PRIVATE", out.getvalue())
            if cleanup_failure:
                self.assertNotIn("HOST ASK: PASS", out.getvalue())
            else:
                self.assertIn("weather=" + ("PASS" if weather else "SKIP"), out.getvalue())
                self.assertIn("native-errors=NOT-TESTED", out.getvalue())


if __name__ == "__main__":
    unittest.main()
