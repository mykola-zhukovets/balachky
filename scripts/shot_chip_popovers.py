"""Живі скріни попапів чіпів (обробка + кадри/сек), день. НЕ offscreen (DWM/Mica).

Слайдер у попапах — єдиний затверджений стиль (23.07): ледь помітна
доріжка, крапки-зупинки, ручка-пігулка. Скрипт відкриває попап чіпа обробки
(Диктування) і чіпа кадрів (Запис екрана) та грабить вікно РАЗОМ із відкритим
попапом системним ImageGrab (як screenshots.py).

Вивід: shots/final/popover-{processing|fps}-day.png
Запуск із кореня репо:
    .venv\\Scripts\\python scripts\\shot_chip_popovers.py [OUT_DIR]
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

# (ім'я, індекс сторінки, ключ чіпа): обробка (Диктування) і кадри/сек (Екран).
TARGETS = [("popover-processing-day", 0, "dict"),
           ("popover-fps-day", 3, "fps")]


def main():
    if sys.platform != "win32":
        sys.exit("Tilky Windows (DWM/Mica).")
    shcore = ctypes.windll.shcore
    shcore.SetProcessDpiAwareness.argtypes = (ctypes.c_int,)
    shcore.SetProcessDpiAwareness.restype = ctypes.c_long
    shcore.SetProcessDpiAwareness(2)
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

    def run(i=0):
        if i >= len(TARGETS):
            app.quit()
            return
        name, page, key = TARGETS[i]
        win.set_page(page)

        def open_and_shot():
            chip = win.dictation._processing if key == "dict" else win.screen._fps
            chip._open()                           # показати попап під чіпом

            def shot():
                grab_window(win, OUT / f"{name}.png")
                print("shot", name)
                pop = chip._popover
                if pop is not None:
                    pop.hide()
                QTimer.singleShot(200, lambda: run(i + 1))
            QTimer.singleShot(400, shot)
        QTimer.singleShot(450, open_and_shot)

    QTimer.singleShot(700, run)
    app.exec()
    print("gotovo:", OUT)


if __name__ == "__main__":
    main()
