"""Offline subscription display. The JSON transport keeps its existing UTC summary."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
import re
import time
import unittest
from unittest.mock import patch

from ai_desktop import account_cli as accounts


NOW = int(datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc).timestamp())


class UsageDisplayTests(unittest.TestCase):
    def rows(self):
        account = {"provider": "claude", "account": "personal", "selected": True, "plan": "pro",
                   "status": "login previously confirmed", "observed_at": NOW - 300, "last_login_at": NOW - 3500,
                   "plan_observed_at": NOW - 3600, "usage": "stored UTC summary", "windows": {
                       "five_hour": {"percent": 37.5, "reset": NOW + 4500},
                       "seven_day": {"percent": 3, "reset": NOW + 2 * 86400 + 3 * 3600}}}
        return [account, {**account, "account": "work", "selected": False, "plan": None, "status": "retired",
                          "windows": {}, "observed_at": None, "usage": "not reported"},
                {**account, "provider": "codex", "account": "default", "plan": "plus", "status": "unchecked",
                 "windows": {"five_hour": {"percent": 100, "reset": NOW - 100}}}]

    def display(self, *, width=90, tty=False, no_color=False, zone="UTC0", argv=()):
        previous = os.environ.get("TZ")
        out = io.StringIO()
        try:
            with patch.dict(os.environ, {"TZ": zone, "TERM": "xterm-256color"}):
                os.environ.pop("NO_COLOR", None)
                if no_color:
                    os.environ["NO_COLOR"] = ""
                time.tzset()
                with redirect_stdout(out), patch.object(out, "isatty", return_value=tty), \
                     patch.object(accounts.shutil, "get_terminal_size", return_value=os.terminal_size((width, 24))), \
                     patch.object(accounts.time, "time", return_value=NOW), patch.object(accounts, "panel_rows", return_value=self.rows()):
                    self.assertEqual(accounts.usage_main(argv), 0)
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()
        return out.getvalue()

    def test_window_percentages_line_up_and_resets_use_local_zone(self):
        output = self.display(zone="Europe/Warsaw", argv=["--provider", "claude"])
        self.assertEqual(output.count("Claude"), 1)
        self.assertIn("Account  personal (selected)", output)
        self.assertIn("Plan     pro", output)
        self.assertIn("Status   login previously confirmed", output)
        self.assertIn("Resets in 1h 15m (2026-07-01 13:15 CEST)", output)
        self.assertIn("Resets in 2d 3h (2026-07-03 15:00 CEST)", output)
        self.assertIn("Last reported 2026-07-01 11:55 CEST", output)
        lines = [line for line in output.splitlines() if " used" in line]
        self.assertEqual([line.index("%") for line in lines], [lines[0].index("%")] * 2)
        self.assertRegex(lines[0], r"5-hour .+37\.5% used")
        self.assertRegex(lines[1], r"7-day .+3% used")
        self.assertIn("Limits   Not reported", output)
        self.assertIn("Plan     unknown", output)
        self.assertIn("Status   retired", output)
        self.assertNotIn("Codex", output)

    def test_expired_and_missing_windows_do_not_claim_current_percentages(self):
        output = self.display(argv=["--provider", "codex"])
        self.assertIn("100% last recorded", output)
        self.assertIn("Window expired (2026-07-01 09:58 UTC)", output)
        self.assertRegex(output, r"7-day\s+Not reported")
        self.assertNotIn("100% used", output)
        self.assertNotIn("Resets in", output)

    def test_codex_empty_after_login_gives_official_usage_url_without_a_percentage(self):
        row = {**self.rows()[-1], "windows": {}, "observed_at": None}
        with patch.object(self, "rows", return_value=[row]):
            text = self.display(argv=["--provider", "codex"])
        self.assertIn("Sign-in does not return usage limits.", text)
        self.assertIn("https://chatgpt.com/settings/usage", text)
        self.assertNotIn("%", text)

    def test_earlier_window_reports_its_own_local_timestamp(self):
        row = self.rows()[-1]
        row["windows"]["five_hour"]["observed_at"] = NOW - 3600
        with patch.object(self, "rows", return_value=[row]):
            text = self.display(argv=["--provider", "codex"], zone="Europe/Warsaw")
        self.assertIn("Reported 2026-07-01 11:00 CEST", text)
        self.assertIn("Last reported 2026-07-01 11:55 CEST", text)
        summary = accounts._window_text(row["observed_at"], row["windows"], NOW)
        self.assertIn("reported 2026-07-01 09:00 UTC", summary)

    def test_narrow_tty_wraps_without_a_bar_and_respects_no_color(self):
        output = self.display(width=42, tty=True, no_color=True)
        self.assertNotIn("\x1b", output)
        self.assertNotRegex(output, r"#{2}|-{3}")
        self.assertLessEqual(max(map(len, output.splitlines())), 42)
        self.assertIn("2026-07-01 11:15 UTC", output)
        self.assertRegex(self.display(tty=True), re.escape("\x1b["))
        self.assertNotIn("\x1b", self.display(tty=False))

    def test_live_warning_colors_do_not_depict_expired_samples_as_exhausted(self):
        rows = self.rows()
        rows[0]["windows"]["five_hour"]["percent"] = 75
        rows[0]["windows"]["seven_day"]["percent"] = 95
        with patch.object(self, "rows", return_value=rows):
            text = self.display(tty=True)
        self.assertIn("\x1b[33;1m   75% used", text)
        self.assertIn("\x1b[31;1m   95% used", text)
        self.assertNotIn("\x1b[31;1m  100% last recorded", text)

    def test_filter_and_json_return_the_original_rows_verbatim(self):
        self.assertEqual(json.loads(self.display(argv=["--provider", "codex", "--json"])), [self.rows()[-1]])
