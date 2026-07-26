"""Smoke-рендер селектора мови розпізнавання — ОКРЕМИЙ процес (як render_nav_smoke).

Будує ЖИВУ сторінку Налаштувань і перевіряє вкладку «Розпізнавання»:
  • у селекторі мови є «Автоматично» (дата AUTO) плюс усі ~99 мов Whisper;
  • типовий cfg.language ("uk") коректно вибрано;
  • вибір мови справді кличе controller.set_language із її кодом.

Окремо від `unittest discover`, бо будуємо живі Qt-віджети (див. render_nav_smoke):
у спільному процесі offscreen-Qt під час static-деструкції давав флакі-крах.

    python -m unittest tests.render_multilang_smoke
    python tests/render_multilang_smoke.py
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

from whisper_core import languages as L
from whisper_core import profiles

from tests.render_nav_smoke import _NavController, _make_sandbox


class MultilangSelectorSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop import motion
        cls._app = QApplication.instance() or QApplication([])
        motion.init_config(SimpleNamespace(animations=False))
        cls._sandbox = _make_sandbox()
        cls._orig_list = profiles.list_profiles
        profiles.list_profiles = lambda root=None: cls._orig_list(cls._sandbox)

    @classmethod
    def tearDownClass(cls):
        profiles.list_profiles = cls._orig_list
        cls._flush(cls._app)

    @staticmethod
    def _flush(app):
        from PySide6.QtCore import QCoreApplication, QEvent
        for _ in range(3):
            app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()

    def setUp(self):
        self._page = None

    def tearDown(self):
        from PySide6.QtCore import QTimer
        page = self._page
        if page is not None:
            for t in page.findChildren(QTimer):
                try:
                    t.stop()
                except RuntimeError:
                    pass
            page.deleteLater()
        self._page = None
        self._flush(self._app)

    def _lang_combo(self, ctrl):
        from PySide6.QtWidgets import QComboBox
        from fronts.desktop.pages.settings import SettingsPage
        page = SettingsPage(ctrl)
        self._page = page
        for cb in page.findChildren(QComboBox):
            datas = [cb.itemData(i) for i in range(cb.count())]
            if L.AUTO in datas and "uk" in datas and "de" in datas:
                return cb
        self.fail("селектор мови розпізнавання не знайдено")

    def test_selector_lists_auto_and_all_languages(self):
        cb = self._lang_combo(_NavController(self._sandbox))
        datas = [cb.itemData(i) for i in range(cb.count())]
        self.assertEqual(datas[0], L.AUTO)                 # «Автоматично» першим
        self.assertEqual(cb.count(), len(L.LANGUAGES) + 1)  # усі мови + авто
        for code, *_ in L.LANGUAGES:
            self.assertIn(code, datas, code)

    def test_default_language_selected(self):
        ctrl = _NavController(self._sandbox)                # cfg.language == "uk"
        cb = self._lang_combo(ctrl)
        self.assertEqual(cb.currentData(), "uk")

    def test_choosing_language_calls_set_language(self):
        ctrl = _NavController(self._sandbox)
        calls = []
        ctrl.set_language = lambda code: calls.append(code)
        cb = self._lang_combo(ctrl)
        cb.setCurrentIndex(cb.findData("de"))
        self.assertEqual(calls, ["de"])
        cb.setCurrentIndex(cb.findData(L.AUTO))
        self.assertEqual(calls, ["de", L.AUTO])


if __name__ == "__main__":
    unittest.main()
