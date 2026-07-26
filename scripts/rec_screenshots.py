"""Скрипт створення живих скріншотів для сторінки «Запис екрана» (хвиля Запис-полірування).

Захоплює 3 живі стани сторінки Запис екрана:
1. screen_no_source.png — до вибору джерела (кнопка disabled + тултіп).
2. screen_with_source.png — з вибраним джерелом.
3. screen_recording_state.png — у стані запису.
"""
import os
os.environ["QT_QPA_PLATFORM"] = "windows"

import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QToolTip
from PySide6.QtCore import QPoint, QObject, Signal

from fronts.desktop.theme import QSS, load_fonts
from fronts.desktop import motion
from fronts.desktop.pages.screen import ScreenPage


class _FakeController(QObject):
    screen_record_state = Signal(str)
    screen_record_error = Signal(str)
    screen_record_finished = Signal(str, bool)

    def __init__(self, has_sources=True):
        super().__init__()
        self.has_sources = has_sources
        self.cfg = SimpleNamespace(
            screen_record_fps=30, screen_record_resolution="native",
            screen_record_quality="medium", screen_record_format="webm",
            screen_record_system_audio=True
        )

    def list_screen_monitors(self):
        if not self.has_sources:
            return []
        return [SimpleNamespace(label="Монітор 1 (1920×1080)", index=1, left=0, top=0, width=1920, height=1080)]

    def list_screen_windows(self):
        return []

    def list_screen_recordings(self):
        return []

    def open_screen_recordings_folder(self):
        pass

    def screen_record_start(self, source, options):
        return True

    def screen_record_stop(self):
        pass

    def save_config(self):
        pass


def capture_screenshots():
    out_dir = ROOT / "docs" / "screenshots" / "recpolish"
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv)
    load_fonts()
    app.setStyleSheet(QSS)
    motion.init_config(SimpleNamespace(animations=False))

    # 1. Без джерел (disabled + tooltip)
    ctl_no = _FakeController(has_sources=False)
    page_no = ScreenPage(ctl_no)
    page_no.resize(1000, 640)
    page_no.show()
    app.processEvents()

    # Показуємо тултип для кнопки
    btn = page_no._rec_action
    tip_pos = btn.mapToGlobal(QPoint(btn.width() // 2, btn.height() // 2))
    QToolTip.showText(tip_pos, btn.toolTip(), btn)
    app.processEvents()
    time.sleep(0.2)

    pix1 = page_no.grab()
    shot1_path = out_dir / "screen_no_source.png"
    pix1.save(str(shot1_path))
    print(f"Captured: {shot1_path}")
    page_no.close()

    # 2. З вибраним джерелом
    ctl_src = _FakeController(has_sources=True)
    page_src = ScreenPage(ctl_src)
    page_src.resize(1000, 640)
    page_src.show()
    app.processEvents()
    time.sleep(0.2)

    pix2 = page_src.grab()
    shot2_path = out_dir / "screen_with_source.png"
    pix2.save(str(shot2_path))
    print(f"Captured: {shot2_path}")

    # 3. У стані запису
    page_src._state("recording")
    page_src._started = time.time() - 42  # 00:42 на таймері
    page_src._update_timer()
    app.processEvents()
    time.sleep(0.2)

    pix3 = page_src.grab()
    shot3_path = out_dir / "screen_recording_state.png"
    pix3.save(str(shot3_path))
    print(f"Captured: {shot3_path}")
    page_src.close()


if __name__ == "__main__":
    capture_screenshots()
