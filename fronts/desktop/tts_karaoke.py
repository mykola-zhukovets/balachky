"""Караоке-підсвічування (§8.5, Хвиля 2): синхронне з голосом підсвічування слова/
речення у редакторі через QTextEdit.setExtraSelections (НЕ QSyntaxHighlighter).

Ключові інваріанти:
  • координати слів — АБСОЛЮТНІ code-points редактора (worker уже додав source_start_cp);
    на Qt-межі конвертуємо в UTF-16 за ПОВНИМ snapshot тексту (один astral-символ
    інакше зсунув би підсвічування). Межі в СИМВОЛАХ, не пікселях → стійко до зуму/шрифту;
  • clock domain — media-ms (§8.4): порівнюємо з QMediaPlayer.position(), таймінги НЕ
    перераховуються при зміні швидкості;
  • revision-hash тексту: редагування під час відтворення → підсвічування ЗУПИНЯЄТЬСЯ
    (старі raw-діапазони підсвічували б інший текст);
  • автоскрол лише коли активне слово вийшло за видиму область.

update(position_ms) кличе панель/контролер по InlinePlayer.position_changed (media-ms)."""
from __future__ import annotations

from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QTextEdit

from whisper_core.tts import timings as _t

GRANULARITY_WORD = "word"
GRANULARITY_SENTENCE = "sentence"


class KaraokeHighlighter:
    def __init__(self, editor, *, on_stopped=None, color=None):
        self._editor = editor
        self._on_stopped = on_stopped or (lambda: None)
        self._word_timings = []
        self._sentence_starts = []
        self._granularity = GRANULARITY_WORD
        self._revision = None
        self._last_key = None
        self._active = False
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(color) if color else QColor(243, 146, 0, 90))
        self._fmt = fmt

    # --- керування ---
    def start(self, word_timings, sentence_starts=None) -> None:
        self._word_timings = list(word_timings or [])
        self._sentence_starts = list(sentence_starts or [])
        self._revision = self._text_hash()
        self._active = True
        self._last_key = None

    def set_granularity(self, granularity: str) -> None:
        self._granularity = (GRANULARITY_SENTENCE
                             if granularity == GRANULARITY_SENTENCE
                             else GRANULARITY_WORD)
        self._last_key = None                    # форсувати перемалювання

    def stop(self) -> None:
        was = self._active
        self._active = False
        self._clear()
        if was:
            self._on_stopped()

    def is_active(self) -> bool:
        return self._active

    # --- тік по позиції плеєра (media-ms) ---
    def update(self, position_ms: int) -> None:
        if not self._active:
            return
        if self._text_hash() != self._revision:
            self.stop()                          # текст редагували → зупинка (§8.5)
            return
        if self._granularity == GRANULARITY_SENTENCE:
            key = ("s", _t.sentence_at(self._sentence_starts, position_ms))
            rng = self._sentence_range(position_ms)
        else:
            idx = _t.active_word_index(self._word_timings, position_ms)
            key = ("w", idx)
            rng = None
            if idx >= 0:
                w = self._word_timings[idx]
                rng = (w["raw_start"], w["raw_end"])
        if key == self._last_key:
            return
        self._last_key = key
        if rng is None:
            self._clear()
        else:
            self._apply(rng[0], rng[1])

    # --- внутрішнє ---
    def _sentence_range(self, position_ms: int):
        # ЯКЕ речення грає — за позицією (ms); ЯКІ слова в ньому — за ТЕКСТОВОЮ
        # належністю (word["sentence"], проставлене merge_sentences), НЕ за ms-вікном.
        # Інакше через допуск hifigan (~400 мс) межове «тут.» перед крапкою могло б
        # підсвітитись у наступному реченні (блокер суду §8.5).
        si = _t.sentence_at(self._sentence_starts, position_ms)
        if si < 0 or not self._word_timings:
            return None
        words = [w for w in self._word_timings if w.get("sentence") == si]
        if not words:
            return None
        return (min(w["raw_start"] for w in words),
                max(w["raw_end"] for w in words))

    def _full_text(self) -> str:
        return self._editor.toPlainText()

    def _text_hash(self):
        return hash(self._full_text())

    def _apply(self, raw_start: int, raw_end: int) -> None:
        text = self._full_text()
        u16_start = _t.codepoint_to_utf16(text, raw_start)
        u16_end = _t.codepoint_to_utf16(text, raw_end)
        cursor = QTextCursor(self._editor.document())
        cursor.setPosition(u16_start)
        cursor.setPosition(u16_end, QTextCursor.MoveMode.KeepAnchor)
        sel = QTextEdit.ExtraSelection()
        sel.cursor = cursor
        sel.format = self._fmt
        self._editor.setExtraSelections([sel])    # РІВНО одна ExtraSelection
        self._ensure_visible(u16_end)

    def _ensure_visible(self, u16_pos: int) -> None:
        # автоскрол лише коли слово поза видимою областю
        cur = QTextCursor(self._editor.document())
        cur.setPosition(u16_pos)
        rect = self._editor.cursorRect(cur)
        vp = self._editor.viewport().rect()
        if not vp.contains(rect.center()):
            self._editor.setTextCursor(cur)
            self._editor.ensureCursorVisible()

    def _clear(self) -> None:
        try:
            self._editor.setExtraSelections([])
        except RuntimeError:
            pass

    # для тестів: поточний виділений діапазон (UTF-16 start,end) або None
    def current_selection_utf16(self):
        sels = self._editor.extraSelections()
        if not sels:
            return None
        c = sels[0].cursor
        return (c.selectionStart(), c.selectionEnd())
