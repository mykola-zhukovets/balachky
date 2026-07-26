"""Заповнення шаблону голосом — тонкий UI над whisper_core.formfill (feature/
voice-form-fill).

Модальний діалог: вибір шаблону → показ тексту з підсвіченим ПОТОЧНИМ полем →
диктант пише у це поле (через controller.formfill_text) → навігація кнопками або
голосом («наступне поле» / «попереднє поле») → «Скопіювати результат»/«Вставити».

Уся логіка (парсер полів, курсор, підстановка, розбір команд) — у
whisper_core.formfill під юніт-тестами. Тут лише Qt-звʼязки й рендер.
"""
import html
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout,
    QWidget,
)

from whisper_core import formfill

from .glass import GlassButton
from .i18n import tr
from . import theme   # нічний режим: підсвітка полів читає палітру наживо


class FormFillDialog(QDialog):
    """exec()-модалка. Живе, доки відкрита; на час диктанту у поле вмикає
    controller.formfill_capturing, щоб текст не пішов у фонове вікно."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.session = None          # formfill.FormSession активного шаблону
        self.setWindowTitle(tr("formfill_title"))
        self.setMinimumSize(560, 560)

        outer = QVBoxLayout(self)
        card = QFrame()
        card.setProperty("card", True)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)
        outer.addWidget(card)

        eyebrow = QLabel(tr("formfill_title"))
        eyebrow.setProperty("eyebrow", True)
        lay.addWidget(eyebrow)
        sub = QLabel(tr("formfill_subtitle"))
        sub.setProperty("hint", True)
        sub.setWordWrap(True)
        lay.addWidget(sub)

        # --- вибір шаблону ---
        pickrow = QHBoxLayout()
        pickrow.setSpacing(10)
        picklbl = QLabel(tr("formfill_pick_label"))
        picklbl.setProperty("formlabel", True)
        pickrow.addWidget(picklbl)
        self._combo = QComboBox()
        self._combo.setMinimumWidth(240)
        pickrow.addWidget(self._combo, stretch=1)
        openbtn = GlassButton(tr("formfill_open_folder"))
        openbtn.clicked.connect(self._open_folder)
        pickrow.addWidget(openbtn)
        lay.addLayout(pickrow)

        # --- поточне поле + підказка диктанту ---
        self._field_lbl = QLabel()
        self._field_lbl.setProperty("block", True)
        lay.addWidget(self._field_lbl)
        self._capture_hint = QLabel(tr("formfill_capture_hint"))
        self._capture_hint.setProperty("hint", True)
        self._capture_hint.setWordWrap(True)
        lay.addWidget(self._capture_hint)

        # --- превʼю шаблону з підсвіткою ---
        self._preview = QLabel()
        self._preview.setTextFormat(Qt.RichText)
        self._preview.setWordWrap(True)
        self._preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._preview.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        pvhost = QWidget()
        pvlay = QVBoxLayout(pvhost)
        pvlay.setContentsMargins(4, 4, 4, 4)
        pvlay.addWidget(self._preview)
        pvlay.addStretch()
        scroll.setWidget(pvhost)
        lay.addWidget(scroll, stretch=1)

        # --- навігація полями ---
        navrow = QHBoxLayout()
        navrow.setSpacing(10)
        self._prev_btn = GlassButton(tr("formfill_prev"))
        self._prev_btn.clicked.connect(self._prev)
        self._next_btn = GlassButton(tr("formfill_next"))
        self._next_btn.clicked.connect(self._next)
        self._clear_btn = GlassButton(tr("formfill_clear"))
        self._clear_btn.clicked.connect(self._clear)
        navrow.addWidget(self._prev_btn)
        navrow.addWidget(self._next_btn)
        navrow.addWidget(self._clear_btn)
        navrow.addStretch()
        lay.addLayout(navrow)

        # --- результат ---
        outrow = QHBoxLayout()
        outrow.setSpacing(10)
        self._status = QLabel()
        self._status.setProperty("hint", True)
        outrow.addWidget(self._status)
        outrow.addStretch()
        self._copy_btn = GlassButton(tr("formfill_copy"))
        self._copy_btn.clicked.connect(self._copy)
        self._paste_btn = GlassButton(tr("formfill_paste"))
        self._paste_btn.clicked.connect(self._paste)
        outrow.addWidget(self._copy_btn)
        outrow.addWidget(self._paste_btn)
        lay.addLayout(outrow)

        self._combo.currentIndexChanged.connect(self._on_pick)
        controller.formfill_text.connect(self._on_dictation)
        self._reload_templates()

    # --- сховище / вибір ---------------------------------------------------
    def _reload_templates(self):
        self._templates = formfill.list_templates()
        self._combo.blockSignals(True)
        self._combo.clear()
        for p in self._templates:
            self._combo.addItem(p.stem, str(p))
        self._combo.blockSignals(False)
        if self._templates:
            self._on_pick(0)
        else:
            self.session = None
            self._field_lbl.setText(tr("formfill_no_templates"))
            self._preview.clear()
            self._set_controls(False)

    def _on_pick(self, index):
        if not (0 <= index < len(self._templates)):
            return
        text = formfill.load_template(self._templates[index])
        self.session = formfill.FormSession(text)
        self._refresh()

    def _open_folder(self):
        from whisper_core import paths
        d = paths.templates_dir()
        try:
            import os
            os.startfile(str(d))     # noqa: S606 — Windows-only, довірена своя тека
        except (OSError, AttributeError):
            logging.warning("Не вдалося відкрити папку шаблонів %s", d)

    # --- навігація ---------------------------------------------------------
    def _next(self):
        if self.session:
            self.session.next_field()
            self._refresh()

    def _prev(self):
        if self.session:
            self.session.prev_field()
            self._refresh()

    def _clear(self):
        if self.session:
            self.session.clear_current()
            self._refresh()

    # --- диктант -----------------------------------------------------------
    def _on_dictation(self, text: str):
        """Слот controller.formfill_text: спершу команда навігації, інакше —
        дописати у поточне поле."""
        if not self.session or not self.isVisible():
            return
        cmd = formfill.match_nav_command(text, self.controller.cfg.language)
        if cmd == "next":
            self.session.next_field()
        elif cmd == "prev":
            self.session.prev_field()
        else:
            self.session.append_value(text)
        self._refresh()

    # --- результат ---------------------------------------------------------
    def _copy(self):
        if self.session:
            from PySide6.QtGui import QGuiApplication
            QGuiApplication.clipboard().setText(self.session.render())
            self._status.setText(tr("formfill_copied"))

    def _paste(self):
        """Скопіювати результат і доставити його існуючим paste-шляхом
        (той самий, що й диктант). Робимо це поза captured-режимом."""
        if not self.session:
            return
        from PySide6.QtGui import QGuiApplication
        result = self.session.render()
        QGuiApplication.clipboard().setText(result)
        deliver = getattr(self.controller, "deliver_text", None)
        if callable(deliver):
            deliver(result)
        self._status.setText(tr("formfill_copied"))

    # --- captured-режим (ловимо диктант у поле, поки відкрито) --------------
    def showEvent(self, event):
        super().showEvent(event)
        self.controller.formfill_capturing = True

    def closeEvent(self, event):
        self.controller.formfill_capturing = False
        super().closeEvent(event)

    # --- рендер ------------------------------------------------------------
    def _set_controls(self, enabled: bool):
        for b in (self._prev_btn, self._next_btn, self._clear_btn,
                  self._copy_btn, self._paste_btn):
            b.setEnabled(enabled)

    def _refresh(self):
        s = self.session
        if s is None:
            return
        has_fields = bool(s.fields)
        self._set_controls(has_fields)
        self._capture_hint.setVisible(has_fields)
        if not has_fields:
            self._field_lbl.setText(tr("formfill_no_fields"))
        elif s.is_complete:
            self._field_lbl.setText(tr("formfill_done"))
        else:
            self._field_lbl.setText(
                tr("formfill_field_current", name=s.current_field))
        self._preview.setText(self._render_html())

    def _render_html(self) -> str:
        """Показ шаблону: заповнені поля — значенням, поточне — золотою плашкою,
        решта незаповнених — приглушеним [ім'ям]. Розбір сегментів — у ядрі."""
        s = self.session
        current = s.current_field
        parts = []
        for kind, val in formfill.iter_segments(s.template):
            if kind == "text":
                parts.append(html.escape(val).replace("\n", "<br>"))
                continue
            filled = s.value_of(val)
            if val == current:
                shown = html.escape(filled) if filled \
                    else html.escape(tr("formfill_placeholder_empty"))
                parts.append(
                    f'<span style="background:{theme.GOLD};color:{theme.TEXT_ON_GOLD};'
                    f'border-radius:3px;">&nbsp;{shown}&nbsp;</span>')
            elif filled:
                parts.append(html.escape(filled))
            else:
                parts.append(
                    f'<span style="color:{theme.TEXT_MUTED};">[{html.escape(val)}]</span>')
        return "".join(parts)
