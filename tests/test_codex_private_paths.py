"""Legacy broad HOME rules never expose a local Codex profile or transcripts."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import permissions as permissions
from ai_desktop import project_scope


def policy(home, *, version=1):
    return permissions.Policy(uid=os.getuid(), home=home, _raw=b"", _identity=(),
                              _read=(permissions._rule("~/**", home, version=version),),
                              _deny=(), _version=version)


class CodexPrivatePathTests(unittest.TestCase):
    def test_legacy_home_rule_denies_codex_files_before_any_target_io(self):
        selected = policy("/home/synthetic")
        paths = ("~/.codex/config.toml", "~/.codex/sessions/2026/11/session.jsonl",
                 "~/project/.codex/config.toml", "~/project/.codex/sessions/private.jsonl")
        with patch.object(permissions.os, "open", side_effect=AssertionError("target opened")) as opened, \
                patch.object(permissions.os, "stat", side_effect=AssertionError("target stat")) as stated:
            for path in paths:
                with self.subTest(path=path):
                    self.assertFalse(selected.allows(path))
                    self.assertTrue(selected.excludes(path))
                    with self.assertRaisesRegex(ValueError, "^files-denied$"):
                        selected.select([path])
                    with self.assertRaisesRegex(ValueError, "^files-denied$"):
                        permissions.read_bounded(selected, path, 4096)
                    with self.assertRaisesRegex(ValueError, "^files-denied$"):
                        permissions.read_selected(selected, [path])
            self.assertEqual(selected.select(["~/project/public.txt"]), ("/home/synthetic/project/public.txt",))
        opened.assert_not_called()
        stated.assert_not_called()

    def test_project_scan_never_opens_codex_directory_or_exposes_its_names(self):
        with tempfile.TemporaryDirectory(prefix="ai-codex-private-") as directory:
            root = Path(directory)
            (root / ".codex/sessions").mkdir(parents=True)
            (root / ".codex/config.toml").write_text("SYNTHETIC_CODEX_PRIVATE\n")
            (root / ".codex/sessions/SECRET-SESSION-NAME.jsonl").write_text("SYNTHETIC_CODEX_PRIVATE\n")
            (root / "public.txt").write_text("public\n")
            selected = policy(str(root))
            opened = []
            original = permissions.open_directory

            def spy(path, uid, **kwargs):
                opened.append(path)
                return original(path, uid, **kwargs)

            with patch.object(permissions.Policy, "recheck"), patch.object(permissions, "open_directory", spy):
                project = project_scope.Project(selected, str(root)).scan()
                self.assertEqual([entry.path for entry in project.files], ["public.txt"])
                self.assertEqual([entry.path for entry in project.listing()[0]], ["public.txt"])
                self.assertEqual(project.find_names("SECRET")[0], ())
                self.assertEqual(project.search_text("SYNTHETIC_CODEX_PRIVATE").hits, ())
            self.assertEqual(project.stats["denied"], 1)
            self.assertFalse(any(".codex" in path.split("/") for path in opened))

    def test_explicit_owner_managed_v2_semantics_remain_the_same_as_claude(self):
        # Version 2 deliberately delegates location choices to the owner;
        # this legacy filter must not silently rewrite that existing policy.
        selected = policy("/home/synthetic", version=2)
        with patch.object(permissions.os, "open", side_effect=AssertionError("target opened")):
            self.assertEqual(selected.select(["~/.codex/config.toml", "~/.claude/config.json"]),
                             ("/home/synthetic/.codex/config.toml", "/home/synthetic/.claude/config.json"))
