"""Smoke-регрес кластера «злипнуті контроли + нульові відступи + фікс. ширини»
на ЖИВИХ сторінках (ScreenPage / SettingsPage / InlinePlayer) — ОКРЕМИЙ процес,
як render_nav_smoke: у сторінок є QTimer (screen _tick, mic-test тощо), і в
спільному наборі недобитий Qt-таймер під час static-деструкції offscreen-Qt дає
флакі-краш 0xC000041D. Тут teardown жорсткий: спиняємо всі QTimer, close →
deleteLater → флаш.

Перевіряє конкретні дефекти живого тесту Миколи:
- Запис екрана: число FPS біля повзунка має зарезервований 2-цифровий слот
  (не ріжеться правим краєм); статус-ряд знизу має відступ >=10px (не «впритул»);
- Налаштування → Система: рядок логів розбито на два ряди зі spacing>=10px
  (кнопка «Відкрити папку логів» більше не впритул до «Рівень логування»);
- Плеєр: кнопка швидкості вміщує «1,25×» (ширина від fontMetrics, не фікс. 62px).

    python -m unittest tests.render_layout_smoke
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtWidgets import QHBoxLayout, QPushButton

from fronts.desktop.i18n import tr
from tests.render_nav_smoke import _NavController, _make_sandbox


def _layout_containing(page, widget):
    """QHBoxLayout, який ПРЯМО тримає widget (для перевірки spacing/розбиття)."""
    for lay in page.findChildren(QHBoxLayout):
        if lay.indexOf(widget) != -1:
            return lay
    return None


class LayoutSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))  # без живих таймерів
        cls._sandbox = _make_sandbox()

    @classmethod
    def tearDownClass(cls):
        try:
            from fronts.desktop import glass
            glass._TAG_DRIVER._timer.stop()
            glass._TAG_DRIVER._pills.clear()
        except Exception:
            pass
        cls._flush(cls._app)

    @staticmethod
    def _flush(app):
        from PySide6.QtCore import QCoreApplication, QEvent
        for _ in range(3):
            app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()

    def setUp(self):
        self._pages = []

    def tearDown(self):
        from PySide6.QtCore import QTimer
        for page in self._pages:
            for t in page.findChildren(QTimer):
                try:
                    t.stop()
                except RuntimeError:
                    pass
            try:
                page.close()
            except Exception:
                pass
            page.deleteLater()
        self._pages = []
        self._flush(self._app)

    def _controller(self):
        return _NavController(self._sandbox)

    def _track(self, page):
        self._pages.append(page)
        return page

    # ── Запис екрана: FPS-слот і статус-ряд ─────────────────────────────
    def test_screen_fps_reserves_two_digit_slot(self):
        """Дизайн-хвиля Т48: інлайн-підпис замінено чіпом «Кадри: NN» —
        перевіряємо, що чіп вміщає двоцифрове значення без обрізання."""
        from fronts.desktop.i18n import tr
        from fronts.desktop.pages.screen import ScreenPage
        page = self._track(ScreenPage(self._controller()))
        chip = page._fps
        fm = chip.fontMetrics()
        widest = tr("screen_fps_chip", value=60)
        self.assertGreaterEqual(
            chip.sizeHint().width(), fm.horizontalAdvance(widest),
            "чіп кадрів не вміщає двоцифрове значення → «30»/«60» ріже правий край")

    def test_screen_status_row_has_spacing(self):
        from fronts.desktop.pages.screen import ScreenPage
        page = self._track(ScreenPage(self._controller()))
        row = _layout_containing(page, page._badge)
        self.assertIsNotNone(row, "не знайдено статус-ряд із бейджем")
        self.assertGreaterEqual(row.spacing(), 10,
                                "статус-ряд «впритул» (spacing<10px)")

    # ── Налаштування → Система: рядок логів розбито й з відступами ───────
    def test_settings_logs_row_split_and_spaced(self):
        from fronts.desktop.pages.settings import SettingsPage
        page = self._track(SettingsPage(self._controller()))
        level_row = _layout_containing(page, page._log_level)
        self.assertIsNotNone(level_row, "не знайдено ряд вибору рівня логів")
        self.assertGreaterEqual(level_row.spacing(), 10,
                                "ряд рівня логів без відступів (spacing<10px)")
        # «Відкрити папку логів» має бути в ІНШОМУ ряду (не впритул до рівня)
        logs_btn = next((b for b in page.findChildren(QPushButton)
                         if b.text() == tr("set_open_logs")), None)
        self.assertIsNotNone(logs_btn, "не знайдено кнопку «Відкрити папку логів»")
        self.assertEqual(level_row.indexOf(logs_btn), -1,
                         "кнопка логів у тому ж ряду, що й рівень — не розбито")

    # ── Налаштування → Запис: чекбокс живої розшифровки (перенос із Наради) ──
    def test_live_transcription_checkbox_lives_in_dictation_settings(self):
        """Аудит Миколи 22.07: чекбокс «Жива розшифровка» перенесено з Наради
        (де він був чужим — стосується диктування) у Налаштування → Запис. Тут
        він має бути: у дереві сторінки, з accessibleName, і перемикатись."""
        from PySide6.QtWidgets import QCheckBox
        from fronts.desktop.pages.settings import SettingsPage
        page = self._track(SettingsPage(self._controller()))
        check = page._live_transcription
        self.assertIsInstance(check, QCheckBox)
        self.assertTrue(check.accessibleName(), "чекбокс без accessibleName")
        self.assertIn(check, page.findChildren(QCheckBox),
                      "чекбокс не в дереві сторінки Налаштування")
        before = check.isChecked()
        check.click()                            # сигнал → controller.set_live_transcription
        self.assertNotEqual(check.isChecked(), before)

    # ── Плеєр: кнопка швидкості вміщує «1,25×» (не фікс. 62px) ───────────
    def test_player_speed_button_fits_label(self):
        from fronts.desktop import player as player_mod
        if player_mod.QMediaPlayer is None:      # QtMultimedia недоступна
            self.skipTest("QtMultimedia недоступна в цьому середовищі")
        page = self._track(player_mod.InlinePlayer())
        fm = page._speed_btn.fontMetrics()
        widest = max(fm.horizontalAdvance(t) for t in ("1,25×", "0,75×"))
        self.assertGreaterEqual(
            page._speed_btn.minimumWidth(), widest,
            "кнопка швидкості ріже «1,25×» (фікс. ширина < fontMetrics)")


if __name__ == "__main__":
    unittest.main()
