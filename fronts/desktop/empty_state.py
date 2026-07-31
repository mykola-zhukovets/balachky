"""Shared, compact empty state for desktop pages.

Один компонент для всіх «ще немає даних» екранів (аудит 31.07.2026):
іконка + заголовок + один рядок пояснення + за потреби кнопка першого
кроку. Кнопка — опційна: сторінки, де дія вже очевидна (велика кнопка
запису в шапці), можуть лишити компонент без неї."""
import qtawesome as qta
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from . import theme   # нічний режим: іконка порожнього стану читає палітру
from .glass import GlassButton


class EmptyState(QWidget):
    """Icon, one-line explanation, a concise next-step hint, and an
    optional first-step button."""

    def __init__(self, icon_name: str, title: str, hint: str, parent=None,
                 button_text: str = "", on_click=None):
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
        self._lay = lay
        self.button = GlassButton("")
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.hide()
        lay.addSpacing(4)
        lay.addWidget(self.button, alignment=Qt.AlignHCenter)
        lay.addStretch(4)
        self._on_click = None
        if button_text:
            self.set_button(button_text, on_click)

    def _restyle_icon(self) -> None:
        self._icon.setPixmap(qta.icon(self._icon_name, color=theme.IDLE).pixmap(32, 32))

    def set_content(self, title: str, hint: str) -> None:
        self.title_label.setText(title)
        self.hint_label.setText(hint)

    def set_button(self, text: str, on_click=None) -> None:
        """Показати кнопку першого кроку з текстом `text`, під'єднану до
        `on_click`. Порожній `text` — сховати кнопку (напр. коли для цього
        стану дія вже показана в шапці сторінки)."""
        if self._on_click is not None:
            try:
                self.button.clicked.disconnect(self._on_click)
            except (TypeError, RuntimeError):
                pass
            self._on_click = None
        if not text:
            self.button.hide()
            return
        self.button.setText(text)
        self.button.setAccessibleName(text)
        if on_click is not None:
            self.button.clicked.connect(on_click)
            self._on_click = on_click
        self.button.show()

    def hide_button(self) -> None:
        self.set_button("")
