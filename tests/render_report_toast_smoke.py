"""Smoke-рендер toast «Звіт про проблему» з вкладки «Про програму» — ОКРЕМИЙ
процес.

Було (до 30.07): звіт викликався з модального хабу AboutDialog, який міг
бути відкритий з будь-якої сторінки, тож toast показувався на видимому
головному вікні (регресія рецензії fix/ux-texts — toast_target=self.window()).

Стало: модальне вікно прибрано. Клік по шапці сайдбара переводить на
сторінку Налаштування → вкладку «Про програму» (settings.select_about_tab).
Кнопка звіту звідти тепер завжди клікається лише коли SettingsPage вже є
видимою сторінкою — тож toast показується прямо на ній
(_report_problem більше не приймає toast_target). Тест стереже: клік по
шапці з НЕ-налаштувань сторінки веде саме на вкладку «Про програму», і
звідти toast видимий.

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

    def _confirm_report_dialog(self):
        """Натиснути «Створити звіт» на модальному діалозі підтвердження, щойно
        він з'явиться (QTimer.singleShot(0, …) стріляє з наступною ітерацією
        event loop — саме тоді, коли dlg.exec() уже блокує потік)."""
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QMessageBox
        from fronts.desktop.i18n import tr

        def _click_ok():
            for w in QApplication.topLevelWidgets():
                if isinstance(w, QMessageBox) and w.isVisible():
                    for btn in w.buttons():
                        if btn.text().replace("&", "") == tr("set_report_confirm_ok"):
                            btn.click()
                            return

        QTimer.singleShot(0, _click_ok)

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

    def test_header_click_from_other_page_opens_about_tab_and_report_toasts(self):
        from fronts.desktop import report as report_mod
        from fronts.desktop.i18n import tr
        from fronts.desktop.main_window import ClickableFrame, MainWindow, _PAGES
        from PySide6.QtWidgets import QPushButton

        # zip не пишемо на реальний Desktop — підміняємо збірку звіту
        orig_build = report_mod.build_report_zip
        report_mod.build_report_zip = lambda *a, **k: Path("C:/тест/report.zip")

        try:
            win = MainWindow(_NavController(self._sandbox))
            self._win = win
            win.show()                        # offscreen, але стан «видиме»

            # починаємо НЕ з Налаштувань — з Диктування
            dict_index = next(i for i, (_icon, key) in enumerate(_PAGES)
                              if key == "nav_dictation")
            win.set_page(dict_index)
            self._app.processEvents()

            headers = [f for f in win.findChildren(ClickableFrame)
                       if f.accessibleName() == tr("about_open")]
            self.assertEqual(len(headers), 1, "не знайдено клікабельну шапку")
            headers[0].clicked.emit()
            self._app.processEvents()

            self.assertEqual(
                win.pages.currentIndex(), win.pages.indexOf(win.settings),
                "клік по шапці не перевів на сторінку Налаштувань")
            self.assertEqual(
                win.settings._tabs.currentIndex(), win.settings._tabs.count() - 1,
                "клік по шапці не відкрив вкладку «Про програму» (мала бути "
                "остання вкладка)")
            self.assertEqual(
                win.settings._tabs.tabText(win.settings._tabs.currentIndex()),
                tr("set_tab_about"))

            buttons = [b for b in win.settings.findChildren(QPushButton)
                       if b.accessibleName() == tr("set_report_problem")
                       and b.isVisible()]
            self.assertEqual(
                len(buttons), 1,
                "мала бути рівно одна видима кнопка звіту — на поточній вкладці")
            # fix/log-privacy: клік тепер спершу відкриває модальний діалог
            # підтвердження (чесний опис вмісту архіву) — авто-натискаємо
            # «Створити звіт» одразу після появи діалогу, інакше exec()
            # заблокує тест.
            self._confirm_report_dialog()
            buttons[0].click()
            self._app.processEvents()

            self.assertEqual(win.pages.currentIndex(),
                             win.pages.indexOf(win.settings),
                             "звіт несподівано перемкнув сторінку")
            toast = getattr(win.settings, "_toast", None)
            self.assertIsNotNone(toast, "toast не створено")
            self.assertTrue(toast.isVisible(),
                            "toast невидимий на вкладці «Про програму»")
            self.assertIn("report.zip", toast.text())
        finally:
            report_mod.build_report_zip = orig_build

    def test_about_tab_help_button_present_and_wired(self):
        """Кнопка «Довідка» на вкладці «Про програму» під'єднана до тієї самої
        дії, що й діагностика вкладки «Система» (без дублювання логіки)."""
        from fronts.desktop.i18n import tr
        from fronts.desktop.main_window import MainWindow
        from PySide6.QtWidgets import QPushButton

        win = MainWindow(_NavController(self._sandbox))
        self._win = win
        win.show()
        win.set_page(win.pages.indexOf(win.settings))
        win.settings.select_about_tab()
        self._app.processEvents()

        help_buttons = [b for b in win.settings.findChildren(QPushButton)
                        if b.accessibleName() == tr("set_help")
                        and b.isVisible()]
        self.assertEqual(len(help_buttons), 1,
                         "мала бути рівно одна видима кнопка довідки")


if __name__ == "__main__":
    unittest.main()
