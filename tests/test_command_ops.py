"""Owner-listed commands: template rules, matching, program resolution, bounded output and the real sandbox."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest

from ai_desktop import command_ops as ops

DIRECT = lambda path, argv: [path, *argv[1:]]          # no sandbox: process handling is tested on its own


class TemplateTests(unittest.TestCase):
    def test_valid_templates_and_placeholders(self):
        self.assertEqual(ops.parse_template("systemd-analyze critical-chain <unit>"),
                         ("systemd-analyze", "critical-chain", "<unit>"))
        self.assertEqual(ops.parse_templates(["uname -r", "journalctl -b -p warning --no-pager -n <int>"])[1][-1], "<int>")

    def test_shells_interpreters_privilege_network_and_writers_are_refused_whatever_the_owner_writes(self):
        for program in ("bash", "sh", "fish", "python3", "perl", "env", "sudo", "su", "pkexec", "ssh", "curl", "wget", "nc",
                        "rm", "dd", "tee", "mv", "cp", "chmod", "find", "xargs", "awk", "sed", "busybox", "kill"):
            with self.subTest(program=program), self.assertRaises(ValueError):
                ops.parse_template(program + " x")

    def test_malformed_templates_are_refused(self):
        for bad in ("", " uname", "uname  -r", "uname -r;id", "uname $(id)", "uname `id`", "uname a|b", "uname >x", "uname *",
                    "uname ~/x", "uname \\x", "uname 'x'", '"uname"', "/usr/bin/uname", "../x", "-x", "uname <other>",
                    "uname " + "x" * 101, " ".join(["a"] * 11), "uname\n-r", "uname -r\x00"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ops.parse_template(bad)
        with self.assertRaises(ValueError):
            ops.parse_templates(["uname -r", "uname -r"])
        with self.assertRaises(ValueError):
            ops.parse_templates(["uname -%d" % i for i in range(ops.MAX_TEMPLATES + 1)])

    def test_matching_is_exact_per_token_and_validates_placeholders(self):
        templates = ops.parse_templates(["uname -r", "systemctl status <unit> --no-pager", "journalctl -b -n <int>",
                                         "lsblk -d <word>"])
        good = [["uname", "-r"], ["systemctl", "status", "sshd.service", "--no-pager"], ["journalctl", "-b", "-n", "50"],
                ["lsblk", "-d", "nvme0n1"]]
        bad = [["uname"], ["uname", "-a"], ["uname", "-r", "x"], ["UNAME", "-r"], ["systemctl", "status", "sshd", "--no-pager"],
               ["systemctl", "status", "a b.service", "--no-pager"], ["systemctl", "status", "x.service;id", "--no-pager"],
               ["systemctl", "status", "x.service"], ["journalctl", "-b", "-n", "-1"], ["journalctl", "-b", "-n", "999999"],
               ["journalctl", "-b", "-n", "5 "], ["lsblk", "-f"], ["lsblk", "-d", "../etc"], ["lsblk", "-d", ""]]
        for argv in good:
            self.assertTrue(ops.match(templates, argv), argv)
        for argv in bad:
            self.assertFalse(ops.match(templates, argv), argv)

    def test_a_proposed_argv_must_be_a_short_list_of_plain_words(self):
        for bad in (None, "uname -r", [], [""], [1], ["a"] * 11, ["a\nb"], ["a\x1b[0m"], ["a b"], ["x" * 101], ["a", None]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ops.clean_argv(bad)
        self.assertEqual(ops.clean_argv(["uname", "-r"]), ["uname", "-r"])


class ResolveTests(unittest.TestCase):
    def test_system_programs_resolve_to_root_owned_absolute_files(self):
        path = ops.resolve("uname")
        self.assertTrue(path.startswith("/") and os.stat(path).st_uid == 0)
        for name in ("no-such-program-xyz", "../bin/uname", "uname/x", "/usr/bin/uname", "", "a b"):
            self.assertIsNone(ops.resolve(name), name)

    def test_a_user_writable_directory_or_file_never_resolves(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            program = Path(directory) / "tool"
            program.write_text("#!/bin/sh\necho x\n")
            program.chmod(0o755)
            self.assertIsNone(ops.resolve("tool", search=(directory,)))      # owned by the user, not root
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "uname"
            link.symlink_to(shutil.which("uname"))
            # a link only ever leads to the real, root-owned file, which is what would run
            self.assertEqual(ops.resolve("uname", search=(directory,)), os.path.realpath(shutil.which("uname")))


class OutputTests(unittest.TestCase):
    def test_sanitize_removes_escapes_and_controls_and_cuts_visibly(self):
        text, cut = ops.sanitize(b"\x1b[31mczerwony\x1b[0m\r\nlinia\x00\x07 koniec\t\xc4\x85", 1000)
        self.assertEqual(text, "czerwony\nlinia koniec\tą")
        self.assertFalse(cut)
        text, cut = ops.sanitize("ą".encode() * 100, 50)
        self.assertTrue(cut)
        self.assertLessEqual(len(text.encode()), 50)
        self.assertEqual(ops.sanitize(b"\xff\xfe ok", 100)[0], "�� ok")


class ExecutionTests(unittest.TestCase):
    def run_command(self, argv, **kwargs):
        kwargs.setdefault("output_limit", 2000)
        kwargs.setdefault("timeout", 5)
        return ops.execute(ops.resolve(argv[0]), argv, command=DIRECT, **kwargs)

    def test_success_failure_timeout_and_output_cap(self):
        ok = self.run_command(["uname", "-s"])
        self.assertEqual((ok["status"], ok["exit"], ok["output"].strip()), ("ok", 0, "Linux"))
        failed = self.run_command(["false"])
        self.assertEqual((failed["status"], failed["exit"]), ("failed", 1))
        slow = self.run_command(["sleep", "10"], timeout=1)
        self.assertEqual((slow["status"], slow["exit"]), ("timeout", None))
        flood = self.run_command(["yes"], output_limit=500)
        self.assertEqual((flood["status"], flood["exit"], flood["truncated"]), ("ok", None, True))
        self.assertLessEqual(len(flood["output"].encode()), 500)

    def test_stderr_of_a_failing_command_is_reported_bounded_and_cleaned(self):
        step = self.run_command(["ls", "/no/such/dir/xyz"])
        self.assertEqual(step["status"], "failed")
        self.assertIn("[stderr]", step["output"])

    def test_the_environment_is_clean_and_stdin_is_closed(self):
        os.environ["AI_DESKTOP_SECRET_PROBE"] = "must-not-leak"
        try:
            step = self.run_command(["printenv"])
        finally:
            del os.environ["AI_DESKTOP_SECRET_PROBE"]
        self.assertNotIn("must-not-leak", step["output"])
        self.assertEqual(self.run_command(["cat"])["output"], "")             # stdin is /dev/null: returns at once

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is not available")
    def test_real_sandbox_runs_a_read_command_and_cannot_write_or_see_the_network(self):
        step = ops.execute(ops.resolve("uname"), ["uname", "-s"], output_limit=500, timeout=10)
        if step["status"] == "unavailable":
            self.skipTest("unprivileged user namespaces are not available here")
        self.assertEqual((step["status"], step["output"].strip()), ("ok", "Linux"))
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            target = Path(directory) / "created-by-sandbox"
            touch = ops.execute(ops.resolve("touch"), ["touch", str(target)], output_limit=500, timeout=10)
            self.assertEqual(touch["status"], "failed")                       # the root is read-only
            self.assertFalse(target.exists())

    def test_missing_sandbox_means_unavailable_never_an_unsandboxed_run(self):
        step = ops.execute(ops.resolve("uname"), ["uname", "-s"], output_limit=500, timeout=5, command=lambda p, a: None)
        self.assertEqual((step["status"], step["exit"], step["output"]), ("unavailable", None, ""))


if __name__ == "__main__":
    unittest.main()


class ReviewFindingsTests(unittest.TestCase):
    """Regressions for the independent review of the execution boundary."""

    def test_a_unit_word_can_never_start_with_a_dash(self):
        templates = ops.parse_templates(["systemctl status <unit> --no-pager"])
        for word in ("-lH.service", "-.slice", "--host=x.service", "-H.service"):
            self.assertFalse(ops.match(templates, ["systemctl", "status", word, "--no-pager"]), word)
        self.assertTrue(ops.match(templates, ["systemctl", "status", "dbus.service", "--no-pager"]))
        self.assertTrue(ops.match(templates, ["systemctl", "status", "app@1.service", "--no-pager"]))

    def test_the_first_argument_must_be_literal_and_more_programs_are_refused(self):
        for bad in ("systemctl <word>", "journalctl <unit>", "lsblk <int>"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ops.parse_template(bad)
        for program in ("systemd-run", "systemd-nspawn", "busctl", "loginctl", "dbus-send", "gdbus", "runuser", "login",
                        "ionice", "taskset", "chrt", "setpriv", "capsh", "time", "git", "less", "man", "tar", "vim"):
            with self.subTest(program=program), self.assertRaises(ValueError):
                ops.parse_template(program + " x")
        ops.parse_template("systemctl status <unit>")                       # a literal verb is the owner's choice

    def test_the_value_of_an_option_is_not_taken_for_the_verb(self):
        for strict, bad in ((False, "systemctl -H host <word>"), (True, "systemctl -H host <word>"),
                            (False, "docker --context synthetic <word> x"), (True, "custom-tool -c value <word>"),
                            (True, "systemctl --user -M box <word> x.service")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ops.parse_template(bad, strict=strict)
        for strict, good in ((True, "systemctl --user restart <unit>"), (True, "systemctl -H host restart <unit>"),
                             (True, "systemctl --no-pager --user status <unit>"), (False, "pactl --format=json list <word>"),
                             (False, "systemctl status <unit> --no-pager"), (False, "nmcli -t connection show <word>"),
                             (False, "journalctl -u <unit> -n <int>"), (False, "pacman -Qi <word>")):
            with self.subTest(good=good):
                ops.parse_template(good, strict=strict)

    def test_the_contract_takes_as_many_change_templates_as_the_policy(self):
        from ai_desktop.assistant_contract import validate_access
        access = {"files": [], "systemd": [], "weather": False, "commands": ["uname -r"]}
        names = ["app-%d restart" % number for number in range(ops.MAX_TEMPLATES + 1)]
        self.assertEqual(len(validate_access({**access, "change_commands": names[:-1]})["change_commands"]), ops.MAX_TEMPLATES)
        with self.assertRaises(ValueError):
            validate_access({**access, "change_commands": names})

    def test_nested_commands_require_a_literal_complete_operation(self):
        for strict in (False, True):
            for template in ("docker container <word> demo", "docker --context inspect container <word> demo",
                             "docker --context <word> inspect example", "docker plugin-name <word> demo",
                             "docker compose alpha <word>", "btrfs subvolume <word> /mnt",
                             "btrfs subv <word> /mnt", "nmcli -t connection <word> demo",
                             "nmcli -f connection device wifi <word> demo", "nmcli radio wifi <word>"):
                with self.subTest(template=template, strict=strict), self.assertRaises(ValueError):
                    ops.parse_template(template, strict=strict)
            for template in ("docker --context lab container inspect <word>", "docker -D inspect <word>",
                             "docker --context=lab container restart <word>", "docker compose ps <word>",
                             "btrfs subvolume get-default <word>", "nmcli -t connection show <word>",
                             "nmcli -f DEVICE,TYPE device wifi connect <word>", "docker plugin-name list all"):
                with self.subTest(template=template, strict=strict):
                    self.assertEqual(ops.parse_template(template, strict=strict), tuple(template.split()))

    def test_a_link_to_a_forbidden_program_never_resolves(self):
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "innocent"
            link.symlink_to(shutil.which("sh"))
            self.assertIsNone(ops.resolve("innocent", search=(directory,)))

    def test_output_is_cut_by_bytes_and_a_stopped_command_is_marked_truncated(self):
        step = ops.execute(shutil.which("sh"), ["sh"], output_limit=100, timeout=5,
                           command=lambda path, argv: [path, "-c", "printf 'é%.0s' $(seq 1 4000); echo oops >&2; exit 1"])
        self.assertLessEqual(len(step["output"].encode("utf-8")), 100)
        flood = ops.execute(shutil.which("sh"), ["sh"], output_limit=200, timeout=5,
                            command=lambda path, argv: [path, "-c", "while :; do printf 'a\\r\\n'; done"])
        self.assertTrue(flood["truncated"])

    def test_a_command_that_closes_its_output_and_succeeds_is_not_reported_as_failed(self):
        step = ops.execute(shutil.which("sh"), ["sh"], output_limit=100, timeout=5,
                           command=lambda path, argv: [path, "-c", "exec >&- 2>&-; sleep 0.3"])
        self.assertEqual((step["status"], step["exit"]), ("ok", 0))

    def test_the_sandbox_hides_homes_and_the_user_runtime_dir_and_sets_no_session_bus_dir(self):
        command = ops.sandbox_command("/usr/bin/uname", ["uname", "-s"])
        if command is None:
            self.skipTest("bubblewrap is not available")
        for hidden in ("/home", "/root", "/run/user"):
            index = command.index(hidden)
            self.assertEqual(command[index - 1], "--tmpfs")
        self.assertNotIn("XDG_RUNTIME_DIR", command)
