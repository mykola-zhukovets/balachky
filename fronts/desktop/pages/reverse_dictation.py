"""Зворотне диктування (feature/reverse-dictation): переслухати своє вимовляння
запису Історії й ВИПРАВИТИ його текст — клавіатурою або голосом.

Унікальний сценарій: запис у Історії диктувань тримає і дослівний оригінал
(raw, verbatim), і — коли пам'ять і тумблер увімкнені — аудіо цього диктування.
Користувач ПЕРЕСЛУХОВУЄ, що саме він сказав, і виправляє розшифровку. raw
ЛИШАЄТЬСЯ цілим (як у транскрипт-редагуванні); переписуємо тільки final і
позначаємо запис «виправлено». Опційно — додати коротку пару «почуто → як
писати» в пам'ять фраз, щоб надалі STT писав правильно сам («я вже виправляв
це вчора» краще за статичний словник).

Плеєр — наявний InlinePlayer; голосове виправлення — наявний Command Mode
контролера (voice_edit_selection) на виділеному в редакторі тексті.
"""
from __future__ import annotations

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QTextEdit, QVBoxLayout,
)

from ..glass import GlassButton
from ..i18n import tr
from ..player import InlinePlayer


class ReverseDictationDialog(QDialog):
    """Переслухати аудіо запису та виправити його текст.

    controller — DesktopApp (потрібні profile, save_correction і, для голосу,
    voice_edit_selection). rec — dict запису історії (ts/raw/final/audio…).
    audio_path — шлях до збереженого аудіо або None (кнопки плеєра не буде).
    autoplay=True — почати відтворення одразу (кнопка «Переслухати» на картці)."""

    def __init__(self, controller, rec, *, audio_path=None, autoplay=False,
                 parent=None):
        super().__init__(parent)
        self._controller = controller
        self._rec = rec if isinstance(rec, dict) else {}
        self._audio_path = str(audio_path) if audio_path else None
        self._player = None
        self._saved = False        # чи збережено виправлення (для того, хто відкрив)
        self._result = None        # LearnResult самонавчання (тост показує власник)
        # feature/selflearn-dict: ЗНІМОК профілю на момент відкриття — навчання
        # піде саме в цей словник, навіть якщо користувач перемкне активний,
        # поки діалог відкритий (ізоляція по словниках).
        self._profile = getattr(controller, "profile", None)

        self._raw = (self._rec.get("raw") or "").strip()
        self._final = (self._rec.get("final") or self._rec.get("raw") or "").strip()

        self.setWindowTitle(tr("revdict_title"))
        self.setModal(True)
        self.setMinimumSize(560, 460)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(12)

        intro = QLabel(tr("revdict_intro"))
        intro.setProperty("muted", True)
        intro.setWordWrap(True)
        lay.addWidget(intro)

        # --- плеєр «Переслухати» (лише коли аудіо збережено) ---
        if self._audio_path:
            play_cap = QLabel(tr("revdict_recording_label"))
            play_cap.setProperty("muted", True)
            lay.addWidget(play_cap)
            self._player = InlinePlayer(self._audio_path)
            lay.addWidget(self._player)
        else:
            note = QLabel(tr("revdict_no_audio_note"))
            note.setProperty("muted", True)
            note.setWordWrap(True)
            lay.addWidget(note)

        # --- дослівно почуте (raw) — читання, коли відрізняється від final ---
        if self._raw and self._raw != self._final:
            verbatim_cap = QLabel(tr("revdict_verbatim_label"))
            verbatim_cap.setProperty("muted", True)
            lay.addWidget(verbatim_cap)
            verbatim = QTextEdit()
            verbatim.setReadOnly(True)
            verbatim.setAcceptRichText(False)
            verbatim.setPlainText(self._raw)
            verbatim.setAccessibleName(tr("revdict_verbatim_label"))
            verbatim.setMaximumHeight(90)
            lay.addWidget(verbatim)

        # --- редактор виправленого тексту (клавіатурою) ---
        edit_cap = QLabel(tr("revdict_edit_label"))
        edit_cap.setProperty("muted", True)
        lay.addWidget(edit_cap)
        self._editor = QTextEdit()
        self._editor.setAcceptRichText(False)
        self._editor.setPlainText(self._final)
        self._editor.setAccessibleName(tr("revdict_edit_label"))
        lay.addWidget(self._editor, stretch=1)

        # --- ряд дій: голосове виправлення виділеного (якщо доступне) ---
        actions = QHBoxLayout()
        actions.setSpacing(8)
        if hasattr(self._controller, "voice_edit_selection"):
            self._voice_btn = GlassButton(tr("revdict_voice_fix"))
            self._voice_btn.setAccessibleName(tr("revdict_voice_fix"))
            self._voice_btn.clicked.connect(self._voice_fix)
            actions.addWidget(self._voice_btn)
        actions.addStretch()
        lay.addLayout(actions)

        # feature/selflearn-dict: окремого чекбокса «запам'ятати» більше немає —
        # збереження виправлення САМЕ вчить активний словник безпечним правилом
        # («виправив раз — назавжди»), а тост підсумку скаже, що саме запам'ятали.

        # --- нижній ряд: Зберегти / Скасувати ---
        btns = QHBoxLayout()
        btns.addStretch()
        cancel = GlassButton(tr("common_cancel"))
        cancel.setAccessibleName(tr("common_cancel"))
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self._save_btn = GlassButton(tr("revdict_save"))
        self._save_btn.setProperty("accent", True)
        self._save_btn.setAccessibleName(tr("revdict_save"))
        self._save_btn.clicked.connect(self._on_save)
        btns.addWidget(self._save_btn)
        lay.addLayout(btns)

        self._editor.setFocus()
        if self._audio_path and autoplay:
            # почати відтворення після старту циклу подій діалогу
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._player.play_from(0.0))

    @property
    def saved(self) -> bool:
        return self._saved

    def _voice_fix(self):
        """Виправити голосом ВИДІЛЕНЕ в редакторі (наявний Command Mode). Нема
        виділення → беремо весь текст. Результат заміняє виділення в редакторі."""
        cursor = self._editor.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.Document)
            self._editor.setTextCursor(cursor)
        # QTextEdit віддає розриви як U+2029 — нормалізуємо у звичайні переводи рядка
        selected = cursor.selectedText().replace(" ", "\n")
        if not selected.strip():
            return
        target = self._editor.textCursor()   # тримає те саме виділення

        def apply_new(new_text):
            target.insertText(new_text or "")

        self._controller.voice_edit_selection(selected, apply_new, parent=self)

    def _on_save(self):
        """Зберегти виправлення: переписати final (raw цілий), позначити
        «виправлено» і САМЕ навчити активний словник безпечним правилом
        (feature/selflearn-dict). Підсумок (тост + undo) показує власник діалогу
        після закриття — на сторінці Історії, а не на модалці, що зникає."""
        new_final = self._editor.toPlainText().strip()
        if not new_final or new_final == self._final:
            self.reject()          # нема змін — нічого не пишемо
            return
        try:
            self._result = self._controller.apply_correction(
                self._rec, new_final, profile=self._profile)
            self._saved = True
        except Exception:
            import logging
            logging.exception("Не вдалося зберегти виправлення зворотного диктування")
        self._stop_player()
        self.accept()

    def _stop_player(self):
        if self._player is not None:
            try:
                self._player.stop()   # звільнити файловий хендл (Windows)
            except Exception:
                pass

    def reject(self):
        self._stop_player()
        super().reject()
