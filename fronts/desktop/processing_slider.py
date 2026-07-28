"""feature/processing-slider — компактний повзунок рівня обробки тексту (Т39).

Три позиції (Дослівно / Без слів-паразитів / З пунктуацією — на Нараді третя зветься
«Під документ») у стилі теми: темне
скло, бурштиновий акцент. Клавіатурно-доступний, з accessibleName та живим
accessibleDescription; шанує reduce-motion (жодного руху — позиція стає одразу).
Повзунок міняє ЛИШЕ рівень обробки виводу, не сам запис.

Повторно використовуваний: та сама коробка стоїть на Диктуванні (підпис
«Рівень обробки тексту») і на Нараді («Рівень обробки протоколу», з міткою
«Протокол», щоб не сплутати з якістю звуку).

Свідоме спрощення (karpathy §2): позиція комітиться миттєво, без анімації
«їзди» ручки — так гарантовано рівно один modeChanged на комміт і жодної живої
анімації під reduce-motion. sync_animations() лишається як безпечний no-op для
сумісності з викликами зі спеки.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from whisper_core import processing

from . import theme
from .chip_popover import Popover, make_slider
from .glass import GlassButton
from .i18n import tr

# Контейнер повзунка. ProcessingSlider завжди живе всередині скляного попапа
# (Popover — уже картка-скло), тож власний фон/рамка тут були б коробкою-в-коробці.
# Лишаємо прозорим — без подвійного контейнера; сам слайдер/пігулки стилізує
# chip_popover (варіант A/B/C — вибір Миколи).
def _frame_qss() -> str:
    return "QFrame#processingControl { background: transparent; border: none; }"


class _StopLabel(QLabel):
    """Клікабельна мітка позиції: клік обирає свій stop. Мишею — зручно,
    з клавіатури працює сам повзунок (StrongFocus)."""

    clicked = Signal(int)

    def __init__(self, index: int, text: str, align, parent=None):
        super().__init__(text, parent)
        self._index = index
        self.setProperty("muted", True)
        self.setAlignment(align | Qt.AlignVCenter)
        self.setWordWrap(True)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._index)
        super().mousePressEvent(event)


class ProcessingSlider(QWidget):
    """Повзунок рівня обробки для однієї поверхні (диктування / нарада).

    modeChanged(str) — комітнута позиція (значення ProcessingMode). Емітиться
    рівно раз на комміт і лише коли режим справді змінився.
    documentUnavailable(str) — спроба обрати «Під документ», коли компонент не
    готовий: позиція НЕ комітиться, лишається попередня (спека §8)."""

    modeChanged = Signal(str)
    documentUnavailable = Signal(str)

    def __init__(self, surface: str, *, caption: str = "", parent=None):
        super().__init__(parent)
        self._surface = surface if surface in processing.SURFACES else processing.DICTATION
        self._document_available = True
        self._document_reason = ""
        self._committed = 0            # індекс комітнутої позиції
        self._guard = False            # захист від реентрантності при відкаті

        name = (tr("proc_slider_meeting_name") if self._surface == processing.MEETING
                else tr("proc_slider_dict_name"))

        frame = QFrame(self)
        frame.setObjectName("processingControl")
        frame.setStyleSheet(_frame_qss())
        theme.register_restyle_call(frame, lambda w: w.setStyleSheet(_frame_qss()))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)

        col = QVBoxLayout(frame)
        col.setContentsMargins(16, 10, 16, 10)
        col.setSpacing(6)

        if caption:
            cap = QLabel(caption)
            cap.setProperty("eyebrow", True)
            col.addWidget(cap)

        # Слайдер стилізує chip_popover.make_slider: ледь помітна
        # доріжка, ручка-пігулка, 3 дискретні зупинки → крапки. Мітки під ним
        # клікабельні. Логіка комітів/діапазону/a11y — нижче.
        self._labels = []
        self._slider = make_slider(stops=len(processing.MODES))
        self._slider.setMinimum(0)
        self._slider.setMaximum(len(processing.MODES) - 1)
        self._slider.setSingleStep(1)
        self._slider.setPageStep(1)
        self._slider.setFocusPolicy(Qt.StrongFocus)
        self._slider.setMinimumHeight(36)          # мінімальна інтерактивна висота (спека §4)
        self._slider.setAccessibleName(name)
        self._slider.valueChanged.connect(self._on_value)
        col.addWidget(self._slider)

        labels = QHBoxLayout()
        labels.setContentsMargins(0, 0, 0, 0)
        labels.setSpacing(8)
        aligns = (Qt.AlignLeft, Qt.AlignHCenter, Qt.AlignRight)
        for i, mode in enumerate(processing.MODES):
            lbl = _StopLabel(i, _mode_label(mode, self._surface), aligns[i], frame)
            lbl.clicked.connect(self._on_label_clicked)
            self._labels.append(lbl)
            labels.addWidget(lbl, 1)
        col.addLayout(labels)

        self._sync_visual()

    # --- публічний контракт (спека §4) ---
    def mode(self) -> str:
        return processing.MODES[self._committed].value

    def setMode(self, mode, *, emit: bool = False) -> None:
        """Встановити позицію без запису на диск. emit=True — сповістити modeChanged."""
        index = processing.mode_index(mode)
        if index == self._committed and not emit:
            self._apply_index(index)
            return
        self._commit(index, emit=emit)

    def setDocumentAvailable(self, available: bool, reason: str = "") -> None:
        """Чи готовий компонент «Під документ». Недоступний — позицію не можна
        закомітити на 2 (спека §8: не комітити, лишити попередню)."""
        self._document_available = bool(available)
        self._document_reason = reason or ""
        self._labels[2].setEnabled(self._document_available)
        if not self._document_available and self._committed == 2:
            self._commit(1, emit=True)

    def sync_animations(self) -> None:
        """Спинити «в польоті» анімацію. Анімації немає — безпечний no-op (спека §4)."""
        return

    # --- внутрішнє ---
    def _on_label_clicked(self, index: int) -> None:
        self._commit(index, emit=True)

    def _on_value(self, value: int) -> None:
        if self._guard:
            return
        self._commit(int(value), emit=True)

    def _commit(self, index: int, *, emit: bool) -> None:
        index = max(0, min(len(processing.MODES) - 1, int(index)))
        if index == 2 and not self._document_available:
            # Відкат на попередню позицію без емісії modeChanged (спека §8).
            self._apply_index(self._committed)
            self.documentUnavailable.emit(self._document_reason)
            return
        changed = index != self._committed
        self._committed = index
        self._apply_index(index)
        if emit and changed:
            self.modeChanged.emit(self.mode())

    def _apply_index(self, index: int) -> None:
        """Синхронізувати слайдер із комітнутою позицією без зайвого сигналу."""
        self._guard = True
        try:
            if self._slider.value() != index:
                self._slider.setValue(index)
        finally:
            self._guard = False
        self._sync_visual()

    def _sync_visual(self) -> None:
        """Виділення обраної позиції: жирніша мітка + бурштин; жива a11y-репліка."""
        mode = processing.MODES[self._committed]
        desc = tr("proc_slider_pos", mode=_mode_label(mode, self._surface),
                  n=self._committed + 1, desc=_mode_desc(mode, self._surface))
        for i, lbl in enumerate(self._labels):
            sel = i == self._committed
            lbl.setProperty("muted", not sel)
            lbl.setProperty("gold", sel)
            _restyle(lbl)
        self._slider.setAccessibleDescription(desc)


# Третя позиція має РІЗНУ назву за поверхнею (спека §3): на Диктуванні —
# «З пунктуацією» (детермінована обробка, без генеративного переписування), на
# Нараді — «Під документ» (генерований протокол). Тому мітка/опис третьої позиції
# залежать від surface — так само, як accessibleName вище.
def _mode_label(mode, surface=processing.DICTATION) -> str:
    m = processing.normalize_mode(mode)
    if m is processing.ProcessingMode.DOCUMENT and surface != processing.MEETING:
        return tr("proc_mode_punct")
    return tr({
        processing.ProcessingMode.VERBATIM: "proc_mode_verbatim",
        processing.ProcessingMode.FILLERS: "proc_mode_fillers",
        processing.ProcessingMode.DOCUMENT: "proc_mode_document",
    }[m])


def _mode_desc(mode, surface=processing.DICTATION) -> str:
    m = processing.normalize_mode(mode)
    if m is processing.ProcessingMode.DOCUMENT and surface != processing.MEETING:
        return tr("proc_mode_punct_desc")
    return tr({
        processing.ProcessingMode.VERBATIM: "proc_mode_verbatim_desc",
        processing.ProcessingMode.FILLERS: "proc_mode_fillers_desc",
        processing.ProcessingMode.DOCUMENT: "proc_mode_document_desc",
    }[m])


def _restyle(widget) -> None:
    """Перепризначити QSS-властивості (Qt не перечитує їх сам після setProperty)."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class ProcessingChip(QWidget):
    """Т47 — компактний чіп рівня обробки біля кнопки запису. Чіп показує
    поточний рівень («Обробка: Дослівно»); клік
    відкриває скляний попап із тим самим ``ProcessingSlider``. Вибір діє миттєво,
    напис чіпа оновлюється; попап закривається по кліку поза ним (``Qt.Popup``).

    Логіка рівнів обробки НЕ дублюється — уся вона в ``ProcessingSlider``; чіп
    лише дзеркалить його публічний контракт (``setMode``/``setDocumentAvailable``/
    ``modeChanged``/``documentUnavailable``), тож заміна на сторінці мінімальна."""

    modeChanged = Signal(str)
    documentUnavailable = Signal(str)

    def __init__(self, surface: str, *, parent=None):
        super().__init__(parent)
        self._surface = surface if surface in processing.SURFACES else processing.DICTATION
        self._slider = ProcessingSlider(self._surface)
        self._slider.modeChanged.connect(self._on_mode_changed)
        self._slider.documentUnavailable.connect(self.documentUnavailable)

        self._chip = GlassButton("")
        self._chip.setAccessibleName(
            tr("proc_slider_meeting_name") if self._surface == processing.MEETING
            else tr("proc_slider_dict_name"))
        self._chip.clicked.connect(self._open)

        self._popover = None

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._chip)
        row.addStretch()
        self._refresh_chip()

    # --- публічний контракт (дзеркало ProcessingSlider, що використовує сторінка) ---
    def mode(self) -> str:
        return self._slider.mode()

    def setMode(self, mode, *, emit: bool = False) -> None:
        self._slider.setMode(mode, emit=emit)
        self._refresh_chip()

    def setDocumentAvailable(self, available: bool, reason: str = "") -> None:
        self._slider.setDocumentAvailable(available, reason)
        self._refresh_chip()

    def sync_animations(self) -> None:
        self._slider.sync_animations()

    def setToolTip(self, tip: str) -> None:      # тултип належить видимому чіпу
        self._chip.setToolTip(tip)

    # --- внутрішнє ---
    def _on_mode_changed(self, mode: str) -> None:
        self._refresh_chip()
        self.modeChanged.emit(mode)

    def _refresh_chip(self) -> None:
        level = _mode_label(self._slider.mode(), self._surface)
        self._chip.setText(tr("proc_chip_label", level=level))

    def _open(self) -> None:
        if self._popover is None:
            self._popover = Popover(self)
            self._popover.set_content(self._slider)
        self._popover.open_under(self._chip)
