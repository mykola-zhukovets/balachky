"""Права панель розшифровки наради — Етап 2 «Єдиного робочого екрана наради»:
текст живе ПОРУЧ із відео, клацання по репліці перемотує відео, відтворення
підсвічує поточну репліку й прокручує до неї.

ПРОДУКТИВНІСТЬ (``2026-07-31-АУДИТ-довгі-наради.md``): нарада на 3 години дає
1000–3000 реплік. Аудит прямо забороняє два підходи, якими це «природно»
хочеться зробити:

1. Окремий ``QLabel``/``QWidget`` на репліку — Qt захлинається на layout pass
   уже за кількасот віджетів (вузьке місце №4 аудиту). Тут натомість
   ``QListView`` + ``QAbstractListModel``: модель тримає лише легкі дані
   (Utterance), делегат малює рядок напряму ``QPainter`` без створення
   віджетів, і в’юпорт рендерить ЛИШЕ видимі рядки.
2. Монолітний ``QLabel.setText(html)`` з перебудовою всього HTML на кожен
   ``positionChanged`` (вузьке місце №1 аудиту, ~15-40 мс на виклик при
   60 Гц — 100% ядра ЦП). Тут підсвічування — точковий ``dataChanged`` на
   ДВА рядки (стара активна репліка й нова), решта моделі не займана.

Пошук активної репліки за часом — ``bisect`` по відсортованому масиву
стартів (O(log N) ≈ 11 порівнянь на 3000 реплік), а не лінійний прохід
(вузьке місце №2 аудиту).

Висота рядка ФІКСОВАНА (``setUniformItemSizes(True)``) — Qt тоді не рахує
``sizeHint`` для кожного з тисяч рядків при відкритті (O(1) замість O(N)).
Ціна: довгий текст репліки обрізається по висоті рядка (без «…» — просто
клipається, як і кешовані шляхи в проєкті). Це свідомий вибір швидкості
над красою для нарад на сотні/тисячі реплік; повний текст лишається
доступним через tooltip рядка.
"""
from __future__ import annotations

import bisect
import re

from PySide6.QtCore import QAbstractListModel, QModelIndex, QPointF, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QTextCharFormat, QTextLayout, QTextOption
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QLineEdit, QListView,
    QStyledItemDelegate, QVBoxLayout, QWidget,
)

from . import theme
from .i18n import tr
from .player import _IconButton
from whisper_core.meeting.postprocess import SPK_ME, SPK_OTHERS, _speaker_label

_ACTIVE_ROLE = Qt.UserRole + 1
# Діапазон (char_start, char_len) активного слова в u.text активної репліки —
# None/(x, 0), якщо слів немає, підсвічування вимкнене чи позиція в паузі
# між словами (крайовий випадок).
_ACTIVE_WORD_RANGE_ROLE = Qt.UserRole + 2
# Список (char_start, char_len, is_active) збігів пошуку (Ctrl+F) у тексті
# репліки цього рядка — порожній список, якщо запиту немає чи збігів нема.
_SEARCH_SPANS_ROLE = Qt.UserRole + 3

_ROW_HEIGHT = 78     # фіксована (uniform) висота рядка — O(1) layout на будь-яку кількість реплік
_BADGE_D = 10
_PAD = 10

_WORD_RE = re.compile(r"\S+")


def _word_spans_for(text: str, start_s: float, end_s: float):
    """(char_start, char_len, start_ms, end_ms) для кожного слова репліки.

    Пословних позначок часу з конвеєра ``Utterance`` сьогодні не несе (лише
    ``word_ids`` — посилання на ledger, без таймкодів на боці UI).
    Тому час слова синтезуємо рівномірним діленням тривалості репліки на
    кількість слів — той самий фолбек, що ``_synthesize_timed_words`` у
    ``meeting_pipeline.py`` застосовує для ASR-сегментів без пословних міток."""
    matches = list(_WORD_RE.finditer(text or ""))
    n = len(matches)
    if n == 0:
        return []
    start_ms = int(round(start_s * 1000))
    end_ms = int(round(end_s * 1000))
    dur = max(0, end_ms - start_ms)
    spans = []
    for i, m in enumerate(matches):
        w_start = start_ms + (dur * i) // n
        w_end = end_ms if i == n - 1 else start_ms + (dur * (i + 1)) // n
        spans.append((m.start(), m.end() - m.start(), w_start, w_end))
    return spans


def _speaker_color(speaker: str) -> str:
    """Код мовця → колір позначки. Канон теми: золото — ЄДИНИЙ хроматичний
    акцент (жодного нового відтінку), тож діаризованих speaker_N розрізняємо
    насиченістю золота, а не кольором."""
    if speaker == SPK_ME:
        return theme.GOLD
    if speaker == SPK_OTHERS:
        return theme.TEXT_MUTED
    if speaker.startswith("speaker_"):
        tints = (theme.GOLD, theme._GOLD_65, theme._GOLD_35, theme._GOLD_15)
        try:
            n = int(speaker.rsplit("_", 1)[-1])
        except ValueError:
            n = 0
        return tints[n % len(tints)]
    return theme.TEXT_MUTED


def _fmt_stamp(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class UtteranceListModel(QAbstractListModel):
    """Легка модель над списком ``Utterance`` — жодних Qt-віджетів на репліку.
    ``speaker_names`` — власні імена мовців (діаризація/multimic), як у
    ``postprocess._speaker_label``."""

    def __init__(self, utterances, speaker_names=None, me_label="", others_label="",
                 parent=None):
        super().__init__(parent)
        self._utterances = list(utterances or [])
        self._speaker_names = dict(speaker_names or {})
        self._me_label = me_label
        self._others_label = others_label
        self._active_row = -1
        self._active_word_idx = -1
        self._word_hilite_enabled = True
        self._last_pos_ms = 0
        # Індекс для bisect: старти в мілісекундах, СУВОРО за порядком реплік
        # (список наради вже хронологічний — stitch() це гарантує).
        self._starts_ms = [int(round(u.start * 1000)) for u in self._utterances]
        self._ends_ms = [int(round(u.end * 1000)) for u in self._utterances]
        # Символьні й часові межі слів — рахуються ОДИН раз при ініціалізації
        # моделі (не читати/парсити текст повторно на кожен тик).
        self._word_spans = [
            _word_spans_for(u.text, u.start, u.end) for u in self._utterances]
        self._word_starts_ms = [
            [s[2] for s in spans] for spans in self._word_spans]
        # Пошук у нараді (Ctrl+F): плаский хронологічний список збігів
        # ``(row, char_start, char_len)`` + індекс за рядком для делегата.
        self._search_query = ""
        self._matches = []
        self._active_match_idx = -1
        self._row_matches = {}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._utterances)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        u = self._utterances[row]
        if role == Qt.DisplayRole or role == Qt.ToolTipRole:
            return u.text
        if role == _ACTIVE_ROLE:
            return row == self._active_row
        if role == _ACTIVE_WORD_RANGE_ROLE:
            if row == self._active_row and self._active_word_idx >= 0:
                spans = self._word_spans[row]
                if 0 <= self._active_word_idx < len(spans):
                    char_start, char_len, _, _ = spans[self._active_word_idx]
                    return (char_start, char_len)
            return None
        if role == _SEARCH_SPANS_ROLE:
            row_matches = self._row_matches.get(row)
            if not row_matches:
                return []
            return [(start, length, gidx == self._active_match_idx)
                    for start, length, gidx in row_matches]
        return None

    def utterance_at(self, row: int):
        if 0 <= row < len(self._utterances):
            return self._utterances[row]
        return None

    def speaker_label(self, speaker: str) -> "str | None":
        return _speaker_label(
            speaker, me_label=self._me_label, others_label=self._others_label,
            speaker_names=self._speaker_names, show_source=True)

    def row_for_ms(self, pos_ms: int) -> int:
        """Bisect по стартах: остання репліка, чий старт <= pos_ms, і лише
        якщо ``pos_ms`` ще в її межах (інакше — пауза між репліками, без
        активного рядка, як і в старому TranscriptViewer)."""
        if not self._starts_ms:
            return -1
        idx = bisect.bisect_right(self._starts_ms, pos_ms) - 1
        if idx < 0:
            return -1
        if pos_ms <= self._ends_ms[idx]:
            return idx
        return -1

    def word_for_ms(self, row: int, pos_ms: int) -> int:
        """Bisect по стартах слів усередині репліки ``row``. -1 — репліка
        поза межами/без слів, або ``pos_ms`` у паузі МІЖ словами (репліка
        лишається активною, але жодне слово не підсвічується)."""
        if not (0 <= row < len(self._word_spans)):
            return -1
        spans = self._word_spans[row]
        if not spans:
            return -1
        starts = self._word_starts_ms[row]
        idx = bisect.bisect_right(starts, pos_ms) - 1
        if idx < 0:
            return -1
        if pos_ms <= spans[idx][3]:
            return idx
        return -1

    def set_word_highlight_enabled(self, enabled: bool) -> bool:
        """Тумблер «підсвічувати слова». Вимкнено — ``word_for_ms`` більше не
        опитується на кожен тик (нульове додаткове навантаження),
        активне слово скидається негайно."""
        enabled = bool(enabled)
        if enabled == self._word_hilite_enabled:
            return False
        self._word_hilite_enabled = enabled
        return self.set_active_pos(self._last_pos_ms)

    def set_active_pos(self, pos_ms: int) -> bool:
        """Єдина точка входу для ``positionChanged`` плеєра: знаходить
        активну репліку й активне слово, порівнює з кешем стану і РАННІМ
        ВИХОДОМ (``return False``, БЕЗ ``dataChanged``) відсікає 90-95%
        зайвих подій — активне слово реально змінюється лише 2-5 разів на
        секунду з ~20-50 тиків. Перемальовує ЛИШЕ рядки, що
        справді змінились (1 — зміна слова в тій самій репліці, 2 — перехід
        між репліками), решту моделі не займає."""
        self._last_pos_ms = pos_ms
        row = self.row_for_ms(pos_ms)
        word_idx = (
            self.word_for_ms(row, pos_ms)
            if (row >= 0 and self._word_hilite_enabled) else -1)
        if row == self._active_row and word_idx == self._active_word_idx:
            return False
        old_row = self._active_row
        self._active_row = row
        self._active_word_idx = word_idx
        if old_row == row:
            if old_row >= 0:
                i = self.index(old_row, 0)
                self.dataChanged.emit(i, i, [_ACTIVE_WORD_RANGE_ROLE])
        else:
            if old_row >= 0:
                i = self.index(old_row, 0)
                self.dataChanged.emit(i, i, [_ACTIVE_ROLE, _ACTIVE_WORD_RANGE_ROLE])
            if row >= 0:
                i = self.index(row, 0)
                self.dataChanged.emit(i, i, [_ACTIVE_ROLE, _ACTIVE_WORD_RANGE_ROLE])
        return True

    # ------------------------------------------------------------- пошук (Ctrl+F)
    def set_search_query(self, query: str) -> None:
        """Перебудовує плаский список збігів для нового запиту. Регістр
        ігнорується (``lower()`` з обох боків). Порожній/пробільний запит —
        збігів немає, без винятків. Активним стає перший збіг (індекс 0), як
        і очікує рядок пошуку одразу після введення тексту."""
        old_rows = set(self._row_matches.keys())
        q = (query or "").strip().lower()
        self._search_query = q
        matches = []
        row_matches = {}
        if q:
            for row, u in enumerate(self._utterances):
                text_lower = (u.text or "").lower()
                found = []
                start = 0
                while True:
                    idx = text_lower.find(q, start)
                    if idx == -1:
                        break
                    gidx = len(matches)
                    matches.append((row, idx, len(q)))
                    found.append((idx, len(q), gidx))
                    start = idx + len(q)
                if found:
                    row_matches[row] = found
        self._matches = matches
        self._row_matches = row_matches
        self._active_match_idx = 0 if matches else -1
        for row in old_rows | set(row_matches.keys()):
            i = self.index(row, 0)
            self.dataChanged.emit(i, i, [_SEARCH_SPANS_ROLE])

    def search_status(self):
        """``(1-based номер активного збігу, загальна кількість)``; ``(0, 0)``
        коли збігів немає (порожній запит або жодного входження)."""
        if not self._matches:
            return (0, 0)
        return (self._active_match_idx + 1, len(self._matches))

    def navigate_match(self, step: int):
        """Перейти на ``step`` позицій по колу (``+1``/``-1`` — Enter/Shift+Enter
        чи кнопки [▼]/[▲]). Повертає номер рядка нового активного збігу, або
        ``None``, якщо збігів немає."""
        n = len(self._matches)
        if n == 0:
            return None
        old_idx = self._active_match_idx
        if old_idx < 0:
            new_idx = 0 if step >= 0 else n - 1
        else:
            new_idx = (old_idx + step) % n
        old_row = self._matches[old_idx][0] if old_idx >= 0 else -1
        new_row = self._matches[new_idx][0]
        self._active_match_idx = new_idx
        for row in {old_row, new_row} - {-1}:
            i = self.index(row, 0)
            self.dataChanged.emit(i, i, [_SEARCH_SPANS_ROLE])
        return new_row

    def active_match_row(self):
        """Рядок поточного активного збігу, або ``-1``, якщо збігів немає."""
        if 0 <= self._active_match_idx < len(self._matches):
            return self._matches[self._active_match_idx][0]
        return -1


class _UtteranceDelegate(QStyledItemDelegate):
    """Малює бейдж мовця + тайм-код + текст репліки напряму ``QPainter`` —
    без жодного дочірнього віджета на рядок (тисячі реплік лишаються
    швидкими: делегат викликається лише для видимих рядків)."""

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), _ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index):
        model = index.model()
        u = model.utterance_at(index.row())
        if u is None:
            return
        active = bool(index.data(_ACTIVE_ROLE))
        active_word = index.data(_ACTIVE_WORD_RANGE_ROLE)
        search_spans = index.data(_SEARCH_SPANS_ROLE)
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if active:
            painter.fillRect(rect, QColor(theme._LINE_HILITE))

        x = rect.x() + _PAD
        y = rect.y() + _PAD
        label = model.speaker_label(u.speaker)
        header_h = 0
        if label:
            color = _speaker_color(u.speaker)
            painter.setBrush(QColor(color))
            painter.setPen(Qt.NoPen)
            dot_y = y + 5
            painter.drawEllipse(x, dot_y, _BADGE_D, _BADGE_D)
            painter.setPen(QColor(color))
            font = painter.font()
            font.setBold(True)
            painter.setFont(font)
            header = f"{label}  ·  {_fmt_stamp(u.start)}"
            painter.drawText(
                QRect(x + _BADGE_D + 6, y, rect.width() - _BADGE_D - 6 - 2 * _PAD, 18),
                Qt.AlignVCenter | Qt.AlignLeft, header)
            header_h = 22
            font.setBold(False)
            painter.setFont(font)
        else:
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(
                QRect(x, y, rect.width() - 2 * _PAD, 18),
                Qt.AlignVCenter | Qt.AlignLeft, _fmt_stamp(u.start))
            header_h = 22

        painter.setPen(QColor(theme.TEXT_BODY))
        body_rect = QRect(x, y + header_h, rect.width() - 2 * _PAD,
                           rect.height() - header_h - _PAD)
        # Свідомо клипаємо, не еліпсуємо: рахувати "…" для тисяч рядків при
        # кожному малюванні дорожче, ніж дати Qt відсікти по rect (uniform
        # висота рядка — швидкість над красою на довгих нарадах).
        self._draw_body(painter, body_rect, u.text, active_word, search_spans)
        painter.restore()

    def _draw_body(self, painter, body_rect, text, active_word, search_spans=None):
        """Малює текст репліки. Без активного слова й без збігів пошуку —
        звичайний ``drawText`` (найшвидший шлях: жоден рядок делегата не
        платить за ``QTextLayout``, доки для нього немає що підсвітити). З
        активним словом чи збігами пошуку — легкий ``QTextLayout`` +
        ``FormatRange`` над самим текстом, БЕЗ ``QLabel``/``QTextDocument``/
        HTML-парсингу (заборонено аудитом продуктивності — докстрінг модуля)."""
        has_word = bool(active_word) and active_word[1] > 0
        has_search = bool(search_spans)
        if not has_word and not has_search:
            painter.drawText(body_rect, Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, text)
            return

        layout = QTextLayout(text, painter.font())
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WordWrap)
        opt.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        layout.setTextOption(opt)

        formats = []
        # Збіги пошуку (Ctrl+F) малюються ПЕРШИМИ — знак активного слова
        # (стрічка відтворення) має пріоритет і йде поверх, якщо позиції
        # перетинаються (рідкісний збіг активного слова й пошукового терміна).
        for start, length, is_active in (search_spans or ()):
            if length <= 0:
                continue
            fmt = QTextLayout.FormatRange()
            fmt.start = start
            fmt.length = length
            char_fmt = QTextCharFormat()
            if is_active:
                char_fmt.setBackground(QColor(theme.GOLD))
                char_fmt.setForeground(QColor(theme.TEXT_ON_GOLD))
            else:
                char_fmt.setBackground(QColor(theme._GOLD_22))
                char_fmt.setForeground(QColor(theme.TEXT_STRONG))
            fmt.format = char_fmt
            formats.append(fmt)

        if has_word:
            char_start, char_len = active_word
            fmt = QTextLayout.FormatRange()
            fmt.start = char_start
            fmt.length = char_len
            char_fmt = QTextCharFormat()
            char_fmt.setBackground(QColor(theme._GOLD_35))
            char_fmt.setForeground(QColor(theme.TEXT_BODY))
            fmt.format = char_fmt
            formats.append(fmt)
        layout.setFormats(formats)

        layout.beginLayout()
        y_cursor = float(body_rect.y())
        max_y = float(body_rect.bottom())
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(body_rect.width())
            if y_cursor + line.height() > max_y + 2:
                break   # відсікаємо, якщо вийшли за фіксовану висоту рядка
            line.setPosition(QPointF(body_rect.x(), y_cursor))
            y_cursor += line.height()
        layout.endLayout()
        layout.draw(painter, QPointF(0, 0))


class _SearchLineEdit(QLineEdit):
    """Поле пошуку в нараді: ``Enter``/``Shift+Enter`` — навігація по збігах
    замість системного «прийняти діалог», ``Escape`` — закрити рядок пошуку
    (не втрачається у батьківському ``QDialog``, як звичайний ``Escape``)."""

    nextRequested = Signal()
    prevRequested = Signal()
    escapePressed = Signal()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                self.prevRequested.emit()
            else:
                self.nextRequested.emit()
            event.accept()
            return
        if key == Qt.Key_Escape:
            self.escapePressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class InMeetingSearchBar(QWidget):
    """Рядок швидкого пошуку (Ctrl+F) над списком реплік: поле вводу,
    лічильник «k з n», навігація [▲]/[▼], закриття [✕].
    Прихована за замовчуванням; ``TranscriptPanel`` показує її на Ctrl+F."""

    queryChanged = Signal(str)
    nextRequested = Signal()
    prevRequested = Signal()
    closeRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self._edit = _SearchLineEdit(self)
        self._edit.setPlaceholderText(tr("meeting_search_placeholder"))
        self._edit.setAccessibleName(tr("meeting_search_placeholder"))
        self._edit.textChanged.connect(self.queryChanged)
        self._edit.nextRequested.connect(self.nextRequested)
        self._edit.prevRequested.connect(self.prevRequested)
        self._edit.escapePressed.connect(self.closeRequested)
        row.addWidget(self._edit, stretch=1)

        self._count_label = QLabel(self)
        self._count_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self._count_label.hide()
        row.addWidget(self._count_label)

        self._prev_btn = _IconButton(
            "fa6s.chevron-up", tr("meeting_search_prev"), parent=self)
        self._prev_btn.clicked.connect(self.prevRequested)
        row.addWidget(self._prev_btn)

        self._next_btn = _IconButton(
            "fa6s.chevron-down", tr("meeting_search_next"), parent=self)
        self._next_btn.clicked.connect(self.nextRequested)
        row.addWidget(self._next_btn)

        self._close_btn = _IconButton(
            "fa6s.xmark", tr("meeting_search_close"), parent=self)
        self._close_btn.clicked.connect(self.closeRequested)
        row.addWidget(self._close_btn)

    def focus_input(self) -> None:
        self._edit.setFocus()
        self._edit.selectAll()

    def clear_input(self) -> None:
        self._edit.blockSignals(True)
        self._edit.clear()
        self._edit.blockSignals(False)

    def set_count_text(self, current: int, total: int) -> None:
        """Порожній запит/без тексту — лічильник прихований;
        інакше — «k з n» (``0 з 0``, якщо запит є, а збігів немає)."""
        if not self._edit.text():
            self._count_label.hide()
            return
        self._count_label.setText(
            tr("meeting_search_count").format(current=current, total=total))
        self._count_label.show()


class TranscriptPanel(QWidget):
    """Права панель: заголовок + список реплік. Згортання самої панелі —
    кнопка в рядку контролів ``VideoPlayerDialog`` (``setVisible`` на цьому
    віджеті; QSplitter сам дає їй 0 ширини, коли вона схована).
    ``seekRequested(int)`` — клацання по репліці, аргумент — мілісекунди
    старту репліки."""

    seekRequested = Signal(int)

    def __init__(self, utterances, speaker_names=None, parent=None):
        super().__init__(parent)
        self._model = UtteranceListModel(
            utterances, speaker_names,
            me_label=tr("meeting_speaker_me"), others_label=tr("meeting_speaker_others"))

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel(tr("meeting_transcript_panel_title"))
        title.setProperty("formlabel", True)
        header.addWidget(title)
        header.addStretch()
        # Тумблер «підсвічувати слова» (за замовчуванням увімкнено):
        # рухоме підсвічування може заважати, коли людина читає репліку цілком
        # очима, тож користувач лишається з повним контролем.
        self._word_hilite_btn = _IconButton(
            "fa6s.highlighter", tr("meeting_word_highlight_toggle"), parent=self)
        self._word_hilite_btn.setCheckable(True)
        self._word_hilite_btn.setChecked(True)
        self._word_hilite_btn.setStyleSheet(
            self._word_hilite_btn.styleSheet()
            + f"QToolButton:checked {{ background: {theme._GOLD_35}; border-radius: 5px; }}")
        self._word_hilite_btn.toggled.connect(self._on_word_hilite_toggled)
        header.addWidget(self._word_hilite_btn)
        root.addLayout(header)

        # Рядок пошуку (Ctrl+F) — прихований до першого виклику open_search().
        self._search_bar = InMeetingSearchBar(self)
        self._search_bar.hide()
        self._search_bar.queryChanged.connect(self._on_search_query)
        self._search_bar.nextRequested.connect(lambda: self._navigate_search(1))
        self._search_bar.prevRequested.connect(lambda: self._navigate_search(-1))
        self._search_bar.closeRequested.connect(self.close_search)
        root.addWidget(self._search_bar)

        self._view = QListView(self)
        self._view.setModel(self._model)
        self._view.setItemDelegate(_UtteranceDelegate(self._view))
        self._view.setUniformItemSizes(True)     # O(1) layout — див. докстрінг модуля
        self._view.setSelectionMode(QAbstractItemView.NoSelection)
        self._view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._view.setAccessibleName(tr("meeting_transcript_panel_title"))
        self._view.clicked.connect(self._on_clicked)
        root.addWidget(self._view, stretch=1)

        theme.register_restyle(self._restyle)

    def _restyle(self) -> None:
        # Делегат читає theme.* напряму при кожному малюванні — потрібен лише
        # форс-репейнт після зміни теми (день/ніч), сам колір не кешується.
        self._view.viewport().update()

    def _on_clicked(self, index):
        u = self._model.utterance_at(index.row())
        if u is not None:
            self.seekRequested.emit(int(round(u.start * 1000)))

    def _on_word_hilite_toggled(self, checked: bool) -> None:
        self._model.set_word_highlight_enabled(checked)

    def set_active_ms(self, pos_ms: int) -> None:
        """Викликати на кожен ``positionChanged`` плеєра: знаходить активну
        репліку й активне слово (O(log N) обидва — bisect) і, якщо активний
        РЯДОК змінився, прокручує до нього (лише коли він поза видимою
        областю; зміна самого слова в тій самій репліці прокрутку не чіпає)."""
        old_row = self._model._active_row
        row = self._model.row_for_ms(pos_ms)
        self._model.set_active_pos(pos_ms)
        if row != old_row and row >= 0:
            idx = self._model.index(row, 0)
            self._view.scrollTo(idx, QAbstractItemView.EnsureVisible)

    # ------------------------------------------------------------- пошук (Ctrl+F)
    def open_search(self) -> None:
        """Показати й сфокусувати рядок пошуку — виклик на ``Ctrl+F`` з
        будь-якого місця відкритого вікна наради."""
        self._search_bar.show()
        self._search_bar.focus_input()

    def close_search(self) -> None:
        """Приховати рядок пошуку, скинути запит і зняти підсвічування
        збігів — ``Escape`` чи кнопка ``[✕]``."""
        self._search_bar.clear_input()
        self._model.set_search_query("")
        self._search_bar.set_count_text(0, 0)
        self._search_bar.hide()
        self._view.setFocus()

    def _on_search_query(self, text: str) -> None:
        self._model.set_search_query(text)
        current, total = self._model.search_status()
        self._search_bar.set_count_text(current, total)
        row = self._model.active_match_row()
        if row >= 0:
            self._reveal_match_row(row)

    def _navigate_search(self, step: int) -> None:
        row = self._model.navigate_match(step)
        current, total = self._model.search_status()
        self._search_bar.set_count_text(current, total)
        if row is not None and row >= 0:
            self._reveal_match_row(row)

    def _reveal_match_row(self, row: int) -> None:
        """Прокрутити список до збігу й синхронно перемотати плеєр на старт
        репліки — той самий канал ``seekRequested``, що й клацання по репліці."""
        idx = self._model.index(row, 0)
        self._view.scrollTo(idx, QAbstractItemView.PositionAtCenter)
        u = self._model.utterance_at(row)
        if u is not None:
            self.seekRequested.emit(int(round(u.start * 1000)))
