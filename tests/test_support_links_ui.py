"""Видимі точки входу до підтримки автора, контакти на вкладці «Про
програму» і чесність картки «Голоси озвучення».

Три зауваження власника:
  1. Рядок підтримки в Налаштуваннях мусить вести й до гаманців — не лише до
     трьох банківських посилань (гаманець у QLabel-лінку не працює за природою).
  2. Вкладка «Про програму» (модальне вікно прибрано 30.07) мусить мати
     кнопку підтримки й ДВА контакти автора (GitHub + X). Пошту не показуємо
     — рішення власника.
  3. Картка озвучення в збірці БЕЗ рушія не має видавати активний голос за
     робочий стан і не має пропонувати активацію.
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication, QLabel, QToolButton

from fronts.desktop import links
from fronts.desktop.i18n import current_language, set_language, tr
from fronts.desktop.main_window import MainWindow
from tests.render_nav_smoke import _NavController

_APP = QApplication.instance() or QApplication([])


class TestSettingsSupportRow(unittest.TestCase):
    """Рядок підтримки в картці «Автор і підтримка» вкладки «Про програму»."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.controller = _NavController(self.tmp_dir)
        self.win = MainWindow(self.controller)

    def tearDown(self):
        self.win.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_support_row_opens_support_menu(self):
        """Поруч із валютними посиланнями є елемент, який відкриває меню
        підтримки — інакше гаманці з цього рядка недосяжні."""
        btn = self.win.settings.findChild(QToolButton, "aboutSupportMoreBtn")
        self.assertIsNotNone(
            btn, "у рядку підтримки нема елемента виходу в меню підтримки")
        self.assertTrue(btn.accessibleName())
        with patch("fronts.desktop.pages.settings.show_support_menu") as mock_menu:
            btn.click()
        mock_menu.assert_called_once()


class TestAboutTab(unittest.TestCase):
    """Вкладка «Про програму»: підтримка + контакти автора (замінила
    модальне вікно AboutDialog, прибране 30.07)."""

    def setUp(self):
        self._language = current_language()
        self.addCleanup(set_language, self._language)
        set_language("uk")
        self.tmp_dir = tempfile.mkdtemp()
        self.controller = _NavController(self.tmp_dir)
        self.win = MainWindow(self.controller)
        self.win.settings.select_about_tab()

    def tearDown(self):
        self.win.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_support_button_opens_same_menu(self):
        btn = self.win.settings.findChild(QToolButton, "aboutSupportMoreBtn")
        self.assertIsNotNone(btn, "на вкладці нема кнопки підтримки автора")
        with patch("fronts.desktop.pages.settings.show_support_menu") as mock_menu:
            btn.click()
        mock_menu.assert_called_once()

    def test_both_contacts_present(self):
        gh = self.win.settings.findChild(QToolButton, "authorGithubLink")
        x_link = self.win.settings.findChild(QToolButton, "authorXLink")
        self.assertIsNotNone(gh, "нема контакту GitHub")
        self.assertIsNotNone(x_link, "нема контакту X (Twitter)")
        self.assertEqual(gh.property("linkUrl"), links.GITHUB_URL)
        self.assertEqual(x_link.property("linkUrl"), links.X_URL)

    def test_no_email_shown(self):
        """Пошту на вкладці не показуємо (рішення власника)."""
        html = " ".join(w.text() for w in self.win.settings.findChildren(QLabel))
        self.assertNotIn("mailto:", html)
        self.assertNotIn("@gmail", html)


class TestTtsCardHonesty(unittest.TestCase):
    """Картка «Голоси озвучення» у Центрі керування моделями."""

    def setUp(self):
        self._language = current_language()
        self.addCleanup(set_language, self._language)
        set_language("uk")
        self.tmp_dir = tempfile.mkdtemp()
        self.controller = _NavController(self.tmp_dir)

    def tearDown(self):
        self.win.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _row(self, engine_available):
        with patch("fronts.desktop.pages.settings._tts_engine_available",
                   return_value=engine_available):
            self.win = MainWindow(self.controller)
            return self.win.settings._models_hub_rows["tts"]

    def test_no_active_voice_without_engine(self):
        """Нема рушія → замість «Активна: Українська (StyleTTS2)» чесний підпис
        про окреме завантаження, і кнопки активації нема."""
        row = self._row(False)
        text = row["active_lbl"].text()
        self.assertEqual(text, tr("models_hub_tts_engine_absent"))
        self.assertNotIn(tr("models_hub_preset_styletts2"), text)
        self.assertNotIn(tr("models_hub_active_label", name=""), text)
        self.assertIsNone(row["btn_rec"],
                          "без рушія кнопку активації пропонувати не можна")

    def test_refresh_keeps_honest_label(self):
        """Перемальовування центру не повертає активний голос назад."""
        row = self._row(False)
        self.win.settings._refresh_models_hub()
        self.assertEqual(row["active_lbl"].text(),
                         "Озвучення не входить у цю збірку — його потрібно "
                         "буде завантажити окремо.")

    def test_active_voice_shown_when_engine_present(self):
        """Є рушій → картка працює як раніше (активний голос + активація)."""
        row = self._row(True)
        self.assertIn("Українська (StyleTTS2)", row["active_lbl"].text())
        self.assertIsNotNone(row["btn_rec"])


if __name__ == "__main__":
    unittest.main()
