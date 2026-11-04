"""Presentation regressions: no model or network; use the real Rich renderer."""
import io
import re
import unittest

from ai_desktop import analysis_terminal as terminal

TEXT = ("Odpowiedź [1][2].\n\nŹródła:\n"
        "[1] Pierwszy tytuł — `https://example.org/a?x=1&y=2`\n"
        "[2] Drugi tytuł — `https://example.org/b_(c)`")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class ReferenceLayoutTests(unittest.TestCase):
    def test_sources_remain_separate_numbered_paragraphs_in_real_renderer(self):
        for width in (20, 80, 160):
            for styled in (False, True):
                with self.subTest(width=width, styled=styled):
                    rendered = ANSI.sub("", terminal._render(TEXT, width=width, styled=styled))
                    lines = [line.rstrip() for line in rendered.splitlines()]
                    heading = lines.index("Źródła:")
                    first = next(i for i, line in enumerate(lines) if line.startswith("[1] "))
                    second = next(i for i, line in enumerate(lines) if line.startswith("[2] "))
                    self.assertGreater(first, heading + 1)
                    self.assertGreater(second, first + 1)
                    self.assertEqual(lines[second - 1], "")
                    self.assertTrue(any("Odpowiedź [1][2]." in line for line in lines))
                    self.assertNotIn("\x1b]8", rendered)

    def test_bracket_numbered_list_without_heading_keeps_each_item(self):
        rendered = terminal._render("[1] Alpha\n[2] Beta\n[3] Gamma", width=80, styled=False)
        self.assertEqual([x.strip() for x in rendered.splitlines()],
                         ["[1] Alpha", "", "[2] Beta", "", "[3] Gamma"])

    def test_inline_citations_and_ordinary_soft_wrap_are_not_split(self):
        text = "Tekst [1] oraz [2].\nDalsze zdanie."
        rendered = terminal._render(text, width=80, styled=False)
        self.assertEqual(rendered.strip(), "Tekst [1] oraz [2]. Dalsze zdanie.")

    def test_existing_bulleted_numbered_and_nested_lists_stay_lists(self):
        for text in ("- Alpha\n- Beta", "1. Alpha\n2. Beta", "- Alpha\n  - Beta"):
            rendered = terminal._render(text, width=80, styled=False)
            self.assertNotEqual(next(i for i, x in enumerate(rendered.splitlines()) if "Alpha" in x),
                                next(i for i, x in enumerate(rendered.splitlines()) if "Beta" in x))

    def test_code_blocks_looking_like_references_are_preserved_exactly(self):
        for fence in ("```", "~~~~"):
            code = "Źródła:\n[1] Alpha\n[2] Beta\nprintf '%s' unchanged"
            rendered = terminal._render(fence + "fish\n" + code + "\n" + fence, width=20, styled=False)
            self.assertIn("```fish\n" + code + "\n```", rendered)

    def test_urls_entities_and_literal_punctuation_are_not_rewritten(self):
        url = "http://example.org/a_(b)?x=1&copy=2&entity=&copy;"
        text = "Źródła:\n[1] Tytuł — `" + url + "`\n[2] Drugi — `https://example.org/`"
        rendered = terminal._render(text, width=160, styled=False)
        self.assertIn(url, rendered)
        self.assertNotIn("©", rendered)

    def test_control_entities_remain_inert_after_spacing(self):
        text = "Źródła:\n[1] &#x1b;]52;clip&#7;\n[2] &#x202e; Title"
        rendered = terminal.render_answer(text, output_format="plain")
        for forbidden in ("\x1b", "\a", "\u202e"):
            self.assertNotIn(forbidden, rendered)

    def test_markdown_mode_preserves_original_wire_text(self):
        self.assertEqual(terminal.render_answer(TEXT, output_format="markdown", stream=io.StringIO()), TEXT)
