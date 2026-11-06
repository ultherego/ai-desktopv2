"""Application installation inspects system clients without redistributing or running them."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import install_agents as install


class SystemInstallTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.target = self.root / "packaging/arch"
        self.target.mkdir(parents=True)
        self.enterContext(patch.object(install.os, "getuid", return_value=1000))
        self.enterContext(patch.object(install.os, "geteuid", return_value=1000))
        self.present = SimpleNamespace(status="present", recheck=Mock(return_value=True))
        self.inspect = self.enterContext(patch.object(install.system_clients, "inspect", return_value=self.present))
        self.discover = self.enterContext(patch.object(install, "discover", return_value=SimpleNamespace(status="missing")))
        self.run = self.enterContext(patch.object(install.subprocess, "run"))
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(redirect_stderr(io.StringIO()))

    def test_only_selected_providers_are_inspected_and_local_needs_none(self):
        for name, expected in (("claude", ["claude"]), ("codex", ["codex"]),
                               ("both", ["claude", "codex"]), ("local", [])):
            with self.subTest(name=name):
                self.inspect.reset_mock()
                self.assertEqual(install.main([name], root=self.root), 0)
                self.assertEqual([c.args[0] for c in self.inspect.call_args_list], expected)
        self.run.assert_not_called()
        self.discover.assert_not_called()
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertIn("Version, protocol and login not checked", self.stdout.getvalue())

    def test_metadata_failures_do_not_install_clients_or_block_application_bootstrap(self):
        for status in ("missing", "unsafe", "unsupported", "changed", "unavailable"):
            self.inspect.return_value = SimpleNamespace(status=status)
            self.assertEqual(install.main(["claude"], root=self.root), 0)
        self.run.assert_not_called()
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertIn("Install claude independently", self.stdout.getvalue())

    def test_old_staged_clients_are_neither_read_replaced_nor_removed(self):
        old = self.target / "claude-code"
        old.write_bytes(b"obsolete vendor bytes")
        (self.target / "codex").symlink_to("missing-old-client")
        self.assertEqual(install.main(["both"], root=self.root), 0)
        self.assertEqual(old.read_bytes(), b"obsolete vendor bytes")
        self.assertTrue((self.target / "codex").is_symlink())
        self.run.assert_not_called()

    def install_context(self):
        self.enterContext(patch.object(install.sys.stdin, "isatty", return_value=True))
        self.enterContext(patch.object(self.stdout, "isatty", return_value=True))
        self.enterContext(patch.object(Path, "is_file", return_value=True))
        return self.enterContext(patch("builtins.input", return_value="y"))

    def test_install_is_explicit_application_only_and_uses_fresh_package_output(self):
        reader = self.install_context()
        reader.return_value = "n"
        self.assertEqual(install.main(["both", "--install"], root=self.root), 2)
        self.run.assert_not_called()
        reader.return_value = "y"
        with patch.dict(install.os.environ, {"AI_DESKTOP_CHECK_CLIENTS": "1", "AI_DESKTOP_EXPECT_CODEX": "old"}):
            self.assertEqual(install.main(["codex", "--install"], root=self.root), 0)
        args, kwargs = self.run.call_args
        self.assertEqual(args[0], ["/usr/bin/makepkg", "--cleanbuild", "-si"])
        self.assertEqual(kwargs["cwd"], self.target)
        self.assertNotIn("AI_DESKTOP_CHECK_CLIENTS", kwargs["env"])
        self.assertNotIn("AI_DESKTOP_EXPECT_CODEX", kwargs["env"])
        self.assertFalse(Path(kwargs["env"]["PKGDEST"]).exists())
        self.present.recheck.assert_not_called()

    def test_user_source_needs_explicit_registration_after_application_installation(self):
        self.install_context()
        self.inspect.return_value = SimpleNamespace(status="missing")
        self.discover.return_value = SimpleNamespace(status="present")
        self.assertEqual(install.main(["claude", "--install"], root=self.root), 0)
        self.assertEqual(self.run.call_args.args[0], ["/usr/bin/makepkg", "--cleanbuild", "-si"])
        self.run.assert_called_once()
        self.assertIn("ai-desktop clients register claude", self.stdout.getvalue())
        self.assertIn("explicit registration is required", self.stdout.getvalue())

    def test_app_only_installation_never_uses_or_rechecks_provider_payload(self):
        self.install_context()
        self.present.recheck.side_effect = AssertionError("app install must not depend on client bytes")
        self.assertEqual(install.main(["claude", "--install"], root=self.root), 0)
        self.run.assert_called_once()
        self.discover.assert_not_called()

    def test_local_install_and_makepkg_error_do_not_require_a_cloud_client(self):
        self.install_context()
        self.run.side_effect = subprocess.CalledProcessError(1, "makepkg")
        self.assertEqual(install.main(["local", "--install"], root=self.root), 2)
        self.inspect.assert_not_called()
        self.run.assert_called_once()

    def test_noninteractive_or_root_install_refuses_without_package_execution(self):
        with patch.object(install.sys.stdin, "isatty", return_value=False):
            self.assertEqual(install.main(["local", "--install"], root=self.root), 2)
        with patch.object(install.os, "getuid", return_value=0), patch.object(install.os, "geteuid", return_value=0):
            self.assertEqual(install.main(["claude"], root=self.root), 2)
        self.run.assert_not_called()
        self.inspect.assert_not_called()

    def test_invalid_programmatic_selections_refuse_before_inspection(self):
        for selected in (("other",), ("claude", "claude"), ["claude"], "claude"):
            with self.assertRaises(install.InstallError):
                install.prepare(self.root, selected)
        self.inspect.assert_not_called()
