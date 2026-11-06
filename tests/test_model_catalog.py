"""Model choices come from the pinned client's own catalog; the catalog is a convenience, never authority."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
import io
import json
import os
from contextlib import redirect_stdout
from unittest.mock import patch

import unittest

from ai_desktop import management_cli, model_catalog as catalog
from system_client_fixtures import FakeBinding

RESULT = {"data": [
    {"id": "gpt-6.1-sol", "displayName": "GPT-6.1-Sol", "hidden": False},
    {"id": "gpt-6-astra", "displayName": "GPT-6-Astra", "hidden": False},
    {"id": "hidden-one", "displayName": "Hidden", "hidden": True},
    {"id": "opus", "displayName": "Claude alias is not a Codex id", "hidden": False},
    {"id": "Bad Id", "displayName": "x", "hidden": False},
    {"id": "gpt-6.1-sol", "displayName": "duplicate", "hidden": False},
    {"id": "gpt-5.5", "displayName": "bad\x1b[31m", "hidden": False}], "nextCursor": None}


class ParseTests(unittest.TestCase):
    def test_visible_well_formed_unique_ids_with_safe_labels(self):
        self.assertEqual(catalog.parse(RESULT), (("gpt-6.1-sol", "GPT-6.1-Sol"), ("gpt-6-astra", "GPT-6-Astra"),
                                                 ("gpt-5.5", "gpt-5.5")))

    def test_unexpected_shapes_make_the_catalog_unavailable(self):
        for bad in (None, [], {}, {"data": []}, {"data": "x"}, {"data": [5]}, {"data": [{"id": "Bad Id"}]}):
            with self.subTest(bad=bad), self.assertRaises(catalog.CatalogUnavailable):
                catalog.parse(bad)


class FetchTests(unittest.TestCase):
    def fetch(self, run):
        with patch.object(catalog.codex_runtime, "runtime_ready"), \
                patch.object(catalog.codex_runtime, "compatibility"), \
                patch.object(catalog.client_binding, "bind", return_value=FakeBinding()):
            return catalog.fetch(run=run)

    def test_command_is_closed_offline_and_carries_no_credentials(self):
        seen = []
        self.fetch(lambda argv, **kw: seen.append(argv) or (0, json.dumps(RESULT).encode(), b""))
        argv = seen[0]
        self.assertEqual(argv[0], "/usr/bin/bwrap")
        for flag in ("--unshare-user", "--unshare-net", "--unshare-pid", "--die-with-parent", "--cap-drop"):
            self.assertIn(flag, argv)
        self.assertNotIn("--share-net", argv)
        self.assertEqual(argv[-2], "catalog")
        self.assertNotIn("--bind", argv)                    # only read-only /usr; HOME is an empty tmpfs

    def test_every_failure_is_catalog_unavailable(self):
        for error in (OSError("x"), ValueError("x"), KeyError("x"), catalog.CatalogUnavailable()):
            def run(argv, error=error, **kwargs):
                raise error
            with self.subTest(error=error), self.assertRaises(catalog.CatalogUnavailable):
                self.fetch(run)

    def test_untrusted_or_missing_binary_is_unavailable(self):
        from ai_desktop.analysis_contract import AnalysisDenied
        with patch.object(catalog.codex_runtime, "runtime_ready", side_effect=AnalysisDenied("client-missing")), \
                self.assertRaises(catalog.CatalogUnavailable):
            catalog.fetch(run=lambda argv, **kw: (0, json.dumps(RESULT).encode(), b""))

    def test_compatibility_and_bound_identity_precede_catalog_and_failure_blocks_it(self):
        from ai_desktop.analysis_contract import AnalysisDenied
        binding = FakeBinding(pass_fds=(71,))
        calls = []
        def compatible(directory, bound, **kwargs):
            self.assertIs(bound, binding)
            calls.append("compatibility")
        def run(argv, **kwargs):
            self.assertEqual(calls, ["compatibility"])
            self.assertEqual(kwargs["pass_fds"], (71,))
            return 0, json.dumps(RESULT).encode(), b""
        with patch.object(catalog.codex_runtime, "runtime_ready"), \
                patch.object(catalog.codex_runtime, "compatibility", side_effect=compatible), \
                patch.object(catalog.client_binding, "bind", return_value=binding):
            self.assertTrue(catalog.fetch(run=run))
        self.assertTrue(binding.closed)
        with patch.object(catalog.codex_runtime, "runtime_ready"), \
                patch.object(catalog.codex_runtime, "compatibility", side_effect=AnalysisDenied("client-protocol")), \
                patch.object(catalog.client_binding, "bind", return_value=FakeBinding()), \
                self.assertRaises(catalog.CatalogUnavailable):
            catalog.fetch(run=lambda *a, **k: self.fail("catalog must not run"))

    def test_one_deadline_covers_compatibility_and_catalog(self):
        now, binding = [0.0], FakeBinding()
        def compatible(directory, bound, *, timeout):
            self.assertEqual(timeout, .5)
            now[0] = .51
        with patch.object(catalog.codex_runtime, "runtime_ready"), \
                patch.object(catalog.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(catalog.codex_runtime, "compatibility", side_effect=compatible), \
                patch.object(catalog.client_binding, "bind", return_value=binding), \
                self.assertRaises(catalog.CatalogUnavailable):
            catalog.fetch(timeout=.5, run=lambda *a, **k: self.fail("expired catalog must not start"))
        self.assertTrue(binding.closed)

    def test_slow_mount_metadata_prevents_catalog_start_after_deadline(self):
        now, binding = [0.0], FakeBinding()
        original = binding.mount_args
        def mount_args():
            result = original()
            now[0] = .51
            return result
        with patch.object(catalog.codex_runtime, "runtime_ready"), \
                patch.object(catalog.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(catalog.codex_runtime, "compatibility"), \
                patch.object(catalog.client_binding, "bind", return_value=binding), \
                patch.object(binding, "mount_args", side_effect=mount_args), \
                self.assertRaises(catalog.CatalogUnavailable):
            catalog.fetch(timeout=.5, run=lambda *a, **k: self.fail("expired catalog must not start"))
        self.assertTrue(binding.closed)

    def test_final_metadata_recheck_over_deadline_discards_valid_catalog(self):
        now, binding, calls = [0.0], FakeBinding(), []
        original = binding.recheck
        def recheck():
            original()
            if binding.rechecks == 2:
                now[0] = .51
        def run(*args, **kwargs):
            calls.append(kwargs["timeout"])
            return 0, json.dumps(RESULT).encode(), b""
        with patch.object(catalog.codex_runtime, "runtime_ready"), \
                patch.object(catalog.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(catalog.codex_runtime, "compatibility"), \
                patch.object(catalog.client_binding, "bind", return_value=binding), \
                patch.object(binding, "recheck", side_effect=recheck), \
                self.assertRaises(catalog.CatalogUnavailable):
            catalog.fetch(timeout=.5, run=run)
        self.assertEqual(calls, [.5])
        self.assertTrue(binding.closed)

    def test_protocol_budget_starts_after_initial_binding(self):
        now = [0.0]
        class SlowBinding(FakeBinding):
            def __enter__(self):
                now[0] = 3.0
                return super().__enter__()
        binding = SlowBinding()
        def compatible(directory, bound, *, timeout):
            self.assertEqual(timeout, .5)
        with patch.object(catalog.codex_runtime, "runtime_ready"), \
                patch.object(catalog.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(catalog.codex_runtime, "compatibility", side_effect=compatible), \
                patch.object(catalog.client_binding, "bind", return_value=binding):
            self.assertTrue(catalog.fetch(timeout=.5, run=lambda *a, **k: (0, json.dumps(RESULT).encode(), b"")))
        self.assertTrue(binding.closed)

    def test_the_real_client_is_never_started_when_disabled(self):
        with patch.dict(os.environ, {"AI_DESKTOP_MODEL_CATALOG": "off"}):
            with self.assertRaises(catalog.CatalogUnavailable):
                catalog.fetch()


class MenuTests(unittest.TestCase):
    def test_codex_choices_are_auto_then_client_catalog_then_free_text(self):
        with patch.object(management_cli.model_catalog, "fetch", return_value=catalog.parse(RESULT)), \
                redirect_stdout(io.StringIO()):
            values, labels, note = management_cli.model_choices("codex")
        self.assertEqual(values, ("default", "gpt-6.1-sol", "gpt-6-astra", "gpt-5.5", "custom"))
        self.assertEqual(labels[0], "Auto (client default)")
        self.assertEqual(labels[1], "GPT-6.1-Sol (gpt-6.1-sol)")
        self.assertEqual(labels[-1], "Other ID (type it)")
        self.assertIsNone(note)

    def test_unavailable_catalog_falls_back_to_auto_and_free_text_with_a_note(self):
        with patch.object(management_cli.model_catalog, "fetch", side_effect=catalog.CatalogUnavailable()), \
                redirect_stdout(io.StringIO()):
            values, _, note = management_cli.model_choices("codex")
        self.assertEqual(values, ("default", "custom"))
        self.assertIn("unavailable", note)

    def test_claude_uses_the_client_aliases_without_starting_anything(self):
        with patch.object(management_cli.model_catalog, "fetch", side_effect=AssertionError("must not run")):
            values, labels, note = management_cli.model_choices("claude")
        self.assertEqual(values, ("default", "best", "fable", "opus", "sonnet", "haiku", "opusplan"))
        self.assertEqual(len(values), len(labels))
        self.assertIsNone(note)


if __name__ == "__main__":
    unittest.main()
