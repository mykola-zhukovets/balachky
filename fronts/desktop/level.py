"""Жива смужка рівня мікрофона (сторінка «Диктування»).

Канон «Balachky»: золото — норма, червоний сегмент — лише на кліпі/перевантаженні
(як стан REC). Смужка показується ЛИШЕ під час запису; QTimer 30 fps крутиться
тільки поки активна (на idle/busy — стоп + сховати).

Метрику дає recorder.take_meter() → (rms, peak) лінійної амплітуди. Тут:
dBFS-мапінг (floor -60), балістика (attack миттєвий, decay ~25 dB/с, peak-hold
~1.75 с) і малювання. Важке з audio-callback навмисно винесено сюди.
"""
import math

from PySide6.QtCore import QRectF, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme   # кольори читаємо при малюванні (нічний режим свопає палітру)

_FLOOR_DB = -60.0            # нижче — тиша/шум (level=0)
_CLIP_DB = -3.0             # вище — перевантаження (червоний хвіст)
CLIP_LEVEL = (_CLIP_DB - _FLOOR_DB) / (0.0 - _FLOOR_DB)   # ≈0.95 у шкалі 0..1

_FPS = 30
_TICK_MS = 1000 // _FPS
_DECAY_DB_S = 25.0                                  # спад тіла/піку, dB/с
_DECAY_PER_TICK = (_DECAY_DB_S / -_FLOOR_DB) / _FPS  # у частках шкали за кадр
_HOLD_TICKS = round(1.75 * _FPS)                     # peak-hold ~1.75 с


def amp_to_level(amp: float, floor_db: float = _FLOOR_DB) -> float:
    """Лінійна амплітуда 0..1 → рівень 0..1 (dBFS з floor). Тиша→0, full scale→1."""
    if amp <= 1e-10:
        return 0.0
    dbfs = 20.0 * math.log10(amp)
    level = (dbfs - floor_db) / (0.0 - floor_db)
    if level < 0.0:
        return 0.0
    if level > 1.0:
        return 1.0
    return level


class LevelMeter(QWidget):
    """Горизонтальна смужка рівня із золотою заливкою й піковою рискою.

    provider() → (rms, peak) лінійної амплітуди; викликається на кожному кадрі
    таймера. Резолвиться лінійно (lazy), бо recorder у контролері зʼявляється
    пізніше за вікно."""

    def __init__(self, provider, parent=None):
        super().__init__(parent)
        self._provider = provider
        self._level = 0.0        # тіло смужки (RMS), у шкалі 0..1
        self._peak = 0.0         # пікова риска (peak-hold)
        self._hold = 0           # лічильник утримання піку
        self.setFixedHeight(10)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)

    def set_active(self, on: bool) -> None:
        """Запис почався/скінчився: старт/стоп таймера рівня. Видимість смужки
        керує сторінка (хост-обгортка). Таймер НЕ крутиться поза записом —
        не тікає вічно, не жере CPU в idle."""
        if on:
            self._reset()
            if not self._timer.isActive():
                self._timer.start()
        else:
            if self._timer.isActive():
                self._timer.stop()
            self._reset()

    def _reset(self) -> None:
        self._level = 0.0
        self._peak = 0.0
        self._hold = 0
        self.update()

    def _tick(self) -> None:
        try:
            rms, peak = self._provider()
        except Exception:
            return                       # мікрофон зник/перемикається — просто пропуск
        target = amp_to_level(rms)
        # attack миттєвий (стрибок угору), decay — плавний спад
        if target >= self._level:
            self._level = target
        else:
            self._level = max(target, self._level - _DECAY_PER_TICK)
        # пік: миттєвий підйом, утримання, потім спад тією ж траєкторією
        pk = amp_to_level(peak)
        if pk >= self._peak:
            self._peak = pk
            self._hold = _HOLD_TICKS
        elif self._hold > 0:
            self._hold -= 1
        else:
            self._peak = max(0.0, self._peak - _DECAY_PER_TICK)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 5.0
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        # трек: напівпрозорий DEEP (у тон полів вводу)
        p.fillPath(path, QColor(*theme.PANEL_RGB, 180))
        p.setClipPath(path)
        w = r.width()
        fill_w = w * self._level
        clip_x = w * CLIP_LEVEL
        # тіло: золото до порогу кліпу
        gold_w = min(fill_w, clip_x)
        if gold_w > 0:
            p.fillRect(QRectF(r.left(), r.top(), gold_w, r.height()), QColor(theme.GOLD))
        # червоний «хвіст» — лише частина за порогом перевантаження
        if fill_w > clip_x:
            p.fillRect(QRectF(r.left() + clip_x, r.top(), fill_w - clip_x, r.height()),
                       QColor(theme.ALERT))
        # пікова риска: золота, у зоні кліпу — червона
        if self._peak > 0.0:
            px = r.left() + w * self._peak
            col = QColor(theme.ALERT) if self._peak > CLIP_LEVEL else QColor(theme.GOLD_EYEBROW)
            p.fillRect(QRectF(px - 1.0, r.top(), 2.0, r.height()), col)
        p.setClipping(False)
        # бордюр замість тіні (канон)
        p.setPen(QPen(QColor(*theme.LIGHT_RGB, 40), 1))
        p.drawPath(path)
