"""Хвиля 1: правило-базований нормалізатор TTS (§5, §11.2).

Red-тести з таблиці §11.2 + межові числа/місяці/некоректні дати (§11.3, table-driven)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.tts.normalize import cardinal, normalize


class TestCardinal(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(cardinal(0), "нуль")

    def test_units(self):
        self.assertEqual(cardinal(1), "один")
        self.assertEqual(cardinal(9), "дев'ять")

    def test_teens(self):
        self.assertEqual(cardinal(11), "одинадцять")
        self.assertEqual(cardinal(19), "дев'ятнадцять")

    def test_tens(self):
        self.assertEqual(cardinal(23), "двадцять три")
        self.assertEqual(cardinal(90), "дев'яносто")

    def test_hundreds(self):
        self.assertEqual(cardinal(100), "сто")
        self.assertEqual(cardinal(256), "двісті п'ятдесят шість")

    def test_thousands_feminine(self):
        self.assertEqual(cardinal(1000), "одна тисяча")
        self.assertEqual(cardinal(2000), "дві тисячі")
        self.assertEqual(cardinal(5000), "п'ять тисяч")
        self.assertEqual(cardinal(21000), "двадцять одна тисяча")

    def test_millions(self):
        self.assertEqual(cardinal(1_000_000), "один мільйон")
        self.assertEqual(cardinal(2_000_000), "два мільйони")
        self.assertEqual(cardinal(5_000_000), "п'ять мільйонів")

    def test_max_range(self):
        # 999_999_999 має розкластися без винятку
        self.assertTrue(cardinal(999_999_999))
        self.assertNotIn("9", cardinal(999_999_999))


class TestNormalize(unittest.TestCase):
    def _t(self, s, abbrev=None):
        return normalize(s, abbrev_map=abbrev or {}).text

    def test_integer_23(self):
        self.assertEqual(self._t("23"), "двадцять три")

    def test_time_hhmm(self):
        self.assertEqual(self._t("14:30"),
                         "чотирнадцята година тридцять хвилин")

    def test_time_hhmmss(self):
        self.assertIn("секунд", self._t("14:30:05"))

    def test_date_full(self):
        self.assertEqual(
            self._t("23.07.2026"),
            "двадцять третє липня дві тисячі двадцять шостого року")

    def test_abbrev_user_map_beats_guess(self):
        out = self._t("78 ТОВ", abbrev={"ТОВ": "те-о-ве"})
        self.assertIn("те-о-ве", out)
        self.assertNotIn("товариство", out)

    def test_unknown_allcaps_spelled(self):
        out = self._t("ХҐЯ")
        self.assertIn("ха", out)
        self.assertIn("ґе", out)
        self.assertIn("я", out)

    def test_idempotent_on_words(self):
        once = self._t("23")
        twice = self._t(once)
        self.assertEqual(once, twice)

    def test_ratio_scale(self):
        # «X:Y» у контексті масштабу → «до». v1 читає число в називному (§5.1),
        # тож «п'ятдесят тисяч», не родове «п'ятдесяти» — свідоме обмеження v1.
        out = self._t("1:50000")
        self.assertTrue(out.startswith("один до "))
        self.assertIn("тисяч", out)

    def test_decimal(self):
        self.assertEqual(self._t("1,5"), "одна кома п'ять")

    def test_fraction_table(self):
        self.assertEqual(self._t("1/2"), "одна друга")

    def test_range(self):
        self.assertEqual(self._t("5-10"), "від п'ять до десять")

    def test_percent(self):
        self.assertIn("відсотків", self._t("50%"))

    def test_number_sign(self):
        self.assertIn("номер", self._t("№"))

    def test_measure_km(self):
        self.assertIn("кілометрів", self._t("5 км"))

    def test_list_item_not_expanded(self):
        # «1.» на початку рядка — номер пункту, не «перше»/«один»
        out = self._t("1. Перше завдання")
        self.assertTrue(out.startswith("1."))

    def test_all_months_genitive(self):
        expected = ["січня", "лютого", "березня", "квітня", "травня", "червня",
                    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
        for i, mo in enumerate(expected, 1):
            self.assertIn(mo, self._t(f"15.{i:02d}.2024"))

    def test_plus_standalone_reads_plyus(self):
        self.assertIn("плюс", self._t("2 + 2"))

    def test_plus_between_cyrillic_reserved_for_stress(self):
        # ВАРТОВИЙ (Хвиля 4): «+» ПІСЛЯ складу — маркер наголосу StyleTTS2 (§4.2),
        # НЕ «плюс». Між кириличними літерами лишаємо «+» як є, щоб словник вимови
        # Хвилі 4 (U+0301) не з'їдався нормалізатором. НЕ читати як «плюс»!
        out = self._t("Коро+стень")
        self.assertNotIn("плюс", out)
        self.assertIn("+", out)

    def test_bad_time_not_crash(self):
        # некоректний час 99:99 — не падати (розкладеться як зможе)
        self.assertTrue(self._t("99:99"))


class TestSpanMap(unittest.TestCase):
    def test_expanded_number_single_raw_span(self):
        r = normalize("23", abbrev_map={})
        # увесь вихід «двадцять три» мапиться на один сирий діапазон [0,2)
        span = r.raw_span_at(0)
        self.assertEqual(span, (0, 2))
        self.assertEqual(r.raw_span_at(len(r.text) - 1), (0, 2))

    def test_literal_run_has_span(self):
        r = normalize("привіт", abbrev_map={})
        self.assertEqual(r.text, "привіт")
        self.assertEqual(r.raw_span_at(0), (0, 6))


if __name__ == "__main__":
    unittest.main()
