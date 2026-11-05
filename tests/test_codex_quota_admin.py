"""Only explicit native pairing configures the read-only systemd endpoint."""
from contextlib import ExitStack, nullcontext, redirect_stdout
import io
import subprocess
import unittest
from unittest.mock import Mock, patch

from ai_desktop import codex_quota_admin as admin
from ai_desktop.analysis_contract import AnalysisDenied


class QuotaAdministrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = self.enterContext(ExitStack())
        self.output = self.enterContext(redirect_stdout(io.StringIO()))
        self.system = self.stack.enter_context(patch.object(admin, "os"))
        self.system.getuid.return_value = self.system.geteuid.return_value = 0
        self.system.isatty.return_value = True
        self.system.environ = {"SUDO_UID": "1001"}
        self.uid = self.stack.enter_context(patch.object(admin, "desktop_uid", return_value=1001))
        self.lock = self.stack.enter_context(patch.object(admin, "_admin_lock", return_value=nullcontext()))
        self.unit = "ai-desktop-codex-limits@1001.socket"
        self.policy = self.stack.enter_context(patch.object(admin, "_policy"))
        self.policy.return_value = self.unit, {"ActiveState": "inactive", "UnitFileState": "disabled"}
        self.command = self.stack.enter_context(patch.object(admin, "command"))
        self.endpoint = self.stack.enter_context(patch("ai_desktop.codex_quota_ipc.endpoint"))
        self.worker = self.stack.enter_context(patch.object(admin, "_interactive_worker", return_value=0))

    def calls(self):
        return [c.args[1:] for c in self.command.call_args_list]

    def test_connect_requires_terminal_admin_and_a_fixed_operation_and_slot(self):
        for action, alias in (("read", "default"), ("connect", "../escape"), ("disconnect", "-n")):
            self.assertEqual(admin.administer(action, alias), 2)
        self.system.isatty.return_value = False
        self.assertEqual(admin.administer("connect", "personal"), 2)
        self.system.geteuid.return_value = 1001
        self.system.isatty.return_value = True
        self.assertEqual(admin.administer("connect", "default"), 2)
        self.command.assert_not_called()
        self.worker.assert_not_called()

    def test_pair_starts_only_callers_socket_then_invokes_the_service_worker(self):
        self.assertEqual(admin.administer("connect", "work"), 0)
        self.uid.assert_called_once_with("1001")
        self.lock.assert_called_once_with(1001)
        self.endpoint.assert_called_once_with(1001)
        self.assertEqual(self.calls(), [("enable", "--now", self.unit)])
        self.worker.assert_called_once_with("connect", 1001, "work")

    def test_failed_pair_rolls_back_only_new_activation(self):
        self.worker.return_value = 2
        self.assertEqual(admin.administer("connect", "default"), 2)
        self.assertEqual(self.calls(), [("enable", "--now", self.unit), ("stop", self.unit), ("disable", self.unit)])
        self.command.reset_mock()
        self.policy.return_value = self.unit, {"ActiveState": "active", "UnitFileState": "enabled"}
        self.assertEqual(admin.administer("connect", "work"), 2)
        self.assertEqual(self.calls(), [("enable", "--now", self.unit)])

    def test_refused_endpoint_never_opens_device_login_and_preserves_previous_boot_setting(self):
        self.policy.return_value = self.unit, {"ActiveState": "inactive", "UnitFileState": "enabled"}
        self.endpoint.side_effect = AnalysisDenied("endpoint")
        self.assertEqual(admin.administer("connect", "default"), 2)
        self.worker.assert_not_called()
        self.assertEqual(self.calls(), [("enable", "--now", self.unit), ("stop", self.unit)])

    def test_failed_stop_still_removes_new_boot_activation(self):
        self.worker.return_value = 2
        self.command.side_effect = [b"", AnalysisDenied("cleanup"), b""]
        self.assertEqual(admin.administer("connect", "default"), 2)
        self.assertEqual(self.calls(), [("enable", "--now", self.unit), ("stop", self.unit), ("disable", self.unit)])

    def test_disconnect_stops_only_after_service_confirms_last_link(self):
        for result, code in ((0, 0), (1, 2), (2, 2)):
            self.worker.return_value = result
            self.assertEqual(admin.administer("disconnect", "work"), code)
            self.command.assert_not_called()
        self.worker.return_value = admin.LAST_LINK_REMOVED
        self.assertEqual(admin.administer("disconnect", "default"), 0)
        self.assertEqual(self.calls(), [("disable", "--now", self.unit)])
        self.endpoint.assert_not_called()


class QuotaAdministrativeBoundaryTests(unittest.TestCase):
    def test_device_challenge_cannot_print_arbitrary_native_text(self):
        for url, code in (("https://untrusted.invalid", "ABC-123"), ("https://auth.openai.com/codex/device", "a\nb")):
            with redirect_stdout(io.StringIO()) as output, self.assertRaises(AnalysisDenied):
                admin._challenge(url, code)
            self.assertEqual(output.getvalue(), "")

    def test_dropin_or_replaced_template_refuses_before_activation(self):
        def state(name):
            return {"LoadState": "loaded", "FragmentPath": "/usr/lib/systemd/system/" + name.replace("1001", ""),
                    "DropInPaths": "", "NeedDaemonReload": "no", "UnitFileState": "disabled", "ActiveState": "inactive"}
        with patch.object(admin, "trusted_file"), patch.object(admin, "properties", side_effect=state):
            self.assertEqual(admin._policy(1001)[0], "ai-desktop-codex-limits@1001.socket")
        for key, value in (("DropInPaths", "override.conf"), ("NeedDaemonReload", "yes"), ("FragmentPath", "/tmp/fake")):
            with patch.object(admin, "trusted_file"), patch.object(admin, "properties", side_effect=lambda n: {**state(n), key: value}):
                with self.assertRaises(AnalysisDenied):
                    admin._policy(1001)

    def test_transient_uses_exact_args_and_verifies_cleanup_after_interruption(self):
        base = ["/usr/bin/systemd-run", "--unit=old", "--pty", "--wait", "/usr/bin/python3",
                "-I", "-B", "/usr/lib/ai-desktop/analysis-worker.py", "login"]
        stopped = subprocess.CompletedProcess([], 0, "LoadState=not-found\nActiveState=inactive\nMainPID=0", "")
        run = Mock(side_effect=[KeyboardInterrupt(), None, stopped])
        with patch.object(admin, "login_command", return_value=base), patch.object(admin.subprocess, "run", run):
            with self.assertRaises(KeyboardInterrupt):
                admin._interactive_worker("connect", 1100, "test")
        self.assertEqual(run.call_args_list[0].args[0][-5:], [str(admin.ENTRY), "worker", "connect", "1100", "test"])
        self.assertEqual(run.call_args_list[1].args[0][1], "stop")
        self.assertEqual(run.call_args_list[2].args[0][1], "show")
