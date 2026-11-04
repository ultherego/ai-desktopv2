import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from ai_desktop import analysis_terminal as terminal, analysis_cli, analysis_contract as contract, cli
from test_report_policy import report_fixture
from tests.profile_fixture import DEFAULT_PROFILE


class TerminalTests(unittest.TestCase):
    def test_real_renderer_formats_headings_lists_and_preserves_code(self):
        command = 'printf "%s\\n" ' + 'very-long-word-' * 30
        for width in (20, 80, 160):
            for fence in ("```fish\n" + command + "\n```", "1. Code:\n\n   ```fish\n   " + command + "\n   ```"):
                rendered = terminal._render("## Stan\n\n**Wazne**\n\n- punkt\n\n" + fence, width=width, styled=False)
                self.assertNotIn("##", rendered)
                self.assertNotIn("**", rendered)
                self.assertIn("```fish\n" + command + "\n```", rendered)
                self.assertIn(command, rendered)

    def test_code_fences_survive_layout_without_truncation_or_delimiter_collision(self):
        code = 'print("```quoted````")\n' + "x" * 500 + "\n\n"
        for width in (20, 80):
            for prefix in ("", "> "):
                source = "\n".join(prefix + line for line in ("~~~~python\n" + code + "~~~~").split("\n"))
                rendered = terminal._render(source, width=width, styled=False)
                self.assertIn("`````python\n" + code + "`````", rendered)
                self.assertEqual(rendered.count("`````"), 2)
                self.assertNotIn("@CODE", rendered)
        indented = terminal._render("    first\n    second", width=20, styled=False)
        self.assertIn("```\nfirst\nsecond\n```", indented)

    def test_literal_code_frame_sanitizes_controls_and_rejects_unsafe_labels(self):
        framed = terminal.fenced_code("start\x1b]52;clip\a\u202e\n````\nend", language="diff")
        self.assertTrue(framed.startswith("`````diff\nstart\\u001b"))
        self.assertTrue(framed.endswith("end\n`````"))
        self.assertNotIn("\x1b", framed)
        self.assertNotIn("\u202e", framed)
        for label in ("fish\n```", "\x1b[31m", "x" * 33, None):
            with self.subTest(label=label), self.assertRaises(ValueError):
                terminal.fenced_code("x", language=label)
        self.assertEqual(terminal.fenced_code(""), "```\n\n```")
        self.assertEqual(terminal.fenced_code("x\n\n"), "```\nx\n\n```")

    def test_model_code_info_stays_inert_and_markdown_mode_preserves_source(self):
        source = "```fish title=untrusted\nprintf '%s\\n' ok\n```"
        rendered = terminal._render(source, width=20, styled=False)
        self.assertIn("```\nprintf '%s\\n' ok\n```", rendered)
        self.assertNotIn("title=", rendered)
        self.assertEqual(terminal.render_answer(source, output_format="markdown"), source)

    def test_controls_entities_bidi_links_and_rich_markup_are_inert(self):
        text = "# X\n\n&#x1b;]52;evil&#7; &#x202e; [link=file:///tmp/x]z[/link] [go](file:///tmp/x)\n\n```sh\n\x1b[31m\n```"
        rendered = terminal.render_answer(text, output_format="plain")
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertNotIn("\a", rendered)
        raw = terminal.render_answer(text, output_format="markdown")
        self.assertIn("# X", raw)
        self.assertNotIn("\x1b", raw)

    def test_no_color_pipe_fallback_and_invalid_format(self):
        with patch.dict(os.environ, {"NO_COLOR": "1", "TERM": "xterm"}), patch.object(terminal, "_render", wraps=terminal._render) as render:
            value = terminal.render_answer("# X", stream=io.StringIO())
            self.assertFalse(render.call_args.kwargs["styled"])
            self.assertNotIn("\x1b", value)
        with patch.object(terminal, "_render", side_effect=RuntimeError("PRIVATE")):
            self.assertEqual(terminal.render_answer("ok\x1b"), "ok\\u001b")
        with self.assertRaises(ValueError):
            terminal.render_answer("x", output_format="shell")

    def test_question_bound_to_wire_and_consent_and_controls_refused(self):
        report = report_fixture()
        gate = contract.AnalysisConsent(report, question="Dlaczego?")
        self.assertNotEqual(gate.sha256, contract.AnalysisConsent(report).sha256)
        auth = gate.authorize(report, question="Dlaczego?", sha256=gate.sha256, nonce=gate.nonce)
        data, sha = contract.parse_request(auth.payload.encode())
        self.assertEqual(json.loads(data)["question"], "Dlaczego?")
        self.assertEqual(sha, gate.sha256)
        for q in ("x\x1b", "x\u202e", "x" * (128 * 1024 + 1), None):
            with self.assertRaises((ValueError, TypeError)):
                contract.AnalysisConsent(report, question=q)
        gate = contract.AnalysisConsent(report, question="one")
        with self.assertRaises(contract.AnalysisDenied):
            gate.authorize(report, question="two", sha256=gate.sha256, nonce=gate.nonce)

    def test_saved_report_preview_does_not_collect_or_start_runtime(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as report:
            json.dump(report_fixture(), report); report.flush()
            with patch.object(analysis_cli.os, "geteuid", return_value=1000), patch.object(analysis_cli.os, "getuid", return_value=1000), \
                    patch("ai_desktop.agent_profiles.resolve", return_value=DEFAULT_PROFILE), \
                    patch.object(analysis_cli.diagnose, "collect") as collect, patch.object(analysis_cli, "managed_session") as run, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["analyze", "--preview", "--report", report.name, "--question", "Dlaczego?"]), 0)
            collect.assert_not_called(); run.assert_not_called()
            self.assertIn("Dlaczego?", out.getvalue())
