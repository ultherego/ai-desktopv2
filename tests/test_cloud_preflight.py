"""Check cloud prerequisites and account lifecycle without systemd, installed clients or real credentials."""
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import agent_panel, analysis_admin, analysis_sandbox, analysis_service, analysis_session
from ai_desktop import codex_admin, codex_runtime, e2_socket_activation, execution
from ai_desktop.analysis_contract import AnalysisDenied


class CloudPreflightTests(unittest.TestCase):
    def test_named_codex_login_on_a_codex_only_installation(self):
        with patch.object(e2_socket_activation, "_worker_identity", return_value=True), \
                patch.object(codex_admin.os, "isatty", return_value=True), \
                patch.object(analysis_sandbox, "supervisor_ready", side_effect=AssertionError("unexpected Claude runtime")), \
                patch.object(analysis_sandbox, "profile_lock", return_value=nullcontext()), \
                patch.object(codex_runtime, "supervisor_ready") as check, \
                patch.object(codex_admin, "auth_worker", return_value=0) as login:
            self.assertEqual(codex_admin.named_worker("login", "1000", "codex", "work"), 0)
            check.assert_called_once_with()
            login.assert_called_once_with("login")
            check.side_effect = AnalysisDenied("identity")
            self.assertEqual(codex_admin.named_worker("login", "1000", "codex", "work"), 2)
            login.assert_called_once()

    def test_request_uses_the_selected_runtime_and_common_worker_checks(self):
        selected = {"provider": "codex", "model": "default", "effort": "default", "instructions": "",
                    "profile": "default", "access": "changes"}
        with execution.using(selected), patch.object(analysis_session, "identity", return_value=1000), \
                patch.object(analysis_session.e2_account, "account_ready", return_value=True), \
                patch.object(analysis_session.Path, "read_text", return_value="systemd"), \
                patch.object(analysis_session, "unit_policy") as units, \
                patch.object(analysis_sandbox, "runtime_ready", side_effect=AssertionError("unexpected Claude runtime")), \
                patch.object(codex_runtime, "runtime_ready") as codex:
            analysis_session.installation_ready()
            codex.assert_called_once_with()
            units.assert_called_once_with(1000)
        with patch.object(analysis_sandbox, "service_identity") as identity, \
                patch.object(analysis_sandbox, "cloud_transport_ready") as transport, \
                patch.object(analysis_sandbox, "runtime_ready", side_effect=AssertionError("provider chosen before request")), \
                patch.object(codex_runtime, "runtime_ready", side_effect=AssertionError("provider chosen before request")):
            analysis_service.supervisor_ready()
            identity.assert_called_once_with()
            transport.assert_called_once_with()

    def test_panel_selects_the_vendor_for_status_without_a_conversation(self):
        data = {"selected": "codex", "errors": []}
        with patch.object(agent_panel, "snapshot", return_value=data), patch.object(agent_panel, "_summary"), \
                patch.object(analysis_session, "status", return_value=0) as check:
            self.assertEqual(agent_panel.status(), 0)
            check.assert_called_once_with("codex")

    def test_collected_unit_and_contradictory_manager_results(self):
        for rc, value, stopped in (
                (1, "LoadState=not-found\nActiveState=inactive\nMainPID=0", True),
                (0, "LoadState=loaded\nActiveState=inactive\nMainPID=0", True),
                (1, "", False), (2, "LoadState=not-found", False),
                (1, "LoadState=not-found\nActiveState=active\nMainPID=123", False)):
            with self.subTest(rc=rc, value=value):
                self.assertEqual(analysis_admin.login_unit_stopped(SimpleNamespace(returncode=rc, stdout=value)), stopped)
