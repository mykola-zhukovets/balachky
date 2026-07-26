"""feature/paste-preview — картка попереднього перегляду перед вставкою.

Патерн Wispr Flow Scratchpad: коли ввімкнено перегляд, розпізнаний текст не
вставляється одразу, а зʼявляється у компактній картці біля курсора. Текст можна
відредагувати й лише тоді «Вставити» (Enter), «Копіювати» або «Скасувати» (Esc).

Ключове рішення про фокус: картка показується БЕЗ активації (WA_ShowWithoutActivating),
тож ціль-вікно лишається активним і вставка потрапляє саме в нього. Коли ж
користувач клікає в редактор (правки), активним стає вікно картки — тому HWND цілі
запамʼятовує оркестратор (app._on_preview_ready) і повертає їй фокус перед вставкою.

UI-рендер тут; чиста логіка розташування (place_near_cursor) винесена окремо й
покрита unit-тестами.
"""
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from . import theme

AUTO_HIDE_MS = 30000        # 30 с автосховання; пауза при наведенні миші
_MARGIN = 14                # відступ картки від курсора / країв екрана
_CARD_W = 380
_CARD_MIN_H = 150


def place_near_cursor(cursor, size, screen, margin=_MARGIN):
    """Розташувати картку біля курсора, тримаючи її повністю в межах екрана.

    cursor=(x,y) глобальні; size=(w,h) картки; screen=(x,y,w,h) доступна геометрія.
    Типово — нижче-праворуч від курсора; якщо не влазить, дзеркалимо ліворуч/вгору,
    а далі клампимо в межі екрана. → (x, y) лівий-верхній кут.
    """
    cx, cy = cursor
    cw, ch = size
    sx, sy, sw, sh = screen
    x = cx + margin
    y = cy + margin
    if x + cw > sx + sw:            # не влазить праворуч → ліворуч від курсора
        x = cx - margin - cw
    if y + ch > sy + sh:            # не влазить знизу → вгору від курсора
        y = cy - margin - ch
    x = max(sx, min(x, sx + sw - cw))   # фінальний кламп у межі екрана
    y = max(sy, min(y, sy + sh - ch))
    return x, y


class PreviewCard(QWidget):
    """Спливна картка перегляду. Сигнали несуть підсумковий (можливо, змінений) текст."""

    accepted = Signal(str)      # «Вставити» — доставити текст у ціль
    copied = Signal(str)        # «Копіювати» — лише в буфер
    cancelled = Signal()        # «Скасувати» / автосховання / Esc

    def __init__(self, text, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint)
        # не красти фокус при показі: ціль-вікно лишається активним для вставки
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._done = False       # рівно один результат (accept/copy/cancel)
        self._build(text)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)
        self._timer.start(AUTO_HIDE_MS)

    def _restyle_card(self):
        self._card.setStyleSheet(_build_style())

    def _build(self, text):
        from .i18n import tr
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        card = QFrame(self)
        card.setObjectName("previewCard")
        self._card = card
        card.setStyleSheet(_build_style())
        theme.register_restyle(self._restyle_card)   # нічний режим
        root.addWidget(card)

        box = QVBoxLayout(card)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(10)

        eyebrow = QLabel(tr("preview_eyebrow"))   # статичний підпис-«вушко»
        eyebrow.setProperty("level", "eyebrow")   # централізований eyebrow-рівень
        box.addWidget(eyebrow)

        self._edit = QPlainTextEdit(text, card)
        self._edit.setMinimumHeight(70)
        self._edit.installEventFilter(self)     # Enter=Вставити, Shift+Enter=новий рядок
        box.addWidget(self._edit)

        row = QHBoxLayout()
        row.setSpacing(8)
        copy = QPushButton(tr("preview_copy"))
        copy.clicked.connect(self._on_copy)
        cancel = QPushButton(tr("preview_cancel"))
        cancel.setObjectName("previewGhost")
        cancel.clicked.connect(self._on_cancel)
        insert = QPushButton(tr("preview_insert"))
        insert.setObjectName("previewAccent")
        insert.clicked.connect(self._on_accept)
        row.addWidget(cancel)
        row.addStretch()
        row.addWidget(copy)
        row.addWidget(insert)
        box.addLayout(row)

        self.resize(_CARD_W, max(_CARD_MIN_H, self.sizeHint().height()))

    # --- розташування ---
    def show_near_cursor(self):
        """Розмістити біля курсора в межах поточного екрана й показати без активації."""
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        x, y = place_near_cursor(
            (QCursor.pos().x(), QCursor.pos().y()),
            (self.width(), self.height()),
            (geo.x(), geo.y(), geo.width(), geo.height()))
        self.move(x, y)
        self.show()
        self.raise_()

    # --- автосховання з паузою на наведенні ---
    def enterEvent(self, _event):
        self._timer.stop()

    def leaveEvent(self, _event):
        if not self._done:
            self._timer.start(AUTO_HIDE_MS)

    def _on_timeout(self):
        self._finish(self.cancelled)

    # --- дії ---
    def _text(self):
        return self._edit.toPlainText()

    def _on_accept(self):
        text = self._text()
        if not text.strip():          # порожній текст вставляти нема сенсу
            self._finish(self.cancelled)
            return
        self._finish(self.accepted, text)

    def _on_copy(self):
        self._finish(self.copied, self._text())

    def _on_cancel(self):
        self._finish(self.cancelled)

    def _finish(self, signal, *args):
        """Рівно один результат: зупинити таймер, емітнути сигнал, закрити картку."""
        if self._done:
            return
        self._done = True
        self._timer.stop()
        signal.emit(*args)
        self.close()

    # --- клавіатура: Esc = Скасувати; Enter у редакторі = Вставити ---
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._on_cancel()
            return
        super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self._edit and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ShiftModifier:
                    return False       # Shift+Enter — звичайний новий рядок
                self._on_accept()      # Enter — Вставити
                return True
            if event.key() == Qt.Key_Escape:
                self._on_cancel()
                return True
        return super().eventFilter(obj, event)


# Локальний стиль картки (канон «Мундір»: без нових кольорів/тіней, radius 6px,
# акцент F39200). Топ-рівневе вікно бере глобальний QSS, але фон/поля редактора
# задаємо тут явно, щоб не чіпати спільний theme.QSS заради одного віджета.
# Функція (не константа): нічний режим свопає палітру — стиль перечитується.
def _build_style() -> str:
    return f"""
QFrame#previewCard {{
    background: {theme.CARD};
    border: 1px solid {theme._LINE_SOFT};
    border-radius: 6px;
}}
QFrame#previewCard QPlainTextEdit {{
    background: {theme.DEEP};
    color: {theme.TEXT_BODY};
    border: 1px solid {theme._LINE_SOFT};
    border-radius: 6px;
    padding: 8px 10px;
    selection-background-color: {theme._GOLD_22};
}}
QFrame#previewCard QPlainTextEdit:focus {{ border: 2px solid {theme.FOCUS}; }}
QFrame#previewCard QPushButton {{
    background: {theme.DEEP};
    color: {theme.TEXT_BODY};
    border: 1px solid {theme._LINE_SOFT};
    border-radius: 6px;
    padding: 7px 16px;
}}
QFrame#previewCard QPushButton:hover {{
    border-color: {theme._GOLD_65}; color: {theme.TEXT_STRONG};
}}
QFrame#previewCard QPushButton:focus {{ border: 2px solid {theme.FOCUS}; }}
QPushButton#previewAccent {{
    background: {theme.GOLD};
    color: {theme.TEXT_ON_GOLD};
    font-weight: 600;
    border: none;
    padding: 8px 18px;
}}
QPushButton#previewAccent:hover {{ background: {theme.GOLD_EYEBROW}; }}
QPushButton#previewGhost {{
    background: transparent;
    border: none;
    color: {theme.TEXT_MUTED};
}}
QPushButton#previewGhost:hover {{ color: {theme.TEXT_STRONG}; }}
"""
