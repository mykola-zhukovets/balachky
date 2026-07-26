"""Offscreen-рендер відеоплеєра — ОКРЕМИЙ процес (як render_screen_smoke):
QMediaPlayer/QVideoWidget крутять нативний бекенд, тож тримаємо їх поза спільним
`unittest discover` і жорстко чистимо у teardown, щоб не було 0xC000041D на
нативній деструкції offscreen-Qt.

Перевіряємо:
- усі контролі присутні й мають accessibleName (канон a11y);
- pitchCompensation увімкнено (тон не пливе на 0,5-2×);
- відсутній/битий файл → людський банер, кнопка play вимкнена, БЕЗ краху.

Запуск:
    python -m unittest tests.render_video_smoke
    python tests/render_video_smoke.py
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class VideoPlayerRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtMultimedia import QMediaPlayer          # наявність бекенда
            from PySide6.QtMultimediaWidgets import QVideoWidget   # наявність полотна
            _ = (QMediaPlayer, QVideoWidget)
        except Exception:                        # pragma: no cover
            raise unittest.SkipTest("QtMultimedia недоступний")
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        from types import SimpleNamespace
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))

    def setUp(self):
        self._live = []

    def tearDown(self):
        for dlg in self._live:
            try:
                dlg.close()
                dlg.deleteLater()
            except Exception:
                pass
        self._live = []
        for _ in range(3):
            self._app.processEvents()

    def _dialog(self, path=None):
        from fronts.desktop.video_player import VideoPlayerDialog
        dlg = VideoPlayerDialog(path, None)
        self._live.append(dlg)
        dlg.resize(760, 540)
        dlg.show()
        for _ in range(4):
            self._app.processEvents()
        return dlg

    def test_controls_present_and_named(self):
        from fronts.desktop.i18n import tr
        dlg = self._dialog()
        self.assertIsNotNone(dlg._video)
        self.assertEqual(dlg._video.accessibleName(), tr("video_surface"))
        self.assertEqual(dlg._play_btn.accessibleName(), tr("player_play"))
        self.assertEqual(dlg._speed_btn.accessibleName(), tr("player_speed"))
        self.assertEqual(dlg._seek.accessibleName(), tr("video_position"))
        self.assertEqual(dlg._vol.accessibleName(), tr("player_volume"))
        self.assertEqual(dlg._speed_label(), "1×")

    def test_pitch_compensation_enabled(self):
        # Головна вимога MVP: тон не «бурундук» на 0,5-2×.
        dlg = self._dialog()
        self.assertTrue(dlg._player.pitchCompensation())

    def test_speed_cycle_sets_playback_rate(self):
        dlg = self._dialog()
        self.assertEqual(dlg._player.playbackRate(), 1.0)
        dlg._cycle_speed()                       # → 1,25×
        self.assertEqual(dlg._player.playbackRate(), 1.25)
        for _ in range(3):                       # 1,5× → 2× → 0,5×
            dlg._cycle_speed()
        self.assertEqual(dlg._player.playbackRate(), 0.5)
        self.assertEqual(dlg._speed_label(), "0,5×")

    def test_missing_file_shows_human_error_no_crash(self):
        from fronts.desktop.i18n import tr
        dlg = self._dialog(str(_ROOT / "tests" / "no_such_video.mp4"))
        # showEvent із неіснуючим файлом синхронно показує банер (os.path.exists)
        self.assertTrue(dlg._status.isVisible())
        self.assertEqual(dlg._status.text(), tr("video_error"))
        self.assertFalse(dlg._play_btn.isEnabled())

    def test_render_not_null(self):
        dlg = self._dialog()
        self.assertFalse(dlg.grab().isNull())


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(VideoPlayerRenderTests))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
