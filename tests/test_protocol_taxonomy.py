"""feature/protocol-enrich: таксономія рішень і клікабельні розділи наради.

Чисті функції парсера/рендеру — без Qt і без реальної моделі (фейк-бекенд для
end-to-end генерації). Дзеркалить стиль test_protocol_generate.py."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.protocol import generate as gen
from whisper_core.meeting.postprocess import Utterance

# Протокол «як від LLM»: рішення з усіма 4 статусами + одне без позначки.
_RAW = """## Підсумок
Робоча нарада.

## Рішення
- [Узгоджено] Затвердити план на тиждень.
- [Потребує обговорення] Бюджет на техніку.
- [Відхилено] Переносити склад.
- [Відкладено] Ротація особового складу.
- Призначити чергового (без статусу).

## Задачі
| Хто | Що | Термін | Час у записі |
|-----|-----|--------|--------------|
| Я | Скласти план | завтра | 00:00 |

## Розділи наради
- [00:00–00:30] Вступ
- [00:30–01:10] Обговорення бюджету
- [01:10-02:00] Підсумки"""


def _fake_generate(prompt, *, max_tokens=2048, n_ctx=None):
    return _RAW


class TestDecisionParsing(unittest.TestCase):
    def test_all_four_statuses_and_none(self):
        decisions = gen.parse_decisions(_RAW)
        self.assertEqual(decisions, [
            ("Узгоджено", "Затвердити план на тиждень."),
            ("Потребує обговорення", "Бюджет на техніку."),
            ("Відхилено", "Переносити склад."),
            ("Відкладено", "Ротація особового складу."),
            (None, "Призначити чергового (без статусу)."),
        ])

    def test_status_case_insensitive(self):
        text = "## Рішення\n- [узгоджено] Ок."
        self.assertEqual(gen.parse_decisions(text), [("Узгоджено", "Ок.")])

    def test_unknown_bracket_is_none_with_full_text(self):
        text = "## Рішення\n- [Невідомо] Щось."
        self.assertEqual(gen.parse_decisions(text), [(None, "[Невідомо] Щось.")])

    def test_no_decisions_section(self):
        self.assertEqual(gen.parse_decisions("## Підсумок\nОк."), [])


class TestDecisionRender(unittest.TestCase):
    def test_grouped_by_status_order_with_badges(self):
        decisions = [
            ("Відхилено", "Б"),
            ("Узгоджено", "А"),
            (None, "В"),
        ]
        out = gen.render_decisions(decisions)
        # Узгоджено йде раніше за Відхилено (канонний порядок), без-статусні — вкінці
        self.assertEqual(out.splitlines(), [
            "- **[Узгоджено]** А",
            "- **[Відхилено]** Б",
            "- В",
        ])


class TestApplyTaxonomy(unittest.TestCase):
    def test_regroups_and_keeps_valid(self):
        out = gen.apply_decision_taxonomy(_RAW)
        self.assertTrue(gen.is_valid_protocol(out))
        # інші секції на місці
        self.assertIn("## Підсумок", out)
        self.assertIn("## Задачі", out)
        self.assertIn("## Розділи наради", out)
        # badge-позначки з'явилися
        self.assertIn("- **[Узгоджено]** Затвердити план на тиждень.", out)
        self.assertIn("- **[Відкладено]** Ротація особового складу.", out)
        # без-статусне лишилось як звичайний пункт
        self.assertIn("- Призначити чергового (без статусу).", out)
        # порядок: Узгоджено раніше за Потребує обговорення
        self.assertLess(out.index("Затвердити план"), out.index("Бюджет на техніку"))

    def test_no_section_unchanged(self):
        text = "## Підсумок\nЛише підсумок."
        self.assertEqual(gen.apply_decision_taxonomy(text), text)

    def test_end_to_end_generate_applies_taxonomy(self):
        us = [Utterance(0.0, 3.0, "me", "привіт", source="mic")]
        out = gen.generate_protocol(us, _fake_generate)
        self.assertTrue(gen.is_valid_protocol(out))
        self.assertIn("- **[Узгоджено]** Затвердити план на тиждень.", out)


class TestChapterParsing(unittest.TestCase):
    def test_parses_ranges_titles_and_seconds(self):
        chapters = gen.parse_chapters(_RAW)
        self.assertEqual(chapters, [
            (0, 30, "Вступ"),
            (30, 70, "Обговорення бюджету"),
            (70, 120, "Підсумки"),          # дефіс-роздільник теж
        ])

    def test_single_bound_and_hhmmss(self):
        text = ("## Розділи наради\n"
                "- [05:00] Одна межа\n"
                "- [01:00:00–01:02:30] Година з чимось")
        self.assertEqual(gen.parse_chapters(text), [
            (300, None, "Одна межа"),
            (3600, 3750, "Година з чимось"),
        ])

    def test_skips_invalid_lines(self):
        text = ("## Розділи наради\n"
                "- просто текст без дужок\n"
                "- [хх:уу] сміття\n"
                "- [00:15] Валідний")
        self.assertEqual(gen.parse_chapters(text), [(15, None, "Валідний")])

    def test_no_chapters_section(self):
        self.assertEqual(gen.parse_chapters("## Підсумок\nОк."), [])


if __name__ == "__main__":
    unittest.main()
