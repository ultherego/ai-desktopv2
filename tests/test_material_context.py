"""LC1 contracts: material context item, transport symmetry, line ranges, policy-bound file read."""
import copy
import json
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import analysis_contract as ac
from ai_desktop import material_context as mc
from ai_desktop import materials as m
from ai_desktop import permissions as p


def make_item(raw=b"alpha\nbeta\ngamma\n" * 40, start=0, end=60):
    material = m.import_bytes(raw, "file")
    fragment = m.read_range(material, material.sha256, start, end)
    item = {"kind": "material", "purpose": "why does it fail",
            "material": {"id": material.material_id, "version": material.sha256, "size": material.size},
            "fragments": [{"ref": fragment.ref.to_json(), "text": fragment.text}]}
    return material, item


class ContextItemTests(unittest.TestCase):
    def test_valid_item_passes_and_is_accepted_by_the_transport_contract(self):
        _, item = make_item()
        self.assertIs(mc.validate_context(item), item)
        payload = ac.application_input(None, "Why?", context=[item])
        value = json.loads(payload)
        self.assertEqual((value["mode"], value["context"][0]["kind"]), ("question", "material"))
        self.assertIn("A material context", value["system_prompt"])

    def test_worker_side_parse_accepts_exactly_the_same_item(self):
        _, item = make_item()
        raw = ac.application_input(None, "Why?", context=[item]).encode("utf-8")
        model_input, _ = ac.parse_request(raw)
        self.assertEqual(json.loads(model_input)["context"][0]["material"], item["material"])

    def test_tampering_is_refused_on_both_sides(self):
        material, item = make_item()
        cases = {}
        for name, mutate in {
            "text-changed": lambda i: i["fragments"][0].update(text=i["fragments"][0]["text"].replace("alpha", "OMEGA")),
            "hash-changed": lambda i: i["fragments"][0]["ref"].update(fragment="0" * 64),
            "range-longer-than-text": lambda i: i["fragments"][0]["ref"].update(end=61),
            "other-material": lambda i: i["material"].update(id="0" * 16),
            "other-version": lambda i: i["material"].update(version="1" * 64),
            "extra-key": lambda i: i.update(extra=1),
            "kind": lambda i: i.update(kind="files"),
            "empty-purpose": lambda i: i.update(purpose="  "),
            "no-fragments": lambda i: i.update(fragments=[]),
            "size-lie": lambda i: i["material"].update(size=10),
            "bool-size": lambda i: i["material"].update(size=True),
            "dup-range": lambda i: i["fragments"].append(copy.deepcopy(i["fragments"][0])),
        }.items():
            bad = copy.deepcopy(item)
            mutate(bad)
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    mc.validate_context(bad)
                with self.assertRaises(ValueError):
                    ac.application_input(None, "Why?", context=[bad])

    def test_data_budgets(self):
        material = m.import_bytes(b"a" * 300000, "file")
        fragments = []
        for i in range(9):                                       # 9 x 32 KiB = 288 KiB > 256 KiB
            f = m.read_range(material, material.sha256, i * 32768, (i + 1) * 32768)
            fragments.append({"ref": f.ref.to_json(), "text": f.text})
        item = {"kind": "material", "purpose": "p",
                "material": {"id": material.material_id, "version": material.sha256, "size": material.size},
                "fragments": fragments}
        with self.assertRaises(ValueError):
            mc.validate_context(item)
        item["fragments"] = fragments[:8]                        # exactly 256 KiB
        mc.validate_context(item)

    def test_two_material_items_for_the_same_material_are_a_duplicate(self):
        _, item = make_item()
        with self.assertRaises(ValueError):
            ac.application_input(None, "Why?", context=[item, copy.deepcopy(item)])


class LineRangeTests(unittest.TestCase):
    def setUp(self):
        self.material = m.import_bytes(b"one\ntwo\r\nthree\nlast", "file")

    def test_line_ranges_cover_exactly_the_requested_lines(self):
        raw = self.material.raw
        self.assertEqual(raw[slice(*m.line_range(self.material, 1, 1))], b"one\n")
        self.assertEqual(raw[slice(*m.line_range(self.material, 2, 3))], b"two\r\nthree\n")
        self.assertEqual(raw[slice(*m.line_range(self.material, 4, 4))], b"last")   # no final newline
        self.assertEqual(raw[slice(*m.line_range(self.material, 1, 4))], raw)

    def test_invalid_line_ranges_are_refused(self):
        for first, last in ((0, 1), (2, 1), (1, 5), (5, 5), (True, 2), ("1", 2)):
            with self.subTest(first=first, last=last):
                with self.assertRaises(m.MaterialError):
                    m.line_range(self.material, first, last)
        trailing = m.import_bytes(b"a\nb\n", "file")
        with self.assertRaises(m.MaterialError):
            m.line_range(trailing, 3, 3)              # the empty "line" after the final newline

    def test_split_range_matches_chunk_rules_and_is_contiguous(self):
        material = m.import_bytes(("zażółć gęślą jaźń\n" * 5000).encode(), "file")
        start, end = m.line_range(material, 100, 4000)
        parts = m.split_range(material, start, end)
        self.assertEqual(parts[0][0], start)
        self.assertEqual(parts[-1][1], end)
        for (a, b), (c, d) in zip(parts, parts[1:]):
            self.assertEqual(b, c)
        for a, b in parts:
            self.assertLessEqual(b - a, m.CHUNK_BYTES)
            material.raw[a:b].decode("utf-8")
        self.assertEqual(parts, m.split_range(material, start, end))
        for bad in ((3, 4), (0, 3), (0, 0), (-1, 5), (0, material.size + 1)):
            with self.assertRaises(m.MaterialError):
                m.split_range(material, *bad)


class ReadBoundedTests(unittest.TestCase):
    def setUp(self):
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ai-material-policy-", dir=real_home)))
        self.enterContext(patch.object(p, "_identity", return_value=(os.getuid(), str(self.home))))
        config = self.home / ".config/ai-desktop/permissions.toml"
        config.parent.mkdir(parents=True, mode=0o700)
        config.write_text('version = 1\n[files]\nread = ["~/data/*"]\ndeny = []\n')
        config.chmod(0o600)
        self.policy = p.load_policy()
        (self.home / "data").mkdir(mode=0o700)
        (self.home / "outside").mkdir(mode=0o700)

    def code(self, action):
        with self.assertRaises(ValueError) as caught:
            action()
        return str(caught.exception)

    def test_large_allowed_file_is_read_exactly_and_over_limit_is_refused(self):
        big = self.home / "data/big.txt"
        big.write_bytes(b"x" * (3 * 1024 * 1024))                  # far above the 64 KiB per-file policy budget
        selected, raw = p.read_bounded(self.policy, str(big), 4 * 1024 * 1024)
        self.assertEqual((selected, len(raw)), (str(big), 3 * 1024 * 1024))
        self.assertEqual(self.code(lambda: p.read_bounded(self.policy, str(big), 1024 * 1024)), "files-size")

    def test_denied_missing_symlink_hardlink_and_special_files_are_refused(self):
        good = self.home / "data/a.txt"
        good.write_text("ok\n")
        (self.home / "outside/b.txt").write_text("no\n")
        (self.home / "data/link.txt").symlink_to(good)
        os.link(good, self.home / "data/hard.txt")
        os.mkfifo(self.home / "data/fifo")
        cases = {
            str(self.home / "outside/b.txt"): "files-denied",
            str(self.home / "data/nothing.txt"): "files-missing",
            str(self.home / "data/link.txt"): "files-symlink",
            str(self.home / "data/hard.txt"): "files-links",
            str(self.home / "data/fifo"): "files-type",
        }
        for path, expected in cases.items():
            with self.subTest(path=path[-12:]):
                self.assertEqual(self.code(lambda: p.read_bounded(self.policy, path, 1024)), expected)

    def test_bad_arguments_are_refused(self):
        for policy, limit in ((object(), 1024), (self.policy, 0), (self.policy, True), (self.policy, 17 * 1024 * 1024)):
            with self.assertRaises(ValueError):
                p.read_bounded(policy, str(self.home / "data/a.txt"), limit)


if __name__ == "__main__":
    unittest.main()
