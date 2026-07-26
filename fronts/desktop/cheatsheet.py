"""Шпаргалка гарячих клавіш — компактна картка-довідка активних комбінацій,
що відкривається пунктом трею «Гарячі клавіші» (feature/ux-center).

Дані бере провайдер (app.hotkey_bindings): список (i18n-ключ назви, значення для
показу). Будується щоразу перед показом (refresh) — комбінації могли змінитись.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
)

from .i18n import tr


class HotkeyCheatSheet(QWidget):
    def __init__(self, provider):
        super().__init__(None)
        self._provider = provider          # () → [(name_key, value), ...]
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setObjectName("cheatSheet")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setProperty("card", True)
        root.addWidget(card)
        self._box = QVBoxLayout(card)
        self._box.setContentsMargins(24, 20, 24, 20)
        self._box.setSpacing(10)

        head = QLabel(tr("hotkeys_title"))
        head.setProperty("eyebrow", True)
        self._box.addWidget(head)
        self._rows_host = QVBoxLayout()
        self._rows_host.setSpacing(8)
        self._box.addLayout(self._rows_host)
        hint = QLabel(tr("cheat_hint"))
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        self._box.addWidget(hint)

    def refresh(self):
        """Перебудувати рядки з провайдера (комбінації могли змінитись)."""
        while self._rows_host.count():
            item = self._rows_host.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for name_key, value in self._provider():
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(16)
            name = QLabel(tr(name_key))
            name.setProperty("muted", True)
            combo = QLabel(value)
            combo.setProperty("kbd", True)
            rl.addWidget(name, 1)
            rl.addWidget(combo, 0, Qt.AlignRight)
            self._rows_host.addWidget(row)
        self.adjustSize()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Escape,):
            self.hide()
        else:
            super().keyPressEvent(ev)
