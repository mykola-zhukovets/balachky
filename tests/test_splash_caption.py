"""Фідбек Миколи 21.07: підпис «ПРОКИДАННЯ»/«WAKING UP» на статичному splash не
в тему. Має бути нейтральне «Завантаження…»/«Loading…» (з трикрапкою U+2026)."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fronts.desktop import i18n
from fronts.desktop.i18n import tr


class SplashCaptionTests(unittest.TestCase):
    def setUp(self):
        self._lang = i18n.current_language()

    def tearDown(self):
        i18n.set_language(self._lang)

    def test_uk_caption_is_neutral_loading(self):
        i18n.set_language("uk")
        self.assertEqual(tr("splash_eyebrow"), "Завантаження…")
        self.assertNotIn("ПРОКИД", tr("splash_eyebrow"))

    def test_en_caption_is_neutral_loading(self):
        i18n.set_language("en")
        self.assertEqual(tr("splash_eyebrow"), "Loading…")
        self.assertNotIn("WAKING", tr("splash_eyebrow"))


if __name__ == "__main__":
    unittest.main()
