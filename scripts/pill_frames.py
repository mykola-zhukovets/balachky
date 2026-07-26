"""Контакт-лист кадрів анімованої плаваючої пілюлі (feature/status-tags-soul).

Рендерить FloatingPill у станах recording (пульс-німб) і busy (shimmer) через
кілька фаз руху + статичний кадр Reduce Motion, компонує на темну підкладку
(як Mica-фон застосунку) і зберігає PNG для перегляду очима.

Кадри беремо widget.grab() (без показу вікна — щоб не смикати екран
користувача): вручну заводимо таймер+годинник і спимо між грабами, тож
_clock.elapsed() дає різну фазу. Рендер РЕАЛЬНИЙ (не offscreen) → справжні
шрифти/згладжування.

Запуск:  .venv\\Scripts\\python scripts\\pill_frames.py <out_dir>
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtCore import Qt, QRectF

from fronts.desktop import motion
from fronts.desktop.i18n import set_language
from fronts.desktop.pill import FloatingPill, _STATE_COLOR

OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

_BG = QColor(38, 34, 26)        # темна підкладка (тон Mica-фону)
_PAD = 18
_FRAMES = 6                      # кадрів на рядок стану
_GAP_MS = 240                    # реальна пауза між грабами → різна фаза


def _grab_frames(pill, state, live):
    pill._state = state
    pill._accent = QColor(_STATE_COLOR[state])
    if live:
        motion.init_config(SimpleNamespace(animations=True))
        motion._system_ok = True
        pill._clock.start()
        pill._timer.start()
    else:
        motion.init_config(SimpleNamespace(animations=False))
        pill._timer.stop()
    frames = []
    n = _FRAMES if live else 1
    for _ in range(n):
        frames.append(pill.grab())
        if live:
            time.sleep(_GAP_MS / 1000.0)
    pill._timer.stop()
    return frames


def _sheet(rows):
    """rows = [(label, [pixmaps]), ...] → одна PNG-плитка."""
    pw, ph = rows[0][1][0].width(), rows[0][1][0].height()
    cols = max(len(f) for _, f in rows)
    W = _PAD + cols * (pw + _PAD)
    row_h = 20 + ph + _PAD
    H = _PAD + len(rows) * row_h
    canvas = QPixmap(W, H)
    canvas.fill(_BG)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.Antialiasing)
    y = _PAD
    for label, frames in rows:
        p.setPen(QColor(220, 216, 200))
        f = p.font(); f.setPixelSize(12); p.setFont(f)
        p.drawText(QRectF(_PAD, y, W, 18), Qt.AlignVCenter | Qt.AlignLeft, label)
        y += 20
        x = _PAD
        for pm in frames:
            p.drawPixmap(x, y, pm)
            x += pw + _PAD
        y += ph + _PAD
    p.end()
    return canvas


def main():
    app = QApplication.instance() or QApplication([])
    for lang in ("uk", "en"):
        set_language(lang)
        pill = FloatingPill(on_moved=lambda *a: None, on_reset=lambda: None)
        rows = [
            ("recording — пульс-німб (дихання)", _grab_frames(pill, "recording", True)),
            ("busy — shimmer (світлова смуга)", _grab_frames(pill, "busy", True)),
            ("Reduce Motion — статика (recording / busy)",
             [_grab_frames(pill, "recording", False)[0],
              _grab_frames(pill, "busy", False)[0]]),
        ]
        out = OUT / f"pill_animation_{lang}.png"
        _sheet(rows).save(str(out))
        print(f"[{lang}] {out}")
        pill._timer.stop()
        pill.deleteLater()
    app.processEvents()
    print("DONE")


if __name__ == "__main__":
    raise SystemExit(main())
