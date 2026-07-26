"""Проба звільнення файлу вбудованим плеєром (feature/player-recordings).

Регресія-блокер: ffmpeg-бекенд QMediaPlayer тримає завантажений файл відкритим
навіть на stop() — unlink/rmtree давав WinError 32 і ламав «Видалити запис» та
«Видалити нараду». Фікс: ледаче завантаження (setSource лише на перший play) +
звільнення джерела setSource(QUrl()) у stop(). Тут перевіряємо ЖИВЦЕМ, що після
завантаження джерела і stop() файл видаляється.

Qt offscreen (без дисплея); QtMultimedia недоступний → skip, не фейл.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from whisper_core import recordings


def _pump(app, seconds=0.6):
    """Дати бекенду час відкрити/відпустити файл (він асинхронний)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


class PlayerReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtMultimedia import QMediaPlayer  # noqa: F401
        except Exception:                                  # pragma: no cover
            raise unittest.SkipTest("QtMultimedia недоступний")
        cls.app = QApplication.instance() or QApplication([])

    def test_constructor_does_not_open_file(self):
        """Ледаче завантаження: сам конструктор не сміє тримати файл."""
        from fronts.desktop.player import InlinePlayer
        with tempfile.TemporaryDirectory() as tmp:
            wav = recordings.save_recording(
                tmp, np.zeros(16000, dtype=np.float32), 16000)
            p = InlinePlayer(wav)
            _pump(self.app, 0.3)
            wav.unlink()                 # WinError 32, якби джерело вантажилось одразу
            self.assertFalse(wav.exists())
            p.deleteLater()

    def test_file_deletable_after_load_and_stop(self):
        """Завантажили джерело (як перший play) → stop() → файл видаляється."""
        from fronts.desktop.player import InlinePlayer
        with tempfile.TemporaryDirectory() as tmp:
            wav = recordings.save_recording(
                tmp, np.zeros(16000, dtype=np.float32), 16000)
            p = InlinePlayer(wav)
            p._ensure_source()           # те, що робить перший play
            _pump(self.app)              # бекенд відкриває файл асинхронно
            p.stop()                     # МУСИТЬ звільнити хендл
            _pump(self.app)
            wav.unlink()                 # головна проба: без фікса тут WinError 32
            self.assertFalse(wav.exists())
            p.deleteLater()


if __name__ == "__main__":
    unittest.main()
