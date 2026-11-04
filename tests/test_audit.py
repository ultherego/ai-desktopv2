import subprocess
import unittest
from unittest.mock import patch
from ai_desktop.audit import audit_lines, read_command


class AuditTests(unittest.TestCase):
    @patch("ai_desktop.audit.subprocess.run")
    def test_without_shell(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["findmnt"], 0, "OK", ""
        )
        self.assertEqual(
            read_command(("findmnt", "-T", "/")), (0, "OK")
        )
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    @patch(
        "ai_desktop.audit.subprocess.run",
        side_effect=subprocess.TimeoutExpired("findmnt", 8)
    )
    def test_timeout(self, run):
        code, text = read_command(("findmnt",))
        self.assertIsNone(code)
        self.assertIn("TimeoutExpired", text)

    @patch(
        "ai_desktop.audit.read_command",
        return_value=(0, "TEST")
    )
    @patch(
        "ai_desktop.audit.shutil.which",
        return_value="/usr/bin/example"
    )
    def test_no_agents_launched(self, which, read):
        self.assertIn("SNAPPER", chr(10).join(audit_lines()))
        commands = [
            call.args[0][0] for call in read.call_args_list
        ]
        self.assertTrue(
            all(x in ("findmnt", "snapper") for x in commands)
        )


if __name__ == "__main__":
    unittest.main()
