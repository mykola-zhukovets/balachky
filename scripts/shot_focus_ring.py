"""Живий скрін попапа обробки з КЛАВІАТУРНИМ фокусом на ручці слайдера, день.

Доказ, що перстень фокуса видно на золотій ручці (блокер суду: FOCUS==GOLD).
Відкриває попап чіпа обробки, ставить фокус на слайдер, грабить вікно.

Вивід: shots/final/popover-focus-day.png
Запуск із кореня репо:
    .venv\\Scripts\\python scripts\\shot_focus_ring.py [OUT_DIR]
"""
import os
os.environ["QT_QPA_PLATFORM"] = "windows"     # ДО будь-якого render_*_smoke імпорту
import ctypes
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication

from whisper_core import profiles
from scripts.screenshots import FakeController, make_sandbox, grab_window

_POS = [a for a in sys.argv[1:] if not a.startswith("--")]
OUT = Path(_POS[0]).resolve() if _POS else ROOT / "shots" / "final"


def main():
    if sys.platform != "win32":
        sys.exit("Tilky Windows (DWM/Mica).")
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)

    from fronts.desktop import theme
    from fronts.desktop.theme import QSS, load_fonts
    load_fonts()
    app.setStyleSheet(QSS)
    theme.apply_link_colors(app)

    sandbox = make_sandbox()
    _orig = profiles.list_profiles
    profiles.list_profiles = lambda root=None: _orig(sandbox)
    ctrl = FakeController(sandbox)
    ctrl.cfg.backdrop = "auto"

    matte = QWidget()
    matte.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    matte.setStyleSheet("background: #202020;")
    matte.setGeometry(QGuiApplication.primaryScreen().geometry())
    matte.show()

    from fronts.desktop.main_window import MainWindow
    win = MainWindow(ctrl)
    win.setWindowState(Qt.WindowNoState)
    win.resize(1856, 1044)
    win.move(24, 16)
    win.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    win.show()
    win.raise_()
    win.activateWindow()
    OUT.mkdir(parents=True, exist_ok=True)

    win.set_page(0)

    def open_and_focus():
        chip = win.dictation._processing
        chip._open()                                # показати попап під чіпом

        def focus_handle():
            # ProcessingChip._slider — це ProcessingSlider-обгортка; справжній
            # _ChipSlider (з ручкою й перснем) лежить ще рівнем нижче.
            sl = chip._slider._slider
            sl.activateWindow()
            sl.setFocus(Qt.TabFocusReason)          # клавіатурний фокус на ручці

            def shot():
                grab_window(win, OUT / "popover-focus-day.png")
                print("shot popover-focus-day  hasFocus:", sl.hasFocus())
                app.quit()
            QTimer.singleShot(400, shot)
        QTimer.singleShot(300, focus_handle)
    QTimer.singleShot(900, open_and_focus)

    app.exec()
    print("gotovo:", OUT)


if __name__ == "__main__":
    main()
