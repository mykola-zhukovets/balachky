"""Т47/Т48 — компактний чіп + скляний попап (ідіома Effort-чіпа Claude Code).

Замість смуги-контрола на всю ширину сторінки — маленький чіп із поточним
значенням; клік відкриває скляну картку-попап (``Qt.Popup``: сама закривається
по кліку поза межами) із самим контролом. Значення застосовується миттєво, чіп
оновлює напис. Реюзабельно: рівень обробки (3-позиційний слайдер, у
``processing_slider.ProcessingChip``) і Кадри/сек (безперервний слайдер, тут).

Кольори читаються з ``theme`` наживо + ``register_restyle`` — попап
перефарбовується на живому day↔night свопі, без хардкоду rgba.
"""
from PySide6.QtCore import Qt, QPoint, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSlider, QStyle, QStyleOptionSlider,
    QVBoxLayout, QWidget,
)

from . import theme
from .glass import GlassButton
from .i18n import tr


# Вигляд слайдера в попапах — «як у Claude» (вибір Миколи 23.07): ледь помітна
# доріжка 2px, дискретні зупинки — крапки, ручка-ПІГУЛКА ~22×14 акцентна;
# пройдена частина НЕ заливається (sub-page = тон доріжки).
_HANDLE_W = 22


def _slider_qss() -> str:
    """QSS слайдера-в-попапі з ПОТОЧНОЇ палітри (день/ніч). Селектор #chipSlider
    перекриває глобальний QSlider, не чіпаючи інші повзунки."""
    n = "chipSlider"
    # Віджет-рівнева коробка прозора; рамку-фокус навколо всього повзунка прибирає
    # paintEvent (State_HasFocus скинуто) — видимість фокуса дає перстень на ручці.
    return f"""
QSlider#{n} {{ background: transparent; border: none; }}
QSlider#{n}::groove:horizontal {{ height: 2px; background: {theme._LINE_SOFT};
    border: none; border-radius: 1px; }}
QSlider#{n}::sub-page:horizontal {{ background: {theme._LINE_SOFT}; border-radius: 1px; }}
QSlider#{n}::add-page:horizontal {{ background: {theme._LINE_SOFT}; border-radius: 1px; }}
QSlider#{n}::handle:horizontal {{ width: 22px; margin: -6px 0; border-radius: 7px;
    background: {theme.GOLD}; border: none; }}
"""


def _focus_ring_color(palette: dict | None = None) -> str:
    """Колір персня фокуса на ручці слайдера попапа.

    Ручка залита ``GOLD``, а токен ``FOCUS`` у денній палітрі — той самий
    #F39200: перстень тим кольором зливався б із заливкою і клавіатурний
    користувач не бачив би активний контрол (блокер доступності). Тож перстень
    малюємо чорнилом ``TEXT_ON_GOLD`` — токеном, який у КОЖНІЙ палітрі
    підібраний контрастним саме до акцентної заливки (день #2E2A1F на золоті,
    ніч #1A0000 на червоному). ``palette`` — для тестів; None = активна.
    """
    p = palette if palette is not None else theme._P
    return p["TEXT_ON_GOLD"]


class _ChipSlider(QSlider):
    """Горизонтальний слайдер попапа «як у Claude»: ледь помітна доріжка, ручка-
    пігулка, дискретні зупинки — намальовані крапки. Перефарбовується на живому
    day↔night свопі через register_restyle (QSS + крапки читають палітру наживо)."""

    def __init__(self, stops: int = 0, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._stops = int(stops)
        self.setObjectName("chipSlider")
        self.setProperty("canon_role", "slider")
        self._restyle()
        theme.register_restyle(self._restyle)   # нічний режим: перечитати палітру

    def _restyle(self) -> None:
        self.setStyleSheet(_slider_qss())
        self.update()

    def paintEvent(self, event):
        # Малюємо самі, БЕЗ State_HasFocus — так Windows-стиль не малює власну
        # рамку-фокус навколо всього повзунка (той бурштиновий прямокутник —
        # «обводка», яку забракував Микола). Видимість фокуса лишає чистий
        # перстень на ручці нижче. Жолоб/ручка/крапки читають палітру наживо.
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        opt.subControls = QStyle.SC_SliderGroove | QStyle.SC_SliderHandle
        had_focus = bool(opt.state & QStyle.State_HasFocus)
        opt.state &= ~QStyle.State_HasFocus
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self.style().drawComplexControl(QStyle.CC_Slider, opt, p, self)

        if self._stops >= 2:                    # дискретні зупинки (обробка) — крапки
            avail = self.width() - _HANDLE_W
            if avail > 0:
                cy = self.height() / 2.0
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(*theme.LIGHT_RGB, 120))
                for i in range(self._stops):
                    if i == self.value():
                        continue                # під ручкою крапку не малюємо
                    x = _HANDLE_W / 2.0 + avail * (i / (self._stops - 1))
                    p.drawEllipse(QPoint(int(round(x)), int(cy)), 2, 2)

        if had_focus:                           # чистий перстень на ручці замість рамки
            hr = self.style().subControlRect(
                QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
            # NB: перстень чорнилом TEXT_ON_GOLD, а НЕ theme.FOCUS — бо в денній
            # палітрі FOCUS==GOLD (обидва #F39200) і перстень тим кольором
            # зливався б із заливкою ручки (блокер доступності). TEXT_ON_GOLD
            # у кожній палітрі контрастний саме до акцентної заливки.
            p.setPen(QPen(QColor(_focus_ring_color()), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(hr.adjusted(1, 1, -1, -1), 7, 7)


def make_slider(stops: int = 0) -> _ChipSlider:
    """Слайдер попапа «як у Claude». ``stops`` >= 2 → дискретні зупинки з крапками
    (обробка); 0 → безперервний (FPS)."""
    return _ChipSlider(stops)


def _popover_qss() -> str:
    """Скляна картка попапа з ПОТОЧНОЇ палітри (день/ніч)."""
    return (
        f"QFrame#chipPopover {{ background: {theme.CARD};"
        f" border: 1px solid {theme._GLASS_EDGE};"
        f" border-top-color: {theme._GLASS_TOP}; border-radius: 12px; }}")


class Popover(QFrame):
    """Скляна картка-попап під якорем. ``Qt.Popup`` дає авто-закриття по кліку
    поза межами (як QMenu) і захоплення миші. Контент кладеться раз через
    ``set_content``; ``open_under`` позиціонує попап під кнопкою-якорем."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("chipPopover")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(12, 12, 12, 12)
        self._lay.setSpacing(8)
        self._restyle()
        theme.register_restyle(self._restyle)   # нічний режим: перечитати палітру

    def _restyle(self) -> None:
        self.setStyleSheet(_popover_qss())

    def set_content(self, widget: QWidget) -> None:
        self._lay.addWidget(widget)

    def open_under(self, anchor: QWidget) -> None:
        """Показати попап під якорем, вирівняно по його лівому краю; якщо не
        влазить праворуч — зсунути вліво в межах екрана."""
        self.adjustSize()
        top_left = anchor.mapToGlobal(QPoint(0, anchor.height() + 6))
        screen = anchor.screen() or (self.window().screen() if self.window() else None)
        if screen is not None:
            avail = screen.availableGeometry()
            x = min(top_left.x(), avail.right() - self.width())
            x = max(x, avail.left())
            y = top_left.y()
            if y + self.height() > avail.bottom():   # не влазить знизу — над якорем
                y = anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 6
            top_left = QPoint(x, y)
        self.move(top_left)
        self.show()


class ValueSliderChip(QWidget):
    """Компактний чіп «{підпис}: {значення}» + попап зі слайдером фіксованої
    ширини (Т48). Безперервний діапазон; напис чіпа оновлюється наживо при
    перетягуванні. Публічний контракт мінімальний: ``value()`` і ``valueChanged``."""

    valueChanged = Signal(int)

    def __init__(self, minimum: int, maximum: int, value: int, *,
                 label_key: str, name_key: str, parent=None):
        super().__init__(parent)
        self._label_key = label_key
        self._value = int(value)

        self._chip = GlassButton("")
        self._chip.setAccessibleName(tr(name_key))
        self._chip.clicked.connect(self._open)

        self._slider = make_slider(stops=0)     # безперервний (FPS), стиль «як у Claude»
        self._slider.setRange(minimum, maximum)
        self._slider.setValue(self._value)
        self._slider.setSingleStep(1)
        self._slider.setPageStep(5)
        self._slider.setFixedWidth(220)
        self._slider.setMinimumHeight(36)
        self._slider.setFocusPolicy(Qt.StrongFocus)
        self._slider.setAccessibleName(tr(name_key))
        self._slider.valueChanged.connect(self._on_value)

        self._value_lbl = QLabel(str(self._value))
        self._value_lbl.setProperty("level", "block")
        self._value_lbl.setMinimumWidth(
            self._value_lbl.fontMetrics().horizontalAdvance("60") + 8)

        self._popover = None

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._chip)
        row.addStretch()                     # чіп лишається компактним зліва
        self._refresh_chip()

    # --- публічний контракт ---
    def value(self) -> int:
        return int(self._slider.value())

    def setValue(self, value: int) -> None:
        self._slider.setValue(int(value))

    # --- внутрішнє ---
    def _on_value(self, value: int) -> None:
        self._value = int(value)
        self._value_lbl.setText(str(self._value))
        self._refresh_chip()
        self.valueChanged.emit(self._value)

    def _refresh_chip(self) -> None:
        self._chip.setText(tr(self._label_key, n=self._value))

    def _open(self) -> None:
        if self._popover is None:
            self._popover = Popover(self)
            body = QWidget()
            hb = QHBoxLayout(body)
            hb.setContentsMargins(0, 0, 0, 0)
            hb.setSpacing(8)
            hb.addWidget(self._slider)
            hb.addWidget(self._value_lbl)
            self._popover.set_content(body)
        self._popover.open_under(self._chip)
