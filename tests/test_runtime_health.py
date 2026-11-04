import subprocess
import unittest
from unittest.mock import patch

from ai_desktop.runtime_health import collect_btrfs_health, collect_systemd_health


class RuntimeHealthTests(unittest.TestCase):
    @staticmethod
    def completed(code=0, output=""):
        return subprocess.CompletedProcess([], code, output, "")

    def test_systemd_running_and_zero_failed(self):
        with patch("ai_desktop.runtime_health.subprocess.run") as run:
            run.side_effect = [
                self.completed(0, "running\n"),
                self.completed(0, ""),
            ]
            self.assertEqual(
                collect_systemd_health(),
                {"state": "running", "failed_units": 0},
            )
            for call in run.call_args_list:
                self.assertFalse(call.kwargs.get("shell", False))
                self.assertEqual(call.kwargs["timeout"], 5)

    def test_systemd_degraded_with_one_failed_unit(self):
        with patch("ai_desktop.runtime_health.subprocess.run") as run:
            run.side_effect = [
                self.completed(1, "degraded\n"),
                self.completed(0, "example.service loaded failed failed Example\n"),
            ]
            self.assertEqual(
                collect_systemd_health(),
                {"state": "degraded", "failed_units": 1},
            )

    def test_systemd_rejects_unexpected_output(self):
        with patch("ai_desktop.runtime_health.subprocess.run") as run:
            run.side_effect = [
                self.completed(0, "private=token\n"),
                self.completed(0, "private=token\n"),
            ]
            self.assertEqual(
                collect_systemd_health(),
                {"state": "unknown", "failed_units": "unknown"},
            )

    def test_systemd_errors_fail_closed(self):
        with patch("ai_desktop.runtime_health.subprocess.run", side_effect=OSError):
            self.assertEqual(
                collect_systemd_health(),
                {"state": "unknown", "failed_units": "unknown"},
            )

    def test_non_btrfs_no_host_reads(self):
        with patch("ai_desktop.runtime_health.os.statvfs") as stat:
            with patch("ai_desktop.runtime_health.subprocess.run") as run:
                self.assertEqual(
                    collect_btrfs_health("ext4"),
                    {"free_percent": "unknown", "compression": "unknown"},
                )
                stat.assert_not_called()
                run.assert_not_called()

    def test_btrfs_percent_and_compression_no_paths(self):
        class Stats:
            f_blocks = 1000
            f_bavail = 250

        with patch("ai_desktop.runtime_health.os.statvfs", return_value=Stats()):
            with patch("ai_desktop.runtime_health.subprocess.run") as run:
                run.return_value = self.completed(
                    0,
                    "rw,relatime,compress=zstd:3,subvol=/@,ssd\n",
                )
                self.assertEqual(
                    collect_btrfs_health("btrfs"),
                    {"free_percent": 25, "compression": "zstd"},
                )
                self.assertEqual(
                    run.call_args.args[0],
                    ["findmnt", "-T", "/", "-n", "-o", "OPTIONS"],
                )

    def test_btrfs_explicit_no_compression(self):
        class Stats:
            f_blocks = 100
            f_bavail = 0

        with patch("ai_desktop.runtime_health.os.statvfs", return_value=Stats()):
            with patch("ai_desktop.runtime_health.subprocess.run") as run:
                run.return_value = self.completed(0, "rw,relatime,ssd\n")
                self.assertEqual(
                    collect_btrfs_health("btrfs"),
                    {"free_percent": 0, "compression": "none"},
                )

    def test_btrfs_unknown_compression(self):
        with patch("ai_desktop.runtime_health.os.statvfs", side_effect=OSError):
            with patch("ai_desktop.runtime_health.subprocess.run") as run:
                run.return_value = self.completed(0, "compress=secret,subvol=/private")
                self.assertEqual(
                    collect_btrfs_health("btrfs"),
                    {"free_percent": "unknown", "compression": "unknown"},
                )

    def test_btrfs_read_error(self):
        with patch("ai_desktop.runtime_health.os.statvfs", side_effect=OSError):
            with patch("ai_desktop.runtime_health.subprocess.run", side_effect=OSError):
                self.assertEqual(
                    collect_btrfs_health("btrfs"),
                    {"free_percent": "unknown", "compression": "unknown"},
                )


if __name__ == "__main__":
    unittest.main()
