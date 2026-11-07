"""Read commands see only a fixed part of /run: no Docker, LACT, varlink or other daemon sockets unless the owner opts in."""
import shutil
import subprocess
import unittest
from unittest.mock import patch

from ai_desktop import command_ops, permissions
from tests.test_change_plan import ChangeBase, ANSWER_PLAN, ops


def bound(command):
    return [command[i + 1] for i, item in enumerate(command) if item == "--ro-bind-try"]


class SandboxRunTests(unittest.TestCase):
    def command(self, **kwargs):
        with patch.object(command_ops.shutil, "which", return_value="/usr/bin/bwrap"):
            return command_ops.sandbox_command("/usr/bin/docker", ["docker", "ps"], **kwargs)

    def test_run_is_an_empty_tmpfs_with_only_the_fixed_list_on_top(self):
        command = self.command()
        start = command.index("/run") - 1
        self.assertEqual(command[start:start + 2], ["--tmpfs", "/run"])
        self.assertGreater(start, command.index("--ro-bind"))           # mounted over the read-only host /
        self.assertEqual(bound(command), list(command_ops.RUN_VISIBLE))
        for hidden in ("/run/docker.sock", "/run/lactd.sock", "/run/containerd", "/run/systemd/private",
                       "/run/systemd/io.systemd.Manager", "/run/polkit", "/run/user"):
            self.assertFalse(any(item == hidden or item.startswith(hidden + "/") for item in bound(command)), hidden)
        for flag in ("--unshare-net", "--unshare-ipc", "--cap-drop", "--clearenv"):
            self.assertIn(flag, command)
        self.assertEqual(command[-1], "ps")

    def test_an_owner_opt_in_adds_exactly_those_sockets(self):
        command = self.command(host_sockets=("/run/docker.sock", "/run/lactd.sock"))
        self.assertEqual(bound(command)[-2:], ["/run/docker.sock", "/run/lactd.sock"])

    def test_an_invalid_opt_in_never_runs_with_a_wider_view(self):
        for bad in ("/run/user/1000/bus", "/run/../home", "/var/run/docker.sock", "run/docker.sock", "/run/a b",
                    "/run/", "/run/a/b/c/d/e"):
            with self.subTest(bad=bad):
                self.assertIsNone(self.command(host_sockets=(bad,)))

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is not available")
    def test_real_sandbox_shows_only_the_fixed_part_of_run(self):
        command = command_ops.sandbox_command(shutil.which("ls"), ["ls", "-A", "/run"])
        listed = set(subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=20, check=True).stdout.split())
        allowed = {item.split("/")[2] for item in command_ops.RUN_VISIBLE}
        self.assertLessEqual(listed, allowed)
        self.assertNotIn("docker.sock", listed)
        self.assertNotIn("user", listed)


class HostSocketPolicyTests(ChangeBase):
    def test_the_option_is_empty_by_default_and_lists_only_run_paths(self):
        self.write_policy()
        self.assertEqual(permissions.load_policy().host_sockets(), ())
        self.share_line = 'host_sockets = ["/run/docker.sock", "/run/lactd.sock"]\n'
        self.write_policy()
        self.assertEqual(permissions.load_policy().host_sockets(), ("/run/docker.sock", "/run/lactd.sock"))
        for bad in ('"/run/docker.sock"', '["/run/user/1000/bus"]', '["/home/x.sock"]', '["/run/../etc"]',
                    '["/run/a", "/run/a"]', "[%s]" % ", ".join('"/run/s%d"' % i for i in range(9)), "[1]"):
            self.share_line = "host_sockets = %s\n" % bad
            self.write_policy()
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                permissions.load_policy()

    def test_read_commands_get_the_opt_in_and_nothing_else_changes_without_it(self):
        views = []

        def fake(path, argv, *, output_limit, timeout, command=None, host=False, role="read"):
            views.append(command)
            return {"op": "run", "argv": list(argv), "status": "ok", "exit": 0, "output": "x\n", "truncated": False,
                    "role": role}
        for line, expected in (("", None), ('host_sockets = ["/run/docker.sock"]\n', ("/run/docker.sock",))):
            views.clear()
            self.share_line = line
            self.write_policy()
            with patch.object(command_ops, "execute", side_effect=fake):
                self.facts([ops.run("uname", "-s"), ANSWER_PLAN])
            if expected is None:
                self.assertIsNone(views[0])
            else:
                with patch.object(command_ops.shutil, "which", return_value="/usr/bin/bwrap"):
                    self.assertEqual(bound(views[0]("/usr/bin/uname", ["uname", "-s"]))[-1], expected[0])


if __name__ == "__main__":
    unittest.main()
