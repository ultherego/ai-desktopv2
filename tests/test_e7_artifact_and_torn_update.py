"""E7 on synthetic data: (1) every application artifact excludes independently installed clients and ignored local files;
(2) a torn update of a real staged package is detected by file integrity, the protocol refuses the mix, the same artifact
restores it, and user data never changes; (3) the hook's upgrade/removal paths are exercised, not just grepped."""
import hashlib
import re
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
FAKE_ELF = b"\x7fELF" + b"synthetic-not-a-real-client\n" * 8


def source_tree(destination, *, checkout=ROOT):
    """Copy only package inputs into a disposable fixture; never require the owner's checkout to lack a local client."""
    for name in ("ai_desktop", "skills", "packaging"):
        shutil.copytree(checkout / name, destination / name,
                        ignore=shutil.ignore_patterns("__pycache__", "claude-code", "codex", "*.pkg.tar.*"))
    return destination


def stage(tree, pkgdir, *, public=False, client=None):
    startdir = tree / "packaging/arch"
    if client is not None:
        client(startdir / "claude-code")
    env = dict(os.environ, startdir=str(startdir), pkgdir=str(pkgdir))
    env.pop("AI_DESKTOP_PUBLIC_ARTIFACT", None)
    if public:
        env["AI_DESKTOP_PUBLIC_ARTIFACT"] = "1"
    return subprocess.run(["/usr/bin/bash", "-c", 'set -e; source "$1"; package', "bash", str(startdir / "PKGBUILD")],
                          env=env, capture_output=True, text=True, timeout=30)


def manifest(root):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and not path.is_symlink():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def verify(root, expected):
    """Emulates `pacman -Qkk`: names of files whose bytes differ from the artifact's manifest or that are missing/extra."""
    actual = manifest(root)
    return sorted(set(name for name in set(expected) | set(actual) if expected.get(name) != actual.get(name)))


class PublicArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ai-artifact-")))
        self.tree = source_tree(self.temp / "src")

    def client(self, path):
        path.write_bytes(FAKE_ELF)
        path.chmod(0o755)

    def test_every_build_ignores_legacy_clients_and_has_the_same_application_manifest(self):
        clean = self.temp / "clean"
        self.assertEqual(stage(self.tree, clean).returncode, 0)
        expected = manifest(clean)
        startdir = self.tree / "packaging/arch"
        for name in ("claude-code", "codex"):
            self.client(startdir / name)
        before = {name: (startdir / name).read_bytes() for name in ("claude-code", "codex")}
        for public in (False, True):
            target = self.temp / ("public" if public else "default")
            result = stage(self.tree, target, public=public)
            self.assertEqual(result.returncode, 0, result.stderr[-600:])
            self.assertEqual(manifest(target), expected)
        self.assertEqual({name: (startdir / name).read_bytes() for name in before}, before)
        for name in expected:
            self.assertNotEqual((clean / name).read_bytes()[:4], b"\x7fELF", name)
        self.assertNotIn("usr/lib/ai-desktop/claude-code", expected)
        self.assertNotIn("usr/lib/ai-desktop/codex", expected)
        self.assertNotIn("usr/bin/claude", expected)
        self.assertNotIn("usr/bin/codex", expected)

    def test_legacy_symlinks_and_special_files_are_never_followed_or_opened(self):
        startdir = self.tree / "packaging/arch"
        (startdir / "claude-code").symlink_to("/nonexistent/claude")
        os.mkfifo(startdir / "codex", 0o600)
        result = stage(self.tree, self.temp / "pkg")
        self.assertEqual(result.returncode, 0, result.stderr[-600:])
        self.assertTrue((startdir / "claude-code").is_symlink())
        self.assertTrue(stat.S_ISFIFO((startdir / "codex").lstat().st_mode))
        self.assertNotIn("usr/lib/ai-desktop/claude-code", manifest(self.temp / "pkg"))
        self.assertNotIn("usr/lib/ai-desktop/codex", manifest(self.temp / "pkg"))

    def test_local_vendor_files_are_not_copied_into_the_isolated_source_fixture(self):
        checkout = source_tree(self.temp / "source-with-client")
        self.client(checkout / "packaging/arch/claude-code")
        self.client(checkout / "packaging/arch/codex")
        isolated = source_tree(self.temp / "isolated-source", checkout=checkout)
        self.assertFalse((isolated / "packaging/arch/claude-code").exists())
        self.assertFalse((isolated / "packaging/arch/codex").exists())
        result = stage(isolated, self.temp / "artifact")
        self.assertEqual(result.returncode, 0, result.stderr[-600:])



WORKER = """
import sys
sys.path.insert(0, sys.argv[1])
from ai_desktop import analysis_contract as ac
try:
    ac.parse_request(sys.stdin.buffer.read())
    print("ACCEPTED")
except ac.AnalysisDenied:
    print("REFUSED")
"""
CLIENT = """
import sys
sys.path.insert(0, sys.argv[1])
from ai_desktop import analysis_contract as ac, material_whole as w, materials as m
plan = w.WholePlan(m.import_bytes(b"alpha\\nbeta\\n" * 40, "file"))
print(ac.application_input(None, "Pytanie?", material=plan.map_input(0, 1, 4096)), end="")
"""


def run_python(code, root, stdin=b""):
    return subprocess.run([sys.executable, "-I", "-B", "-c", code, str(root)], input=stdin, capture_output=True, timeout=60)


class TornUpdateTests(unittest.TestCase):
    """A real staged package, a synthetic installation, a controlled interrupted replacement of one file."""

    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ai-torn-")))
        tree = source_tree(self.temp / "src")
        self.assertEqual(stage(tree, self.temp / "artifact").returncode, 0)
        self.artifact = self.temp / "artifact/usr/lib/ai-desktop"
        self.expected = manifest(self.artifact)                                  # the package's own file list and hashes
        self.installed = self.temp / "installed"
        shutil.copytree(self.artifact, self.installed)
        newer = self.temp / "newer"
        shutil.copytree(self.artifact, newer)
        contract = newer / "ai_desktop/analysis_contract.py"
        text = contract.read_text()
        self.assertIn('POLICY = "a-plus-assistant-v7"', text)
        contract.write_text(text.replace('POLICY = "a-plus-assistant-v7"', 'POLICY = "a-plus-assistant-v8"'))
        self.newer = newer
        self.user = self.temp / "user-data"                                       # synthetic config, history and OAuth profile
        for name, content in (("home/.config/ai-desktop/permissions.toml", b"version = 1\n"),
                              ("workspace/.ai-desktop-tasks/journal.json", b'{"synthetic": true}\n'),
                              ("var-lib/ai-desktop/claude/.credentials.json", b"SYNTHETIC-NOT-A-TOKEN\n"),
                              ("var-lib/ai-desktop-clients/claude/payload/bin/claude", FAKE_ELF),
                              ("var-lib/ai-desktop-clients/claude/manifest.json", b'{"synthetic": true}\n')):
            path = self.user / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.user_before = manifest(self.user)

    def test_an_interrupted_replacement_is_detected_refused_restored_and_leaves_user_data_alone(self):
        self.assertEqual(verify(self.installed, self.expected), [])               # consistent installation
        request = run_python(CLIENT, self.installed).stdout
        self.assertEqual(run_python(WORKER, self.installed, request).stdout.strip(), b"ACCEPTED")

        shutil.copy2(self.newer / "ai_desktop/analysis_contract.py", self.installed / "ai_desktop/analysis_contract.py")   # torn
        self.assertEqual(verify(self.installed, self.expected), ["ai_desktop/analysis_contract.py"])       # integrity check names it
        self.assertEqual(run_python(WORKER, self.installed, request).stdout.strip(), b"REFUSED")           # old client, torn worker
        newer_request = run_python(CLIENT, self.newer).stdout
        self.assertEqual(run_python(WORKER, self.installed, newer_request).stdout.strip(), b"ACCEPTED")    # same file on both sides
        self.assertEqual(run_python(WORKER, self.artifact, newer_request).stdout.strip(), b"REFUSED")      # mixed with the old worker
        version = subprocess.run([sys.executable, "-I", "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1]);"
                                  "from ai_desktop.cli import main; sys.exit(main(['--version']))", str(self.installed)],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(version.returncode, 0)                                   # the CLI itself still starts: only the integrity
        self.assertIn("ai-desktop 0.1.0", version.stdout)                          # check (pacman -Qkk) or the protocol notices

        for name in verify(self.installed, self.expected):                         # recovery = restore from the SAME artifact
            shutil.copy2(self.artifact / name, self.installed / name)
        self.assertEqual(verify(self.installed, self.expected), [])
        self.assertEqual(run_python(WORKER, self.installed, request).stdout.strip(), b"ACCEPTED")
        self.assertEqual(manifest(self.user), self.user_before)                    # config, history and profile never changed

    def test_a_missing_or_extra_file_is_also_reported_by_the_integrity_check(self):
        (self.installed / "ai_desktop/cli.py").unlink()
        (self.installed / "ai_desktop/stray.py").write_text("x = 1\n")
        self.assertEqual(verify(self.installed, self.expected), ["ai_desktop/cli.py", "ai_desktop/stray.py"])


STUBS = {"getent": 'case "$1" in passwd) echo "ai-desktop:x:990:990::/var/lib/ai-desktop:/usr/bin/nologin";; group) echo "ai-desktop:x:990:";; esac',
         "id": 'case "$1" in -u) echo 990;; -g) echo 990;; -nG) echo ai-desktop;; esac',
         "stat": 'case "$*" in *"%a:%U:%G"*) echo "700:ai-desktop:ai-desktop";; *"%u:%g"*) echo "0:0";; esac'}
MUTATORS = ("rm", "rmdir", "mv", "cp", "chown", "chmod", "userdel", "usermod", "groupdel", "systemctl", "systemd-sysusers",
            "systemd-tmpfiles", "tee", "sudo", "pacman", "sed", "truncate", "ln", "mkdir", "touch", "install")


class HookExecutionTests(unittest.TestCase):
    """The hook functions are RUN with stubs; every state-changing command would be logged and must never be called."""

    def run_hook(self, function, stub_overrides=None):
        temp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ai-hook-")))
        bin_dir, log = temp / "bin", temp / "calls.log"
        bin_dir.mkdir()
        stubs = dict(STUBS, **(stub_overrides or {}))
        for name in set(stubs) | set(MUTATORS):
            body = stubs.get(name, "")
            (bin_dir / name).write_text("#!/bin/sh\necho \"%s $*\" >> '%s'\n%s\n" % (name, log, body))
            (bin_dir / name).chmod(0o755)
        # The real directory checks need /var/lib/ai-desktop on the test host; the stubbed `stat` stands in for them so
        # the test is the same on a machine with and without an installation (the validation order is unchanged).
        override = '_ai_desktop_dir_valid() { [ "$(stat -c %a:%U:%G -- "$1")" = "700:ai-desktop:ai-desktop" ]; }'
        script = '. "$1"; %s; %s; echo "RC=$?"' % (override, function)
        result = subprocess.run(["/bin/sh", "-c", script, "sh", str(ROOT / "packaging/arch/ai-desktop.install")],
                                env={"PATH": "%s:/usr/bin:/bin" % bin_dir}, capture_output=True, text=True, timeout=20)
        calls = log.read_text().splitlines() if log.exists() else []
        return result.stdout.strip(), calls

    def test_upgrade_with_a_valid_system_validates_only_and_mutates_nothing(self):
        out, calls = self.run_hook("post_upgrade")
        self.assertIn("RC=0", out)
        self.assertTrue(calls)                                                     # it did look (getent/id/stat) ...
        self.assertEqual([c for c in calls if c.split()[0] in MUTATORS], [])      # ... and changed nothing

    def test_upgrade_with_an_invalid_account_state_refuses_without_repairing_anything(self):
        out, calls = self.run_hook("post_upgrade", {"stat": 'echo "755:root:root"'})
        self.assertNotIn("RC=0", out)
        self.assertEqual([c for c in calls if c.split()[0] in MUTATORS], [])

    def test_removal_calls_no_command_at_all(self):
        out, calls = self.run_hook("post_remove")
        self.assertIn("RC=0", out)
        self.assertEqual(calls, [])                                                # not even a read: it only preserves


if __name__ == "__main__":
    unittest.main()
