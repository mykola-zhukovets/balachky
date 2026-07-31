"""Live Qt coverage for language-specific brand widgets and About consumers."""
import os
import re
import shutil
import sys
import unittest
from html import unescape
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QLabel

from fronts.desktop import motion
from fronts.desktop.i18n import set_language
from fronts.desktop.theme import QSS, load_fonts
from tests.render_nav_smoke import _NavController, _make_sandbox
from whisper_core import profiles


EXPECTED_ABOUT_LEAD = {
    "en": "“Balachky” turns your voice into text.",
    "uk": "“Балачки у Коростені” перетворюють Ваш голос на текст.",
}


class BrandRuntimeSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))
        cls._sandbox = _make_sandbox()
        cls._orig_list = profiles.list_profiles
        profiles.list_profiles = lambda root=None: cls._orig_list(cls._sandbox)

    @classmethod
    def tearDownClass(cls):
        profiles.list_profiles = cls._orig_list
        set_language("uk")
        try:
            from fronts.desktop import glass
            glass._TAG_DRIVER._timer.stop()
            glass._TAG_DRIVER._pills.clear()
        except Exception:
            pass
        cls._flush_deferred()
        sandbox = cls._sandbox
        shutil.rmtree(sandbox)
        if sandbox.exists():
            raise AssertionError(f"runtime sandbox was not removed: {sandbox}")

    @classmethod
    def _flush_deferred(cls):
        for _ in range(3):
            cls._app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        cls._app.processEvents()

    def setUp(self):
        self._widgets = []

    def tearDown(self):
        for widget in reversed(self._widgets):
            for timer in widget.findChildren(QTimer):
                try:
                    timer.stop()
                except RuntimeError:
                    pass
            widget.close()
            widget.deleteLater()
        self._widgets.clear()
        self._flush_deferred()

    def _window(self, language):
        from fronts.desktop.main_window import MainWindow

        set_language(language)
        window = MainWindow(_NavController(self._sandbox))
        self._widgets.append(window)
        window.show()
        self._app.processEvents()
        return window

    @staticmethod
    def _visible_exact_labels(widget, expected):
        return [
            label
            for label in widget.findChildren(QLabel)
            if label.isVisible()
            and unescape(re.sub(r"<[^>]+>", "", label.text())) == expected
        ]

    def test_runtime_sandbox_exists_during_suite(self):
        self.assertTrue(self._sandbox.is_dir())
        self.assertTrue((self._sandbox / "profiles").is_dir())

    def test_english_sidebar_has_no_logosub_widget(self):
        window = self._window("en")
        logosub = [
            label.text()
            for label in window.findChildren(QLabel)
            if label.property("logosub")
        ]
        self.assertEqual(logosub, [])

    def test_ukrainian_sidebar_has_one_localized_logosub_widget(self):
        window = self._window("uk")
        logosub = [
            label.text()
            for label in window.findChildren(QLabel)
            if label.property("logosub")
        ]
        self.assertEqual(logosub, ["у Коростені"])

    def test_english_settings_about_tab_uses_exact_lead(self):
        """Модальне вікно «Про програму» прибрано (30.07) — той самий вступний
        рядок тепер живе на вкладці «Про програму» Налаштувань."""
        from fronts.desktop.i18n import tr
        window = self._window("en")
        window.set_page(window.pages.indexOf(window.settings))
        about_idx = next(i for i in range(window.settings._tabs.count())
                         if window.settings._tabs.tabText(i) == tr("set_tab_about"))
        window.settings._tabs.setCurrentIndex(about_idx)
        self._app.processEvents()
        matches = self._visible_exact_labels(
            window.settings,
            EXPECTED_ABOUT_LEAD["en"],
        )
        self.assertEqual(len(matches), 1)

    def test_ukrainian_settings_about_tab_uses_exact_lead(self):
        from fronts.desktop.i18n import tr
        window = self._window("uk")
        window.set_page(window.pages.indexOf(window.settings))
        about_idx = next(i for i in range(window.settings._tabs.count())
                         if window.settings._tabs.tabText(i) == tr("set_tab_about"))
        window.settings._tabs.setCurrentIndex(about_idx)
        self._app.processEvents()
        matches = self._visible_exact_labels(
            window.settings,
            EXPECTED_ABOUT_LEAD["uk"],
        )
        self.assertEqual(len(matches), 1)


if __name__ == "__main__":
    unittest.main()
