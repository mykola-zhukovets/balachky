"""feature/output-formats — детерміновані профілі форматування виводу.

Кожен режим перевіряється на прикладі: той самий вхід → очікуваний вихід
(жодного ШІ, повна детермінованість).
"""
import unittest

from whisper_core.textformat import (
    apply_format, split_sentences, MODES, PLAIN, MARKDOWN, CODE, LETTER,
)


class SplitSentencesTests(unittest.TestCase):
    def test_splits_on_terminal_punctuation(self):
        self.assertEqual(
            split_sentences("Купити хліб. Купити молоко! Готово?"),
            ["Купити хліб.", "Купити молоко!", "Готово?"])

    def test_empty(self):
        self.assertEqual(split_sentences(""), [])
        self.assertEqual(split_sentences("   "), [])


class PlainModeTests(unittest.TestCase):
    def test_normalizes_spaces(self):
        self.assertEqual(apply_format("  привіт    світ  ", PLAIN), "привіт світ")

    def test_no_structural_change(self):
        self.assertEqual(apply_format("Одне речення. Друге.", PLAIN),
                         "Одне речення. Друге.")


class MarkdownModeTests(unittest.TestCase):
    def test_sentences_become_bullets(self):
        self.assertEqual(
            apply_format("Купити хліб. Купити молоко. Подзвонити мамі.", MARKDOWN),
            "- Купити хліб.\n- Купити молоко.\n- Подзвонити мамі.")

    def test_single_sentence(self):
        self.assertEqual(apply_format("Одне завдання.", MARKDOWN), "- Одне завдання.")


class CodeModeTests(unittest.TestCase):
    def test_preserves_indentation_and_adds_nothing(self):
        src = "def foo():\n    return 1  \n"
        self.assertEqual(apply_format(src, CODE), "def foo():\n    return 1")

    def test_does_not_capitalize_or_punctuate(self):
        self.assertEqual(apply_format("import os", CODE), "import os")


class LetterModeTests(unittest.TestCase):
    def test_lines_become_paragraphs(self):
        self.assertEqual(
            apply_format("Доброго дня.\nПрошу розглянути звернення.", LETTER),
            "Доброго дня.\n\nПрошу розглянути звернення.")

    def test_normalizes_spaces_within_paragraph(self):
        self.assertEqual(apply_format("Шановний   пане.", LETTER), "Шановний пане.")


class FallbackTests(unittest.TestCase):
    def test_unknown_mode_falls_back_to_plain(self):
        self.assertEqual(apply_format("  a   b  ", "nonsense"), "a b")

    def test_empty_text(self):
        for mode in MODES:
            self.assertEqual(apply_format("", mode), "")


if __name__ == "__main__":
    unittest.main()
