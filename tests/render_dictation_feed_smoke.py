"""Smoke-рендер стрічки диктування — ОКРЕМИЙ процес (конвенція render_*_smoke).

Регрес краху 21.07 на РІВНІ add_entry (краш жив у зв'язці main_window.add_entry
→ motion.smooth_scroll_to_end): швидка серія диктувань — N карток за одну
ітерацію event loop + N карток із подіями між ними — не сміє кидати
RuntimeError «Internal C++ object (QPropertyAnimation) already deleted».

Ключова відмінність від render_nav_smoke: анімації тут УВІМКНЕНІ (краш
відтворювався лише з ними) — wrap_appear і smooth_scroll_to_end живі, тож
teardown додатково добиває всі QAbstractAnimation, не тільки QTimer.

    python -m unittest tests.render_dictation_feed_smoke   # звичайний прогін
    python tests/render_dictation_feed_smoke.py            # standalone-раннер
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

# Віджети без екрана: рендеру потрібен QApplication, не реальний екран.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import shiboken6
from PySide6.QtCore import QAbstractAnimation

from whisper_core import profiles
from tests.render_nav_smoke import _NavController, _make_sandbox

_N = 8   # карток у кожній з двох фаз (burst + серія з подіями)


class DictationFeedBurstSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        # Анімації УВІМКНЕНІ — суть регресу; системний перемикач машини не вирішує
        motion.init_config(SimpleNamespace(animations=True))
        motion._system_ok = True
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
            # анімації тут живі — спершу добити їх, потім таймери (активний
            # таймер/анімація під час деструкції = флакі 0xC000041D)
            for a in win.findChildren(QAbstractAnimation):
                try:
                    a.stop()
                except RuntimeError:
                    pass                       # C++-частина вже знесена
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

    def _finish_running_animations(self, win):
        """Детерміновано добігти всі верхньорівневі анімації (DeleteWhenStopped
        самознищує); дітей груп не чіпаємо — ними керує група."""
        for a in win.findChildren(QAbstractAnimation):
            if (shiboken6.isValid(a) and a.group() is None
                    and a.state() == QAbstractAnimation.Running):
                a.setCurrentTime(a.duration())
        self._flush_deferred(self._app)

    def test_rapid_add_entry_never_crashes(self):
        """N add_entry за одну ітерацію event loop + N із подіями між ними:
        жодного RuntimeError, всі картки у стрічці, посилання на анімацію
        скролу після завершення знято (не звисає на мертвий C++ об'єкт)."""
        from fronts.desktop.main_window import MainWindow
        win = MainWindow(_NavController(self._sandbox))
        self._win = win
        win.resize(900, 700)
        win.show()
        self._app.processEvents()
        page = win.dictation                   # стрічка живе на DictationPage

        for i in range(_N):                    # фаза 1: burst без processEvents
            page.add_entry(f"фраза {i}", f"фраза {i}")
        self._app.processEvents()              # усі singleShot-_run поспіль
        for i in range(_N, 2 * _N):            # фаза 2: серія з подіями між
            page.add_entry(f"фраза {i}", f"фраза {i}")
            self._app.processEvents()

        self._finish_running_animations(win)

        # всі 2N карток у стрічці (+1 — stretch наприкінці feedbox)
        self.assertEqual(page._feedbox.count(), 2 * _N + 1,
                         "не всі картки дожили до стрічки")
        # по finished посилання знято — корінь краху 21.07 не повернувся
        self.assertIsNone(getattr(page._scroll, "_scroll_anim", None),
                          "звисає посилання на завершену анімацію скролу")


if __name__ == "__main__":
    # Standalone-раннер: після ВЕРДИКТУ виходимо os._exit — навмисно обходимо
    # нативну static-деструкцію offscreen-Qt (джерело флакі-0xC000041D після «OK»).
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(DictationFeedBurstSmoke))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
