"""Synthetic policy/files only; never inspect a real user's configuration."""
import json
import os
from pathlib import Path
import pwd
import stat
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import permissions as p


class PolicyTests(unittest.TestCase):
    def setUp(self):
        # Unlike /tmp, passwd HOME has a secure ancestor chain on supported
        # hosts. Only this fresh temporary subtree is accessed by the tests.
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix="ai-policy-test-", dir=real_home))
        self.home = Path(self.temp)
        self.uid = os.getuid()
        self.enterContext(patch.object(p, "_identity", return_value=(self.uid, str(self.home))))
        self.config = self.home / ".config/ai-desktop/permissions.toml"
        self.config.parent.mkdir(parents=True, mode=0o700)
        self.project = self.home / "project"
        self.project.mkdir(mode=0o700)

    def policy(self, read=("~/project/*",), deny=(), sections="", write=None, version=1):
        text = "version = " + str(version) + "\n[files]\nread = " + json.dumps(list(read)) + "\ndeny = " + json.dumps(list(deny)) + "\n"
        if write is not None:
            text += "write = " + json.dumps(write) + "\n"
        self.config.write_text(text + sections, encoding="utf-8")
        self.config.chmod(0o600)
        return p.load_policy()

    def file(self, relative="project/a.txt", content="synthetic text\n"):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if type(content) is bytes else content.encode("utf-8"))
        return path

    def assert_code(self, action):
        with self.assertRaises(ValueError) as caught:
            action()
        self.assertRegex(str(caught.exception), r"\A(?:files|policy)-[a-z-]+\Z")
        return str(caught.exception)

    def test_missing_optional_policy_only_absence_is_none(self):
        self.assertIsNone(p.load_policy(optional=True))
        self.assert_code(p.load_policy)
        self.config.parent.rmdir()
        self.assertIsNone(p.load_policy(optional=True))
        self.config.parent.mkdir(mode=0o700)
        self.config.write_text("INVALID SECRET", encoding="utf-8")
        self.config.chmod(0o600)
        self.assertEqual(self.assert_code(lambda: p.load_policy(optional=True)), "policy-invalid")
        self.config.chmod(0o644)
        self.assert_code(lambda: p.load_policy(optional=True))

    def test_empty_policy_denies_and_metadata_selection_has_no_target_io(self):
        empty = self.policy(read=())
        self.assertEqual(empty.scopes(), ())
        self.assert_code(lambda: empty.select(("~/project/a",)))
        policy = self.policy(read=("~/project/*", "~/other.txt"))
        with patch.object(p.os, "open", side_effect=AssertionError("target access")), \
                patch.object(p.os, "stat", side_effect=AssertionError("target access")):
            self.assertEqual([(s.index, s.pattern) for s in policy.scopes()],
                             [(0, "~/project/*"), (1, "~/other.txt")])
            self.assertEqual(policy.select(("~/project/a",)), (str(self.project / "a"),))
            self.assert_code(lambda: policy.select(("~/project/sub/a",)))
            self.assert_code(lambda: policy.select(("~/project/a", "~/project/a")))
            self.assert_code(lambda: policy.select(("~/other",)))
        with self.assertRaises(AttributeError):
            policy.scopes()[0].index = 10

    def test_strict_schema_version_rules_and_nonexpanding_paths(self):
        invalid = ("version=1\n[files]\nread=[]", "version=true\n[files]\nread=[]\ndeny=[]",
                   "version=1\nwrite=[]\n[files]\nread=[]\ndeny=[]", "version=1\n[files]\nread='x'\ndeny=[]",
                   "version=1\n[files]\nread=[]\ndeny=[]\ncreate=[]", "version=3\n[files]\nread=[]\ndeny=[]")
        for text in invalid:
            self.config.write_text(text, encoding="utf-8")
            self.config.chmod(0o600)
            self.assertEqual(self.assert_code(p.load_policy), "policy-invalid")
        for rule in ("relative", "~someone/file", "$HOME/file", "~/../file", "~/project/./a",
                     "~/project//a", "~/project/a*", "~/project/?", "~/project/[a]",
                     "~/project/**/a", "~/project/", "~/project/\nname", "~/project/\u202ename"):
            with self.subTest(rule=rule):
                self.assert_code(lambda: self.policy(read=(rule,)))
        self.assert_code(lambda: self.policy(read=("~/project/a", "~/project/a")))
        self.assert_code(lambda: self.policy(read=("~/project/a",) * 65))

    def with_file_size(self, literal):
        self.config.write_text('version = 1\n[files]\nread = ["~/project/*"]\ndeny = []\nfile_size = %s\n' % literal, encoding="utf-8")
        self.config.chmod(0o600)
        return p.load_policy()

    def test_file_size_defaults_to_five_mib_and_accepts_bounded_integers_and_unit_strings(self):
        self.assertEqual(self.policy().file_size(), 5 * 1024 * 1024)
        for literal, expected in (("2097152", 2097152), ('"2097152"', 2097152), ('"512KiB"', 512 * 1024), ('"5 MiB"', 5 * 1024 * 1024),
                                  ('" 16MiB "', 16 * 1024 * 1024), ('"4KiB"', 4096), ("4096", 4096), ('"8192B"', 8192)):
            with self.subTest(literal=literal):
                self.assertEqual(self.with_file_size(literal).file_size(), expected)

    def test_invalid_file_size_values_are_a_policy_error_not_a_silent_default(self):
        for literal in ("4095", '"3KiB"', '"17MiB"', "16777217", '"5MB"', '"5 mib"', '"-1"', '"five"', "true", "1.5", '"1e6"',
                        "-4096", '""', '"5MiB extra"', "[5]", '"9999999999"'):
            with self.subTest(literal=literal):
                self.assertEqual(self.assert_code(lambda: self.with_file_size(literal)), "policy-invalid")

    def with_part_size(self, literal):
        self.config.write_text('version = 1\n[files]\nread = ["~/project/*"]\ndeny = []\npart_size = %s\n' % literal, encoding="utf-8")
        self.config.chmod(0o600)
        return p.load_policy()

    def test_part_size_defaults_to_240_kib_and_accepts_bounded_values(self):
        self.assertEqual(self.policy().part_size(), 240 * 1024)
        for literal, expected in (('"64KiB"', 64 * 1024), ("30720", 30720), ('"512 KiB"', 512 * 1024), ('"30KiB"', 30 * 1024)):
            with self.subTest(literal=literal):
                self.assertEqual(self.with_part_size(literal).part_size(), expected)

    def test_invalid_part_size_values_are_a_policy_error(self):
        for literal in ("30719", '"29KiB"', '"513KiB"', '"1MiB"', "true", '"big"', "[1]", "-1"):
            with self.subTest(literal=literal):
                self.assertEqual(self.assert_code(lambda: self.with_part_size(literal)), "policy-invalid")

    def test_a_bad_size_names_the_setting_but_never_the_value(self):
        for key, literal in (("part_size", '"1MiB"'), ("file_size", '"3KiB"')):
            with self.subTest(key=key):
                try:
                    (self.with_part_size if key == "part_size" else self.with_file_size)(literal)
                except ValueError as exc:
                    self.assertEqual((exc.args, exc.setting), (("policy-invalid",), key))
                else:
                    self.fail("accepted")

    def test_file_size_is_not_advertised_to_the_model_as_access(self):
        self.assertNotIn("file_size", json.dumps(self.with_file_size('"1MiB"').access()))

    def test_optional_sources_default_to_no_access(self):
        for sections in ("", "[network]\n", "[system]\n", "[network]\n[system]\n"):
            with self.subTest(sections=sections):
                policy = self.policy(sections=sections)
                self.assertEqual(policy.sources(), {"claude": False, "weather": False, "systemd": []})
                self.assertEqual(policy.access(), {"files": [{"id": "f0", "pattern": "~/project/*"}],
                                                   "systemd": [], "weather": False})

    def test_web_requires_explicit_boolean_in_each_policy_version_and_revokes_old_grant(self):
        for version in (1, 2):
            for setting in ("", "web=false\n", "web=true\n"):
                with self.subTest(version=version, setting=setting):
                    policy = self.policy(version=version, sections="[network]\nclaude=true\n" + setting)
                    allowed = setting == "web=true\n"
                    with patch.object(p.os, "open", side_effect=AssertionError("target read")), \
                            patch.object(p.os, "scandir", side_effect=AssertionError("target enumeration")):
                        self.assertEqual(policy.sources().get("web", False), allowed)
                        self.assertEqual(policy.access().get("web", False), allowed)
                        self.assertEqual("web" in policy.sources(), allowed)
                        self.assertEqual("web" in policy.access(), allowed)
            for value in ("1", "0", "'true'", "[]", "{}"):
                with self.subTest(version=version, value=value):
                    self.assertEqual(self.assert_code(lambda: self.policy(version=version,
                        sections="[network]\nweb=" + value + "\n")), "policy-invalid")
        policy = self.policy(version=2, sections="[network]\nclaude=true\nweb=true\n")
        self.policy(version=2, sections="[network]\nclaude=true\nweb=false\n")
        self.assertEqual(self.assert_code(policy.recheck), "policy-changed")

    def test_write_is_optional_empty_and_never_inferred_from_read(self):
        for write in (None, []):
            policy = self.policy(write=write)
            self.assertEqual(policy.write_scopes(), ())
            self.assertNotIn("write", policy.access())
            self.assertFalse(policy.can_write("~/project/a.txt"))
            self.assertEqual(self.assert_code(lambda: policy.require_write(("~/project/a.txt",))),
                             "files-write-denied")
        for write in (True, "~/project/*", {}, [True], ["~/project/a"] * 2,
                      ["~/project/" + str(i) for i in range(65)]):
            with self.subTest(write=write):
                self.assertEqual(self.assert_code(lambda: self.policy(write=write)), "policy-invalid")

    def test_write_metadata_is_immutable_detached_and_has_no_target_io(self):
        policy = self.policy(write=["~/project/*", "~/project/sub/**", "~/project/new.txt"])
        with patch.object(p.os, "open", side_effect=AssertionError("unexpected target access")), \
                patch.object(p.os, "stat", side_effect=AssertionError("unexpected target access")):
            scopes = policy.write_scopes()
            self.assertEqual([(s.index, s.pattern, s.base, s.mode) for s in scopes],
                [(0, "~/project/*", str(self.project), "children"),
                 (1, "~/project/sub/**", str(self.project / "sub"), "tree"),
                 (2, "~/project/new.txt", str(self.project / "new.txt"), "exact")])
            with self.assertRaises(AttributeError):
                scopes[0].base = "/different"
            access = policy.access()
            self.assertEqual(access["write"], [{"id": "w0", "pattern": "~/project/*"},
                                               {"id": "w1", "pattern": "~/project/sub/**"},
                                               {"id": "w2", "pattern": "~/project/new.txt"}])
            access["write"][0]["pattern"] = "~/elsewhere/**"
            self.assertEqual(policy.access()["write"][0]["pattern"], "~/project/*")
            self.assertTrue(policy.can_write("~/project/new.txt"))

    def test_write_grants_are_lexical_deny_first_and_independent_of_read(self):
        policy = self.policy(read=(), write=["~/project/**", "~/project/private/a.txt"],
                             deny=["~/project/private/**", "~/project/denied.txt"])
        # The missing file is a proposed creation, not permission to read it.
        target = str(self.project / "new.txt")
        original_read = p._read
        def config_only(path, *args, **kwargs):
            self.assertEqual(path, str(self.config), "target was read before the task snapshot")
            return original_read(path, *args, **kwargs)
        with patch.object(p, "_read", side_effect=config_only), \
                patch.object(p.os, "scandir", side_effect=AssertionError("target enumeration")):
            self.assertEqual(policy.require_write(("~/project/new.txt",)), (target,))
            self.assert_code(lambda: policy.select((target,)))
            for path in ("~/project/private/a.txt", "~/project/private", "~/project/denied.txt",
                         "~/other/file.txt"):
                self.assertFalse(policy.can_write(path))
                self.assertEqual(self.assert_code(lambda: policy.require_write((path,))), "files-write-denied")
        self.assertFalse(Path(target).exists())
        for paths in ([], [target, target], [True], target, {target}, ["~/../bad"]):
            self.assert_code(lambda: policy.require_write(paths))
        policy = self.policy(write=["~/project/*"])
        self.assertTrue(policy.can_write("~/project/a.txt"))
        self.assertFalse(policy.can_write("~/project/sub/a.txt"))

    def test_write_hardblocks_cannot_be_overridden_by_configuration(self):
        forbidden = ("~/**", "~/*", str(self.home), "/home/**", "/root/**", "/etc/example",
                     "/usr/share/example", "/var/tmp/example", "/opt/example", "/boot/example",
                     "/proc/self/status", "~/.ssh/config", "~/.ssh/id_ed25519.pub",
                     "~/.config/ai-desktop/permissions.toml", "~/.local/state/ai-desktop/a",
                     "~/project/.ai-desktop-tasks/a", "~/project/.ai-task-stage", "~/project/.git/config",
                     "~/project/.env", "~/project/credentials/a", "~/project/cert.pem/a")
        for path in forbidden:
            with self.subTest(path=path):
                self.assertEqual(self.assert_code(lambda: self.policy(write=[path])), "policy-invalid")
        policy = self.policy(write=["~/project/**"])
        for path in forbidden:
            if not path.endswith(("/*", "/**")):
                self.assertFalse(policy.can_write(path))
        niri = self.policy(read=["~/.config/niri/**"], write=["~/.config/niri/**"])
        self.assertTrue(niri.can_write("~/.config/niri/config.kdl"))
        self.assertEqual(p.validate_write_path(str(self.home / ".config/niri"), str(self.home)),
                         str(self.home / ".config/niri"))

    def test_selected_write_scope_cannot_borrow_other_rule_in_same_workspace(self):
        policy = self.policy(write=["~/project/first.txt", "~/project/second.txt", "~/project/sub/**"])
        self.assertEqual(policy.write_workspace(0), policy.write_workspace(1))
        self.assertTrue(policy.can_write("~/project/second.txt"))
        self.assertFalse(policy.can_write("~/project/second.txt", index=0))
        self.assertTrue(policy.can_write("~/project/second.txt", index=1))
        self.assertTrue(policy.can_write("~/project/sub/deeper/new.txt", index=2))
        self.assertEqual(policy.require_write(["~/project/second.txt"], index=1),
                         (str(self.project / "second.txt"),))
        self.assertEqual(self.assert_code(lambda: policy.require_write(["~/project/second.txt"], index=0)),
                         "files-write-denied")
        for index in (True, False, "0", -1, 3):
            self.assertEqual(self.assert_code(lambda: policy.require_write(["~/project/first.txt"], index=index)),
                             "files-scope")

    def test_write_retains_backup_oauth_secret_and_local_component_exclusions(self):
        for component in ("oauth", "secrets", "backup", "backups", ".local",
                          "OAuth", "SECRETS", "Backup", "BACKUPS", ".LOCAL"):
            target = str(self.project / component / "file.txt")
            with self.subTest(component=component):
                self.assertEqual(self.assert_code(lambda: p.validate_write_path(target, str(self.home))),
                                 "files-write-denied")
                for pattern in ("~/project/" + component + "/file.txt",
                                "~/project/" + component + "/*", "~/project/" + component + "/**"):
                    self.assertEqual(self.assert_code(lambda: self.policy(write=[pattern])), "policy-invalid")
                policy = self.policy(write=["~/project/**"])
                self.assertFalse(policy.can_write(target))
                self.assertEqual(self.assert_code(lambda: policy.require_write([target])), "files-write-denied")

    def test_write_workspace_requires_existing_private_owned_no_follow_root(self):
        policy = self.policy(write=["~/project/*", "~/project/new.txt"])
        self.assertEqual(policy.write_workspace(0), str(self.project))
        self.assertEqual(policy.write_workspace(1), str(self.project))
        self.assertFalse((self.project / "new.txt").exists())
        for index in (True, False, -1, 2, "0", None):
            self.assert_code(lambda: policy.write_workspace(index))
        self.project.chmod(0o770)
        self.assert_code(lambda: policy.write_workspace(0))
        self.project.chmod(0o700)
        for mode in (0o1700, 0o2700, 0o4700):
            self.project.chmod(mode)
            self.assert_code(lambda: policy.write_workspace(0))
        self.project.chmod(0o700)
        self.assert_code(lambda: self.policy(write=["~/project/missing/new.txt"]).write_workspace(0))
        self.assertFalse((self.project / "missing").exists())
        (self.home / "link").symlink_to(self.project, target_is_directory=True)
        self.assert_code(lambda: self.policy(write=["~/link/*"]).write_workspace(0))
        self.assert_code(lambda: self.policy(write=["~/direct.txt"]).write_workspace(0))
        self.assert_code(lambda: self.policy(write=["~/project/**"], deny=["~/project/**"]).write_workspace(0))
        self.file("project/not-directory")
        self.assert_code(lambda: self.policy(write=["~/project/not-directory/*"]).write_workspace(0))

    def test_write_authority_rechecks_policy_and_combined_metadata_budget(self):
        policy = self.policy(write=["~/project/*"])
        self.config.write_bytes(self.config.read_bytes().replace(b'write = ["~/project/*"]', b'write = []'))
        self.assertEqual(self.assert_code(lambda: policy.require_write(("~/project/new.txt",))), "policy-changed")
        self.assertEqual(self.assert_code(lambda: policy.write_workspace(0)), "policy-changed")
        rules = tuple("~/project/" + str(i) + "-" + "a" * 180 for i in range(23))
        self.policy(read=rules)
        self.assertEqual(self.assert_code(lambda: self.policy(read=rules, write=rules)), "policy-invalid")

    def test_sources_are_exact_booleans_and_unique_systemd_subset(self):
        cases = (
            ("[network]\nclaude=true\n", {"claude": True, "weather": False, "systemd": []}),
            ("[network]\nclaude=false\n", {"claude": False, "weather": False, "systemd": []}),
            ("[network]\nweather=true\n", {"claude": False, "weather": True, "systemd": []}),
            ("[network]\nclaude=false\nweather=false\n[system]\nsystemd=[]\n",
             {"claude": False, "weather": False, "systemd": []}),
            ("[system]\nsystemd=['user']\n", {"claude": False, "weather": False, "systemd": ["user"]}),
            ("[system]\nsystemd=['user','system']\n",
             {"claude": False, "weather": False, "systemd": ["user", "system"]}),
        )
        for sections, expected in cases:
            with self.subTest(sections=sections):
                policy = self.policy(sections=sections)
                self.assertEqual(policy.sources(), expected)
                self.assertEqual(policy.access()["weather"], expected["weather"])
                self.assertEqual(policy.access()["systemd"], expected["systemd"])
                self.assertNotIn("claude", policy.access())
        invalid = (
            "[network]\nclaude=1", "[network]\nclaude='true'", "[network]\nweather=0",
            "[network]\nweather=[]", "[network]\nclaude={}", "[network]\nurl='https://example.test'",
            "[network]\nother=false", "[system]\ncommands=[]", "[system]\nsystemd='system'",
            "[system]\nsystemd=['both']", "[system]\nsystemd=['SYSTEM']",
            "[system]\nsystemd=['system','system']", "[system]\nsystemd=['user',true]",
            "[system]\nsystemd=[[]]", "[system]\nsystemd=['system','user','system']",
            "[other]\nclaude=true", "[[network]]\nclaude=true", "[[system]]\nsystemd=[]",
        )
        for sections in invalid:
            with self.subTest(sections=sections):
                self.assertEqual(self.assert_code(lambda: self.policy(sections=sections)), "policy-invalid")

    def test_sources_access_are_detached_metadata_without_target_io(self):
        policy = self.policy(read=("~/project/*", "~/.ssh/known_hosts"),
                             sections="[network]\nweather=true\n[system]\nsystemd=['user']\n")
        with patch.object(p.os, "open", side_effect=AssertionError("target access")), \
                patch.object(p.os, "stat", side_effect=AssertionError("target access")):
            sources = policy.sources()
            sources["systemd"].append("system")
            sources["claude"] = True
            access = policy.access()
            self.assertEqual(access["files"], [{"id": "f0", "pattern": "~/project/*"},
                                               {"id": "f1", "pattern": "~/.ssh/known_hosts"}])
            access["files"][0]["pattern"] = "~/unrelated/**"
            access["files"].clear()
            access["systemd"].clear()
            access["weather"] = False
            self.assertEqual(policy.sources(), {"claude": False, "weather": True, "systemd": ["user"]})
            self.assertEqual(len(policy.access()["files"]), 2)
            self.assertEqual(policy.access()["files"][0]["pattern"], "~/project/*")
            self.assertEqual(policy.access()["systemd"], ["user"])
            self.assertTrue(policy.access()["weather"])

    def test_access_metadata_byte_limit_fails_closed_before_transfer(self):
        rules = tuple("~/project/" + str(i) + "-" + "ą" * 180 for i in range(24))
        self.assertLess(len(json.dumps(list(rules), ensure_ascii=False).encode("utf-8")), p.MAX_POLICY_BYTES)
        # Write UTF-8 directly so configuration remains below 16 KiB while
        # planner metadata exceeds 8 KiB after its IDs and JSON framing.
        data = "version=1\n[files]\nread=" + json.dumps(list(rules), ensure_ascii=False) + "\ndeny=[]\n"
        self.config.write_text(data, encoding="utf-8")
        self.config.chmod(0o600)
        self.assertEqual(self.assert_code(p.load_policy), "policy-invalid")
        policy = self.policy(read=rules[:12])
        encoded = json.dumps(policy.access(), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), p.MAX_ACCESS_BYTES)
        with patch.object(p, "MAX_ACCESS_BYTES", len(encoded)):
            self.assertEqual(len(policy.access()["files"]), 12)
        with patch.object(p, "MAX_ACCESS_BYTES", len(encoded) - 1):
            self.assertEqual(self.assert_code(policy.access), "policy-access-size")

    def test_source_changes_are_part_of_exact_policy_recheck(self):
        policy = self.policy(sections="[network]\nclaude=true\nweather=true\n")
        policy.recheck()
        self.config.write_bytes(self.config.read_bytes().replace(b"weather=true", b"weather=false"))
        self.assertEqual(self.assert_code(policy.recheck), "policy-changed")

    def test_deeply_nested_toml_refuses_without_parser_traceback(self):
        data = 'version=1\n[files]\nread=' + '[' * 1100 + '"SYNTHETIC_PRIVATE"' + ']' * 1100 + '\ndeny=[]\n'
        self.assertLess(len(data), p.MAX_POLICY_BYTES)
        self.config.write_text(data, encoding="utf-8")
        self.config.chmod(0o600)
        self.assertEqual(self.assert_code(p.load_policy), "policy-invalid")

    def test_deny_wins_over_exact_and_wildcard_read(self):
        policy = self.policy(read=("~/project/**", "~/project/denied.txt"),
                             deny=("~/project/denied.txt", "~/project/sub/**"))
        self.file("project/ok.txt")
        self.file("project/denied.txt")
        self.file("project/sub/secret.txt")
        self.assert_code(lambda: policy.select(("~/project/denied.txt",)))
        self.assert_code(lambda: policy.select(("~/project/sub/secret.txt",)))
        self.assertEqual(policy.expand((0,)), (str(self.project / "ok.txt"),))
        self.assertEqual(self.assert_code(lambda: policy.expand((0, 1))), "files-denied")

    def test_pseudofs_credentials_and_application_policy_cannot_be_allowed(self):
        paths = ("/proc/self/status", "/sys/kernel/a", "/dev/null", "/run/user/1000/a",
                 "/var/lib/ai-desktop/claude/a", "~/.config/ai-desktop/permissions.toml",
                 "~/.local/state/ai-desktop/claude/a", "~/.claude/config.json", "~/.gnupg/a",
                 "~/.aws/credentials", "~/.config/rclone/rclone.conf", "~/.local/share/keyrings/login.keyring",
                 "~/project/.env", "~/project/.env.local", "~/project/cert.key", "~/project/id_rsa",
                 "~/project/.git-credentials", "/home/someone-else/a")
        for path in paths:
            with self.subTest(path=path):
                self.assert_code(lambda: self.policy(read=(path,)))
        policy = self.policy(read=("~/**",))
        for path in paths:
            self.assert_code(lambda: policy.select((path,)))

    def test_ssh_requires_exact_allowed_filename_and_never_wildcards(self):
        for rule in ("~/.ssh/*", "~/.ssh/**", "~/.ssh/id_ed25519", "~/.ssh/other", "~/.ssh/nested/a.pub"):
            self.assert_code(lambda: self.policy(read=(rule,)))
        public = ("~/.ssh/config", "~/.ssh/known_hosts", "~/.ssh/authorized_keys", "~/.ssh/id_ed25519.pub")
        policy = self.policy(read=public)
        for path in public:
            self.file(path[2:], "synthetic public information\n")
        selected = policy.expand((0, 1, 2, 3))
        self.assertEqual(len(p.read_selected(policy, selected)["files"]), 4)
        wildcard = self.policy(read=("~/**",))
        for path in public:
            self.assert_code(lambda: wildcard.select((path,)))

    def test_policy_mode_owner_type_nlink_size_and_secure_parents(self):
        policy = self.policy()
        for mode in (0o644, 0o660, 0o400, 0o1600):
            self.config.chmod(mode)
            self.assert_code(lambda: p.load_policy(optional=True))
        self.config.chmod(0o600)
        link = self.home / "config-link"
        os.link(self.config, link)
        self.assert_code(p.load_policy)
        link.unlink()
        self.config.parent.chmod(0o770)
        self.assert_code(lambda: p.load_policy(optional=True))
        self.config.parent.chmod(0o700)
        self.config.write_bytes(b"x" * (p.MAX_POLICY_BYTES + 1))
        self.assert_code(p.load_policy)
        self.config.unlink()
        os.mkfifo(self.config, mode=0o600)
        self.assert_code(p.load_policy)
        self.config.unlink()
        self.policy().recheck()

    def test_symlinks_in_policy_or_any_parent_refuse_even_optional(self):
        self.policy()
        original = self.home / "original.toml"
        self.config.rename(original)
        self.config.symlink_to(original)
        self.assert_code(lambda: p.load_policy(optional=True))
        self.config.unlink()
        original.rename(self.config)
        directory = self.config.parent
        moved = self.home / "moved"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        self.assert_code(lambda: p.load_policy(optional=True))

    def test_policy_bytes_identity_and_metadata_are_rechecked(self):
        policy = self.policy()
        policy.recheck()
        data = self.config.read_bytes()
        self.config.write_bytes(data.replace(b"read", b"dead"))
        self.assert_code(policy.recheck)
        policy = self.policy()
        same = self.config.read_bytes()
        self.config.unlink()
        self.config.write_bytes(same)
        self.config.chmod(0o600)
        self.assert_code(policy.recheck)
        policy = self.policy()
        self.config.chmod(0o644)
        self.assert_code(policy.recheck)

    def test_regular_utf8_files_and_whole_request_limits(self):
        policy = self.policy()
        a = self.file(content="zażółć\n\ttekst")
        self.assertEqual(p.read_selected(policy, (str(a),)),
                         {"kind": "files", "files": [{"path": str(a), "content": "zażółć\n\ttekst"}]})
        a.write_bytes(b"a" * p.MAX_FILE_BYTES)
        self.assertEqual(len(p.read_selected(policy, (str(a),))["files"][0]["content"]), p.MAX_FILE_BYTES)
        a.write_bytes(b"a" * (p.MAX_FILE_BYTES + 1))
        self.assertEqual(self.assert_code(lambda: p.read_selected(policy, (str(a),))), "files-size")
        a.write_bytes(b"\xff")
        self.assert_code(lambda: p.read_selected(policy, (str(a),)))
        a.write_bytes(b"a\x1b[31m")
        self.assert_code(lambda: p.read_selected(policy, (str(a),)))
        files = tuple(str(self.file("project/f" + str(i), "a" * 43691)) for i in range(3))
        self.assertEqual(self.assert_code(lambda: p.read_selected(policy, files)), "files-total")
        self.assert_code(lambda: policy.select(tuple("~/project/" + str(i) for i in range(65))))

    def test_readonly_utf8_file_and_total_boundaries_keep_all_bytes(self):
        policy = self.policy()
        first = self.file("project/first.txt", "ą" * 32768)
        second = self.file("project/second.txt", "ż" * 16384)
        third = self.file("project/third.txt", "ź" * 16384)
        paths = (str(first), str(second), str(third))
        item = p.read_selected(policy, paths)
        self.assertEqual([entry["content"].encode("utf-8") for entry in item["files"]],
                         [first.read_bytes(), second.read_bytes(), third.read_bytes()])
        self.assertEqual(sum(len(entry["content"].encode("utf-8")) for entry in item["files"]), 131072)
        second.write_bytes(second.read_bytes() + b"x")
        self.assertEqual(self.assert_code(lambda: p.read_selected(policy, paths)), "files-total")
        first.write_bytes(first.read_bytes() + b"x")
        self.assertEqual(self.assert_code(lambda: p.read_selected(policy, paths)), "files-size")

    def test_size_and_metadata_refusals_are_distinct_before_content_read(self):
        path = self.file(content=b"a" * (p.MAX_FILE_BYTES + 1))
        with patch.object(p.os, "read", side_effect=AssertionError("content read")):
            self.assertEqual(self.assert_code(lambda: p._read(str(path), self.uid, p.MAX_FILE_BYTES)), "files-size")
            self.assertEqual(self.assert_code(lambda: p._read(str(path), self.uid + 1, p.MAX_FILE_BYTES)), "files-owner")
            link = self.project / "linked.txt"
            os.link(path, link)
            self.assertEqual(self.assert_code(lambda: p._read(str(path), self.uid, p.MAX_FILE_BYTES)), "files-links")
            link.unlink()
            path.chmod(0o4600)
            self.assertEqual(self.assert_code(lambda: p._read(str(path), self.uid, p.MAX_FILE_BYTES)), "files-mode")
            self.assertEqual(self.assert_code(lambda: p._read(str(self.project), self.uid, p.MAX_FILE_BYTES)), "files-type")
        self.policy()
        self.config.write_bytes(b"x" * (p.MAX_POLICY_BYTES + 1))
        with patch.object(p.os, "read", side_effect=AssertionError("content read")):
            self.assertEqual(self.assert_code(p.load_policy), "policy-size")

    def test_private_key_content_never_becomes_context(self):
        policy = self.policy()
        for header in ("-----BEGIN OPENSSH PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----",
                       "-----BEGIN ENCRYPTED PRIVATE KEY-----", "-----BEGIN PGP PRIVATE KEY BLOCK-----",
                       "-----begin private key-----", "PuTTY-User-Key-File-3: ssh-ed25519"):
            path = self.file(content=header + "\nSYNTHETIC NOT A KEY\n")
            self.assertEqual(self.assert_code(lambda: p.read_selected(policy, (str(path),))), "files-private-key")

    def test_target_symlinks_ancestor_links_hardlinks_fifo_and_foreign_owner_refuse(self):
        policy = self.policy(read=("~/project/**",))
        source = self.file("project/source.txt")
        target = self.project / "link.txt"
        target.symlink_to(source)
        self.assert_code(lambda: p.read_selected(policy, (str(target),)))
        target.unlink()
        os.link(source, target)
        self.assert_code(lambda: p.read_selected(policy, (str(source),)))
        target.unlink()
        os.mkfifo(target, mode=0o600)
        self.assert_code(lambda: p.read_selected(policy, (str(target),)))
        target.unlink()
        folder = self.project / "folder"
        folder.symlink_to(self.project, target_is_directory=True)
        self.assert_code(lambda: p.read_selected(policy, (str(folder / "source.txt"),)))
        # Metadata fixture: the file owner must match even when readable.
        real_fstat = os.fstat
        def foreign(fd):
            actual = real_fstat(fd)
            if stat.S_ISREG(actual.st_mode) and actual.st_ino == source.stat().st_ino:
                fields = list(actual)
                fields[4] = self.uid + 1
                return os.stat_result(fields)
            return actual
        with patch.object(p.os, "fstat", side_effect=foreign):
            self.assert_code(lambda: p.read_selected(policy, (str(source),)))

    def test_target_or_policy_mutation_during_read_refuses_without_partial_context(self):
        path = self.file()
        policy = self.policy()
        original_read = os.read
        mutated = []
        def changing_target(fd, count):
            data = original_read(fd, count)
            if os.fstat(fd).st_ino == path.stat().st_ino and not mutated:
                mutated.append(True)
                path.write_text("changed content", encoding="utf-8")
            return data
        with patch.object(p.os, "read", side_effect=changing_target):
            self.assert_code(lambda: p.read_selected(policy, (str(path),)))
        mutated.clear()
        def changing_policy(fd, count):
            data = original_read(fd, count)
            if os.fstat(fd).st_ino == path.stat().st_ino and not mutated:
                mutated.append(True)
                self.config.write_text("invalid after read", encoding="utf-8")
            return data
        with patch.object(p.os, "read", side_effect=changing_policy):
            self.assert_code(lambda: p.read_selected(policy, (str(path),)))

    def test_expand_children_tree_indices_and_empty_scope(self):
        a = self.file("project/a.txt")
        b = self.file("project/dir/b.txt")
        policy = self.policy(read=("~/project/*", "~/project/**"))
        self.assertEqual(policy.expand((0,)), (str(a),))
        self.assertEqual(policy.expand((1,)), tuple(sorted((str(a), str(b)))))
        for indices in ((), (True,), (0, 0), (-1,), (2,), ("0",), {0}):
            self.assert_code(lambda: policy.expand(indices))
        self.assert_code(lambda: self.policy(read=("~/nothing/*",)).expand((0,)))

    def test_expand_never_silently_truncates_file_entry_or_depth_limits(self):
        policy = self.policy()
        for i in range(64):
            self.file("project/" + str(i))
        selected = policy.expand((0,))
        self.assertEqual(len(selected), 64)
        self.assertEqual(len(p.read_selected(policy, selected)["files"]), 64)
        self.file("project/64")
        self.assertEqual(self.assert_code(lambda: policy.expand((0,))), "files-count")
        for file in self.project.iterdir():
            file.unlink()
        for i in range(p.MAX_DIRECTORY_ENTRIES + 1):
            (self.project / str(i)).mkdir()
        self.assertEqual(self.assert_code(lambda: policy.expand((0,))), "files-entries")
        for folder in self.project.iterdir():
            folder.rmdir()
        self.file("project/" + "sub/" * (p.MAX_DEPTH + 1) + "leaf.txt")
        policy = self.policy(read=("~/project/**",))
        self.assertEqual(self.assert_code(lambda: policy.expand((0,))), "files-depth")

    def test_denied_subtree_boundary_is_never_opened_or_enumerated(self):
        allowed = self.file()
        private = self.project / "private"
        private.mkdir()
        for index in range(p.MAX_DIRECTORY_ENTRIES + 1):
            (private / str(index)).write_text("synthetic excluded", encoding="utf-8")
        policy = self.policy(read=("~/project/**", "~/project/private/*"),
                             deny=("~/project/private/**",))
        original_open, original_scandir = os.open, os.scandir
        private_inode = private.stat().st_ino
        def guarded_open(path, *args, **kwargs):
            self.assertNotEqual(path, "private", "denied directory was opened")
            return original_open(path, *args, **kwargs)
        def guarded_scandir(fd):
            self.assertNotEqual(os.fstat(fd).st_ino, private_inode, "denied directory was scanned")
            return original_scandir(fd)
        with patch.object(p.os, "open", side_effect=guarded_open), \
                patch.object(p.os, "scandir", side_effect=guarded_scandir):
            self.assertEqual(policy.expand((0,)), (str(allowed),))
            self.assertEqual(self.assert_code(lambda: policy.expand((1,))), "files-empty")

    def test_task_journal_and_stages_never_enter_recursive_reads_or_budgets(self):
        expected = tuple(sorted(str(self.file("project/file" + str(index), "public")) for index in range(64)))
        private = self.project / ".ai-desktop-tasks"
        private.mkdir(mode=0o700)
        # Durable journals contain original private bytes. Neither their size
        # nor count may consume the selected public-file or enumeration budget.
        self.file("project/.ai-desktop-tasks/plan.json", "SYNTHETIC OLD CONTENT" * 10000)
        for index in range(p.MAX_DIRECTORY_ENTRIES + 1):
            self.file("project/.ai-desktop-tasks/history" + str(index))
        self.file("project/.ai-task-stage", "SYNTHETIC TEMPORARY CONTENT" * 10000)
        self.file("project/.ai-task-hidden/leaf", "SYNTHETIC TEMPORARY CONTENT")
        policy = self.policy(read=["~/project/**"])
        original_open, original_scandir = os.open, os.scandir
        def guarded_open(path, *args, **kwargs):
            self.assertFalse(path == ".ai-desktop-tasks" or str(path).startswith(".ai-task-"),
                             "private task storage was opened")
            return original_open(path, *args, **kwargs)
        def guarded_scandir(fd):
            self.assertNotEqual(os.fstat(fd).st_ino, private.stat().st_ino,
                                "private task journal directory was enumerated")
            return original_scandir(fd)
        with patch.object(p.os, "open", side_effect=guarded_open), \
                patch.object(p.os, "scandir", side_effect=guarded_scandir):
            paths = policy.expand((0,))
            self.assertEqual(paths, expected)
            self.assertEqual([entry["content"] for entry in p.read_selected(policy, paths)["files"]],
                             ["public"] * 64)
        for path in ("~/project/.ai-desktop-tasks/plan.json", "~/project/.ai-task-stage",
                     "~/project/.ai-task-hidden/leaf"):
            self.assertEqual(self.assert_code(lambda: policy.select([path])), "files-denied")
            self.assertEqual(self.assert_code(lambda: self.policy(read=[path])), "policy-invalid")

    def test_total_enumeration_budget_counts_entries_across_directories(self):
        self.file()
        for group in range(5):
            parent = self.project / ("group" + str(group))
            parent.mkdir()
            for index in range(900):
                (parent / str(index)).mkdir()
        policy = self.policy(read=("~/project/**",))
        self.assertEqual(self.assert_code(lambda: policy.expand((0,))), "files-entries")

    def test_ancestor_replacement_during_content_read_is_detected(self):
        path = self.file()
        policy = self.policy()
        original_read = os.read
        inode = path.stat().st_ino
        changed = []
        def read_and_replace(fd, count):
            raw = original_read(fd, count)
            if os.fstat(fd).st_ino == inode and not changed:
                changed.append(True)
                self.project.rename(self.home / "old-project")
                self.project.mkdir()
                self.file(content="replacement")
            return raw
        with patch.object(p.os, "read", side_effect=read_and_replace):
            self.assertEqual(self.assert_code(lambda: p.read_selected(policy, (str(path),))), "files-changed")

    def test_v2_directory_aliases_require_explicit_version_and_preserve_policy_bytes(self):
        for alias in ("~/", "~/project/", str(self.project) + "/"):
            with self.subTest(alias=alias):
                self.assertEqual(self.assert_code(lambda: self.policy(read=[alias])), "policy-invalid")
                policy = self.policy(read=[alias], write=[alias], version=2)
                self.assertEqual(policy._version, 2)
                self.assertEqual(policy.scopes()[0].mode, "tree")
                self.assertEqual(policy.scopes()[0].pattern, alias)
                before = self.config.read_bytes()
                policy.recheck()
                self.assertEqual(self.config.read_bytes(), before)
        for rules in (["~/", "~/**"], ["~/project/", "~/project/**"]):
            self.assertEqual(self.assert_code(lambda: self.policy(read=rules, version=2)), "policy-invalid")
        self.assertEqual(self.assert_code(lambda: self.policy(read=["/"], version=2)), "policy-invalid")
        self.assertEqual(self.policy()._version, 1)

    def test_v2_owner_rules_allow_names_and_text_without_hidden_secret_filters(self):
        names = (".ssh/id_ed25519", "secrets/key.pem", "backups/.env", ".local/credentials",
                 ".git/config", ".config/ai-desktop/extra.txt", ".claude/data.json")
        content = "-----BEGIN OPENSSH PRIVATE KEY-----\nSYNTHETIC NOT A KEY\n"
        paths = tuple(str(self.file(name, content)) for name in names)
        policy = self.policy(read=["~/"], write=["~/"], version=2)
        self.assertEqual(policy.write_workspace(0), str(self.home))
        selected = policy.expand_selected([(0, name) for name in names])
        self.assertEqual(selected, tuple(sorted(paths)))
        self.assertEqual(policy.require_write(paths), paths)
        self.assertEqual([item["content"] for item in p.read_selected(policy, selected)["files"]],
                         [content] * len(paths))
        # The legacy path keeps its envelope check; v2 is never an implicit migration.
        legacy = self.policy(read=["~/project/*"])
        target = self.file(content=content)
        self.assertEqual(self.assert_code(lambda: p.read_selected(legacy, [str(target)])), "files-private-key")

    def test_v2_read_rules_are_owner_checked_not_filtered_by_location_names(self):
        policy = self.policy(read=["/home/another-owner/shared.txt", "/run/user/fixture.txt"], version=2)
        with patch.object(p.os, "open", side_effect=AssertionError("metadata selection performed I/O")):
            self.assertEqual(policy.select(["/home/another-owner/shared.txt"]),
                             ("/home/another-owner/shared.txt",))
            self.assertEqual(policy.select(["/run/user/fixture.txt"]), ("/run/user/fixture.txt",))

    def test_v2_deny_aliases_win_before_exact_target_io_or_directory_enumeration(self):
        allowed = self.file("project/ok.txt")
        private = self.file("project/private/secret.txt")
        self.file("project/.ssh/id_ed25519")
        policy = self.policy(read=["~/project/"], write=["~/project/"],
                             deny=["~/project/private/", "~/project/.ssh/"], version=2)
        with patch.object(p.os, "open", side_effect=AssertionError("denied selection accessed target")), \
                patch.object(p.os, "scandir", side_effect=AssertionError("denied selection enumerated")):
            self.assertEqual(self.assert_code(lambda: policy.expand_selected([(0, "private/secret.txt")])),
                             "files-denied")
            self.assertFalse(policy.can_write(str(private)))
            self.assertFalse(policy.can_write(str(private.parent)))
        original_open = p._open_directory
        def allowed_directories(path, *args, **kwargs):
            self.assertFalse(p._under(path, str(private.parent)))
            self.assertFalse(p._under(path, str(self.project / ".ssh")))
            return original_open(path, *args, **kwargs)
        with patch.object(p, "_open_directory", side_effect=allowed_directories):
            self.assertEqual(policy.expand((0,)), (str(allowed),))
            self.assertEqual(self.assert_code(lambda: policy.expand_selected([(0, "private/**")])),
                             "files-empty")

    def test_selected_exact_from_home_does_not_scan_unselected_files_or_consume_budget(self):
        for index in range(p.MAX_FILES + 16):
            self.file("noise/" + str(index))
        target = self.file("project/selected.txt")
        policy = self.policy(read=["~/"], version=2)
        with patch.object(p.os, "scandir", side_effect=AssertionError("unneeded enumeration")):
            self.assertEqual(policy.expand_selected([(0, "project/selected.txt")]), (str(target),))
            self.assertEqual(p.read_selected(policy, [str(target)])["files"][0]["content"], "synthetic text\n")

    def test_selected_relative_subtrees_are_contained_in_the_named_scope(self):
        one = self.file("project/top.txt")
        two = self.file("project/sub/a.txt")
        three = self.file("project/sub/deeper/b.txt")
        policy = self.policy(read=["~/project/", "~/project/*", "~/project/sub/**", "~/project/top.txt"], version=2)
        self.assertEqual(policy.expand_selected([(0, "sub/*")]), (str(two),))
        self.assertEqual(policy.expand_selected([(0, "sub/**")]), tuple(sorted((str(two), str(three)))))
        self.assertEqual(policy.expand_selected([(1, "top.txt"), (2, None)]),
                         tuple(sorted((str(one), str(two), str(three)))))
        self.assertEqual(policy.expand_selected([(3, "")]), (str(one),))
        for selections in ([(1, "sub/a.txt")], [(1, "sub/*")], [(1, "sub/**")], [(3, "anything")],
                           [(0, "../outside.txt")], [(0, "/etc/a")], [(0, "sub/../top.txt")],
                           [(0, "sub//a.txt")], [(0, "sub/")], [(0, "*")], [(0, "**")],
                           [(0, "sub/a*")], [(0, "sub/**/a.txt")], [(0, "$HOME/a")],
                           [(0, "~/.ssh/config")], [(0, "sub/\u202ename")], [(0, "sub/\ud800")]):
            with self.subTest(selections=repr(selections)), \
                    patch.object(p.os, "scandir", side_effect=AssertionError("invalid selector enumerated")):
                self.assert_code(lambda: policy.expand_selected(selections))

    def test_all_selected_boundaries_are_checked_before_first_enumeration(self):
        policy = self.policy(read=["~/"], deny=["~/private/"], version=2)
        with patch.object(p.os, "scandir", side_effect=AssertionError("earlier source enumerated")), \
                patch.object(p.os, "open", side_effect=AssertionError("earlier source opened")):
            self.assertEqual(self.assert_code(lambda: policy.expand_selected([(0, "project/**"),
                                                                             (0, "private/key")])),
                             "files-denied")
        for selections in ([], {}, [(True, None)], [(-1, None)], [(1, None)], [("0", None)],
                           [(0, None, "extra")], [0], [(0, [])], [(0, False)],
                           [(0, None), (0, "")], [(0, "project/a"), (0, "project/a")]):
            with self.subTest(selections=selections):
                self.assert_code(lambda: policy.expand_selected(selections))

    def test_v2_recovery_storage_remains_reserved_even_with_empty_deny(self):
        target = self.file("project/ok.txt")
        self.file("project/.ai-desktop-tasks/plan.json")
        self.file("project/.ai-task-stage")
        self.file("project/.ai-task-directory/hidden")
        policy = self.policy(read=["~/project/"], write=["~/"], version=2)
        self.assertEqual(policy.expand((0,)), (str(target),))
        for path in ("project/.ai-desktop-tasks/plan.json", "project/.ai-task-stage",
                     "project/.ai-task-directory/hidden"):
            with self.subTest(path=path):
                self.assertFalse(policy.can_write("~/" + path))
                self.assertEqual(self.assert_code(lambda: policy.select(["~/" + path])), "files-denied")
                self.assertEqual(self.assert_code(lambda: self.policy(read=["~/" + path], version=2)),
                                 "policy-invalid")

    def test_managed_write_boundaries_keep_root_ancestors_foreign_home_and_recovery_out(self):
        home = str(self.home)
        for path in (home, home + "/.ssh", home + "/secrets", home + "/backups/.env", "/opt/owned-project"):
            self.assertEqual(p.validate_write_path(path, home, managed=True), path)
        for path in ("/", str(self.home.parent), "/home/someone-else/a", "/root/another-user/a",
                     home + "/.ai-desktop-tasks/a", home + "/.ai-task-stage"):
            with self.subTest(path=path):
                self.assert_code(lambda: p.validate_write_path(path, home, managed=True))
        self.assert_code(lambda: p.validate_write_path(home, home))
        self.assert_code(lambda: p.validate_write_path(home, home, managed=1))

    def test_v2_keeps_file_type_owner_link_text_and_policy_revocation_guards(self):
        target = self.file("project/selected.txt")
        policy = self.policy(read=["~/"], write=["~/"], version=2)
        link = self.project / "link"
        link.symlink_to(target)
        self.assert_code(lambda: p.read_selected(policy, [str(link)]))
        link.unlink()
        os.link(target, link)
        self.assertEqual(self.assert_code(lambda: p.read_selected(policy, [str(target)])), "files-links")
        link.unlink()
        os.mkfifo(link, mode=0o600)
        self.assertEqual(self.assert_code(lambda: p.read_selected(policy, [str(link)])), "files-type")
        link.unlink()
        real_fstat = os.fstat
        inode = target.stat().st_ino
        def foreign_owner(fd):
            actual = real_fstat(fd)
            if stat.S_ISREG(actual.st_mode) and actual.st_ino == inode:
                fields = list(actual)
                fields[4] = self.uid + 1
                return os.stat_result(fields)
            return actual
        with patch.object(p.os, "fstat", side_effect=foreign_owner):
            self.assertEqual(self.assert_code(lambda: p.read_selected(policy, [str(target)])), "files-owner")
        target.write_bytes(b"not text\xff")
        self.assertEqual(self.assert_code(lambda: p.read_selected(policy, [str(target)])), "files-text")
        with patch.object(p, "_identity", return_value=(self.uid + 1, str(self.home))):
            self.assertEqual(self.assert_code(lambda: p.read_selected(policy, [str(target)])), "policy-changed")
        self.config.write_bytes(self.config.read_bytes().replace(b'version = 2', b'version = 1'))
        self.assertEqual(self.assert_code(lambda: policy.expand_selected([(0, "project/selected.txt")])),
                         "policy-changed")

    def test_example_opts_into_v2_without_granting_files_and_exposes_editable_deny(self):
        example = Path(__file__).resolve().parents[1] / "config/permissions.example.toml"
        self.config.write_bytes(example.read_bytes())
        self.config.chmod(0o600)
        policy = p.load_policy()
        self.assertEqual(policy._version, 2)
        self.assertEqual(policy.scopes(), ())
        self.assertEqual(policy.write_scopes(), ())
        self.assertTrue(policy._excluded(str(self.home / ".ssh/id_ed25519")))
        self.assertTrue(policy._excluded(str(self.config)))
        # These exclusions are configuration values, so an explicit v2 edit
        # can remove them without any hidden filename/content backstop.
        policy = self.policy(read=["~/"], write=["~/"], deny=[], version=2)
        self.assertTrue(policy.can_write(str(self.config)))
        self.assertEqual(policy.select([str(self.config)]), (str(self.config),))

    def test_expand_symlink_and_special_file_refuse_but_denied_entries_not_opened(self):
        self.file()
        link = self.project / "link"
        link.symlink_to("/proc/self/environ")
        policy = self.policy()
        self.assertEqual(self.assert_code(lambda: policy.expand((0,))), "files-symlink")
        policy = self.policy(deny=("~/project/link",))
        self.assertEqual(policy.expand((0,)), (str(self.project / "a.txt"),))
        link.unlink()
        os.mkfifo(link, mode=0o600)
        policy = self.policy()
        self.assertEqual(self.assert_code(lambda: policy.expand((0,))), "files-type")


class IdentityTests(unittest.TestCase):
    def test_passwd_home_not_environment_and_privileged_identity_refused(self):
        with patch.object(p.os, "getuid", return_value=1000), \
                patch.object(p.os, "geteuid", return_value=1000), \
                patch.object(p.os, "getgid", return_value=1000), \
                patch.object(p.os, "getegid", return_value=1000), \
                patch.object(p.pwd, "getpwuid", return_value=type("PW", (), {"pw_dir": "/home/synthetic"})()), \
                patch.dict(os.environ, {"HOME": "/tmp/evil", "XDG_CONFIG_HOME": "/tmp/evil"}):
            self.assertEqual(p._identity(), (1000, "/home/synthetic"))
            with patch.object(p.os, "geteuid", return_value=0):
                with self.assertRaises(ValueError):
                    p._identity()
            with patch.object(p.os, "getegid", return_value=0):
                with self.assertRaises(ValueError):
                    p._identity()
        with patch.object(p.os, "getuid", return_value=0):
            with self.assertRaises(ValueError):
                p.load_policy(optional=True)


if __name__ == "__main__":
    unittest.main()
