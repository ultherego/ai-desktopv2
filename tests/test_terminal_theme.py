"""The theme is colour only: without a colour terminal every helper returns the plain text."""
import io
import os
import unittest
from unittest.mock import patch

from ai_desktop import terminal_style as style


class Stream(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True


class ThemeTests(unittest.TestCase):
    def colour(self, **env):
        return patch.dict(os.environ, {"TERM": "xterm-256color", **env}, clear=True)

    def test_without_colour_everything_is_plain(self):
        with patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": "1"}, clear=True):
            self.assertEqual(style.greeting("W czym mogę pomóc?", stream=Stream()), "W czym mogę pomóc?")
            self.assertEqual(style.rule(stream=Stream()), "─" * style.RULE_WIDTH)
            self.assertEqual(style.paint("x", "accent", stream=Stream()), "x")

    def test_the_greeting_is_coloured_and_its_text_stays_contiguous(self):
        with self.colour():
            shown = style.greeting("W czym mogę pomóc?", stream=Stream())
        self.assertIn("W czym mogę pomóc?", shown)
        self.assertIn("\x1b[", shown)

    def test_true_colour_only_where_the_terminal_declares_it(self):
        with self.colour(COLORTERM="truecolor"):
            self.assertIn("38;2;", style.paint("x", "accent", stream=Stream()))
            self.assertEqual(style.rule(stream=Stream()).count("─"), style.RULE_WIDTH)
        with self.colour():
            shown = style.paint("x", "accent", stream=Stream())
        self.assertNotIn("38;2;", shown)
        self.assertIn("96", shown)

    def test_sizes_read_like_a_person_writes_them(self):
        self.assertEqual(style.size_label(567), "567 B")
        self.assertEqual(style.size_label(3569), "3.5 KiB")


if __name__ == "__main__":
    unittest.main()
