"""E7: test real systemd parser in an isolated fake root, never the host.

The package hook is not executed in CI. Root-owned package installation and
actual CachyOS UID creation require a separate real-host acceptance.
"""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from pathlib import Path
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SYSUSERS = ROOT / "packaging/sysusers.d/ai-desktop.conf"
TMPFILES = ROOT / "packaging/tmpfiles.d/ai-desktop.conf"
HOOK = ROOT / "packaging/arch/ai-desktop.install"
PKGBUILD = ROOT / "packaging/arch/PKGBUILD"


class SystemAccountPackagingTests(unittest.TestCase):
    def test_only_fixed_unprivileged_system_account_is_declared(self):
        self.assertFalse(SYSUSERS.is_symlink())
        self.assertEqual(
            SYSUSERS.read_text(encoding="utf-8").splitlines()[-1],
            'u ai-desktop - "AI Desktop isolated Claude worker" /var/lib/ai-desktop /usr/bin/nologin',
        )
        self.assertEqual(
            [line for line in SYSUSERS.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")],
            ['u ai-desktop - "AI Desktop isolated Claude worker" /var/lib/ai-desktop /usr/bin/nologin'],
        )

    def test_only_private_persistent_directories_are_created(self):
        self.assertFalse(TMPFILES.is_symlink())
        rules = [line for line in TMPFILES.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
        self.assertEqual(rules, [
            "d /var/lib/ai-desktop 0700 ai-desktop ai-desktop - -",
            "d /var/lib/ai-desktop/claude 0700 ai-desktop ai-desktop - -",
        ])
        self.assertTrue(all(len(line.split()) == 7 for line in rules))
        self.assertFalse(any(line.startswith(("r ", "R ", "D ", "z ", "Z ")) for line in rules))

    def test_real_systemd_sysusers_parser_writes_only_fake_root(self):
        binary = shutil.which("systemd-sysusers")
        self.assertIsNotNone(binary, "systemd-sysusers required for E7 packaging test")
        with tempfile.TemporaryDirectory(prefix="ai-desktop-sysusers-") as temp:
            fake_root = Path(temp)
            (fake_root / "etc").mkdir()
            (fake_root / "var/lib").mkdir(parents=True)
            fake_policy = fake_root / "ai-desktop.conf"
            fake_policy.write_bytes(SYSUSERS.read_bytes())
            proc = subprocess.run(
                [binary, "--root=" + str(fake_root), str(fake_policy)],
                check=False, capture_output=True, text=True, timeout=12,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[:400])
            passwd = (fake_root / "etc/passwd").read_text(encoding="utf-8").splitlines()
            groups = (fake_root / "etc/group").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(passwd), 1)
            self.assertEqual(len(groups), 1)
            user = passwd[0].split(":")
            group = groups[0].split(":")
            self.assertEqual(user[0], "ai-desktop")
            self.assertEqual(group[0], "ai-desktop")
            self.assertEqual(user[2], user[3])
            self.assertEqual(user[3], group[2])
            self.assertGreater(int(user[2]), 0)
            self.assertEqual(user[5:], ["/var/lib/ai-desktop", "/usr/bin/nologin"])
            self.assertEqual(group[3], "")
            self.assertFalse((fake_root / "var/lib/ai-desktop").exists(),
                             "sysusers must not create a home on its own")

    def test_real_arch_package_staging_without_host_install_or_pacman(self):
        syntax = subprocess.run(["/usr/bin/bash", "-n", str(PKGBUILD)],
                                check=False, capture_output=True, text=True, timeout=5)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        with tempfile.TemporaryDirectory(prefix="ai-desktop-arch-stage-") as temp:
            package_root = Path(temp) / "pkg"
            environment = dict(os.environ)
            environment["startdir"] = str(PKGBUILD.parent)
            environment["pkgdir"] = str(package_root)
            staged = subprocess.run(
                ["/usr/bin/bash", "-c", 'set -e; source "$1"; package', "bash", str(PKGBUILD)],
                env=environment, check=False, capture_output=True, text=True, timeout=12,
            )
            self.assertEqual(staged.returncode, 0, staged.stderr[-1000:])
            self.assertEqual(
                (package_root / "usr/lib/ai-desktop/provisioning/sysusers.conf").read_bytes(),
                SYSUSERS.read_bytes(),
            )
            self.assertEqual(
                (package_root / "usr/lib/ai-desktop/provisioning/tmpfiles.conf").read_bytes(),
                TMPFILES.read_bytes(),
            )
            # Pacman systemd auto-hooks must have nothing to trigger.
            self.assertFalse((package_root / "usr/lib/sysusers.d/ai-desktop.conf").exists())
            self.assertFalse((package_root / "usr/lib/tmpfiles.d/ai-desktop.conf").exists())
            for name in ("ai-desktop-handoff@.socket", "ai-desktop-handoff@.service"):
                source_unit = ROOT / "packaging/systemd" / name
                staged_unit = package_root / "usr/lib/systemd/system" / name
                self.assertEqual(staged_unit.read_bytes(), source_unit.read_bytes())
                self.assertEqual(stat.S_IMODE(staged_unit.stat().st_mode), 0o644)
                self.assertNotIn("\n[Install]", staged_unit.read_text())
            self.assertEqual(
                (package_root / "usr/lib/ai-desktop/activated-worker.py").read_bytes(),
                (ROOT / "packaging/systemd/activated-worker.py").read_bytes())
            self.assertFalse((package_root / "etc/systemd").exists())
            exe = package_root / "usr/bin/ai-desktop"
            self.assertTrue(exe.is_file())
            self.assertFalse(exe.is_symlink())
            self.assertTrue(exe.stat().st_mode & stat.S_IXUSR)
            script = exe.read_text(encoding="utf-8")
            self.assertTrue(script.startswith("#!/bin/sh\n"))
            self.assertIn("exec /usr/bin/python3 -I -B -c", script)
            self.assertIn("sys.path.insert(0, \"/usr/lib/ai-desktop\")", script)
            self.assertIn("' \"$@\"", script)
            self.assertNotIn("#!/usr/bin/python3 -I -B", script)
            shell_syntax = subprocess.run(
                ["/bin/sh", "-n", str(exe)], check=False,
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(shell_syntax.returncode, 0, shell_syntax.stderr)
            # Exercise the actual packaged launcher against the *staged* trusted
            # code path, never the currently installed host package. Absolute
            # production path is swapped only in this synthetic test copy.
            staged_root = package_root / "usr/lib/ai-desktop"
            wrapper = Path(temp) / "staged-ai-desktop"
            wrapper.write_text(script.replace("/usr/lib/ai-desktop",
                                              str(staged_root)), encoding="utf-8")
            wrapper.chmod(0o700)
            hostile = Path(temp) / "untrusted-cwd"
            (hostile / "ai_desktop").mkdir(parents=True)
            (hostile / "ai_desktop/__init__.py").write_text(
                'raise RuntimeError("PYTHONPATH injection")', encoding="utf-8")
            hostile_env = dict(os.environ)
            hostile_env.update({
                "PYTHONPATH": str(hostile),
                "PYTHONHOME": "/nonexistent/hostile-python",
                "PYTHONDONTWRITEBYTECODE": "0",
            })
            for args, expected_exit, expected_text in (
                (("--version",), 0, "ai-desktop 0.1.0"),
                (("doctor",), 0, "Login and AI operation: not checked."),
                (("diagnose", "invalid"), 2, "invalid choice"),
                (("worker-once", "f" * 32, "1000"), 2, "WORKER: REFUSED"),
            ):
                with self.subTest(arguments=args):
                    launched = subprocess.run(
                        [str(wrapper), *args], cwd=hostile, env=hostile_env,
                        check=False, capture_output=True, text=True, timeout=12,
                    )
                    self.assertEqual(
                        launched.returncode, expected_exit,
                        f"stdout={launched.stdout[:500]} stderr={launched.stderr[:500]}")
                    self.assertIn(expected_text, launched.stdout + launched.stderr)
                    self.assertNotIn("PYTHONPATH injection", launched.stdout + launched.stderr)
                    self.assertNotIn("Unknown option", launched.stderr)
            self.assertFalse(list(staged_root.rglob("__pycache__")),
                             "Packaged -B must prevent cache writes to staged code")
            self.assertTrue((package_root / "usr/lib/ai-desktop/ai_desktop/cli.py").is_file())
            skill_names = {path.name for path in (ROOT / "skills").iterdir()}
            self.assertEqual({p.name for p in (staged_root / "skills").iterdir()}, skill_names)
            for name in skill_names:
                source = ROOT / "skills" / name / "SKILL.md"
                target = staged_root / "skills" / name / "SKILL.md"
                self.assertFalse(target.is_symlink())
                self.assertEqual(target.read_bytes(), source.read_bytes())
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
            self.assertFalse((package_root / "var/lib/ai-desktop").exists(),
                             "Build must never create a system account directory")

    def test_package_hook_is_valid_shell_with_collision_and_uninstall_denials(self):
        self.assertFalse(HOOK.is_symlink())
        script = HOOK.read_text(encoding="utf-8")
        result = subprocess.run(["/bin/sh", "-n", str(HOOK)], check=False,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        for requirement in (
            "post_install()", "post_upgrade()", "post_remove()",
            "_ai_desktop_account_valid", "_ai_desktop_dir_valid",
            "/usr/bin/systemd-sysusers /usr/lib/ai-desktop/provisioning/sysusers.conf",
            "/usr/bin/systemd-tmpfiles --create /usr/lib/ai-desktop/provisioning/tmpfiles.conf",
            "getent passwd ai-desktop", "getent group ai-desktop",
            "[ ! -e /var/lib/ai-desktop ]",
            "[ ! -L /var/lib/ai-desktop ]",
            "stat -c '%a:%U:%G'", 
            "# Deliberately preserve the UID and profile",
        ):
            self.assertIn(requirement, script)
        self.assertLess(script.index("[ ! -e /var/lib/ai-desktop ]"),
                        script.index("/usr/bin/systemd-sysusers /usr/lib/ai-desktop/provisioning/sysusers.conf"))
        self.assertNotIn("userdel", script)
        self.assertNotIn("groupdel", script)
        self.assertNotIn("rm -", script)
        self.assertNotIn("sudo", script)
        self.assertNotIn("systemctl", script)

    def test_systemd_units_parse_without_installation_or_service_start(self):
        units = ROOT / "packaging/systemd"
        result = subprocess.run([
            "/usr/bin/systemd-analyze", "verify",
            str(units / "ai-desktop-handoff@.socket"),
            str(units / "ai-desktop-handoff@.service"),
        ], check=False, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr[:2000])
        # Unknown settings are sometimes warnings even with exit 0.
        self.assertNotIn("Unknown", result.stderr)
        self.assertNotIn("Failed", result.stderr)

    def test_hook_account_validation_denies_wrong_shell_or_supplementary_group(self):
        # Source the real package helper but replace lookup commands as shell
        # functions. No post_install, no systemd calls, no touching host.
        policy = str(HOOK)
        template = (
            '. "$1"\n'
            'getent() { if [ "$1" = passwd ]; then '
            'printf "ai-desktop:x:999:999:AI Desktop:/var/lib/ai-desktop:%s\\n" "$2"; '
            'else printf "ai-desktop:x:999:%s\\n" "$3"; fi; }\n'
            'id() { case "$1" in -u|-g) echo 999 ;; -nG) echo "$4" ;; esac; }\n'
            '_ai_desktop_account_valid\n'
        )
        for shell, members, secondary, expected in [
            ("/usr/bin/nologin", "", "ai-desktop", 0),
            ("/bin/bash", "", "ai-desktop", 1),
            ("/usr/bin/nologin", "otheruser", "ai-desktop", 1),
            ("/usr/bin/nologin", "", "wheel ai-desktop", 1),
        ]:
            with self.subTest(shell=shell, members=members, groups=secondary):
                shell_script = template.replace('"$2"', "'" + shell + "'").replace(
                    '"$3"', "'" + members + "'").replace('"$4"', "'" + secondary + "'")
                proc = subprocess.run(["/bin/sh", "-c", shell_script, "test", policy],
                                      check=False, capture_output=True, text=True, timeout=5)
                self.assertEqual(proc.returncode == 0, expected == 0,
                                 f"unexpected result: {proc.stderr}")


if __name__ == "__main__":
    unittest.main()
