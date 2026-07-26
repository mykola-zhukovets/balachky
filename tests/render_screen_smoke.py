"""Offscreen-рендер сторінки «Запис екрана» — ОКРЕМИЙ процес (як
render_meeting_smoke): сторінка має живий QTimer (_tick 1s), тож тримаємо її
поза спільним `unittest discover`, а таймери жорстко спиняємо у teardown, щоб
не було 0xC000041D на нативній деструкції offscreen-Qt.

Візуальний аудит 1.2.1 №3/№4 та хвиля «Запис-полірування»:
- єдина текстова кнопка з відео-іконкою (fa6s.video);
- кнопка disabled до вибору джерела при «Не знайдено екранів» з тултіпом;
- перемикач джерела у сегмент-контролі;
- WebM у дропдауні форматів.

Запуск:
    python -m unittest tests.render_screen_smoke
    python tests/render_screen_smoke.py
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


class _ScreenController:
    """Легкий контролер із Qt-сигналами запису екрана для рендеру сторінки."""

    def __init__(self, has_sources=True):
        from PySide6.QtCore import QObject, Signal

        class _Ctl(QObject):
            screen_record_state = Signal(str)
            screen_record_error = Signal(str)
            screen_record_finished = Signal(str, bool)

            def __init__(self, has_sources):
                super().__init__()
                self.has_sources = has_sources
                self.cfg = SimpleNamespace(
                    screen_record_fps=30, screen_record_resolution="native",
                    screen_record_quality="medium", screen_record_format="webm",
                    screen_record_system_audio=False)
                self.started = []

            def list_screen_monitors(self):
                if not self.has_sources:
                    return []
                return [SimpleNamespace(label="Екран 1", index=1,
                                        left=0, top=0, width=1920, height=1080)]

            def list_screen_windows(self):
                return []

            def list_screen_recordings(self):
                return []

            def open_screen_recordings_folder(self):
                pass

            def screen_record_start(self, source, options):
                self.started.append((source, options))
                return True

            def screen_record_stop(self):
                pass

            def save_config(self):
                pass

        self._impl = _Ctl(has_sources)

    def __getattr__(self, name):
        return getattr(self._impl, name)


class ScreenRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))

    @classmethod
    def tearDownClass(cls):
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
        self._live = []

    def tearDown(self):
        from PySide6.QtCore import QTimer
        for page, ctl in self._live:
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
            ctl._impl.deleteLater()
        self._live = []
        self._flush_deferred(self._app)

    def _page(self, has_sources=True):
        from fronts.desktop.pages.screen import ScreenPage
        ctl = _ScreenController(has_sources=has_sources)
        page = ScreenPage(ctl)
        self._live.append((page, ctl))
        page.resize(1000, 640)
        page.show()
        for _ in range(4):
            self._app.processEvents()
        return page, ctl

    def test_single_start_button_uses_video_icon(self):
        """Хвиля 'Запис-полірування': одна текстова кнопка з відео-іконкою в шапці."""
        page, _ = self._page()
        self.assertFalse(page._rec_action.icon().isNull())
        self.assertFalse(hasattr(page, "_rec"), "Окрема кругла кнопка вилучена")

    def test_start_button_disabled_when_no_source(self):
        """Кнопка 'Почати запис' disabled при 'Не знайдено екранів' з тултіпом."""
        from fronts.desktop.i18n import tr
        page, _ = self._page(has_sources=False)
        self.assertFalse(page._rec_action.isEnabled())
        self.assertEqual(page._rec_action.toolTip(), tr("screen_no_source_tooltip"))

    def test_explicit_start_stop_button_tracks_state(self):
        """Явна текстова кнопка «Почати запис» ↔ «Зупинити запис», синхронна зі станом."""
        from fronts.desktop.i18n import tr
        page, _ = self._page(has_sources=True)
        self.assertTrue(page._rec_action.isEnabled())
        self.assertEqual(page._rec_action.text(), tr("screen_start"))
        page._state("recording")
        for _ in range(2):
            self._app.processEvents()
        self.assertEqual(page._rec_action.text(), tr("screen_stop"))
        self.assertEqual(page._rec_action.accessibleName(), tr("screen_stop"))
        page._state("idle")
        self.assertEqual(page._rec_action.text(), tr("screen_start"))

    def test_format_dropdown_shows_webm(self):
        """Формат 'webm' показується як 'WebM'."""
        page, _ = self._page()
        texts = [page._format.itemText(i) for i in range(page._format.count())]
        self.assertIn("WebM", texts)
        self.assertNotIn("WEBM", texts)

    def test_render_screen_page(self):
        page, _ = self._page()
        pix = page.grab()
        self.assertFalse(pix.isNull())


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(ScreenRenderTests))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
