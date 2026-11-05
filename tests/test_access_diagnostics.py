"""Exact provider denials and no-follow file failures, without OAuth or models."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
from contextlib import ExitStack, redirect_stdout
import errno
import io
import json
import os
from pathlib import Path
import pwd
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_desktop import ask_cli, assistant_flow, execution, file_context, permissions
from ai_desktop.question_consent import QuestionConsent


def configuration(provider):
    return {"provider": provider, "model": "default", "effort": "default", "instructions": "",
            "profile": "diagnostic", "access": "read-only"}


class AccessDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ai-access-", dir=real_home)))
        self.uid = os.getuid()
        self.enterContext(patch.object(permissions, "_identity", return_value=(self.uid, str(self.home))))
        self.path = self.home / ".config/ai-desktop/permissions.toml"
        self.path.parent.mkdir(parents=True, mode=0o700)

    def policy(self, *, provider="", read=None, deny=None):
        text = ("version = 2\n[network]\nclaude = true\n" + provider
                + "\n[files]\nread = " + json.dumps(read or ["~/.config/**"])
                + "\ndeny = " + json.dumps(deny or []) + "\n")
        self.path.write_text(text, encoding="utf-8")
        self.path.chmod(0o600)
        return permissions.load_policy()

    def file(self, relative, contents="synthetic"):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def assert_reason(self, reason, action):
        with self.assertRaises(ValueError) as caught:
            action()
        self.assertEqual(caught.exception.args, (reason,))

    def test_missing_or_false_codex_permission_names_the_exact_key_before_consent_or_auth(self):
        for setting in ("", "codex = false\n"):
            for context in ([], ["--no-context"]):
                self.policy(provider=setting)
                output = io.StringIO()
                output.isatty = lambda: True
                with self.subTest(setting=setting, context=context), ExitStack() as stack:
                    stack.enter_context(patch("ai_desktop.agent_profiles.resolve", return_value=configuration("codex")))
                    stack.enter_context(patch.object(ask_cli.os, "getuid", return_value=1000))
                    stack.enter_context(patch.object(ask_cli.os, "geteuid", return_value=1000))
                    stack.enter_context(patch.object(ask_cli.sys.stdin, "isatty", return_value=True))
                    confirm = stack.enter_context(patch.object(assistant_flow, "confirm"))
                    ready = stack.enter_context(patch.object(ask_cli, "installation_ready"))
                    session = stack.enter_context(patch.object(ask_cli, "managed_session"))
                    send = stack.enter_context(patch.object(ask_cli, "send_once"))
                    stack.enter_context(redirect_stdout(output))
                    code = ask_cli.main(["--question", "Sprawdź monitory Niri DMS"] + context)
                self.assertEqual(code, 2, output.getvalue())
                self.assertIn("network.codex=false", output.getvalue())
                self.assertIn("codex = true in [network]", output.getvalue())
                self.assertNotIn("The access policy does not allow", output.getvalue())
                for operation in (confirm, ready, session, send):
                    operation.assert_not_called()
                self.assertNotIn("codex = true", self.path.read_text(encoding="utf-8"))

    def test_permission_reason_is_bound_to_captured_provider(self):
        self.policy()
        with execution.using(configuration("codex")):
            snapshot = assistant_flow.capture_access()
        with execution.using(configuration("claude")):
            with self.assertRaises(assistant_flow.AccessDenied) as caught:
                snapshot.require_claude()
        self.assertEqual(caught.exception.reason, "network-codex-disabled")

    def test_explicit_codex_permission_preserves_local_sources_and_readonly_limits(self):
        policy = self.policy(provider="codex = true\n")
        with execution.using(configuration("codex")):
            snapshot = assistant_flow.capture_access()
            snapshot.require_claude()
        self.assertEqual(snapshot.provider, "codex")
        self.assertTrue(snapshot.sources["claude"])
        self.assertEqual(snapshot.access["files"], policy.access()["files"])
        self.assertNotIn("write", snapshot.access)
        self.assertNotIn("web", snapshot.access)

    def test_config_allow_with_gh_deny_reads_niri_dms_without_touching_gh(self):
        niri = self.file(".config/niri/config.kdl", 'output "DP-1" { scale 1.25; }')
        dms = self.file(".config/DankMaterialShell/settings.json", '{"monitor":"DP-1"}')
        secret = self.file(".config/gh/hosts.yml", "PRIVATE GH TOKEN")
        (secret.parent / "link").symlink_to(niri)
        os.mkfifo(secret.parent / "fifo", mode=0o600)
        secret.parent.chmod(0o000)
        self.addCleanup(secret.parent.chmod, 0o700)
        policy = self.policy(deny=["~/.config/gh/", "~/.config/ai-desktop/"])
        real_open, real_stat, real_scandir = os.open, os.stat, os.scandir
        gh_inode = real_stat(secret.parent, follow_symlinks=False).st_ino

        def guarded_open(path, *args, **kwargs):
            self.assertNotEqual(path, "gh", "denied gh directory was opened")
            return real_open(path, *args, **kwargs)

        def guarded_stat(path, *args, **kwargs):
            self.assertNotEqual(path, "gh", "denied gh directory metadata was read")
            return real_stat(path, *args, **kwargs)

        def guarded_scandir(descriptor):
            self.assertNotEqual(os.fstat(descriptor).st_ino, gh_inode, "denied gh directory was enumerated")
            return real_scandir(descriptor)

        with patch.object(permissions.os, "open", side_effect=guarded_open), \
                patch.object(permissions.os, "stat", side_effect=guarded_stat), \
                patch.object(permissions.os, "scandir", side_effect=guarded_scandir):
            paths = policy.expand((0,))
            result = permissions.read_selected(policy, paths)
        self.assertEqual(paths, tuple(sorted((str(niri), str(dms)))))
        self.assertNotIn("PRIVATE GH TOKEN", json.dumps(result))
        self.assertFalse(policy.allows(str(secret)))
        with patch.object(permissions.os, "open", side_effect=AssertionError("denied direct selection performed I/O")):
            self.assert_reason("files-denied", lambda: policy.expand_selected(((0, "gh/hosts.yml"),)))

    def test_actual_symlink_and_non_directory_ancestors_have_specific_denials_without_read(self):
        source = self.file("project/source.txt")
        directory_link = self.home / "linked-directory"
        directory_link.symlink_to(source.parent, target_is_directory=True)
        file_link = self.home / "linked-file"
        file_link.symlink_to(source)
        cases = ((directory_link / "source.txt", "files-symlink"),
                 (file_link, "files-symlink"),
                 (source / "config.kdl", "files-directory"))
        for path, reason in cases:
            with self.subTest(reason=reason, path=path), \
                    patch.object(permissions.os, "read", side_effect=AssertionError("refused target content was read")):
                self.assert_reason(reason, lambda: permissions._read(str(path), self.uid, 1024))

    def test_permission_errno_is_specific_and_unrelated_io_error_stays_generic(self):
        source = self.file("project/source.txt")
        real_open = os.open
        for error, expected in ((errno.EACCES, "files-permission"), (errno.EPERM, "files-permission"),
                                (errno.EIO, "files-access")):
            def blocked(path, *args, **kwargs):
                if path == source.name:
                    raise OSError(error, "PRIVATE RAW ERROR", "PRIVATE PATH")
                return real_open(path, *args, **kwargs)
            with self.subTest(error=error), patch.object(permissions.os, "open", side_effect=blocked):
                self.assert_reason(expected, lambda: permissions._read(str(source), self.uid, 1024))


class FileRefusalDiagnosticsTests(unittest.TestCase):
    def test_quiet_failure_shows_only_selected_safe_scope_and_actual_provider(self):
        policy = Mock()
        policy.scopes.return_value = (permissions.Scope(0, "~/.config/**"), permissions.Scope(1, "~/unrelated/**"))
        access = {"files": [{"id": "f0", "pattern": "~/.config/**"}, {"id": "f1", "pattern": "~/unrelated/**"}],
                  "systemd": [], "weather": False}
        policy.access.return_value = access
        policy.expand_selected.side_effect = ValueError("files-symlink")
        snapshot = Mock(policy=policy, access=access, sources={"claude": True, "systemd": [], "weather": False})
        output = io.StringIO()
        args = SimpleNamespace(no_context=False, preview=False, details=False)
        with execution.using(configuration("codex")):
            permit = QuestionConsent("Pytanie", snapshot)
            permit.approve()
            with redirect_stdout(output), self.assertRaises(file_context.FileReadDenied):
                file_context.collect(policy, [{"id": "f0", "path": "niri/**"}], args, question_consent=permit)
        text = output.getvalue()
        self.assertIn("symbolic link", text)
        self.assertIn('Selected: "~/.config/** → niri/**"', text)
        self.assertIn("The file contents were not sent to Codex.", text)
        self.assertNotIn("Claude", text)
        self.assertNotIn("unrelated", text)


if __name__ == "__main__":
    unittest.main()
