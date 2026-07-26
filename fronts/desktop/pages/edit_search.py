"""Панель редагування розшифровки + пошук по тексту (feature/transcript-editing).

Вбудовується під тілом картки (QLabel body) на вкладках «Файли» та «Нарада».
«Редагувати» ховає body, показує QPlainTextEdit; «Зберегти» → apply_text(новий),
«Скасувати» → відкат. Пошук (Ctrl+F у межах перегляду) підсвічує збіги в
редакторі, показує лічильник N/M і дає кнопки вгору/вниз.

Логіка пошуку (позиції збігів, циклічна навігація) — у fronts.desktop.textsearch
(чиста, покрита unittest). Тут — лише Qt-обв'язка.
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QLineEdit, QLabel,
    QTextEdit,
)

from .. import theme   # нічний режим: кольори підсвітки читаємо з активної палітри
from ..glass import GlassButton
from ..i18n import tr
from ..textsearch import find_matches, step_index


def _hl_colors():
    """Кольори підсвітки збігів пошуку — ЖИВЛЯТЬСЯ з активної палітри
    (theme.ACCENT_RGB): золото вдень, червоне вночі. Рахуємо при кожному рендері,
    бо модуль-рівневі константи не оновились би на живому свопі теми (мілітарі-
    вимога «жодного не-червоного світла» вночі). Повертає (активний, решта)."""
    r, g, b = theme.ACCENT_RGB
    return QColor(r, g, b, 110), QColor(r, g, b, 45)


class _Editor(QPlainTextEdit):
    """QPlainTextEdit, що відкриває пошук на Ctrl+F (у межах перегляду)."""

    def __init__(self, on_find, parent=None):
        super().__init__(parent)
        self._on_find = on_find

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key_F
                and event.modifiers() & Qt.ControlModifier):
            self._on_find()
            return
        super().keyPressEvent(event)


class TranscriptEditPanel(QWidget):
    """body — QLabel тіла картки (ховаємо на час правки). get_text() — поточний
    текст для редактора. apply_text(new) — застосувати збережений текст (оновити
    body й записати у сховище); викликається лише коли текст справді змінився."""

    def __init__(self, body, get_text, apply_text, parent=None, ai_edit_fn=None):
        super().__init__(parent)
        self._body = body
        self._get_text = get_text
        self._apply_text = apply_text
        self._ai_edit_fn = ai_edit_fn          # feature/voice-edit-selection (Scenario B)
        self._matches = []
        self._cur = -1

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # --- рядок пошуку (прихований, поки не Ctrl+F) ---
        self._search_row = QWidget()
        sr = QHBoxLayout(self._search_row)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.setSpacing(8)
        self._search = QLineEdit()
        self._search.setPlaceholderText(tr("edit_search_placeholder"))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_query)
        self._search.returnPressed.connect(lambda: self._step(True))
        self._count = QLabel("0/0")
        self._count.setProperty("muted", True)
        prev = GlassButton("↑")   # ↑ попередній збіг
        prev.setToolTip(tr("edit_search_prev"))
        prev.setFixedWidth(44)
        prev.clicked.connect(lambda: self._step(False))
        nxt = GlassButton("↓")    # ↓ наступний збіг
        nxt.setToolTip(tr("edit_search_next"))
        nxt.setFixedWidth(44)
        nxt.clicked.connect(lambda: self._step(True))
        sr.addWidget(self._search, stretch=1)
        sr.addWidget(self._count)
        sr.addWidget(prev)
        sr.addWidget(nxt)
        self._search_row.hide()
        root.addWidget(self._search_row)

        # --- редактор (прихований, поки не «Редагувати») ---
        self._editor = _Editor(self._toggle_search)
        self._editor.setMinimumHeight(120)
        self._editor.hide()
        root.addWidget(self._editor)

        # --- дії правки: «Зберегти» / «Скасувати» (приховані, поки не правимо) ---
        self._act_row = QWidget()
        ar = QHBoxLayout(self._act_row)
        ar.setContentsMargins(0, 0, 0, 0)
        ar.setSpacing(10)
        self._save_btn = GlassButton(tr("edit_save"))
        self._save_btn.clicked.connect(self._save)
        self._cancel_btn = GlassButton(tr("edit_cancel"))
        self._cancel_btn.clicked.connect(self._cancel)
        ar.addWidget(self._save_btn)
        ar.addWidget(self._cancel_btn)
        # feature/voice-edit-selection: «AI-редагувати виділене» голосом/командою —
        # діє на виділений у редакторі фрагмент (лише коли ввімкнено callback).
        if self._ai_edit_fn is not None:
            self._ai_btn = GlassButton(tr("cmdedit_edit_selection"))
            self._ai_btn.setAccessibleName(tr("cmdedit_edit_selection"))
            self._ai_btn.setToolTip(tr("cmdedit_edit_selection_tip"))
            self._ai_btn.clicked.connect(self._ai_edit_selection)
            ar.addWidget(self._ai_btn)
        else:
            self._ai_btn = None
        ar.addStretch()
        self._act_row.hide()
        root.addWidget(self._act_row)

        # кнопку «Редагувати» тримає сторінка (у ряду дій картки) — щоб лягала в
        # той самий ряд, що «Копіювати/Зберегти…». Створюємо, віддаємо назовні.
        self.edit_button = GlassButton(tr("edit_edit"))
        self.edit_button.clicked.connect(self.begin_edit)

        # нічний режим: підсвітка вже показаних збігів вшита в extraSelections
        # (своп палітри її сам не перефарбовує) — на живій зміні теми перемальовуємо
        # з поточної гами. Порожній пошук → порожній перемал, без побічних ефектів.
        theme.register_restyle(self._render_highlights)

    # ------------------------------------------------------------- режим правки
    def begin_edit(self):
        self._editor.setPlainText(self._get_text())
        self._body.hide()
        self._editor.show()
        self._act_row.show()
        self.edit_button.hide()
        self._editor.setFocus()

    def _end_edit(self):
        self._toggle_search(force_off=True)
        self._editor.hide()
        self._act_row.hide()
        self.edit_button.show()
        self._body.show()

    def _save(self):
        new = self._editor.toPlainText()
        if new != self._get_text():
            self._apply_text(new)
        self._end_edit()

    def _cancel(self):
        self._end_edit()

    # ------------------------------------------ AI-редагування виділеного (Scenario B)
    def _ai_edit_selection(self):
        """Виділений у редакторі фрагмент → діалог Command Mode; результат заміняє
        саме цей фрагмент (за діапазоном, а не за поточним курсором — модальний
        діалог міг зняти виділення)."""
        if self._ai_edit_fn is None:
            return
        cur = self._editor.textCursor()
        if not cur.hasSelection():
            return
        start, end = cur.selectionStart(), cur.selectionEnd()
        # QPlainTextEdit віддає розриви рядків як U+2029 — вертаємо звичайні \n.
        selected = cur.selectedText().replace(" ", "\n")
        if not selected.strip():
            return

        def replace_with(new_text, s=start, e=end):
            c = self._editor.textCursor()
            c.setPosition(s)
            c.setPosition(e, QTextCursor.KeepAnchor)
            c.insertText(new_text)
            self._editor.setTextCursor(c)

        self._ai_edit_fn(selected, replace_with)

    # ------------------------------------------------------------------- пошук
    def _toggle_search(self, force_off=False):
        show = False if force_off else not self._search_row.isVisible()
        self._search_row.setVisible(show)
        if show:
            self._search.setFocus()
            self._search.selectAll()
            self._on_query(self._search.text())
        else:
            self._search.clear()
            self._matches = []
            self._cur = -1
            self._editor.setExtraSelections([])

    def _on_query(self, text):
        self._matches = find_matches(self._editor.toPlainText(), text)
        self._cur = 0 if self._matches else -1
        self._render_highlights()
        self._scroll_to_current()

    def _step(self, forward: bool):
        if not self._matches:
            return
        self._cur = step_index(self._cur, len(self._matches), forward)
        self._render_highlights()
        self._scroll_to_current()

    def _render_highlights(self):
        hl_current, hl_other = _hl_colors()   # з активної палітри (день/ніч)
        selections = []
        for idx, (start, end) in enumerate(self._matches):
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(hl_current if idx == self._cur else hl_other)
            cur = self._editor.textCursor()
            cur.setPosition(start)
            cur.setPosition(end, QTextCursor.KeepAnchor)
            sel.cursor = cur
            selections.append(sel)
        self._editor.setExtraSelections(selections)
        total = len(self._matches)
        shown = (self._cur + 1) if self._cur >= 0 else 0
        self._count.setText(f"{shown}/{total}")

    def _scroll_to_current(self):
        # Курсор ставимо на початок збігу БЕЗ виділення (виділений збіг легко
        # затерти набором) — підсвітка йде через extra selections, а курсор лише
        # прокручує в'юпорт до активного збігу.
        if self._cur < 0 or self._cur >= len(self._matches):
            return
        start, _end = self._matches[self._cur]
        cur = self._editor.textCursor()
        cur.setPosition(start)
        self._editor.setTextCursor(cur)
        self._editor.ensureCursorVisible()
