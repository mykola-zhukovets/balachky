"""Smoke-рендер toast «Звіт про проблему» з хабу «Про програму» — ОКРЕМИЙ процес.

Регресія (суд fix/ux-texts): _report_from_about() показував toast на сторінці
Налаштувань (self.settings). Коли хаб «Про програму» відкрито НЕ зі сторінки
Налаштувань, ця сторінка — прихована сторінка QStackedWidget, тож дочірній
QLabel toast'у не ставав видимим (show() на дитині прихованого віджета). Фікс:
toast показуємо на ВИДИМОМУ головному вікні (toast_target=self.window()).

Живий тест: головна сторінка — Диктування (НЕ Налаштування), викликаємо звіт,
перевіряємо win._toast.isVisible() == True (offscreen).

    python -m unittest tests.render_report_toast_smoke
    python tests/render_report_toast_smoke.py
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

from whisper_core import profiles

from tests.render_nav_smoke import _NavController, _make_sandbox


class ReportToastSmokeTests(unittest.TestCase):
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
                    pass
            try:
                win.close()
            except Exception:
                pass
            win.deleteLater()
        self._win = None
        self._flush_deferred(self._app)

    def test_about_report_toast_visible_from_three_current_pages(self):
        from fronts.desktop import report as report_mod
        from fronts.desktop import about as about_mod
        from fronts.desktop.i18n import tr
        from fronts.desktop.main_window import ClickableFrame, MainWindow, _PAGES
        from PySide6.QtWidgets import QPushButton

        # zip не пишемо на реальний Desktop — підміняємо збірку звіту
        orig_build = report_mod.build_report_zip
        orig_exec = about_mod.AboutDialog.exec
        report_mod.build_report_zip = lambda *a, **k: Path("C:/тест/report.zip")

        def _click_report(dialog):
            """Натиснути реальну кнопку хабу, не входячи в модальний event loop."""
            dialog.show()
            self._app.processEvents()
            buttons = [b for b in dialog.findChildren(QPushButton)
                       if b.accessibleName() == tr("set_report_problem")]
            self.assertEqual(len(buttons), 1, "у хабі нема кнопки звіту")
            buttons[0].click()
            self._app.processEvents()
            return dialog.result()

        about_mod.AboutDialog.exec = _click_report
        try:
            win = MainWindow(_NavController(self._sandbox))
            self._win = win
            win.show()                        # offscreen, але стан «видиме»
            headers = [f for f in win.findChildren(ClickableFrame)
                       if f.accessibleName() == tr("about_open")]
            self.assertEqual(len(headers), 1, "не знайдено клікабельну шапку")

            # Диктування / Аудіофайли («Записи») / Нарада: три різні сторінки,
            # жодна не є SettingsPage, на якій живе делегована логіка звіту.
            for page_key in ("nav_dictation", "nav_audio", "nav_meeting"):
                with self.subTest(page=page_key):
                    index = next(i for i, (_icon, key) in enumerate(_PAGES)
                                 if key == page_key)
                    win.set_page(index)
                    self._app.processEvents()
                    headers[0].clicked.emit()  # шапка → AboutDialog → кнопка звіту
                    self._app.processEvents()

                    self.assertEqual(win.pages.currentIndex(), index,
                                     "звіт несподівано перемкнув сторінку")
                    toast = getattr(win, "_toast", None)
                    self.assertIsNotNone(toast, "toast не створено")
                    self.assertIs(toast.parentWidget(), win,
                                  "toast причеплено до прихованої SettingsPage")
                    self.assertTrue(
                        toast.isVisible(),
                        f"toast невидимий зі сторінки {page_key}")
                    self.assertIn("report.zip", toast.text())
        finally:
            about_mod.AboutDialog.exec = orig_exec
            report_mod.build_report_zip = orig_build


if __name__ == "__main__":
    unittest.main()
