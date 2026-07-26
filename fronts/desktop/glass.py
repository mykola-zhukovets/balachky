"""Скляні кнопки «застосунку» — Qt-адаптація дизайн-канону (liquid glass).

Опукле статичне скло: напівпрозора підложка + світловий градієнт зверху
(світло, не колір — кольорові градієнти заборонені каноном), бордюр 1px,
радіус 6px. На hover — світлова radial-пляма, що слідує за мишею (як glow
на сайті). Активний стан — золота рамка + заливка 12%.

Малюємо все у paintEvent (super().paintEvent не викликається — QSS-фон
QPushButton сюди не потрапляє, лишаються тільки його метрики sizeHint).
"""
from PySide6.QtCore import (
    QAbstractAnimation, QEasingCurve, QElapsedTimer, QObject, QPointF, QRect,
    QRectF, QSize, Qt, QTimer, QVariantAnimation,
)
from PySide6.QtGui import (
    QAccessible, QAccessibleEvent, QBrush, QColor, QFont, QFontMetrics, QIcon,
    QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient,
)
from PySide6.QtWidgets import QPushButton, QToolButton, QToolTip, QWidget

import math

import qtawesome as qta

from . import motion
from . import theme
# Кольори читаємо через `theme.X` ПРИ малюванні (не через `from .theme import`),
# щоб нічний режим (свопнута палітра) перемальовував ці власні малювальники без
# перезапуску — feature/night-mode.

_RADIUS = 12        # радіус скляної кнопки (канон макета 22.07: контроли 12px)
_ICON = 18          # розмір іконки nav-пункту (dp; масштабує Qt)


class TipToolButton(QToolButton):
    """QToolButton із ГАРАНТОВАНИМ показом підказки при наведенні.

    Штатний QToolTip інколи не спливає, коли Qt доставляє QEvent.ToolTip не
    листовій кнопці, а батьку/вікну. Тож показуємо тултип явно з enterEvent —
    незалежно від маршрутизації події. setToolTip() лишаємо для доступності.
    Спільний для ⓘ-підказок (settings.py) і ✕-кнопки картки (main_window.py)."""

    def __init__(self, tip: str = "", parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self._tip = tip
        if tip:
            self.set_tip(tip)

    def set_tip(self, tip: str) -> None:
        self._tip = tip
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.setAccessibleDescription(tip)

    def keyPressEvent(self, event):
        """Іконкові кнопки однаково активуються Enter і Space."""
        if (self.isEnabled()
                and event.key() in (Qt.Key_Enter, Qt.Key_Return, Qt.Key_Space)):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def enterEvent(self, event):
        if self._tip:
            QToolTip.showText(
                self.mapToGlobal(self.rect().bottomLeft()), self._tip, self)
        super().enterEvent(event)


class GlassButton(QPushButton):
    """Опукла скляна кнопка. nav=True — пункт сайдбара: іконка+текст зліва,
    checkable; активний стан — золота рамка + заливка + золотий текст."""

    def __init__(self, text="", icon=None, nav=False, parent=None):
        super().__init__(text, parent)
        self._nav = nav
        self._hover = False
        self._mouse = QPointF()
        self.setMouseTracking(True)          # glow має знати позицію миші
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        if icon is not None:
            self.setIcon(icon)
        if nav:
            self.setCheckable(True)
            self.setMinimumHeight(44)
        else:
            # наведення «підйом + тінь» — лише для дій, не для сайдбар-навігації
            # (у неї власний активний стан: золота рамка + заливка checked-кнопки)
            motion.lift_on_hover(self)

    def sizeHint(self):
        # Текст малюємо самі по центру self.rect() (див. paintEvent). QSS-padding
        # QPushButton інколи недооцінює ширину для широких шрифтів (Segoe UI) —
        # гарантуємо запас ~20px з боків, щоб текст не торкався краю кнопки.
        s = super().sizeHint()
        if not self._nav and self.text():
            needed = QFontMetrics(self.font()).horizontalAdvance(self.text()) + 40
            if not self.icon().isNull():
                needed += _ICON + 8      # іконка зліва + проміжок (див. paintEvent)
            if s.width() < needed:
                s.setWidth(needed)
        return s

    # --- миша: glow слідує за курсором, press — анімація натиску ---
    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        self._mouse = event.position()
        if self._hover:
            self.update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            motion.press(self)
        super().mousePressEvent(event)

    def _content_color(self) -> QColor:
        """Колір іконки+тексту за станом. Disabled — тьмяний (≈45% альфи, як QSS
        `QPushButton:disabled` для звичайних кнопок). Раніше брали IDLE (#D6CDB8) —
        майже те саме, що enabled TEXT_BODY (#E6E5D1), тож вимкнена скляна кнопка
        не читалась як вимкнена (рубрика §14)."""
        checked = self.isChecked()
        ghost = bool(self.property("ghost")) and not checked
        if not self.isEnabled():
            return QColor(*theme.DISABLED_RGB, 115)   # вміст @ ~45%, як QSS:disabled
        if checked:
            return QColor(theme.GOLD_EYEBROW if self._nav else theme.TEXT_STRONG)
        if self._hover:
            return QColor(theme.TEXT_STRONG)
        return QColor(theme.TEXT_MUTED if self._nav or ghost else theme.TEXT_BODY)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, _RADIUS, _RADIUS)
        checked = self.isChecked()
        ghost = bool(self.property("ghost")) and not checked

        light = theme.LIGHT_RGB          # «світло» скла (біле вдень / червоне вночі)
        # 1) підложка: скло або акцент 12% у активного
        p.setClipPath(path)
        if checked:
            p.fillPath(path, QColor(*theme.ACCENT_RGB, 31))
        else:
            p.fillPath(path, QColor(*light, 0 if ghost else 14 if self._hover else 8))
        # 2) світловий градієнт: світло зверху, тінь знизу — ефект опуклості
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0.0, QColor(*light, 0 if ghost else 26))
        g.setColorAt(0.45, QColor(*light, 0 if ghost else 5))
        g.setColorAt(1.0, QColor(0, 0, 0, 0 if ghost else 20))
        p.fillPath(path, g)
        # 3) glow під курсором — світлова пляма слідує за мишею
        if self._hover and self.isEnabled() and not ghost:
            glow = QRadialGradient(self._mouse, max(float(self.width()), 80.0))
            glow.setColorAt(0.0, QColor(*light, 26))
            glow.setColorAt(1.0, QColor(*light, 0))
            p.fillPath(path, glow)
        p.setClipping(False)
        # 4) бордюр: золотий у активного, світлова кромка у решти. Активний стан
        # читається з рамки + золотої заливки + золотого тексту — окремий лівий
        # маркер-«полоска» прибрано (виглядав як типовий AI-акцент)
        nav_focus = self._nav and self.hasFocus()
        border_color = (QColor(theme.FOCUS) if nav_focus else
                        QColor(theme.GOLD) if checked or ghost and self._hover else QColor(*light, 0 if ghost else 40))
        p.setPen(QPen(border_color, 2 if nav_focus else 1))
        p.drawPath(path)
        # У навігації фокус уже показує єдина зовнішня рамка вище. Раніше тут
        # поверх золотої active-рамки малювалася ще одна внутрішня — саме
        # подвійна обводка з LIVE-07. Для звичайних GlassButton окрема focus-ring
        # лишається, бо в них немає постійної active-рамки.
        if self.hasFocus() and not self._nav:
            focus_path = QPainterPath()
            focus_path.addRoundedRect(r.adjusted(2, 2, -2, -2),
                                      _RADIUS - 1, _RADIUS - 1)
            p.setPen(QPen(QColor(theme.FOCUS), 2))
            p.setBrush(Qt.NoBrush)
            p.drawPath(focus_path)

        # 5) вміст: іконка + текст
        color = self._content_color()
        p.setPen(color)
        if self._nav:
            mode = QIcon.Selected if checked else QIcon.Normal
            icon_rect = QRect(14, (self.height() - _ICON) // 2, _ICON, _ICON)
            if not self.icon().isNull():
                self.icon().paint(p, icon_rect, Qt.AlignCenter, mode)
            text_rect = self.rect().adjusted(14 + _ICON + 10, 0, -8, 0)
            p.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, self.text())
        elif not self.icon().isNull():
            # іконка зліва + текст, згруповані по центру кнопки
            fm = QFontMetrics(self.font())
            tw = fm.horizontalAdvance(self.text())
            gap = 8
            group = _ICON + gap + tw
            x0 = (self.width() - group) // 2
            icy = (self.height() - _ICON) // 2
            mode = QIcon.Normal if self.isEnabled() else QIcon.Disabled
            self.icon().paint(p, QRect(x0, icy, _ICON, _ICON), Qt.AlignCenter, mode)
            p.drawText(QRect(x0 + _ICON + gap, 0, tw + 4, self.height()),
                       Qt.AlignLeft | Qt.AlignVCenter, self.text())
        else:
            p.drawText(self.rect(), Qt.AlignCenter, self.text())


class RecButton(QPushButton):
    """Кругла кнопка запису: золота з іконкою спокою; під час запису — червона
    REC зі стоп-іконкою та м'яким пульсом. Іконка спокою параметризована
    (`idle_glyph`): «Диктування» лишає мікрофон за замовчуванням, «Запис екрана»
    передає відео-гліф (семантика дії = відео, не мікрофон — аудит 1.2.1 №3).
    Нескінченний пульс — дозволений виняток ТЗ, ЛИШЕ для стану запису
    (канон трею: червоне = активний запис)."""

    _SIDE = 56          # діаметр у dp
    _GLYPH = 20         # розмір іконки

    def __init__(self, parent=None, idle_glyph="fa6s.microphone"):
        super().__init__(parent)
        self.setFixedSize(self._SIDE, self._SIDE)
        # інстанс-QSS: глобальне правило QPushButton{min-height:20px; padding:8px}
        # при polish затирає min-height від setFixedSize до 38px і кнопку стискає
        # layout (коло 44px різалось площинами) — фіксуємо метрики явно
        self.setStyleSheet(
            f"QPushButton {{ min-width:{self._SIDE}px; max-width:{self._SIDE}px;"
            f" min-height:{self._SIDE}px; max-height:{self._SIDE}px;"
            f" padding:0; border:none; background:transparent; }}")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self._recording = False
        self._hover = False
        self._pulse = 0.0       # 0..1 — фаза пульс-кільця
        self._anim = None
        self._idle_glyph = idle_glyph
        self._build_icons()
        theme.register_restyle(self._restyle)   # нічний режим: перефарбувати іконки

    def _build_icons(self):
        # іконки з поточної палітри; QIcon.paint сам масштабує під DPI
        self._ico_idle = qta.icon(self._idle_glyph, color=theme.TEXT_ON_GOLD,
                                  color_disabled=theme.TEXT_MUTED)
        self._ico_stop = qta.icon("fa6s.stop", color=theme.TEXT_STRONG)

    def _restyle(self):
        self._build_icons()
        self.update()

    def set_recording(self, on: bool) -> None:
        """Перемкнути стан: запускає/зупиняє пульс (no-op при вимкнених анімаціях)."""
        changed = on != self._recording
        self._recording = on
        if changed:
            self._stop_pulse()
        self.sync_animations()
        self.update()

    def _start_pulse(self) -> None:
        if self._anim is None and self._recording and motion.animations_enabled():
            anim = QVariantAnimation(self)     # батько = ref (GC)
            anim.setDuration(1400)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setLoopCount(-1)              # виняток ТЗ: пульс REC
            anim.valueChanged.connect(self._set_pulse)
            self._anim = anim
            anim.start()

    def _stop_pulse(self) -> None:
        if self._anim is not None:
            self._anim.stop()
            self._anim.deleteLater()
            self._anim = None
        self._pulse = 0.0

    def sync_animations(self) -> None:
        """Негайно синхронізувати REC-пульс із живим налаштуванням."""
        if self._recording and motion.animations_enabled():
            self._start_pulse()
        else:
            self._stop_pulse()
        self.update()

    def _set_pulse(self, v):
        self._pulse = float(v)
        self.update()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            motion.press(self)
        super().mousePressEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        # від ФАКТИЧНОГО розміру, не константи: навіть стиснута кнопка
        # малює коло всередині себе, а не ріже його об край
        base_r = min(self.width(), self.height()) / 2.0 - 6
        # пульс: червоне кільце розходиться і тане
        if self._recording and self._pulse > 0.0:
            # кліп по краю: перо 2px має вміститись у віджет цілком
            ring = min(base_r + 1 + self._pulse * 5,
                       min(self.width(), self.height()) / 2.0 - 2)
            alpha = int(90 * (1.0 - self._pulse))
            p.setPen(QPen(QColor(*theme.ALERT_RGB, alpha), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, ring, ring)
        # тіло кнопки
        if not self.isEnabled():
            fill = QColor(theme.IDLE)
        elif self._recording:
            fill = QColor(theme.ALERT)
        else:
            fill = QColor(theme.GOLD_EYEBROW) if self._hover else QColor(theme.GOLD)
        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawEllipse(c, base_r, base_r)
        # іконка по центру
        ico = self._ico_stop if self._recording else self._ico_idle
        mode = QIcon.Normal if self.isEnabled() else QIcon.Disabled
        half = self._GLYPH // 2
        ico.paint(p, QRect(int(c.x()) - half, int(c.y()) - half,
                           self._GLYPH, self._GLYPH), Qt.AlignCenter, mode)
        if self.hasFocus():
            p.setPen(QPen(QColor(theme.FOCUS), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, base_r + 3, base_r + 3)


# ---------------------------------------------------------------------------
# StatusTag «Жива кромка» — статус-пілюля з живою кольоровою кромкою
# ---------------------------------------------------------------------------
# Скляна пілюля в ідіомі GlassButton + один новий примітив: кольоровий німб по
# ВЕРХНЬОМУ краю — мʼяке внутрішнє сяйво від кромки + тонкий акцентний штрих
# по верхній дузі бордера (НЕ суцільна заливка). Чотири стани; спінер —
# обертове пунктирне кільце; стейт-морф — плавна зміна акценту + пружинний
# спалах (референс Adrian Daniluk «status tags with soul»). Рух жене ОДИН
# спільний таймер, що сам спиняється, коли жодна пілюля не «busy» → 0% CPU.
_QUEUED, _BUSY, _DONE, _ERROR, _WARN = (
    "queued", "busy", "done", "error", "warn")


def _tag_accent(kind: str) -> str:
    # читаємо з ПОТОЧНОЇ палітри (нічний режим свопає її на льоту)
    # _WARN — теплий бурштиновий (GOLD_PRESSED), а НЕ DANGER_MUTED: той теракот
    # удень (#CF7B62) і червоний уночі (#E06A6A) лишав попередження червонуватим
    # (суд-3, п.7). Токен існуючий — нового hex не вводимо.
    return {_QUEUED: theme.IDLE, _BUSY: theme.GOLD, _DONE: theme.SUCCESS,
            _ERROR: theme.ALERT, _WARN: theme.GOLD_PRESSED}[kind]


def _tag_label(kind: str) -> str:
    return {_QUEUED: theme.TEXT_MUTED, _BUSY: theme.GOLD_EYEBROW,
            _DONE: theme.SUCCESS_EYEBROW, _ERROR: theme.ALERT_TEXT,
            _WARN: theme.GOLD_EYEBROW}[kind]


_TAG_ALIAS = {"pending": _QUEUED, "processing": _BUSY, "transcribing": _BUSY,
              "failed": _ERROR, "warning": _WARN, _QUEUED: _QUEUED,
              _BUSY: _BUSY, _DONE: _DONE, _ERROR: _ERROR, _WARN: _WARN}


class _PillDriver(QObject):
    """ОДИН спільний ~60fps годинник+таймер на всі анімовані пілюлі. Стартує з
    першою busy-пілюлею, спиняється коли остання пішла → 0% CPU у спокої. Лише
    busy-пілюлі реєструються (контролер розпізнає по одному файлу за раз)."""

    def __init__(self):
        super().__init__()
        self._pills = set()
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)              # батько = self (правило GC)
        self._timer.setInterval(16)             # ~60fps
        self._timer.timeout.connect(self._tick)

    def elapsed_ms(self) -> int:
        return self._clock.elapsed() if self._clock.isValid() else 0

    def register(self, pill) -> None:
        self._pills.add(pill)
        self.set_enabled(motion.animations_enabled())

    def set_enabled(self, enabled: bool) -> None:
        """Негайно синхронізувати timer з живим прапорцем анімацій."""
        if not enabled:
            if self._timer.isActive():
                self._timer.stop()
            return
        if not self._pills:
            return
        if not self._clock.isValid():
            self._clock.start()
        if not self._timer.isActive():
            self._timer.start()

    def unregister(self, pill) -> None:
        self._pills.discard(pill)
        if not self._pills and self._timer.isActive():
            self._timer.stop()

    def _tick(self) -> None:
        if not motion.animations_enabled():
            self._timer.stop()
            return
        dead = []
        for pill in self._pills:
            try:
                if pill.isVisible() and not pill.visibleRegion().isEmpty():
                    pill._repaint()
            except RuntimeError:
                dead.append(pill)               # C++-обʼєкт уже знищено
        for d in dead:
            self._pills.discard(d)
        if not self._pills:
            self._timer.stop()


_TAG_DRIVER = _PillDriver()                     # модуль-глобал, сильне посилання


def sync_status_animations() -> None:
    """Виклик після зміни налаштування: stop/start без очікування наступного tick."""
    _TAG_DRIVER.set_enabled(motion.animations_enabled())


class StatusTag(QWidget):
    """Скляна статус-пілюля з живою кольоровою кромкою. Drop-in для _set_badge:
    status = StatusTag("queued", text);  status.set_state("busy", text)."""

    QUEUED, BUSY, DONE, ERROR, WARN = _QUEUED, _BUSY, _DONE, _ERROR, _WARN

    PILL_H = 30
    RADIUS = 15                                 # PILL_H/2 → справжня пілюля
    PAD_H = 14
    ICON = 16
    GAP = 8
    GLOW = 8                                     # вертикальні поля (німб — усередині пілюлі)
    HEIGHT = GLOW + PILL_H + GLOW               # 46 — пілюля по центру віджета

    def __init__(self, kind="queued", text="", parent=None):
        super().__init__(parent)
        # непокриті пікселі прозорі → пілюля лягає на картку без власного фону
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._kind = _TAG_ALIAS.get(kind, kind)
        self._text = text
        self._accent = QColor(_tag_accent(self._kind))
        self._label_color = QColor(_tag_label(self._kind))
        self._flare = 0.0
        theme.register_restyle(self._restyle)   # нічний режим: перечитати палітру
        self._seed = (id(self) % 997) / 997.0   # десинк дихання по колонці
        self._morph_anim = None
        self._flare_anim = None

        self.setFixedHeight(self.HEIGHT)
        self._path = QPainterPath()
        self._icon_rect = QRect()
        self._text_rect = QRectF()
        self._dirty = QRect()
        self._cx = 0.0
        self._rimY = float(self.GLOW)
        self._sync_accessibility(notify=False)

    # ---- публічний API (дзеркало старого _set_badge) ----
    def set_state(self, kind, text=None, *, animated=True) -> None:
        kind = _TAG_ALIAS.get(kind, kind)
        text_changed = text is not None and text != self._text
        if text is not None and text != self._text:
            self._text = text
            self.updateGeometry()
        if kind == self._kind:
            if text is not None:
                self._sync_accessibility(notify=text_changed)
                self.update()
            return
        old_accent = QColor(self._accent)
        self._kind = kind
        self._label_color = QColor(_tag_label(kind))
        target = QColor(_tag_accent(kind))

        if kind == _BUSY:                        # тільки busy анімується вічно
            _TAG_DRIVER.register(self)
        else:
            _TAG_DRIVER.unregister(self)

        if animated and motion.animations_enabled():
            self._start_morph(old_accent, target)
            self._start_flare()
        else:
            self._stop_anims()
            self._accent = target
            self._flare = 0.0
        self._sync_accessibility(notify=True)
        self.update()                           # повний: підпис/тіло/іконка

    def kind(self) -> str:
        return self._kind

    def _restyle(self) -> None:
        """Нічний режим: перечитати акцент/підпис з нової палітри (без анімації)."""
        if self._morph_anim is None:            # не перебивати активний морф
            self._accent = QColor(_tag_accent(self._kind))
        self._label_color = QColor(_tag_label(self._kind))
        self.update()

    def _sync_accessibility(self, *, notify: bool) -> None:
        """Локалізований підпис статусу є і назвою, і коротким описом."""
        label = self._text.strip()
        self.setAccessibleName(label)
        self.setAccessibleDescription(label)
        if notify:
            for event_type in (QAccessible.Event.NameChanged,
                               QAccessible.Event.DescriptionChanged):
                QAccessible.updateAccessibility(QAccessibleEvent(self, event_type))

    # ---- розміри ----
    def _pill_font(self) -> QFont:
        f = QFont(self.font())                  # успадковує сімʼю з QSS (Segoe UI)
        f.setPixelSize(14)
        f.setWeight(QFont.DemiBold)
        return f

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self._pill_font())
        w = (self.PAD_H + self.ICON + self.GAP
             + fm.horizontalAdvance(self._text) + self.PAD_H)
        return QSize(w, self.HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # ---- життєвий цикл ----
    def showEvent(self, event):
        if self._kind == _BUSY:
            _TAG_DRIVER.register(self)
        super().showEvent(event)

    def hideEvent(self, event):
        self._stop_anims()
        _TAG_DRIVER.unregister(self)
        super().hideEvent(event)

    def resizeEvent(self, event):
        self._relayout(self.width())
        super().resizeEvent(event)

    def _relayout(self, w):
        pill = QRectF(0.5, self.GLOW + 0.5, w - 1, self.PILL_H - 1)
        self._path = QPainterPath()
        self._path.addRoundedRect(pill, self.RADIUS, self.RADIUS)
        self._cx = w / 2.0
        icy = self.GLOW + self.PILL_H / 2.0
        self._icon_rect = QRect(self.PAD_H, int(icy - self.ICON / 2),
                                self.ICON, self.ICON)
        tx = self.PAD_H + self.ICON + self.GAP
        self._text_rect = QRectF(tx, self.GLOW, w - tx - self.PAD_H, self.PILL_H)
        self._dirty = QRect(0, 0, w, self.HEIGHT)   # покриває німб цілком

    def _repaint(self):
        self.update(self._dirty) if not self._dirty.isNull() else self.update()

    # ---- переходи (одноразові, DeleteWhenStopped, батько = пілюля) ----
    def _stop_anims(self):
        for name in ("_morph_anim", "_flare_anim"):
            a = getattr(self, name)
            if a is not None:
                a.stop()                        # DeleteWhenStopped сам знищить
                setattr(self, name, None)

    def _start_morph(self, start_c, end_c):
        if self._morph_anim is not None:
            self._morph_anim.stop()
        a = QVariantAnimation(self)
        a.setDuration(240)
        a.setEasingCurve(QEasingCurve.OutCubic)
        a.setStartValue(start_c)
        a.setEndValue(end_c)                    # QVariantAnimation інтерполює QColor
        a.valueChanged.connect(self._on_morph)
        a.finished.connect(lambda: setattr(self, "_morph_anim", None))
        self._morph_anim = a
        a.start(QAbstractAnimation.DeleteWhenStopped)

    def _on_morph(self, v):
        self._accent = QColor(v)
        self._repaint()

    def _start_flare(self):
        if self._flare_anim is not None:
            self._flare_anim.stop()
        self._flare = 1.0
        a = QVariantAnimation(self)
        a.setDuration(240)                      # у каноні руху 120–250мс
        a.setEasingCurve(motion._out_back())    # OutBack(1.2): смачний відскок
        a.setStartValue(1.0)
        a.setEndValue(0.0)
        a.valueChanged.connect(self._on_flare)
        a.finished.connect(self._end_flare)
        self._flare_anim = a
        a.start(QAbstractAnimation.DeleteWhenStopped)

    def _on_flare(self, v):
        self._flare = float(v)
        self._repaint()

    def _end_flare(self):
        self._flare_anim = None
        self._flare = 0.0

    # ---- інтенсивність німба (дихання + спалах) ----
    def _breathe(self) -> float:
        t = _TAG_DRIVER.elapsed_ms() / 1000.0
        return 0.5 + 0.5 * math.sin(2.0 * math.pi * (t + self._seed) / 3.0)

    def _glow_peak(self) -> float:
        if self._kind == _QUEUED:
            base = 0.0                          # тихий стан очікування — без німба
        elif self._kind == _BUSY:
            base = (42.0 + 14.0 * self._breathe()
                    if motion.animations_enabled() else 50.0)
        else:                                   # done / error — статичний
            base = 46.0
        return base + self._flare * 20.0

    def _glow_rfactor(self) -> float:
        f = 1.0 + self._flare * 0.18
        if self._kind == _BUSY and motion.animations_enabled():
            f *= 1.0 + 0.04 * (self._breathe() * 2.0 - 1.0)
        return f

    # ---- малювання ----
    def paintEvent(self, _event):
        if self._path.isEmpty() and self.width() > 0:
            self._relayout(self.width())        # ліниво, якщо paint до resize
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = self._path

        light = theme.LIGHT_RGB
        # 1) скляне тіло — ідіома GlassButton (світло, не колір)
        p.setClipPath(path)
        p.fillPath(path, QColor(*light, 14))
        g = QLinearGradient(0, self._rimY, 0, self._rimY + self.PILL_H)
        g.setColorAt(0.0, QColor(*light, 26))
        g.setColorAt(0.45, QColor(*light, 5))
        g.setColorAt(1.0, QColor(0, 0, 0, 20))
        p.fillPath(path, g)
        p.setClipping(False)
        p.setPen(QPen(QColor(*light, 40), 1))     # нейтральна канон-кромка
        p.drawPath(path)

        # 2) німб по верхньому краю — мʼяке ВНУТРІШНЄ сяйво: кліп по шляху
        #    пілюлі, перетнутому з вузькою верхньою смугою; сплюснутий радіал
        #    гасне до нуля ще В МЕЖАХ смуги → жодних прямокутних обрізів
        peak = self._glow_peak()
        if peak > 0.5:
            accent = self._accent
            band_h = 11.0 * self._glow_rfactor()    # смуга сяйва від кромки
            band = QPainterPath()
            band.addRect(QRectF(0.0, self._rimY, float(self.width()), band_h))
            p.save()
            p.setClipPath(path.intersected(band))
            p.translate(self._cx, self._rimY)
            R = max(self.width() * 0.55, 1.0)
            p.scale(1.0, band_h / R)                # радіал рівно у висоту смуги
            grad = QRadialGradient(QPointF(0, 0), R)
            c0 = QColor(accent); c0.setAlpha(min(255, int(peak)))
            c1 = QColor(accent); c1.setAlpha(min(255, int(peak / 3)))
            c2 = QColor(accent); c2.setAlpha(0)
            grad.setColorAt(0.0, c0)
            grad.setColorAt(0.5, c1)
            grad.setColorAt(1.0, c2)
            p.fillRect(QRectF(-R, -R, 2 * R, 2 * R), grad)
            p.restore()

            # «жива кромка»: тонкий акцентний штрих по верхній дузі бордера —
            # градієнтне перо саме гасне до прозорого на плечах пілюлі
            rim = QLinearGradient(0, self._rimY, 0, self._rimY + self.PILL_H)
            r0 = QColor(accent); r0.setAlpha(min(255, int(peak * 1.8)))
            r1 = QColor(accent); r1.setAlpha(0)
            rim.setColorAt(0.0, r0)
            rim.setColorAt(0.55, r1)
            p.setPen(QPen(QBrush(rim), 1))
            p.drawPath(path)

        # 3) іконка стану у 16px-слоті зліва
        if self._kind == _BUSY:
            self._paint_spinner(p)
        elif self._kind == _DONE:
            self._paint_check(p)
        elif self._kind == _ERROR:
            self._paint_cross(p)
        elif self._kind == _WARN:
            self._paint_warn(p)
        else:
            self._paint_dot(p)

        # 4) підпис
        p.setFont(self._pill_font())
        p.setPen(self._label_color)
        p.drawText(self._text_rect, Qt.AlignLeft | Qt.AlignVCenter, self._text)

    def _icenter(self):
        r = self._icon_rect
        return r.center().x() + 0.5, r.center().y() + 0.5

    def _paint_dot(self, p):
        icx, icy = self._icenter()
        p.setPen(QPen(QColor(theme.IDLE), 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(icx, icy), 2.2, 2.2)

    def _paint_spinner(self, p):
        # обертове ПУНКТИРНЕ кільце (референс: пунктирне коло, що крутиться)
        icx, icy = self._icenter()
        live = motion.animations_enabled()
        phase = (_TAG_DRIVER.elapsed_ms() / 1200.0) * 360.0 % 360.0 if live else 0.0
        p.save()
        p.translate(icx, icy)
        p.rotate(phase)
        pen = QPen(QColor(self._accent), 1.8)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        # РІВНОМІРНІ дуги, а не dash-pen: пунктирне перо кладе риски нерівно по
        # колу (візерунок не ділиться рівно → «кострубате» кільце зі швом). N
        # однакових дуг з однаковими проміжками + обертання = чистий спінер.
        r = 6.2
        rect = QRectF(-r, -r, 2 * r, 2 * r)
        n = 8
        gap = 24.0                               # проміжок між рисками (градуси) —
        span = 360.0 / n - gap                   # чіткі проміжки, щоб оберт було видно
        for i in range(n):
            start = i * (360.0 / n) + gap / 2.0
            p.drawArc(rect, int(round(start * 16)), int(round(span * 16)))
        p.restore()

    def _paint_ring(self, p, r=6.4):
        icx, icy = self._icenter()
        pen = QPen(QColor(self._accent), 1.6)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(icx, icy), r, r)

    def _paint_check(self, p):
        self._paint_ring(p)
        icx, icy = self._icenter()
        pen = QPen(QColor(self._accent), 1.8)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        path = QPainterPath()
        path.moveTo(icx - 3.2, icy + 0.4)
        path.lineTo(icx - 1.0, icy + 2.7)
        path.lineTo(icx + 3.4, icy - 2.6)
        p.drawPath(path)

    def _paint_cross(self, p):
        self._paint_ring(p)
        icx, icy = self._icenter()
        pen = QPen(QColor(self._accent), 1.8)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        d = 2.9
        p.drawLine(QPointF(icx - d, icy - d), QPointF(icx + d, icy + d))
        p.drawLine(QPointF(icx - d, icy + d), QPointF(icx + d, icy - d))

    def _paint_warn(self, p):
        self._paint_ring(p)
        icx, icy = self._icenter()
        pen = QPen(QColor(self._accent), 1.8)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(icx, icy - 2.9), QPointF(icx, icy + 1.0))
        p.drawPoint(QPointF(icx, icy + 2.9))
