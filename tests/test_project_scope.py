"""LC4 slice 1: read-only project scope on synthetic trees (no model, no network)."""
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import permissions as p
from ai_desktop import project_scope as ps


class Base(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.geteuid(), 0)
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ai-project-", dir=real_home)))
        self.enterContext(patch.object(p, "_identity", return_value=(os.getuid(), str(self.home))))
        self.config = self.home / ".config/ai-desktop/permissions.toml"
        self.config.parent.mkdir(parents=True, mode=0o700)
        self.proj = self.home / "proj"
        self.proj.mkdir(mode=0o700)
        self.policy_text('["~/proj/**"]', '["~/proj/secret/**"]')

    def policy_text(self, read, deny="[]"):
        self.config.write_text("version = 1\n[files]\nread = %s\ndeny = %s\n[network]\nclaude = true\n" % (read, deny))
        self.config.chmod(0o600)

    def project(self, root=None):
        return ps.Project(p.load_policy(), str(root or self.proj))

    def put(self, relative, text="x\n"):
        target = self.proj / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target


class ScanTests(Base):
    def test_ten_thousand_files_are_indexed_deterministically_and_pages_cover_each_once(self):
        for d in range(100):
            for f in range(100):
                self.put("pkg%03d/mod%03d.py" % (d, f), "v = %d\n" % f)
        project = self.project().scan()
        self.assertTrue(project.complete)
        self.assertEqual(project.stats["indexed"], 10000)
        again = [e.path for e in self.project().scan().files]
        self.assertEqual(again, [e.path for e in project.files])
        seen, cursor, pages = [], 0, 0
        while cursor is not None:
            items, cursor, total = project.listing(cursor=cursor)
            seen += [e.path for e in items]
            pages += 1
        self.assertEqual(len(seen), 10000)
        self.assertEqual(len(set(seen)), 10000)
        self.assertEqual(total, 10000)
        self.assertEqual(pages, 100)

    def test_denied_names_never_appear_anywhere_only_a_count(self):
        self.put("src/app.py")
        self.put("secret/TOP-SECRET-NAME.txt", "hidden\n")
        project = self.project().scan()
        self.assertEqual([e.path for e in project.files], ["src/app.py"])
        self.assertGreaterEqual(project.stats["denied"], 1)
        names = [e.path for e in project.listing()[0]] + [e.path for e in project.find_names("SECRET")[0]]
        self.assertFalse(any("secret" in n.lower() for n in names))
        self.assertEqual(project.search_text("hidden").hits, ())
        with self.assertRaises(ps.ProjectError) as caught:
            project.read_file("secret/TOP-SECRET-NAME.txt")
        self.assertEqual(caught.exception.code, "PROJECT_PATH")

    def test_a_denied_directory_is_never_opened_or_enumerated(self):
        self.put("src/app.py")
        self.put("secret/inner/deep.txt", "hidden\n")
        opened = []
        real = p.open_directory
        def spy(path, uid, **kwargs):
            opened.append(path)
            return real(path, uid, **kwargs)
        with patch.object(p, "open_directory", spy):
            self.project().scan()
        self.assertIn(str(self.proj), opened)
        self.assertFalse([o for o in opened if "/secret" in o])

    def test_links_special_files_hard_links_and_git_are_skipped_and_counted(self):
        self.put("real.txt", "real\n")
        outside = self.home / "outside.txt"
        outside.write_text("LEAK\n")
        os.symlink(outside, self.proj / "link.txt")
        os.symlink(self.home, self.proj / "dirlink")
        os.mkfifo(self.proj / "pipe")
        os.link(self.proj / "real.txt", self.proj / "hard.txt")
        self.put(".git/config", "url = https://token@example\n")
        self.put("ok/fine.txt", "fine\n")
        project = self.project().scan()
        self.assertEqual([e.path for e in project.files], ["ok/fine.txt"])
        self.assertEqual(project.stats["links"], 2)
        self.assertEqual(project.stats["special"], 1)
        self.assertEqual(project.stats["unsafe"], 2)       # both names of the hard-linked file
        self.assertEqual(project.stats["vcs"], 1)
        self.assertEqual(project.search_text("LEAK").hits, ())
        self.assertEqual(project.search_text("token").hits, ())

    def test_limits_end_the_scan_incomplete_with_a_reason(self):
        for i in range(60):
            self.put("f%03d.txt" % i)
        with patch.object(ps, "MAX_FILES", 50):
            project = self.project().scan()
        self.assertFalse(project.complete)
        self.assertEqual(project.reason, "files")
        self.assertEqual(project.stats["indexed"], 50)
        with patch.object(ps, "MAX_DEPTH", 2):
            self.put("a/b/c/d.txt")
            deep = self.project().scan()
        self.assertEqual(deep.reason, "depth")

    def test_one_oversized_or_unreadable_directory_does_not_hide_its_siblings(self):
        for i in range(8):
            self.put("aaa/f%d.txt" % i)
        self.put("zzz/keep.txt")
        self.put("locked/inner.txt")
        (self.proj / "locked").chmod(0)
        try:
            with patch.object(ps, "MAX_DIR_ENTRIES", 5):
                project = self.project().scan()
        finally:
            (self.proj / "locked").chmod(0o700)
        self.assertIn("zzz/keep.txt", [e.path for e in project.files])      # scanned after the skipped directories
        self.assertFalse(project.complete)
        self.assertEqual(project.reason, "entries")
        locked = self.project()
        (self.proj / "locked").chmod(0)
        try:
            locked.scan()
        finally:
            (self.proj / "locked").chmod(0o700)
        self.assertFalse(locked.complete)
        self.assertEqual(locked.reason, "unreadable")

    def test_a_same_size_edit_after_the_scan_is_reported_as_changed(self):
        target = self.put("a.txt", "one\n")
        project = self.project().scan()
        target.write_text("two\n")
        self.assertTrue(project.read_file("a.txt").changed)

    def test_root_must_be_inside_the_read_policy_and_a_real_directory(self):
        (self.home / "other").mkdir()
        for root in (self.home / "other", self.home, "/", self.home / "proj/../other"):
            with self.subTest(root=str(root)):
                with self.assertRaises(ps.ProjectError) as caught:
                    self.project(root)
                self.assertEqual(caught.exception.code, "PROJECT_ROOT")
        with self.assertRaises(ps.ProjectError):                 # a link outside the policy is not a root
            self.project(self.home / "alias")
        (self.proj / "real").mkdir()
        os.symlink(self.proj / "real", self.proj / "inner")      # a link inside the policy still is not followed
        with self.assertRaises(ps.ProjectError) as caught:
            self.project(self.proj / "inner").scan()
        self.assertEqual(caught.exception.code, "PROJECT_ROOT")
        self.policy_text('["~/proj/secret/**"]')
        with self.assertRaises(ps.ProjectError):
            self.project()

    def test_a_changed_policy_stops_the_scan(self):
        self.put("a.txt")
        project = self.project()
        self.policy_text('["~/proj/**"]', '["~/proj/other/**"]')
        with self.assertRaises(ValueError):
            project.scan()


class ReadTests(Base):
    def test_a_large_file_is_read_as_a_pinned_material_with_line_ranges(self):
        lines = ["line %07d some filler text to make it longer\n" % n for n in range(110000)]   # ~5 MiB
        self.put("big/data.log", "".join(lines))
        project = self.project().scan()
        loaded = project.read_file("big/data.log")
        self.assertGreater(loaded.material.size, 5 * 1024 * 1024)
        self.assertFalse(loaded.changed)
        start, end = materials_line_range(loaded.material, 50001, 50003)
        self.assertEqual(loaded.material.raw[start:end].decode().splitlines()[0], "line 0050000 some filler text to make it longer")
        self.assertEqual(len(loaded.material.sha256), 64)

    def test_over_sixteen_mib_text_and_binary_are_refused(self):
        self.put("huge.txt", "a" * (ps.FILE_READ_BYTES + 1))
        (self.proj / "bin.dat").write_bytes(b"\x00\x01\x02")
        project = self.project().scan()
        for path in ("huge.txt", "bin.dat"):
            with self.subTest(path=path), self.assertRaises(ps.ProjectError) as caught:
                project.read_file(path)
            self.assertEqual(caught.exception.code, "PROJECT_FILE")

    def test_unknown_and_traversal_paths_are_refused(self):
        self.put("a.txt")
        project = self.project().scan()
        for path in ("nope.txt", "../x", "/etc/passwd", "a.txt/", "a//b", "./a.txt", "", "\x00"):
            with self.subTest(path=path), self.assertRaises(ps.ProjectError) as caught:
                project.read_file(path)
            self.assertEqual(caught.exception.code, "PROJECT_PATH")

    def test_a_file_changed_after_the_scan_is_detected_and_version_compare_fails(self):
        target = self.put("a.txt", "one\n")
        project = self.project().scan()
        version = project.read_file("a.txt").material.sha256
        self.assertTrue(project.unchanged("a.txt", version))
        target.write_text("one\ntwo\n")
        self.assertTrue(project.read_file("a.txt").changed)
        self.assertFalse(project.unchanged("a.txt", version))
        target.unlink()
        self.assertFalse(project.unchanged("a.txt", version))

    def test_a_file_replaced_by_a_link_after_the_scan_is_not_followed(self):
        target = self.put("a.txt", "one\n")
        project = self.project().scan()
        target.unlink()
        os.symlink(self.home / "proj/../outside.txt", target)
        (self.home / "outside.txt").write_text("LEAK\n")
        with self.assertRaises(ps.ProjectError):
            project.read_file("a.txt")


class MapTests(Base):
    def test_children_returns_one_level_with_aggregates_directories_first(self):
        self.put("README.md", "x\n")
        self.put("src/a/one.py", "abc\n")
        self.put("src/a/two.py", "de\n")
        self.put("src/b.py", "f\n")
        project = self.project().scan()
        items, following, total = project.children("")
        self.assertEqual([(e["name"], e["dir"], e["files"]) for e in items], [("src", True, 3), ("README.md", False, 1)])
        self.assertEqual(next(e for e in items if e["name"] == "src")["size"], 4 + 3 + 2)
        items, _, total = project.children("src")
        self.assertEqual([(e["name"], e["dir"], e["files"]) for e in items], [("a", True, 2), ("b.py", False, 1)])
        self.assertEqual((project.children("nope")[0], project.children("nope")[2]), ([], 0))      # an unknown prefix leaks nothing
        for bad in ("..", "/etc", "a//b"):
            with self.assertRaises(ps.ProjectError):
                project.children(bad)

    def test_overview_summarises_a_large_project_without_listing_it(self):
        for d in range(60):
            for f in range(200):
                self.put("pkg%02d/m%03d.go" % (d, f), "x\n")
        self.put("go.mod", "module x\n")
        self.put("docs/README.md", "x\n")
        self.put("secret/hidden.txt", "no\n")
        project = self.project().scan()
        view = project.overview()
        self.assertEqual(view["files"], 12002)
        self.assertEqual(len(view["top"]), ps.OVERVIEW_TOP)
        self.assertEqual(view["extensions"][0], {"ext": ".go", "files": 12000})
        self.assertEqual(view["key_files"], ["go.mod", "docs/README.md"])
        self.assertNotIn("secret", str(view))
        import json
        self.assertLess(len(json.dumps(view)), 6000)                                   # compact however large the project is


class SearchTests(Base):
    def test_search_spans_files_and_resumes_from_the_cursor(self):
        for i in range(30):
            self.put("m%02d.txt" % i, "alpha\nNEEDLE %d\nomega\n" % i)
        project = self.project().scan()
        found, cursor, calls = [], (0, 0), 0
        while cursor is not None:
            result = project.search_text("NEEDLE", cursor)
            found += [(h.path, h.line) for h in result.hits]
            cursor, calls = result.next_cursor, calls + 1
        self.assertEqual(len(found), 30)
        self.assertEqual(len(set(found)), 30)
        self.assertEqual(calls, 2)
        self.assertTrue(result.complete)

    def test_two_hits_in_one_file_are_not_lost_at_a_page_boundary(self):
        self.put("one.txt", "".join("NEEDLE row %d\n" % i for i in range(45)))
        project = self.project().scan()
        total, cursor = 0, (0, 0)
        while cursor is not None:
            result = project.search_text("NEEDLE", cursor)
            total += len(result.hits)
            cursor = result.next_cursor
        self.assertEqual(total, 45)

    def test_binary_large_and_unreadable_files_are_counted_not_searched(self):
        self.put("t.txt", "NEEDLE\n")
        (self.proj / "b.bin").write_bytes(b"NEEDLE\x00\x01")
        self.put("big.txt", "x" * (ps.SEARCH_FILE_BYTES + 1) + "NEEDLE")
        project = self.project().scan()
        result = project.search_text("NEEDLE")
        self.assertEqual([h.path for h in result.hits], ["t.txt"])
        self.assertEqual(result.skipped["large"], 1)
        self.assertEqual(result.skipped["binary"], 1)
        self.assertTrue(result.complete)

    def test_budget_exhaustion_returns_a_cursor_not_a_complete_answer(self):
        for i in range(5):
            self.put("f%d.txt" % i, "x" * 1000 + "\n")
        project = self.project().scan()
        with patch.object(ps, "SEARCH_BUDGET_BYTES", 2500):
            result = project.search_text("NEEDLE")
        self.assertFalse(result.complete)
        self.assertIsNotNone(result.next_cursor)
        rest = project.search_text("NEEDLE", result.next_cursor)
        self.assertEqual(rest.hits, ())

    def test_bad_needles_and_cursors_are_refused(self):
        self.put("a.txt")
        project = self.project().scan()
        for needle in ("", "x" * 300, "bad\x1b"):
            with self.subTest(needle=needle), self.assertRaises(ps.ProjectError):
                project.search_text(needle)
        for cursor in ((-1, 0), (0, -1), (99, 0), "x", (0,)):
            with self.subTest(cursor=cursor), self.assertRaises(ps.ProjectError):
                project.search_text("x", cursor)


def materials_line_range(material, first, last):
    from ai_desktop import materials
    return materials.line_range(material, first, last)


if __name__ == "__main__":
    unittest.main()
