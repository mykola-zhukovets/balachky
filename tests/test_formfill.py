"""feature/voice-form-fill — тести чистої логіки заповнення шаблонів голосом
(whisper_core.formfill): парсер полів, курсор, підстановка, навігаційні команди,
сховище шаблонів. Без Qt. Стиль — як test_snippets.py."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from whisper_core.formfill import (
    parse_fields, FormSession, match_nav_command,
    list_templates, load_template, iter_segments,
)


class ParseFieldsTests(unittest.TestCase):
    def test_extracts_in_order(self):
        self.assertEqual(
            parse_fields("Командиру [посада] [звання] [ПІБ]"),
            ["посада", "звання", "ПІБ"])

    def test_dedup_preserves_first_order(self):
        # поле, що трапляється двічі, — одна позиція курсора (спільне значення)
        self.assertEqual(
            parse_fields("[ПІБ] доповідає. Підпис: [ПІБ]"),
            ["ПІБ"])

    def test_no_fields(self):
        self.assertEqual(parse_fields("Звичайний текст без полів"), [])

    def test_strips_inner_whitespace_in_name(self):
        self.assertEqual(parse_fields("[  посада  ]"), ["посада"])

    def test_ignores_empty_brackets(self):
        self.assertEqual(parse_fields("порожні [] [ ] дужки"), [])

    def test_underscore_and_cyrillic(self):
        self.assertEqual(parse_fields("[назва_поля]"), ["назва_поля"])


class IterSegmentsTests(unittest.TestCase):
    def test_splits_text_and_fields(self):
        self.assertEqual(
            iter_segments("Кому [посада]!"),
            [("text", "Кому "), ("field", "посада"), ("text", "!")])

    def test_reconstructs_text_parts(self):
        segs = iter_segments("[а]середина[б]")
        self.assertEqual(segs, [("field", "а"), ("text", "середина"),
                                ("field", "б")])

    def test_empty_brackets_stay_text(self):
        self.assertEqual(iter_segments("порожні []"), [("text", "порожні []")])

    def test_no_fields(self):
        self.assertEqual(iter_segments("самий текст"), [("text", "самий текст")])


class SessionCursorTests(unittest.TestCase):
    def setUp(self):
        self.s = FormSession("Командиру [посада] [звання] [ПІБ]")

    def test_initial_field(self):
        self.assertEqual(self.s.current_field, "посада")
        self.assertEqual(self.s.index, 0)

    def test_next_prev(self):
        self.s.next_field()
        self.assertEqual(self.s.current_field, "звання")
        self.s.next_field()
        self.assertEqual(self.s.current_field, "ПІБ")
        self.s.prev_field()
        self.assertEqual(self.s.current_field, "звання")

    def test_next_clamps_at_end(self):
        self.s.next_field(); self.s.next_field(); self.s.next_field()
        self.assertEqual(self.s.index, 2)  # не виходить за межі
        self.assertEqual(self.s.current_field, "ПІБ")

    def test_prev_clamps_at_start(self):
        self.s.prev_field()
        self.assertEqual(self.s.index, 0)

    def test_empty_template_has_no_field(self):
        s = FormSession("текст без полів")
        self.assertIsNone(s.current_field)
        s.next_field()  # не падає
        self.assertIsNone(s.current_field)


class SubstitutionTests(unittest.TestCase):
    def test_set_value_and_render(self):
        s = FormSession("Командиру [посада] [ПІБ]")
        s.set_value("командиру роти")
        s.next_field()
        s.set_value("Шевченко Т.Г.")
        self.assertEqual(s.render(), "Командиру командиру роти Шевченко Т.Г.")

    def test_unfilled_stays_as_placeholder(self):
        s = FormSession("[а] і [б]")
        s.set_value("X")
        self.assertEqual(s.render(), "X і [б]")

    def test_repeated_field_filled_everywhere(self):
        s = FormSession("[ПІБ] — підпис [ПІБ]")
        s.set_value("Шевченко")
        self.assertEqual(s.render(), "Шевченко — підпис Шевченко")

    def test_append_value_accumulates(self):
        s = FormSession("[поле]")
        s.append_value("перше")
        s.append_value("друге")
        self.assertEqual(s.value_of("поле"), "перше друге")

    def test_set_value_targets_current_field(self):
        s = FormSession("[а] [б]")
        s.next_field()
        s.set_value("значення-б")
        self.assertEqual(s.value_of("б"), "значення-б")
        self.assertEqual(s.value_of("а"), "")

    def test_is_complete(self):
        s = FormSession("[а] [б]")
        self.assertFalse(s.is_complete)
        s.set_value("1"); s.next_field(); s.set_value("2")
        self.assertTrue(s.is_complete)

    def test_clear_field(self):
        s = FormSession("[а]")
        s.set_value("щось")
        s.clear_current()
        self.assertEqual(s.value_of("а"), "")


class NavCommandTests(unittest.TestCase):
    def test_uk_next(self):
        self.assertEqual(match_nav_command("наступне поле", "uk"), "next")
        self.assertEqual(match_nav_command("Наступне поле.", "uk"), "next")

    def test_uk_prev(self):
        self.assertEqual(match_nav_command("попереднє поле", "uk"), "prev")

    def test_en(self):
        self.assertEqual(match_nav_command("next field", "en"), "next")
        self.assertEqual(match_nav_command("previous field", "en"), "prev")

    def test_not_a_command(self):
        self.assertIsNone(match_nav_command("командиру роти", "uk"))

    def test_partial_no_match(self):
        # команда лише при точному збігу нормалізованої фрази (безпека, як сніпети)
        self.assertIsNone(match_nav_command("а тепер наступне поле форми", "uk"))


class StorageTests(unittest.TestCase):
    def _dir(self, files):
        d = tempfile.mkdtemp()
        for name, text in files.items():
            (Path(d) / name).write_text(text, encoding="utf-8")
        return Path(d)

    def test_list_templates_filters_extensions(self):
        d = self._dir({"a.txt": "[x]", "b.md": "[y]", "c.png": "no",
                       "d.toml": "no"})
        with patch("whisper_core.formfill.paths.templates_dir", return_value=d), \
             patch("whisper_core.formfill.paths.bundled_templates_dir",
                   return_value=None):
            names = [p.name for p in list_templates()]
        self.assertEqual(names, ["a.txt", "b.md"])

    def test_load_template_reads_text(self):
        d = self._dir({"r.txt": "Командиру [посада]"})
        self.assertEqual(load_template(d / "r.txt"), "Командиру [посада]")

    def test_list_seeds_from_bundle_when_empty(self):
        user = Path(tempfile.mkdtemp())
        bundle = self._dir({"sample.txt": "[поле]"})
        with patch("whisper_core.formfill.paths.templates_dir", return_value=user), \
             patch("whisper_core.formfill.paths.bundled_templates_dir",
                   return_value=bundle):
            names = [p.name for p in list_templates()]
        self.assertEqual(names, ["sample.txt"])
        self.assertTrue((user / "sample.txt").exists())  # скопійовано у writable


if __name__ == "__main__":
    unittest.main()
