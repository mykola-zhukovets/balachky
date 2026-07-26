"""Хвиля UX-текстів (№7/№12/№13/№14): перевірки на рівні i18n без живих віджетів.

Живі-віджетні перевірки (accessibleName на нових контролях, розташування кнопки
«Налаштування», лінк «Про модель») — у tests/render_nav_smoke.py, бо потребують
MainWindow/MeetingPage в окремому процесі (канон teardown offscreen-Qt).
"""
import unittest

from fronts.desktop.i18n import STRINGS

UK = STRINGS["uk"]
EN = STRINGS["en"]

# Нові ключі цієї хвилі — мусять бути в ОБОХ мовах (дублює гарантію parity, але
# явно фіксує намір: забутий ключ провалить саме цей тест із зрозумілою назвою).
_NEW_KEYS = [
    "about_title", "about_open", "about_version_line", "about_github",
    "about_net_note", "about_thanks", "net_link_hint",
    "protocol_model_about", "protocol_model_about_name",
]


class NewKeysBothLanguages(unittest.TestCase):
    def test_present_in_uk_and_en(self):
        for key in _NEW_KEYS:
            self.assertIn(key, UK, f"нема UK-рядка: {key}")
            self.assertIn(key, EN, f"нема EN-рядка: {key}")


class ObsidianHintPlainLanguage(unittest.TestCase):
    def test_uk_first_sentence_explains_and_offers_skip(self):
        hint = UK["set_obsidian_hint"]
        self.assertTrue(
            hint.startswith("Obsidian — безкоштовна програма для особистих "
                            "нотаток; якщо Ви нею не користуєтеся — цю секцію "
                            "можна пропустити"),
            f"перше речення хінта не пояснює/не пропонує пропустити: {hint[:80]!r}")

    def test_en_first_sentence_explains_and_offers_skip(self):
        hint = EN["set_obsidian_hint"]
        self.assertTrue(
            hint.startswith("Obsidian is a free app for personal notes; if you "
                            "don’t use it, you can skip this section"),
            f"EN first sentence off: {hint[:80]!r}")


class NetLinkMarkedTexts(unittest.TestCase):
    def test_external_links_have_no_decorative_arrows(self):
        """Стрілок ↗ у видимих рядках більше немає (вказівка власника 25.07).

        Були як маркер «відкриється назовні», але власник попросив прибрати:
        посилання й так підкреслені й кольорові, а стрілка додавала шуму.
        Тест перевернуто — тепер він стереже ВІДСУТНІСТЬ стрілок, щоб вони
        не повернулись непомітно у майбутніх правках текстів."""
        for lang in ("uk", "en"):
            for key, text in STRINGS[lang].items():
                if isinstance(text, str):
                    self.assertNotIn("↗", text,
                                     f"стрілка повернулась у рядок {key} ({lang})")


if __name__ == "__main__":
    unittest.main()
