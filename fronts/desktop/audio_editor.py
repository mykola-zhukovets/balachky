"""Панель неруйнівного редагування WAV під вбудованим плеєром."""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QInputDialog, QLabel,
                               QMessageBox, QVBoxLayout, QWidget)

from whisper_core import audioedit
from .crash import anonymize_path
from .glass import GlassButton
from .i18n import tr
from .player import fmt_time, _IconButton
from . import theme   # кольори читаємо при малюванні (нічний режим свопає палітру)


def mark_x_position(pos_ms: int, total_ms: int, width: int) -> int:
    """Час позначки (мс) → X-піксель у смузі шириною width, затиснутий у [0, width].
    total_ms<=0 → 0; за кінець → width; від'ємний → 0."""
    if total_ms <= 0:
        return 0
    x = int(pos_ms / total_ms * width)
    return max(0, min(width, x))


def should_loop_seek(pos: int, start: int, end: int, loop_active: bool) -> bool:
    """Чи час перемотати на початок при активній петлі A-B."""
    if not loop_active:
        return False
    # pos може перескочити end через низьку частоту оновлень (tick)
    return pos >= end


class Waveform(QWidget):
    """Малює min/max-піки, а не всі семпли: робота не залежить від довжини WAV."""
    seek_requested = Signal(int)
    selection_changed = Signal(int, int)

    def __init__(self, audio, sample_rate: int, parent=None):
        super().__init__(parent)
        mono = np.asarray(audio, dtype=np.float32)
        self._mono = mono.mean(axis=1) if mono.ndim == 2 else mono.reshape(-1)
        self._rate = sample_rate
        self._total_ms = int(len(self._mono) * 1000 / sample_rate) if sample_rate else 0
        self._position = 0
        self._start = self._end = None
        self._pressed = None
        self._dragged = False
        self._marks = []
        self._peaks = self._downsample(self._mono)
        self.setMinimumHeight(118)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tr("audioedit_waveform_tip"))

    def set_marks(self, marks: list[dict]):
        self._marks = marks or []
        self.update()

    @staticmethod
    def _downsample(audio, bins=4096):
        if not len(audio):
            return np.zeros((0, 2), dtype=np.float32)
        edges = np.linspace(0, len(audio), min(bins, len(audio)) + 1, dtype=int)
        return np.asarray([(audio[edges[i]:edges[i + 1]].min(),
                            audio[edges[i]:edges[i + 1]].max())
                           for i in range(len(edges) - 1)], dtype=np.float32)

    @property
    def selection(self):
        return (self._start, self._end) if self._start is not None else None

    def set_position(self, ms: int):
        self._position = max(0, min(self._total_ms, int(ms)))
        self.update()

    def _ms_at(self, x):
        return int(max(0, min(1, x / max(1, self.width() - 1))) * self._total_ms)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pressed = self._ms_at(event.position().x())
            self._dragged = False

    def mouseMoveEvent(self, event):
        if self._pressed is not None:
            now = self._ms_at(event.position().x())
            self._dragged = self._dragged or abs(now - self._pressed) > 30
            if self._dragged:
                self._start, self._end = sorted((self._pressed, now))
                self.selection_changed.emit(self._start, self._end)
                self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._pressed is not None:
            now = self._ms_at(event.position().x())
            if self._dragged:
                self._start, self._end = sorted((self._pressed, now))
                self.selection_changed.emit(self._start, self._end)
            else:
                # Snap to mark if clicked very close
                clicked_x = event.position().x()
                w = max(1, self.width())
                snapped_now = now
                for mark in self._marks:
                    ms = float(mark.get("timestamp", 0)) * 1000
                    mark_x = mark_x_position(ms, self._total_ms, w)
                    if abs(mark_x - clicked_x) <= 3:
                        snapped_now = int(ms)
                        break
                self._start = self._end = None
                self.seek_requested.emit(snapped_now)
            self._pressed = None
            self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.DEEP))
        w, h = max(1, self.width()), self.height()
        middle = h / 2
        p.setPen(QPen(QColor(theme.TEXT_MUTED), 1))
        p.drawLine(0, int(middle), w, int(middle))
        if len(self._peaks):
            p.setPen(QPen(QColor(theme.GOLD_EYEBROW), 1))
            for x in range(w):
                lo, hi = self._peaks[min(len(self._peaks) - 1, int(x * len(self._peaks) / w))]
                p.drawLine(x, int(middle - hi * (h * .43)), x, int(middle - lo * (h * .43)))

        # Draw bookmarks
        if self._marks:
            p.setPen(QPen(QColor(theme.GOLD), 1))
            for mark in self._marks:
                ms = float(mark.get("timestamp", 0)) * 1000
                mx = mark_x_position(ms, self._total_ms, w)
                p.drawLine(mx, 0, mx, h)

        if self._start is not None:
            left = int(self._start / max(1, self._total_ms) * w)
            right = int(self._end / max(1, self._total_ms) * w)
            selected = QColor(theme.GOLD); selected.setAlpha(70)
            p.fillRect(left, 0, max(1, right - left), h, selected)
        cursor = int(self._position / max(1, self._total_ms) * w)
        p.setPen(QPen(QColor(theme.GOLD), 2))
        p.drawLine(cursor, 0, cursor, h)


class AudioEditorPanel(QWidget):
    """Дії редактора завжди створюють інший WAV через діалог «Зберегти як…»."""
    def __init__(self, path, player, controller, parent=None, marks=None, source=None):
        super().__init__(parent)
        self._path, self._player, self._controller = str(path), player, controller
        # Код джерела доріжки (me/others/mic1…): редакція чіпає лише репліки цієї
        # доріжки. None — одна доріжка (затирати все перекрите виділенням).
        self._source = source
        self._audio, self._rate = audioedit.read_wav(self._path)
        self._wave = Waveform(self._audio, self._rate)
        if marks:
            self._wave.set_marks(marks)
        self._range = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)
        caption = QLabel(tr("audioedit_hint")); caption.setProperty("muted", True)
        caption.setObjectName("audioEditCaption"); caption.setWordWrap(True)  # канон §3
        lay.addWidget(caption)
        lay.addWidget(self._wave)
        self._range_label = QLabel(tr("audioedit_no_selection")); self._range_label.setProperty("muted", True)
        self._range_label.setWordWrap(True)   # канон §3: статус-примітка переноситься
        lay.addWidget(self._range_label)
        actions = QHBoxLayout(); actions.setSpacing(8)
        self._range_buttons = []
        for key, callback in (("audioedit_trim", self._trim), ("audioedit_cut", self._cut),
                              ("audioedit_redact", self._redact),
                              ("audioedit_silence", self._silence), ("audioedit_normalize", self._normalize),
                              ("audioedit_transcribe", self._transcribe)):
            btn = GlassButton(tr(key)); btn.setAccessibleName(tr(key)); btn.clicked.connect(callback)
            if key in ("audioedit_trim", "audioedit_cut", "audioedit_redact", "audioedit_transcribe"):
                btn.setEnabled(False); self._range_buttons.append(btn)
            actions.addWidget(btn)
        actions.addStretch(); lay.addLayout(actions)
        
        self._loop_btn = _IconButton("fa6s.repeat", tr("audioedit_loop"))
        self._loop_btn.setCheckable(True)
        self._loop_btn.setEnabled(False)
        self._range_buttons.append(self._loop_btn)
        actions.addWidget(self._loop_btn)

        self._wave.seek_requested.connect(self._player.seek_ms)
        self._wave.selection_changed.connect(self._set_range)
        self._player.position_changed.connect(self._on_position)

    def _on_position(self, pos: int):
        self._wave.set_position(pos)
        if self._range:
            start_ms, end_ms = int(self._range[0] * 1000), int(self._range[1] * 1000)
            if should_loop_seek(pos, start_ms, end_ms, self._loop_btn.isChecked()):
                self._player.seek_ms(start_ms)

    def _set_range(self, start, end):
        self._range = (start / 1000, end / 1000)
        self._range_label.setText(tr("audioedit_selection", start=fmt_time(start), end=fmt_time(end)))
        for button in self._range_buttons:
            button.setEnabled(end > start)

    def _need_range(self):
        return self._range

    def _save(self, audio, suffix):
        stem = Path(self._path).stem + suffix + ".wav"
        path, _ = QFileDialog.getSaveFileName(self, tr("audioedit_save"), stem, "WAV (*.wav)")
        if path:
            audioedit.write_wav(path, audio, self._rate)
            return path
        return None

    def _trim(self):
        selected = self._need_range()
        if selected is None:
            return
        start, end = selected
        self._save(audioedit.trim_to_range(self._audio, self._rate, start, end), "-trim")

    def _cut(self):
        selected = self._need_range()
        if selected is None:
            return
        start, end = selected
        self._save(audioedit.cut_range(self._audio, self._rate, start, end), "-cut")

    def _silence(self):
        out = audioedit.remove_silence(self._audio, self._rate)
        self._range_label.setText(tr("audioedit_shortened", before=fmt_time(len(self._audio) * 1000 // self._rate), after=fmt_time(len(out) * 1000 // self._rate)))
        self._save(out, "-without-silence")

    def _redact(self):
        """Заглушити виділення (тиша/біп) у ОКРЕМІЙ «-redacted»-копії й
        синхронно заредагувати транскрипт. Оригінальний WAV не змінюється."""
        selected = self._need_range()
        if selected is None:
            return
        start, end = selected
        modes = [tr("audioedit_redact_silence"), tr("audioedit_redact_beep")]
        choice, ok = QInputDialog.getItem(
            self, tr("audioedit_redact"), tr("audioedit_redact_mode"), modes, 0, False)
        if not ok:
            return
        mode = "beep" if choice == modes[1] else "silence"
        # Явно попереджаємо: у нараді mic+sys кнопка чіпає ЛИШЕ відкриту доріжку.
        confirm = tr("audioedit_redact_confirm") + "\n\n" + tr("audioedit_redact_multitrack_note")
        if QMessageBox.question(self, tr("audioedit_redact_confirm_title"),
                                confirm) != QMessageBox.Yes:
            return
        redacted = audioedit.redact_range(self._audio, self._rate, start, end, mode=mode)
        if self._save(redacted, "-redacted") is None:
            return   # користувач скасував «Зберегти як…» — нічого не зафіксовано
        note = tr("audioedit_redact_note",
                  start=fmt_time(int(start * 1000)), end=fmt_time(int(end * 1000)))
        try:
            result = self._controller.redact_transcript(
                self._path, start, end, marker=tr("audioedit_redact_marker"), note=note,
                source=self._source)
        except Exception:
            logging.exception("Не вдалося заредагувати транскрипт: %s",
                              anonymize_path(self._path))
            result = None
        if result is None:
            # Ні винятку не ковтаємо, ні None не видаємо за успіх: транскрипт НЕ
            # заредаговано (не резолвнули сесію / не прочитали / не записали).
            # Аудіо-копія вже є, але чутливі слова могли лишитись у transcript.json
            # і потрапити в evidence — показуємо чесну помилку, а не «готово».
            QMessageBox.warning(self, tr("audioedit_redact"), tr("audioedit_redact_failed"))
            self._range_label.setText(tr("audioedit_redact_failed"))
            return
        self._range_label.setText(note)

    def _normalize(self):
        self._save(audioedit.normalize_archive(self._audio), "-normalized")

    def _transcribe(self):
        selected = self._need_range()
        if selected is None:
            return
        start, end = selected
        self._controller.transcribe_audio_range(self._path, start, end)
