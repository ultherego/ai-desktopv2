import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_desktop import agent_panel, cli, session_presence as presence


class AgentPanelTests(unittest.TestCase):
    def test_provider_snapshot_uses_shared_metadata_without_running_clients(self):
        for present in ((), ("claude",), ("codex",), ("claude", "codex")):
            with self.subTest(present=present), \
                    patch.object(agent_panel.system_clients, "inspect", side_effect=lambda name:
                        SimpleNamespace(source="system", status="present" if name in present else "missing")) as inspect, \
                    patch.object(agent_panel.agent_profiles, "Store", side_effect=ValueError), \
                    patch.object(agent_panel.permissions, "load_policy", return_value=None), \
                    patch.object(agent_panel.session_presence, "live", return_value=[]), \
                    patch("ai_desktop.account_cli.panel_rows", return_value=[]), \
                    patch("subprocess.Popen", side_effect=AssertionError("metadata only")), \
                    patch("subprocess.run", side_effect=AssertionError("metadata only")), \
                    patch("socket.socket", side_effect=AssertionError("no network")):
                data = agent_panel.snapshot()
            self.assertEqual([call.args for call in inspect.call_args_list], [("claude",), ("codex",)])
            for row in data["providers"][:2]:
                self.assertEqual(row["client_present"], row["id"] in present)
                self.assertEqual(row["client_source"], "system")
                self.assertNotIn("pinned_version", row)
                self.assertFalse(row["version_checked"])
                self.assertFalse(row["protocol_checked"])

    def test_provider_refusals_are_visible_without_claiming_version_or_readiness(self):
        for state in ("unsafe", "unsupported", "changed", "unavailable"):
            with self.subTest(state=state), \
                    patch.object(agent_panel.system_clients, "inspect", return_value=SimpleNamespace(source="system", status=state)), \
                    patch.object(agent_panel.agent_profiles, "Store", side_effect=ValueError), \
                    patch.object(agent_panel.permissions, "load_policy", return_value=None), \
                    patch.object(agent_panel.session_presence, "live", return_value=[]), \
                    patch("ai_desktop.account_cli.panel_rows", return_value=[]), \
                    redirect_stdout(io.StringIO()) as output:
                data = agent_panel.snapshot()
                agent_panel._summary(data)
            self.assertFalse(data["providers"][0]["client_present"])
            self.assertIn("system client: " + state + ", compatibility unchecked", output.getvalue())

    def test_registered_source_is_reported_without_claiming_current_source_or_compatibility(self):
        with patch.object(agent_panel.system_clients, "inspect",
                return_value=SimpleNamespace(source="registered", status="present")), \
                patch.object(agent_panel.agent_profiles, "Store", side_effect=ValueError), \
                patch.object(agent_panel.permissions, "load_policy", return_value=None), \
                patch.object(agent_panel.session_presence, "live", return_value=[]), \
                patch("ai_desktop.account_cli.panel_rows", return_value=[]), \
                patch("subprocess.Popen", side_effect=AssertionError("metadata only")), \
                patch("subprocess.run", side_effect=AssertionError("metadata only")), \
                redirect_stdout(io.StringIO()) as output:
            data = agent_panel.snapshot()
            agent_panel._summary(data)
        for item in data["providers"][:2]:
            self.assertEqual(item["client_source"], "registered")
            self.assertTrue(item["client_present"])
            self.assertFalse(item["version_checked"])
            self.assertFalse(item["protocol_checked"])
        self.assertIn("registered client: present, compatibility unchecked", output.getvalue())

    def test_live_rows_include_only_conversation_metadata_and_disappear_on_exit(self):
        with tempfile.TemporaryDirectory() as location, patch.object(presence, "_runtime", return_value=Path(location)):
            marker = presence.Presence()
            marker.start()
            try:
                marker.update(3, "synthetic-id")
                rows = presence.live()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["id"], "synthetic-id")
                self.assertEqual(rows[0]["exchanges"], 3)
                self.assertEqual(set(rows[0]), {"id", "provider", "role", "model", "account", "directory", "exchanges", "started"})
                self.assertEqual(os.stat(Path(location, "ai-desktop-presence", marker.name)).st_mode & 0o777, 0o600)
            finally:
                marker.close()
            self.assertEqual(presence.live(), [])

    def test_insecure_runtime_fails_without_affecting_chat(self):
        with tempfile.TemporaryDirectory() as location, patch.object(presence, "_runtime", return_value=Path(location)):
            os.chmod(location, 0o755)
            marker = presence.Presence()
            marker.start()
            marker.update(1)
            self.assertEqual(presence.live(), [])
            self.assertFalse(Path(location, "ai-desktop-presence").exists())
            marker.close()

    def test_stale_presence_and_links_do_not_report_active_sessions(self):
        with tempfile.TemporaryDirectory() as location, patch.object(presence, "_runtime", return_value=Path(location)):
            marker = presence.Presence()
            marker.start()
            try:
                folder = Path(location, "ai-desktop-presence")
                contents = (folder / marker.name).read_bytes()
                (folder / ("f" * 24)).symlink_to(folder / marker.name)
                stale = folder / ("e" * 24)
                stale.write_bytes(contents)
                stale.chmod(0o600)
                self.assertEqual(len(presence.live()), 1)
                self.assertTrue(stale.exists(), "status is read-only even for stale entries")
            finally:
                marker.close()

    def test_status_json_and_local_readiness_do_not_contact_a_server_or_start_a_client(self):
        data = {"schema": 1, "selected": "local", "role": "default", "providers": [], "accounts": [],
                "policy": None, "sessions": [], "errors": []}
        with patch.object(agent_panel, "snapshot", return_value=data), \
                patch.object(agent_panel.analysis_session, "status", side_effect=AssertionError("cloud")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["status", "--json"]), 0)
            import json
            self.assertEqual(json.loads(output.getvalue()), data)
            self.assertEqual(cli.main(["status"]), 0)

    def test_service_shortcut_changes_the_provider_without_logging_in(self):
        with patch("ai_desktop.management_cli.main", return_value=0) as change, \
                patch("subprocess.Popen", side_effect=AssertionError("selection should not launch a program")):
            self.assertEqual(cli.main(["agent", "use", "local"]), 0)
        change.assert_called_once_with(["set", "--provider", "local"], prog="ai-desktop settings", provider_neutral=True)
