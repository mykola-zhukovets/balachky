"""Shared, compact empty state for desktop pages."""
import qtawesome as qta
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from . import theme   # нічний режим: іконка порожнього стану читає палітру


class EmptyState(QWidget):
    """Icon, one-line explanation, and a concise next-step hint."""

    def __init__(self, icon_name: str, title: str, hint: str, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(8)
        lay.addStretch(3)
        icon = QLabel()
        self._icon_name = icon_name
        icon.setPixmap(qta.icon(icon_name, color=theme.IDLE).pixmap(32, 32))
        icon.setAlignment(Qt.AlignCenter)
        self._icon = icon
        theme.register_restyle(self._restyle_icon)   # нічний режим
        lay.addWidget(icon)
        self.title_label = QLabel(title)
        self.title_label.setProperty("emptytitle", True)
        self.title_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.title_label)
        self.hint_label = QLabel(hint)
        self.hint_label.setProperty("muted", True)
        self.hint_label.setWordWrap(True)
        self.hint_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.hint_label)
        lay.addStretch(4)

    def _restyle_icon(self) -> None:
        self._icon.setPixmap(qta.icon(self._icon_name, color=theme.IDLE).pixmap(32, 32))

    def set_content(self, title: str, hint: str) -> None:
        self.title_label.setText(title)
        self.hint_label.setText(hint)
