import tempfile
from pathlib import Path
import unittest

from ai_desktop.agent import PROBE, build_agent_command


class AgentTests(unittest.TestCase):

    def test_probe_syntax(self):
        compile(PROBE, "<probe>", "exec")

    def test_offline_skills_and_separate_configuration(self):
        with tempfile.TemporaryDirectory(
            prefix="ai-desktop-launch-", dir="/tmp"
        ) as path:
            args = build_agent_command(
                Path(path), Path("/tmp/mock-claude"),
                Path("/tmp/skills"), "version", "012345",
                Path("/home/test"), "net:[123]"
            )

        self.assertIn("--unshare-net", args)
        self.assertIn("--clearenv", args)
        self.assertEqual(args[-2:], ["/opt/claude", "--version"])

        mounts = [
            args[i:i + 3]
            for i, flag in enumerate(args)
            if flag == "--ro-bind"
        ]
        self.assertIn(
            ["--ro-bind", "/tmp/skills",
             "/home/sandbox/.claude/skills"],
            mounts,
        )

        environment = [
            args[i:i + 3]
            for i, flag in enumerate(args)
            if flag == "--setenv"
        ]
        self.assertIn(
            ["--setenv", "CLAUDE_CONFIG_DIR",
             "/home/sandbox/.claude"],
            environment,
        )

    def test_no_prompts_or_permission_bypass(self):
        with tempfile.TemporaryDirectory(
            prefix="ai-desktop-launch-", dir="/tmp"
        ) as path:
            with self.assertRaises(ValueError):
                build_agent_command(
                    Path(path), Path("/tmp/mock-claude"),
                    Path("/tmp/skills"), "-p", "012345",
                    Path("/home/test"), "net:[123]"
                )


if __name__ == "__main__":
    unittest.main()
