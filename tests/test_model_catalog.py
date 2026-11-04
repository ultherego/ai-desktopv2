"""Model choices come from the pinned client's own catalog; the catalog is a convenience, never authority."""
import os as _os
_os.environ.setdefault("AI_DESKTOP_MODEL_CATALOG", "off")
import io
import os
from contextlib import redirect_stdout
from unittest.mock import patch

import unittest

from ai_desktop import management_cli, model_catalog as catalog

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
        with patch.object(catalog, "trusted_file", return_value=None), patch("os.access", return_value=True):
            return catalog.fetch(binary="/usr/lib/ai-desktop/codex", run=run)

    def test_command_is_closed_offline_and_carries_no_credentials(self):
        seen = []
        self.fetch(lambda argv, timeout: seen.append(argv) or RESULT)
        argv = seen[0]
        self.assertEqual(argv[0], "bwrap")
        for flag in ("--unshare-all", "--die-with-parent", "--cap-drop"):
            self.assertIn(flag, argv)
        self.assertNotIn("--share-net", argv)
        self.assertEqual(argv[-1], "app-server")
        self.assertNotIn("--bind", argv)                    # only read-only /usr; HOME is an empty tmpfs

    def test_every_failure_is_catalog_unavailable(self):
        for error in (OSError("x"), ValueError("x"), KeyError("x"), catalog.CatalogUnavailable()):
            def run(argv, timeout, error=error):
                raise error
            with self.subTest(error=error), self.assertRaises(catalog.CatalogUnavailable):
                self.fetch(run)

    def test_untrusted_or_missing_binary_is_unavailable(self):
        with self.assertRaises(catalog.CatalogUnavailable):
            catalog.fetch(binary="/nonexistent/codex", run=lambda argv, timeout: RESULT)

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
        self.assertEqual(labels[0], "Auto (domyślny klienta)")
        self.assertEqual(labels[1], "GPT-6.1-Sol (gpt-6.1-sol)")
        self.assertEqual(labels[-1], "Inny identyfikator (wpisz)")
        self.assertIsNone(note)

    def test_unavailable_catalog_falls_back_to_auto_and_free_text_with_a_note(self):
        with patch.object(management_cli.model_catalog, "fetch", side_effect=catalog.CatalogUnavailable()), \
                redirect_stdout(io.StringIO()):
            values, _, note = management_cli.model_choices("codex")
        self.assertEqual(values, ("default", "custom"))
        self.assertIn("niedostępna", note)

    def test_claude_uses_the_client_aliases_without_starting_anything(self):
        with patch.object(management_cli.model_catalog, "fetch", side_effect=AssertionError("must not run")):
            values, labels, note = management_cli.model_choices("claude")
        self.assertEqual(values, ("default", "best", "fable", "opus", "sonnet", "haiku", "opusplan"))
        self.assertEqual(len(values), len(labels))
        self.assertIsNone(note)


if __name__ == "__main__":
    unittest.main()
