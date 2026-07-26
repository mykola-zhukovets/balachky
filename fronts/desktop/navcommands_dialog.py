"""Шпаргалка команд голосової навігації (feature/office-voice-nav).

Тонкий модальний список над whisper_core.navcommands.command_reference: показує
фразу й ефект кожної команди для поточної мови диктування. «Поповнюється» новими
командами автоматично — джерело рядків одне з розбором команд, плюс користувацькі
аліаси з navcommands.toml. Уся логіка (які команди існують) — у ядрі під тестами;
тут лише Qt-рендер.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from whisper_core import navcommands

from .i18n import tr


class NavCommandsDialog(QDialog):
    """exec()-модалка зі списком (фраза → ефект). language — мова диктування
    (uk|en); aliases — {фраза: id_дії} користувацьких аліасів (може бути None)."""

    def __init__(self, language: str, aliases: dict = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("nav_cmds_title"))
        self.setMinimumSize(460, 480)

        outer = QVBoxLayout(self)
        card = QFrame()
        card.setProperty("card", True)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)
        outer.addWidget(card)

        eyebrow = QLabel(tr("nav_cmds_title"))
        eyebrow.setProperty("eyebrow", True)
        lay.addWidget(eyebrow)
        sub = QLabel(tr("nav_cmds_subtitle"))
        sub.setProperty("hint", True)
        sub.setWordWrap(True)
        lay.addWidget(sub)

        rows_host = QWidget()
        rows = QVBoxLayout(rows_host)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(8)
        for phrase, effect_key in navcommands.command_reference(language, aliases):
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(16)
            cmd = QLabel(f"“{phrase}”")
            cmd.setProperty("kbd", True)
            effect = QLabel(tr(effect_key))
            effect.setProperty("muted", True)
            effect.setWordWrap(True)
            rl.addWidget(cmd, 0, Qt.AlignLeft | Qt.AlignTop)
            rl.addWidget(effect, 1)
            rows.addWidget(row)
        rows.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(rows_host)
        lay.addWidget(scroll, stretch=1)
