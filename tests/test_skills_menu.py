"""Public Skills menu delegates to the same profile-bound settings flow."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from contextlib import redirect_stderr
import io
import unittest
from unittest.mock import patch

from ai_desktop import cli, skills


class SkillsMenuTests(unittest.TestCase):
    def test_direct_menu_uses_profile_management_without_native_process(self):
        with patch("ai_desktop.management_cli.main", return_value=0) as manage, \
                patch("subprocess.run") as run, patch("subprocess.Popen") as spawn:
            self.assertEqual(skills.main(["menu"]), 0)
        manage.assert_called_once_with(["skills"])
        run.assert_not_called()
        spawn.assert_not_called()

    def test_public_menu_routes_without_losing_action(self):
        with patch("ai_desktop.cli.skills.main", return_value=0) as manage:
            self.assertEqual(cli.main(["skills", "menu"]), 0)
        manage.assert_called_once_with(["menu"])

    def test_extra_name_does_not_open_an_unreviewed_assignment(self):
        with patch("ai_desktop.management_cli.main") as manage, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                skills.main(["menu", "other"])
        self.assertEqual(stopped.exception.code, 2)
        manage.assert_not_called()
