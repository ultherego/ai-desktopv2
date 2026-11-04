"""Preparation is not delivery; only the exact handed-off packet counts. No model or filesystem authority."""
import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_desktop import material_loop, materials, project_loop
from ai_desktop import material_cli, material_grant
from ai_desktop.analysis_contract import AnalysisDenied


class DeliveryTests(unittest.TestCase):
    def test_material_reads_and_searches_count_only_after_handoff_and_are_idempotent(self):
        loop = material_loop.Loop(materials.import_bytes(b"one\ntwo\n", "file"))
        loop.execute({"action": "search", "needle": "two"})
        loop.execute({"action": "read", "ranges": [(1, 1)]})
        value = loop.request_input(1, 1000)
        self.assertEqual(loop.delivered_summary(), ([], 0))
        self.assertEqual(loop.confirm_sources([(1, 2)]), ([], [(1, 2)]))
        loop.mark_handed_off(value)
        self.assertEqual(loop.delivered_summary(), ([[1, 2]], 8))
        loop.mark_handed_off(value)
        self.assertEqual(loop.delivered_summary(), ([[1, 2]], 8))

    def project(self):
        self.raw = b"one\ntwo\n"
        return SimpleNamespace(
            read_file=lambda path: SimpleNamespace(material=materials.import_bytes(self.raw, "file"), changed=False),
            overview=lambda: dict(files=1, size=8, top=[], extensions=[], key_files=[]),
            stats=dict(indexed=1, denied=0, links=0, special=0, unsafe=0, vcs=0, other=0),
            project_id="a" * 16, complete=True)

    def test_project_local_read_is_not_a_delivered_file_and_version_is_pinned_at_handoff(self):
        loop = project_loop.Loop(self.project())
        loop.execute({"action": "read", "path": "a.txt", "ranges": [(1, 2)]})
        self.assertFalse(loop.fully_read("a.txt"))
        self.assertEqual(loop.visited, [])
        value = loop.request_input(1, 1000)
        self.assertEqual(loop.delivered_summary(), ({}, 0))
        loop.mark_handed_off(value)
        version = loop.delivered_versions["a.txt"]
        self.assertTrue(loop.fully_read("a.txt"))
        self.assertEqual(loop.visited, ["a.txt"])
        loop.mark_handed_off(value)
        self.assertEqual(loop.delivered_summary(), ({"a.txt": [[1, 2]]}, 8))
        self.raw = b"new private contents\n"
        self.assertNotEqual(version, loop.project.read_file("a.txt").material.sha256)
        # The next local read detects the change; it is not permission to
        # classify the new version as already handed off.
        loop.execute({"action": "read", "path": "a.txt", "ranges": [(1, 1)]})
        self.assertFalse(loop.fully_read("a.txt"))
        self.assertEqual(loop.delivered_versions["a.txt"], version)

    def test_dropped_unsent_observations_never_enter_delivery_evidence(self):
        loop = material_loop.Loop(materials.import_bytes(b"one\ntwo\n", "file"))
        loop.execute({"action": "read", "ranges": [(1, 1)]})
        loop.observations.clear()     # compaction/cancellation before transfer
        loop.mark_handed_off(loop.request_input(1, 1000))
        self.assertEqual(loop.delivered_summary(), ([], 0))

    def test_opening_failure_is_not_delivery_but_sender_failure_is_an_unknown_handoff(self):
        material = materials.import_bytes(b"one\ntwo\n", "file")
        for failure in ("opening", "sender"):
            with self.subTest(failure=failure):
                loop = material_loop.Loop(material)
                loop.preload_whole()
                session = material_grant.TransferSession(material,
                    material_grant.MaterialGrant(material.material_id, material.sha256, "purpose", 600), clock=lambda: 0)
                value = loop.request_input(1, session.bytes_left())
                packet = session.prepare_step("question", value, loop.data_bytes(), loop.request_ranges())

                def fail():
                    raise AnalysisDenied("network")

                window = SimpleNamespace(is_open=False, seconds_left=lambda: 0, close=lambda: None,
                                         ensure=fail if failure == "opening" else lambda: None)
                ui = material_cli.Session(material, "file", SimpleNamespace(sources=lambda: {"claude": True}),
                    SimpleNamespace(details=False, format="plain"), input, clock=lambda: 0, window=window,
                    sender=lambda approval: fail())
                if failure == "sender":
                    # The prepared DTO can be discarded after authorization.
                    # Evidence must still describe the immutable wire payload.
                    ui.pacer.record = lambda: value["observations"].clear()
                with contextlib.redirect_stdout(io.StringIO()), patch.object(material_cli, "installation_ready"):
                    self.assertIsNone(ui._deliver(session, packet, on_handoff=loop.mark_handed_off))
                if failure == "opening":
                    self.assertEqual(loop.delivered_summary(), ([], 0))
                    self.assertEqual(session.ledger()[0]["state"], "cancelled")
                else:
                    self.assertEqual(loop.delivered_summary(), ([[1, 2]], 8))
                    self.assertEqual(session.ledger()[0]["state"], "unknown")
