"""Діалог «Як вимовляти слово» (§6.4, Хвиля 4) — «почув → виправив → прев'ю → зберіг».

Два таби (за патерном NVDA/Voice Dream): «Простими словами» (text_replace, дефолт) і
«Точний наголос» (клік по голосній ставить U+0301 — користувач НЕ вводить Unicode).
Кнопка «Прослухати як звучатиме» — прев'ю ДО збереження (обовʼязково). Валідація ПЕРЕД
збереженням (криве правило не зникає мовчки — NVDA #11407). «Поширити на відмінки?» —
pymorphy3-форми з ПІДТВЕРДЖЕННЯМ. Список збережених правил + «Видалити» (undo, §6).
Режим збігу — Ціле слово / Будь-де (regex прибрано у v1 — ReDoS, суд хвилі 4).

Колбеки звʼязує app: on_preview, on_save(...)→status, on_forms, on_list()→rules,
on_delete(id). Панель самодостатня для visual_gate."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QRadioButton, QTabWidget, QVBoxLayout, QWidget)

from .glass import GlassButton
from .i18n import tr
from whisper_core.tts import lexicon as _lex

_VOWELS = set("аеиіоуяюєїАЕИІОУЯЮЄЇ")
_STRESS = "́"


class PronunciationDialog(QDialog):
    def __init__(self, parent=None, *, word="", on_preview=None, on_save=None,
                 on_forms=None, on_list=None, on_delete=None):
        super().__init__(parent)
        self.setWindowTitle(tr("tts_pron_title"))
        self.setMinimumWidth(480)
        self._on_preview = on_preview or (lambda match, value, ctype: None)
        self._on_save = on_save or (lambda **kw: "learned")
        self._on_forms = on_forms or (lambda w: [w])
        self._on_list = on_list or (lambda: [])
        self._on_delete = on_delete or (lambda rid: None)
        self._stress_value = word

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        eyebrow = QLabel(tr("tts_pron_title"))
        eyebrow.setProperty("eyebrow", True)
        outer.addWidget(eyebrow)

        wrow = QWidget()
        wl = QHBoxLayout(wrow)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(QLabel(tr("tts_pron_word")))
        self._word = QLineEdit(word)
        self._word.textChanged.connect(self._rebuild_stress_row)
        wl.addWidget(self._word, stretch=1)
        outer.addWidget(wrow)

        # режим збігу: Ціле слово / Будь-де (regex прибрано у v1 — ReDoS)
        mrow = QWidget()
        ml = QHBoxLayout(mrow)
        ml.setContentsMargins(0, 0, 0, 0)
        self._mode_group = QButtonGroup(self)
        self._mode_word = QRadioButton(tr("tts_pron_match_word"))
        self._mode_word.setChecked(True)
        self._mode_anywhere = QRadioButton(tr("tts_pron_match_anywhere"))
        self._mode_group.addButton(self._mode_word)
        self._mode_group.addButton(self._mode_anywhere)
        ml.addWidget(self._mode_word)
        ml.addWidget(self._mode_anywhere)
        ml.addStretch(1)
        outer.addWidget(mrow)

        self._tabs = QTabWidget()
        t1 = QWidget()
        t1l = QVBoxLayout(t1)
        self._simple = QLineEdit(word)
        t1l.addWidget(QLabel(tr("tts_pron_tab_simple")))
        t1l.addWidget(self._simple)
        self._tabs.addTab(t1, tr("tts_pron_tab_simple"))
        t2 = QWidget()
        t2l = QVBoxLayout(t2)
        t2l.addWidget(QLabel(tr("tts_pron_stress_hint")))
        self._stress_row = QWidget()
        self._stress_row_l = QHBoxLayout(self._stress_row)
        self._stress_row_l.setContentsMargins(0, 0, 0, 0)
        t2l.addWidget(self._stress_row)
        self._stress_preview = QLabel(word)
        t2l.addWidget(self._stress_preview)
        note = QLabel(tr("tts_pron_custom_engine_note"))
        note.setProperty("card_note", True)
        note.setWordWrap(True)
        t2l.addWidget(note)
        self._tabs.addTab(t2, tr("tts_pron_tab_stress"))
        outer.addWidget(self._tabs)
        self._rebuild_stress_row(word)

        self._forms_ask = QCheckBox(tr("tts_pron_forms_ask"))
        self._forms_ask.toggled.connect(self._on_forms_toggled)
        outer.addWidget(self._forms_ask)
        self._forms_list = QListWidget()
        self._forms_list.setVisible(False)
        self._forms_list.setMaximumHeight(110)
        outer.addWidget(self._forms_list)

        self._error = QLabel("")
        self._error.setProperty("card_note", True)
        self._error.setWordWrap(True)
        outer.addWidget(self._error)

        brow = QWidget()
        bl = QHBoxLayout(brow)
        bl.setContentsMargins(0, 0, 0, 0)
        self._preview_btn = GlassButton(tr("tts_pron_preview"))
        self._preview_btn.setToolTip(tr("hint_tts_pron_preview"))
        self._preview_btn.clicked.connect(self._do_preview)
        bl.addWidget(self._preview_btn)
        self._save_btn = GlassButton(tr("tts_pron_save"))
        self._save_btn.clicked.connect(self._do_save)
        bl.addWidget(self._save_btn)
        bl.addStretch(1)
        outer.addWidget(brow)

        # список збережених правил + видалення (undo, §6 БЛОКЕР 2 суду)
        outer.addWidget(QLabel(tr("tts_pron_menu")))
        self._rules_list = QListWidget()
        self._rules_list.setMaximumHeight(140)
        outer.addWidget(self._rules_list)
        self._delete_btn = GlassButton(tr("tts_pron_delete"))
        self._delete_btn.clicked.connect(self._do_delete)
        outer.addWidget(self._delete_btn)
        self._refresh_rules()

    # --- наголос по кліку ---
    def _rebuild_stress_row(self, word):
        while self._stress_row_l.count():
            item = self._stress_row_l.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._stress_base = word or ""
        self._stress_pos = -1
        for i, ch in enumerate(self._stress_base):
            if ch in _VOWELS:
                btn = GlassButton(ch)
                btn.clicked.connect(lambda _=False, idx=i: self._place_stress(idx))
                self._stress_row_l.addWidget(btn)
        self._stress_row_l.addStretch(1)
        self._update_stress_preview()

    def _place_stress(self, idx: int):
        self._stress_pos = idx
        self._update_stress_preview()

    def _update_stress_preview(self):
        base = getattr(self, "_stress_base", "")
        pos = getattr(self, "_stress_pos", -1)
        if 0 <= pos < len(base):
            self._stress_value = base[:pos + 1] + _STRESS + base[pos + 1:]
        else:
            self._stress_value = base
        self._stress_preview.setText(self._stress_value)

    # --- поточне правило ---
    def _current(self):
        if self._tabs.currentIndex() == 1:
            return (_lex.CORRECTION_STRESS, self._stress_value)
        return (_lex.CORRECTION_TEXT_REPLACE, self._simple.text())

    def _match_mode(self):
        return (_lex.MATCH_ANYWHERE if self._mode_anywhere.isChecked()
                else _lex.MATCH_WORD)

    # --- дії ---
    def _do_preview(self):
        ctype, value = self._current()
        self._error.setText("")
        self._on_preview(self._word.text(), value, ctype)

    def _on_forms_toggled(self, on: bool):
        self._forms_list.setVisible(on)
        if on:
            self._forms_list.clear()
            for f in self._on_forms(self._word.text()):
                self._forms_list.addItem(f)

    def _do_save(self):
        ctype, value = self._current()
        match = self._word.text()
        mode = self._match_mode()
        try:
            _lex.validate_rule(match, value, match_mode=mode, correction_type=ctype)
        except _lex.RuleError as exc:
            # криве правило НЕ зникає — показуємо помилку, лишаємо введене (NVDA #11407)
            self._error.setText(tr(exc.reason_key))
            return
        # відмінки — ЛИШЕ якщо користувач підтвердив (чекбокс); інакше НЕ поширюємо
        forms = []
        if self._forms_ask.isChecked():
            forms = [self._forms_list.item(i).text()
                     for i in range(self._forms_list.count())]
        status = self._on_save(match=match, value=value, correction_type=ctype,
                               match_mode=mode, forms=forms)
        # коректний текст за статусом (оновлено/додано), не завжди «збережено»
        self._error.setText(tr("tts_pron_updated") if status == "updated"
                            else tr("tts_pron_saved"))
        self._refresh_rules()                    # список одразу показує зміну

    def _refresh_rules(self):
        self._rules_list.clear()
        for r in self._on_list():
            item = QListWidgetItem(f"{r.match} → {r.value}  ({r.correction_type})")
            item.setData(Qt.ItemDataRole.UserRole, r.id)
            self._rules_list.addItem(item)

    def _do_delete(self):
        item = self._rules_list.currentItem()
        if item is None:
            return
        rid = item.data(Qt.ItemDataRole.UserRole)
        if rid:
            self._on_delete(rid)
            self._refresh_rules()
