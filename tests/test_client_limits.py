"""Real bundle metadata fits; the shared byte and memory ceilings remain finite."""
import json
from pathlib import Path
import re
import stat
import tempfile
from types import SimpleNamespace
import unittest

from ai_desktop import client_binding as binding, client_limits as limits
from ai_desktop import client_registration as registration, client_sources as sources
from ai_desktop.analysis_contract import AnalysisDenied


ROOT = Path(__file__).resolve().parents[1]
MEASUREMENT = ROOT / "tests/fixtures/codex-0.167.0-linux-musl-sizes.json"


def manifest(size, count=1):
    return json.dumps({"schema": 1, "provider": "codex", "source_uid": 1000,
                       "source": "/home/fixture/.local/bin/codex", "layout": "codex-bundle",
                       "digest": "a" * 64, "bytes": size, "files_count": count}).encode()


class ClientLimitsTests(unittest.TestCase):
    def test_discovery_and_runtime_share_the_same_finite_budgets(self):
        for name in ("MAX_FILE_BYTES", "MAX_TOTAL_BYTES", "MAX_ENTRIES", "MAX_DEPTH"):
            self.assertEqual(getattr(sources, name), getattr(limits, name))
            self.assertEqual(getattr(binding, name), getattr(limits, name))
        self.assertEqual(limits.MAX_FILE_BYTES, 384 * 1024 * 1024)
        self.assertEqual(limits.MAX_TOTAL_BYTES, 768 * 1024 * 1024)
        self.assertEqual((limits.MAX_ENTRIES, limits.MAX_DEPTH), (4096, 24))

    def test_measured_official_standalone_bundle_fits_without_dropping_resources(self):
        measurement = json.loads(MEASUREMENT.read_text())
        files, directories = measurement["files"], measurement["directories"]
        self.assertEqual((len(files), len(directories)), (44, 8))
        selected = {"bin", "codex-path", "codex-resources", "codex-package.json"}
        self.assertTrue(all(item["path"].split("/")[0] in selected for item in files))
        self.assertEqual(len({item["path"] for item in files}), len(files))
        # Exercise production accounting with measured stat metadata, without
        # allocating hundreds of MiB or fetching/running a vendor in tests.
        capture = binding._Capture(copy=False)
        for directory in directories:
            capture.account(SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_size=0))
        for item in files:
            capture.account(SimpleNamespace(st_mode=stat.S_IFREG | 0o555, st_size=item["size"]))
        self.assertEqual((capture.bytes, capture.entries), (540530246, 52))
        self.assertGreater(capture.bytes, 512 * 1024 * 1024)
        self.assertLess(capture.bytes, limits.MAX_TOTAL_BYTES)
        self.assertEqual(capture.files, {})
        value = registration.validate_manifest(manifest(capture.bytes, len(files)), "codex")
        self.assertEqual(value["bytes"], capture.bytes)

    def test_snapshot_exact_total_is_accepted_and_one_more_byte_is_refused(self):
        capture = binding._Capture(copy=False)
        for _ in range(2):
            capture.account(SimpleNamespace(st_mode=stat.S_IFREG | 0o555, st_size=limits.MAX_FILE_BYTES))
        self.assertEqual(capture.bytes, limits.MAX_TOTAL_BYTES)
        with self.assertRaisesRegex(AnalysisDenied, "client-layout"):
            capture.account(SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_size=1))
        with self.assertRaisesRegex(AnalysisDenied, "client-layout"):
            binding._Capture(copy=False).account(SimpleNamespace(
                st_mode=stat.S_IFREG | 0o555, st_size=limits.MAX_FILE_BYTES + 1))

    def test_source_copy_continues_above_old_total_but_stops_before_over_limit_write(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            path = Path(temporary) / "helper"
            path.write_bytes(b"payload")
            for before in (512 * 1024 * 1024, limits.MAX_TOTAL_BYTES - 7):
                written = []
                scanner = sources._Scan(sources._uid(None), lambda entry, fd: written.append(entry))
                scanner.total_bytes = before
                scanner.file(str(path), "bin/helper")
                self.assertEqual(scanner.total_bytes, before + 7)
                self.assertEqual(len(written), 1)
            written = []
            scanner = sources._Scan(sources._uid(None), lambda entry, fd: written.append(entry))
            scanner.total_bytes = limits.MAX_TOTAL_BYTES - 6
            with self.assertRaises(sources.SourceError) as raised:
                scanner.file(str(path), "bin/helper")
            self.assertEqual(raised.exception.detail, "byte-limit")
            self.assertEqual(written, [])

    def test_registration_manifest_uses_the_same_total_ceiling(self):
        self.assertEqual(registration.validate_manifest(manifest(limits.MAX_TOTAL_BYTES), "codex")["bytes"],
                         limits.MAX_TOTAL_BYTES)
        with self.assertRaisesRegex(registration.RegistrationError, "unsafe-cache"):
            registration.validate_manifest(manifest(limits.MAX_TOTAL_BYTES + 1), "codex")

    def test_service_memory_covers_both_payload_copies_and_native_headroom(self):
        required = limits.PAYLOAD_MEMORY_COPIES * limits.MAX_TOTAL_BYTES + limits.MIN_NATIVE_MEMORY_BYTES
        self.assertEqual(limits.PAYLOAD_MEMORY_COPIES, 2)
        self.assertEqual(required, limits.SERVICE_MEMORY_BYTES)
        for name in ("ai-desktop-analysis@.service", "ai-desktop-codex-limits@.service"):
            with self.subTest(name=name):
                unit = (ROOT / "packaging/systemd" / name).read_text()
                values = re.findall(r"^MemoryMax=([0-9]+)([KMG])$", unit, re.M)
                self.assertEqual(len(values), 1)
                value, suffix = values[0]
                actual = int(value) * 1024 ** {"K": 1, "M": 2, "G": 3}[suffix]
                self.assertEqual(actual, limits.SERVICE_MEMORY_BYTES)


if __name__ == "__main__":
    unittest.main()
