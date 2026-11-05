import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ai_desktop import agent_panel, cli, session_presence as presence


class AgentPanelTests(unittest.TestCase):
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
                self.assertFalse(stale.exists())
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
