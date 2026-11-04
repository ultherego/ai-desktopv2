"""Launcher refusals and report validation; kernel evidence is a separate test."""
import errno
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import e2_subscription_boundary as boundary
from scripts import verify


IMPORT_CHECK = """
import json, runpy, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
def deny_effects(event, args):
    if event.startswith('socket.') or event in (
            'subprocess.Popen', 'os.system', 'os.fork', 'os.posix_spawn', 'os.exec'):
        raise RuntimeError('dependency import attempted a live operation')
sys.addaudithook(deny_effects)
__import__(sys.argv[2])
result = 'IMPORT: PASS'
if sys.argv[2] == 'ai_desktop.e2_subscription_boundary':
    fixture = runpy.run_path(str(root / 'fixture.py'), run_name='dependency_check')
    result = json.dumps(fixture['inner_command']())
for name, module in tuple(sys.modules.items()):
    if name == 'ai_desktop' or name.startswith('ai_desktop.'):
        assert Path(module.__file__).is_relative_to(root), 'checkout import escaped the copied mount set'
print(result)
"""


class MinimalMountDependencyTests(unittest.TestCase):
    """Import exactly the mounted sources without bwrap, listeners or clients."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aid-minimal-imports-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def copy_python_mounts(self, command, destination, *, source_root=None):
        copied = []
        for index, arg in enumerate(command):
            if arg != "--ro-bind":
                continue
            source, target = command[index + 1:index + 3]
            if target != "/opt/fixture.py" and not target.startswith("/opt/ai_desktop/"):
                continue
            self.assertTrue(target.endswith(".py"))
            selected = Path(source) if source_root is None else source_root / Path(source).relative_to("/opt")
            path = destination / Path(target).relative_to("/opt")
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(selected, path)
            copied.append(path.relative_to(destination).as_posix())
        return copied

    def import_only(self, directory, module):
        return subprocess.run([sys.executable, "-I", "-B", "-c", IMPORT_CHECK, str(directory), module],
                              cwd=directory, env={"PATH": "/usr/bin"}, text=True,
                              capture_output=True, timeout=5)

    def test_exact_outer_and_inner_mounts_have_complete_imports(self):
        outer = self.root / "outer"
        copied = self.copy_python_mounts(boundary.fixture_command(), outer)
        self.assertEqual(set(copied), {"fixture.py", *("ai_desktop/" + name for name in boundary.MODULE_FILES)})
        imported = self.import_only(outer, "ai_desktop.e2_subscription_boundary")
        self.assertEqual(imported.returncode, 0, imported.stderr)
        inner_command = json.loads(imported.stdout)
        self.assertIn("--disable-userns", inner_command)
        self.assertIn("--assert-userns-disabled", inner_command)
        inner = self.root / "inner"
        inner_copied = self.copy_python_mounts(inner_command, inner, source_root=outer)
        self.assertEqual(set(inner_copied), set(copied))
        imported = self.import_only(inner, "ai_desktop.e2_subscription_boundary")
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertEqual(json.loads(imported.stdout), inner_command)

    def test_missing_dependency_cannot_fall_back_to_full_checkout(self):
        copied = self.root / "missing"
        self.copy_python_mounts(boundary.fixture_command(), copied)
        (copied / "ai_desktop/provider_errors.py").unlink()
        imported = self.import_only(copied, "ai_desktop.e2_subscription_boundary")
        self.assertNotEqual(imported.returncode, 0)
        self.assertIn("ModuleNotFoundError", imported.stderr)
        self.assertIn("ai_desktop.provider_errors", imported.stderr)

    def test_other_minimal_workers_still_import_from_only_their_declared_files(self):
        from ai_desktop import e2_boundary, executor
        for name, files, module in (("probe", e2_boundary.FILES, "ai_desktop.e2_probe"),
                                    ("worker", executor.WORKER_FILES, "ai_desktop.worker")):
            with self.subTest(worker=name):
                copied = self.root / name
                package = copied / "ai_desktop"
                package.mkdir(parents=True)
                for filename in files:
                    shutil.copyfile(boundary.ROOT / "ai_desktop" / filename, package / filename)
                imported = self.import_only(copied, module)
                self.assertEqual(imported.returncode, 0, imported.stderr)
                self.assertEqual(imported.stdout, "IMPORT: PASS\n")


class SubscriptionBoundaryTests(unittest.TestCase):
    def test_no_real_client_profile_or_host_write_mount_is_accepted(self):
        argv = boundary.fixture_command()
        self.assertIn("--unshare-net", argv)
        self.assertIn("--unshare-pid", argv)
        self.assertIn("--clearenv", argv)
        self.assertNotIn("--bind", argv)
        self.assertNotIn("--share-net", argv)
        self.assertEqual(argv[-1], "outer")
        with self.assertRaises(TypeError):
            boundary.run_synthetic(profile="/home/user/.claude")
        with self.assertRaises(TypeError):
            boundary.run_synthetic(command="claude")

    def test_missing_sandbox_or_root_cannot_start_anything(self):
        for uid, binary in ((0, "/usr/bin/bwrap"), (1000, None)):
            with patch.object(boundary.os, "geteuid", return_value=uid), patch.object(boundary.shutil, "which", return_value=binary), patch.object(boundary, "bounded_run") as run:
                with self.assertRaises(RuntimeError):
                    boundary.run_synthetic()
                run.assert_not_called()

    def test_missing_false_extra_or_live_report_denied(self):
        valid = {"schema": 1, "checks": dict.fromkeys(boundary.CHECKS, True),
                 "real_client": False, "real_auth": False, "upstream": False}
        variants = [{}, {**valid, "real_auth": True}, {**valid, "extra": "secret"},
                    {**valid, "checks": {**valid["checks"], "dns_udp": False}},
                    {**valid, "checks": {**valid["checks"], "client_privileges": False}}]
        with patch.object(boundary.os, "geteuid", return_value=1000), patch.object(boundary.shutil, "which", return_value="/usr/bin/bwrap"):
            for value in variants:
                with self.subTest(value=value), patch.object(boundary, "bounded_run", return_value=(0, json.dumps(value), b"")), self.assertRaises(RuntimeError):
                    boundary.run_synthetic()
            with patch.object(boundary, "bounded_run", return_value=(0, json.dumps(valid), b"")):
                self.assertEqual(boundary.run_synthetic(), valid)

    def test_synthetic_error_diagnostics_only_allow_fixed_identifiers(self):
        variants = (
            ({"failure": "inner-checks", "failed_checks": ["dns_udp"]},
             "reason=inner-checks:dns_udp"),
            ({"failure": "outer-privileges"}, "reason=outer-privileges"),
            ({"failure": "fixture-exception", "phase": "approved-tls"},
             "reason=fixture-exception:approved-tls"),
            ({"failure": "userns-probe", "result": "allowed"},
             "reason=userns-probe:allowed"),
            ({"failure": "userns-probe", "result": "invalid"},
             "reason=userns-probe:invalid"),
            ({"failure": "userns-probe", "result": "multithreaded"},
             "reason=userns-probe:multithreaded"),
            ({"failure": "userns-probe", "result": "limit"},
             "reason=userns-probe:limit"),
            ({"failure": "userns-probe", "result": "/home/private/OAUTH"},
             "reason=fixture"),
            ({"failure": "inner-checks", "failed_checks": ["/home/private/OAUTH"]},
             "reason=fixture"),
            ({"failure": "fixture-exception", "phase": "/home/private/OAUTH"},
             "reason=fixture"),
            ({"failure": "inner-checks", "failed_checks": ["dns_udp", "dns_udp"]},
             "reason=fixture"),
        )
        with (patch.object(boundary.os, "geteuid", return_value=1000),
              patch.object(boundary.shutil, "which", return_value="/usr/bin/bwrap")):
            for response, reason in variants:
                with self.subTest(response=response), patch.object(
                        boundary, "bounded_run",
                        return_value=(1, json.dumps(response).encode(), b"PRIVATE_OAUTH_VALUE")):
                    with self.assertRaises(RuntimeError) as err:
                        boundary.run_synthetic()
                    self.assertIn(reason, str(err.exception))
                    self.assertNotIn("PRIVATE_OAUTH_VALUE", str(err.exception))
                    self.assertNotIn("/home/private", str(err.exception))

    def test_userns_denial_requires_failed_syscall_and_bwrap_assertion(self):
        # The actual sandbox must enforce this property before the child starts;
        # the syscall denial alone is not a security boundary.
        source = Path(__file__).parent / "e2_namespace_fixture.py"
        fixture = source.read_text(encoding="utf-8")
        self.assertIn('"--disable-userns", "--assert-userns-disabled"', fixture)
        self.assertIn('thread_count = len(list(Path("/proc/self/task").iterdir()))', fixture)
        self.assertIn("userns_refused(", fixture)
        self.assertIn('"/usr/bin/python3", "-I", "-B", "/opt/fixture.py", "userns-probe"', fixture)
        self.assertIn('userns_probe_passed(', fixture)
        for error in (errno.EPERM, errno.ENOSPC, errno.EACCES, errno.EINVAL):
            self.assertTrue(boundary.userns_refused(-1, error, thread_count=1))
            self.assertFalse(boundary.userns_refused(0, error, thread_count=1))
            self.assertFalse(boundary.userns_refused(-1, error, thread_count=2))
        for error in (0, errno.EIO):
            self.assertFalse(boundary.userns_refused(-1, error, thread_count=1))

    def test_userns_probe_requires_exact_clean_denial_from_new_process(self):
        self.assertTrue(boundary.userns_probe_passed(0, b"USERNS_DENIED\n", b""))
        for code, output, errors in (
            (1, b"USERNS_DENIED\n", b""),
            (0, b"USERNS_ALLOWED\n", b""),
            (0, b"USERNS_DENIED\nEXTRA", b""),
            (0, b"USERNS_DENIED\n", b"error"),
            (0, b"USERNS_UNEXPECTED\n", b""),
            (0, "USERNS_DENIED\n", b""),
        ):
            with self.subTest(code=code, output=output, errors=errors):
                self.assertFalse(boundary.userns_probe_passed(code, output, errors))

    def test_kernel_privileges_must_be_locked_in_both_namespace_layers(self):
        # The fixture must read the kernel status inside each process, not
        # trust CLI flags or a synthetic assertion from the caller.
        fixture = (Path(__file__).parent / "e2_namespace_fixture.py").read_text()
        self.assertIn('checks = {"client_privileges": locked_privileges()}', fixture)
        self.assertIn('if not locked_privileges():', fixture)
        self.assertIn('Path("/proc/self/status").read_text', fixture)
        self.assertIn('"--cap-drop", "ALL"', (Path(__file__).parent.parent /
                                             "ai_desktop/e2_subscription_boundary.py").read_text())
        required = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        lines = ["NoNewPrivs:\t1"] + [k + ":\t0000000000000000" for k in required]
        status = "\n".join(lines) + "\n"
        self.assertTrue(boundary.privileges_locked(status))
        for key in ("NoNewPrivs",) + required:
            with self.subTest(field=key):
                missing = "\n".join(line for line in lines if not line.startswith(key + ":"))
                self.assertFalse(boundary.privileges_locked(missing))
                self.assertFalse(boundary.privileges_locked(status + key + ":\t0\n"))
                changed = [line if not line.startswith(key + ":") else
                           key + (":\t0" if key == "NoNewPrivs" else ":\t0000000000000001")
                           for line in lines]
                self.assertFalse(boundary.privileges_locked("\n".join(changed)))
        for invalid in (None, b"NoNewPrivs: 1", "", status + "x" * 32769,
                        status.replace("CapEff:\t0000000000000000",
                                       "CapEff:\tno-caps")):
            with self.subTest(invalid=type(invalid)):
                self.assertFalse(boundary.privileges_locked(invalid))

    def test_local_mode_never_starts_real_claude_even_for_version(self):
        for step in ("launcher", "agent", "e2-claude-compat", "controller-ask"):
            self.assertNotIn(step, verify.STEPS["local"])
        self.assertIn("host_e2_*checks.py", verify.command_for("e2-host-probe"))


if __name__ == "__main__":
    unittest.main()
