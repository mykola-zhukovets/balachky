"""Click-through усіх 7 пунктів навігації РЕАЛЬНИМ кліком миші — ОКРЕМИЙ процес.

Різниця з render_nav_smoke: там перемикання через `win.set_page(i)` (програмно).
Тут — `QTest.mouseClick` по САМІЙ кнопці сайдбара, як робить рука Миколи, і
перевіряємо, що відкрилась саме її сторінка (правильний індекс І правильний
клас віджета). Це Рівень 2.1 гейта: ловить розрив «кнопка N → чужа сторінка»
навіть якщо він у слоті кліку, а не в порядку стека.

Файл НЕ підхоплюється `unittest discover -s tests` (патерн `test*.py`), як і
решта render_*_smoke — живе MainWindow з таймерами тримаємо в окремому процесі.

    python -m unittest tests.render_clickthrough_smoke     # звичайний прогін
    python -m unittest discover -s tests -p "render_*.py"  # discover-варіант
    python tests/render_clickthrough_smoke.py              # standalone-раннер

Teardown жорсткий (як render_nav_smoke): спиняємо всі QTimer вікна, потім
close -> deleteLater -> флаш DeferredDelete — захист від флакі-0xC000041D під
час static-деструкції offscreen-Qt.
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

# Віджети без екрана: рендеру потрібен QApplication, не реальний екран.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# standalone-запуск: корінь репо у sys.path (щоб імпортувати tests.render_nav_smoke)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from whisper_core import profiles
# DRY: фейк-контролер і пісочниця вже є в render_nav_smoke — переюзовуємо їх.
from tests.render_nav_smoke import _NavController, _make_sandbox


class ClickthroughSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()                          # як у реальному main(): ДО QSS
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))  # без живих таймерів
        cls._sandbox = _make_sandbox()
        cls._orig_list = profiles.list_profiles
        profiles.list_profiles = lambda root=None: cls._orig_list(cls._sandbox)

    @classmethod
    def tearDownClass(cls):
        profiles.list_profiles = cls._orig_list
        try:
            from fronts.desktop import glass
            glass._TAG_DRIVER._timer.stop()
            glass._TAG_DRIVER._pills.clear()
        except Exception:
            pass
        cls._flush_deferred(cls._app)

    @staticmethod
    def _flush_deferred(app):
        from PySide6.QtCore import QCoreApplication, QEvent
        for _ in range(3):
            app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()

    def setUp(self):
        self._win = None

    def tearDown(self):
        from PySide6.QtCore import QTimer
        win = self._win
        if win is not None:
            for t in win.findChildren(QTimer):
                try:
                    t.stop()
                except RuntimeError:
                    pass                       # C++-частина вже знесена
            try:
                win.close()
            except Exception:
                pass
            win.deleteLater()
        self._win = None
        self._flush_deferred(self._app)

    def _window(self):
        from fronts.desktop.main_window import MainWindow
        win = MainWindow(_NavController(self._sandbox))
        self._win = win
        win.resize(1000, 640)
        win.show()
        for _ in range(3):
            self._app.processEvents()
        return win

    def test_each_button_click_opens_its_page(self):
        from fronts.desktop.main_window import _PAGES, DictationPage, FilesPage
        from fronts.desktop.pages.meeting import MeetingPage
        from fronts.desktop.pages.screen import ScreenPage
        from fronts.desktop.pages.history import HistoryPage
        from fronts.desktop.pages.vocab import VocabPage
        from fronts.desktop.pages.settings import SettingsPage
        from fronts.desktop.pages.search import SearchPage

        expected = [DictationPage, FilesPage, MeetingPage, ScreenPage,
                    HistoryPage, VocabPage, SettingsPage, SearchPage]
        self.assertEqual(len(expected), len(_PAGES))

        win = self._window()

        # спершу відведемо активну сторінку від 0, щоб клік по 0 теж щось «робив»
        for i in range(len(_PAGES)):
            btn = win._nav.button(i)
            self.assertIsNotNone(btn, f"нема кнопки навігації №{i}")
            QTest.mouseClick(btn, Qt.LeftButton)   # РЕАЛЬНИЙ клік по кнопці сайдбара
            self._app.processEvents()

            self.assertEqual(
                win.pages.currentIndex(), i,
                f"клік по кнопці №{i} відкрив індекс {win.pages.currentIndex()}")
            current = win.pages.currentWidget()
            self.assertIsInstance(
                current, expected[i],
                f"кнопка №{i} відкрила {type(current).__name__}, "
                f"очікували {expected[i].__name__}")


if __name__ == "__main__":
    # Standalone-раннер: після ВЕРДИКТУ виходимо os._exit — навмисно обходимо
    # нативну static-деструкцію offscreen-Qt (джерело флакі-0xC000041D ПІСЛЯ «OK»).
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(ClickthroughSmokeTests))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
