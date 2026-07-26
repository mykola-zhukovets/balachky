"""Фідбек Миколи 21-22.07: «гортати» (гортати слайди) розпізнавалось як
«гордати»/«гартати». Слово додано у стандартний словник terms.toml як БІАСИНГ
(канон без варіантів) — воно підмішується в hotwords + initial_prompt Whisper й
нуджить модель ДО транскрипції. БЕЗ автозаміни: «гартати» (гартувати метал) —
легітимне слово, його чіпати не можна.
"""
import unittest
from pathlib import Path

from whisper_core import terms

_TERMS = Path(__file__).resolve().parent.parent / "terms.toml"


class GortatyBiasTests(unittest.TestCase):
    def setUp(self):
        self.terms = terms.load_terms(_TERMS)

    def test_gortaty_is_in_biasing(self):
        """«гортати» присутнє у hotwords і initial_prompt (нудж моделі)."""
        self.assertIn("гортати", self.terms.hotwords)
        self.assertIn("гортати", self.terms.initial_prompt)

    def test_gartaty_not_auto_replaced(self):
        """«гартати» НЕ замінюється на «гортати» — це легітимне слово."""
        self.assertEqual(
            terms.apply_glossary("я хочу гартати метал", self.terms),
            "я хочу гартати метал")


if __name__ == "__main__":
    unittest.main()
