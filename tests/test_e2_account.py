"""Synthetic E2 account metadata preflight, no host edits or OAuth access."""
from types import SimpleNamespace
import stat
import unittest
from unittest.mock import Mock, patch

from ai_desktop import e2_account


def user(**overrides):
    values = {"pw_name": "ai-desktop", "pw_uid": 980, "pw_gid": 980,
              "pw_dir": "/var/lib/ai-desktop", "pw_shell": "/usr/bin/nologin"}
    values.update(overrides)
    return SimpleNamespace(**values)


def group(**overrides):
    values = {"gr_name": "ai-desktop", "gr_mem": []}
    values.update(overrides)
    return SimpleNamespace(**values)


def directory(**overrides):
    values = {"st_mode": stat.S_IFDIR | 0o700, "st_uid": 980, "st_gid": 980}
    values.update(overrides)
    return SimpleNamespace(**values)


class AccountPreflightTests(unittest.TestCase):
    def baseline(self):
        stack = self.enterContext(patch.object(e2_account.os, "geteuid", return_value=1000))
        self.enterContext(patch.object(e2_account.os, "getgroups", return_value=[1000]))
        self.enterContext(patch.object(e2_account.os, "getegid", return_value=1000))
        found = self.enterContext(patch.object(e2_account.pwd, "getpwnam", return_value=user()))
        account_group = self.enterContext(patch.object(e2_account.grp, "getgrgid", return_value=group()))
        checked = self.enterContext(patch.object(e2_account.os, "lstat", return_value=directory()))
        return stack, found, account_group, checked

    def test_valid_metadata_only_no_credentials_or_subprocess(self):
        _, found, account_group, checked = self.baseline()
        with patch("builtins.open") as raw, patch("subprocess.run") as spawned:
            self.assertTrue(e2_account.account_ready())
        self.assertEqual(found.call_args.args, ("ai-desktop",))
        account_group.assert_called_once_with(980)
        checked.assert_called_once_with("/var/lib/ai-desktop")
        raw.assert_not_called()
        spawned.assert_not_called()

    def test_missing_account_and_root_fail_closed(self):
        self.baseline()
        with patch.object(e2_account.pwd, "getpwnam", side_effect=KeyError):
            self.assertFalse(e2_account.account_ready())
        with patch.object(e2_account.os, "geteuid", return_value=0), patch.object(
                e2_account.pwd, "getpwnam") as looked:
            self.assertFalse(e2_account.account_ready())
            looked.assert_not_called()

    def test_wrong_uid_gid_shell_home_or_group_membership_denied(self):
        _, found, account_group, checked = self.baseline()
        invalid = [
            {"pw_uid": 0}, {"pw_uid": 1000}, {"pw_gid": 0}, {"pw_dir": "/home/someone"},
            {"pw_shell": "/bin/bash"}, {"pw_name": "attacker"}, {"pw_uid": "980"},
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                found.return_value = user(**fields)
                self.assertFalse(e2_account.account_ready())
        found.return_value = user()
        with patch.object(e2_account.os, "getgroups", return_value=[1000, 980]):
            self.assertFalse(e2_account.account_ready())
        with patch.object(e2_account.os, "getegid", return_value=980):
            self.assertFalse(e2_account.account_ready())
        for fields in ({"gr_name": "wheel"}, {"gr_mem": ["somebody"]}):
            with self.subTest(fields=fields):
                account_group.return_value = group(**fields)
                self.assertFalse(e2_account.account_ready())
        checked.assert_not_called()

    def test_untrusted_home_directory_identity_symlink_or_mode_denied(self):
        self.baseline()
        for invalid in (
                {"st_mode": stat.S_IFLNK | 0o700},
                {"st_mode": stat.S_IFDIR | 0o755},
                {"st_mode": stat.S_IFREG | 0o700},
                {"st_uid": 1000}, {"st_gid": 1000}):
            with self.subTest(invalid=invalid):
                with patch.object(e2_account.os, "lstat", return_value=directory(**invalid)):
                    self.assertFalse(e2_account.account_ready())
        with patch.object(e2_account.os, "lstat", side_effect=FileNotFoundError):
            self.assertFalse(e2_account.account_ready())

    def test_bad_lookup_and_host_metadata_errors_are_refused(self):
        self.baseline()
        with patch.object(e2_account.grp, "getgrgid", side_effect=KeyError):
            self.assertFalse(e2_account.account_ready())
        with patch.object(e2_account.os, "lstat", side_effect=PermissionError):
            self.assertFalse(e2_account.account_ready())


if __name__ == "__main__":
    unittest.main()
