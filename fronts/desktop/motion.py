"""Анімації інтерфейсу застосунку.

Канон руху: 120–250мс, OutCubic / OutBack (overshoot 1.2, не дефолтні 1.7).
Залізні правила:
- ref на анімацію тримає Qt-батько (інакше GC зупиняє її на півдорозі);
- QGraphicsOpacityEffect знімається по finished (ефект — пастка рендера дітей);
- все no-op, коли анімації вимкнені (config або системний перемикач Windows).
"""
import ctypes
import sys

import shiboken6
from PySide6.QtCore import (
    QAbstractAnimation, QEasingCurve, QEvent, QObject, QParallelAnimationGroup,
    QPoint, QPropertyAnimation, QSequentialAnimationGroup, QTimer,
    QVariantAnimation, Qt,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QHBoxLayout,
    QLabel, QPushButton, QVBoxLayout, QWidget,
)

from . import theme   # нічний режим: glow-плівка читає палітру наживо

_QWIDGETSIZE_MAX = 16777215   # QWIDGETSIZE_MAX — скидання maximumHeight по finished

_cfg = None          # виставляє MainWindow через init_config
_system_ok = None    # кеш системного перемикача (WinAPI-виклик не безкоштовний)


def init_config(cfg) -> None:
    """Запам'ятати конфіг — джерело прапорця animations (діє одразу, без кешу)."""
    global _cfg
    _cfg = cfg


def _system_animations_ok() -> bool:
    """Системні «Ефекти анімації» Windows (SPI_GETCLIENTAREAANIMATION).
    Qt styleHints на Windows цей перемикач не бачить — питаємо WinAPI напряму."""
    global _system_ok
    if _system_ok is None:
        _system_ok = True
        if sys.platform == "win32":
            try:
                val = ctypes.c_int(1)
                ctypes.windll.user32.SystemParametersInfoW(
                    0x1042, 0, ctypes.byref(val), 0)   # SPI_GETCLIENTAREAANIMATION
                _system_ok = bool(val.value)
            except Exception:
                pass
    return _system_ok


def animations_enabled() -> bool:
    return bool(getattr(_cfg, "animations", True)) and _system_animations_ok()


def _out_back() -> QEasingCurve:
    curve = QEasingCurve(QEasingCurve.OutBack)
    curve.setOvershoot(1.2)
    return curve


def press(widget) -> None:
    """ТЗ п.4: натиск кнопки — geometry на 1px вниз 60мс OutQuad → назад 120мс
    OutBack. Вихідний rect відновлюється по finished: resize під час анімації
    не лишає кнопку зсунутою."""
    if not animations_enabled():
        return
    if getattr(widget, "_press_anim", None) is not None:
        return                      # попередній натиск ще програється
    rect = widget.geometry()
    down = QPropertyAnimation(widget, b"geometry")
    down.setDuration(60)
    down.setEasingCurve(QEasingCurve.OutQuad)
    down.setStartValue(rect)
    down.setEndValue(rect.translated(0, 1))
    up = QPropertyAnimation(widget, b"geometry")
    up.setDuration(120)
    up.setEasingCurve(_out_back())
    up.setStartValue(rect.translated(0, 1))
    up.setEndValue(rect)
    seq = QSequentialAnimationGroup(widget)   # батько = ref (GC)
    seq.addAnimation(down)
    seq.addAnimation(up)
    widget._press_anim = seq

    def _done():
        widget._press_anim = None
        parent = widget.parentWidget()
        lay = parent.layout() if parent is not None else None
        if lay is not None:
            # layout міг перерахуватись під час анімації (напр., змінився текст
            # кнопки) — не затираємо його геометрію застарілим rect
            lay.invalidate()
            lay.activate()
        else:
            widget.setGeometry(rect)

    seq.finished.connect(_done)
    seq.start(QAbstractAnimation.DeleteWhenStopped)


def wrap_appear(card: QWidget) -> QWidget:
    """ТЗ п.1: поява картки — fade 0→1 + верхній відступ 12→0, 180мс OutCubic.
    Повертає обгортку для вставки у layout стрічки; коли анімації вимкнені —
    саму картку (жодних зайвих віджетів у дереві)."""
    if not animations_enabled():
        return card
    host = QWidget()
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 12, 0, 0)
    lay.setSpacing(0)
    lay.addWidget(card)
    eff = QGraphicsOpacityEffect(card)
    eff.setOpacity(0.0)
    card.setGraphicsEffect(eff)
    fade = QPropertyAnimation(eff, b"opacity")
    fade.setDuration(180)
    fade.setEasingCurve(QEasingCurve.OutCubic)
    fade.setStartValue(0.0)
    fade.setEndValue(1.0)
    marg = QVariantAnimation()
    marg.setDuration(180)
    marg.setEasingCurve(QEasingCurve.OutCubic)
    marg.setStartValue(12.0)
    marg.setEndValue(0.0)
    marg.valueChanged.connect(lambda v: lay.setContentsMargins(0, int(v), 0, 0))
    group = QParallelAnimationGroup(host)     # батько host = ref; група володіє дітьми
    group.addAnimation(fade)
    group.addAnimation(marg)

    def _done():
        card.setGraphicsEffect(None)          # зняти ефект — пастка рендера
        lay.setContentsMargins(0, 0, 0, 0)

    group.finished.connect(_done)
    group.start(QAbstractAnimation.DeleteWhenStopped)
    return host


class _GoldFilm(QWidget):
    """Плівка поверх dropzone: заливка акцентом з альфою 0.06 (ТЗ п.10) — золото
    вдень, червоне вночі (theme.ACCENT_RGB читаємо при малюванні).
    Прозора для миші й drop-подій — лише малює."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._level = 0.0    # 0..1 — частка від цільової альфи

    def level(self) -> float:
        return self._level

    def set_level(self, v: float) -> None:
        self._level = float(v)
        self.update()

    def paintEvent(self, _event):
        if self._level <= 0.0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)
        p.fillPath(path, QColor(*theme.ACCENT_RGB, int(255 * 0.06 * self._level)))


def _film_glow(widget: QWidget, on: bool, film_key: str, anim_key: str) -> None:
    """Золота плівка-оверлей 0⇄0.06, 150мс OutCubic. Плівка — окремий лист поверх
    widget (не ефект на самому widget: пастка рендера дітей). film_key/anim_key —
    імена атрибутів на widget, щоб drag і hover не ділили одну плівку."""
    if not animations_enabled():
        return
    film = getattr(widget, film_key, None)
    if film is None:
        film = _GoldFilm(widget)
        setattr(widget, film_key, film)
    film.setGeometry(widget.rect())
    film.show()
    old = getattr(widget, anim_key, None)
    if old is not None:
        old.stop()                             # дві зустрічні анімації не б'ються
        old.deleteLater()                      # і не накопичуються дітьми widget
    anim = QVariantAnimation(widget)           # батько widget = ref (GC)
    anim.setDuration(150)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.setStartValue(film.level())
    anim.setEndValue(1.0 if on else 0.0)
    anim.valueChanged.connect(film.set_level)
    setattr(widget, anim_key, anim)
    anim.start()


def drag_glow(zone: QWidget, on: bool) -> None:
    """ТЗ п.10: реакція dropzone на перетягування — золота плівка 0⇄0.06, 150мс."""
    _film_glow(zone, on, "_drag_film", "_drag_anim")


# hover_glow (анімований золотий оверлей рядків словника) прибрано — ревізія
# Kimi K3: «рідка» плівка на кожному рядку при русі миші дратує. Наведення тепер
# миттєве через QSS (QFrame[profileRow]:hover). _film_glow лишається для drag_glow
# (dropzone) — велика первинна поверхня, де плавна реакція доречна.


# ---------------------------------------------------------------------------
# HoverLift «підйом + тінь» — наведення на інтерактивні кнопки й картки-плитки
# ---------------------------------------------------------------------------
class HoverLift(QObject):
    """Наведення «підйом + тінь»: елемент підіймається на 3px з мʼякою тінню.
    Вхід 160мс, вихід 120мс, ease-out (OutCubic — канон руху проєкту).

    reduce-motion (`animations_enabled() == False`) → ефекту НЕМА зовсім: лишаються
    штатні стани наведення (QSS-рамка кнопки, glow GlassButton). Жодного руху й тіні.

    Продуктивність: тінь (QGraphicsDropShadowEffect) створюється ЛІНИВО — лише на
    час наведення — і знімається на виході. Тому стрічка з десятками карток НЕ
    тримає десятки offscreen-ефектів у спокої: прокрутка списку йде без накладних
    витрат, ефект живе рівно на ОДНІЙ картці під курсором.

    Підйом реалізовано `move()` (позиція, не geometry) — не зачіпає розмір/розкладку
    вмісту; на виході позицію повертає сам layout (invalidate+activate, як у press).
    Обгортка через event-filter, тож чіпляється до будь-якого QWidget, не переписуючи
    його enterEvent/leaveEvent (glow GlassButton і підйом співіснують)."""

    RISE = 3.0            # на скільки px підіймається елемент
    BLUR = 24.0           # радіус розмиття тіні на піку
    Y_OFFSET = 8.0        # зсув тіні вниз на піку
    SHADOW_ALPHA = 120    # чорна тінь, помірна непрозорість (мʼяка)
    ENTER_MS = 160
    LEAVE_MS = 120

    def __init__(self, target: QWidget):
        super().__init__(target)          # батько = target: ref (GC) + спільне життя
        self._w = target
        self._t = 0.0                     # 0 спокій … 1 повний підйом
        self._home_y = None               # y у спокої (свіжий на кожен вхід)
        self._shadow = None
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._apply)
        self._anim.finished.connect(self._on_finished)
        target.installEventFilter(self)

    def eventFilter(self, _obj, event):
        et = event.type()
        # Sol-ревізія №2: якщо елемент опинився ВСЕРЕДИНІ контейнера, що вже має
        # lift (картка-плитка), власний підйом дав би подвійний рух (кнопка в
        # наведеній картці підстрибувала б на 6px). Тож щойно виявляємо lift-
        # предка (потрапляння в картку / показ) — знімаємо власний підйом.
        if et in (QEvent.Type.ParentChange, QEvent.Type.Show, QEvent.Type.Enter):
            if _has_lift_ancestor(self._w):
                self._dismantle()
                return False
        if et == QEvent.Type.Enter:
            if self._w.isEnabled():
                self._start(1.0)
        elif et == QEvent.Type.Leave:
            self._start(0.0)
        return False                      # не поглинаємо — enterEvent/glow працюють далі

    def _dismantle(self) -> None:
        """Зняти власний підйом (елемент живе в контейнері з lift): спинити
        анімацію, прибрати тінь, зняти event-filter і посилання ``_hover_lift``."""
        self._anim.stop()
        self._w.removeEventFilter(self)
        if self._shadow is not None:
            self._w.setGraphicsEffect(None)
            self._shadow = None
        if getattr(self._w, "_hover_lift", None) is self:
            self._w._hover_lift = None
        self.deleteLater()

    def _start(self, target: float) -> None:
        if not animations_enabled():
            return                        # reduce-motion: жодного підйому й тіні
        if target > 0.0:
            if self._t <= 0.001:
                self._home_y = self._w.y()   # свіжий «дім» на вході зі спокою
            self._ensure_shadow()
        self._anim.stop()
        self._anim.setDuration(self.ENTER_MS if target > self._t else self.LEAVE_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)   # ease-out в обидва боки
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(float(target))
        self._anim.start()

    def _ensure_shadow(self) -> None:
        if self._shadow is not None:
            return
        if self._w.graphicsEffect() is not None:
            return                        # чужий ефект (fade) не чіпаємо — лише підйом
        sh = QGraphicsDropShadowEffect(self._w)
        sh.setColor(QColor(0, 0, 0, self.SHADOW_ALPHA))
        sh.setBlurRadius(0.0)
        sh.setOffset(0.0, 0.0)
        self._w.setGraphicsEffect(sh)
        self._shadow = sh

    def _apply(self, v) -> None:
        self._t = float(v)
        if self._shadow is not None:
            self._shadow.setBlurRadius(self.BLUR * self._t)
            self._shadow.setOffset(0.0, self.Y_OFFSET * self._t)
        if self._home_y is not None:
            self._w.move(self._w.x(),
                         int(round(self._home_y - self.RISE * self._t)))

    def _on_finished(self) -> None:
        # завершення виходу (t≈0): зняти тінь і повернути точну позицію layout-у
        if self._t > 0.001:
            return
        if self._shadow is not None:
            self._w.setGraphicsEffect(None)   # 0 offscreen-ефектів у спокої
            self._shadow = None
        parent = self._w.parentWidget()
        lay = parent.layout() if parent is not None else None
        if lay is not None:
            lay.invalidate()
            lay.activate()                # layout — джерело правди про позицію


def _has_lift_ancestor(widget: QWidget) -> bool:
    """Чи має котрийсь із ПРЕДКІВ власний lift (картка-плитка). Тоді дочірньому
    елементу підйом не потрібен — інакше подвійний рух при наведенні."""
    parent = widget.parentWidget()
    while parent is not None:
        if getattr(parent, "_hover_lift", None) is not None:
            return True
        parent = parent.parentWidget()
    return False


def lift_on_hover(widget: QWidget) -> None:
    """Причепити наведення «підйом + тінь» до інтерактивного елемента. Ідемпотентно
    (повторний виклик — no-op). Застосовувати до кнопок і карток-плиток стрічки;
    НЕ до сайдбар-навігації (має власний активний стан) і статус-пілюль."""
    if getattr(widget, "_hover_lift", None) is not None:
        return
    widget._hover_lift = HoverLift(widget)


def fade_switch(stack, new_index: int) -> None:
    """ТЗ п.3: перехід сторінок — плоский знімок СТАРОЇ сторінки поверх стека
    тане (opacity 1→0, 140мс OutCubic), нова вже під ним. SLIDE не робимо (рве
    шов Mica). Оверлей — QLabel-лист із pixmap (пастки дітей нема)."""
    if not animations_enabled():
        stack.setCurrentIndex(new_index)
        return
    if new_index == stack.currentIndex():
        return
    old_page = stack.currentWidget()
    if old_page is None:
        stack.setCurrentIndex(new_index)
        return
    # прибрати недограний оверлей від дуже швидкого перемикання
    prev = getattr(stack, "_fade_overlay", None)
    if prev is not None:
        prev.deleteLater()
        stack._fade_overlay = None
    pm = old_page.grab()
    stack.setCurrentIndex(new_index)
    overlay = QLabel(stack)                     # дитина стека = ref
    overlay.setPixmap(pm)
    overlay.setScaledContents(False)
    overlay.setGeometry(0, 0, stack.width(), stack.height())
    overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
    overlay.raise_()
    overlay.show()
    stack._fade_overlay = overlay
    eff = QGraphicsOpacityEffect(overlay)
    overlay.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", overlay)
    anim.setDuration(140)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.setStartValue(1.0)
    anim.setEndValue(0.0)

    def _done():
        if getattr(stack, "_fade_overlay", None) is overlay:
            stack._fade_overlay = None
        overlay.deleteLater()

    anim.finished.connect(_done)
    anim.start(QAbstractAnimation.DeleteWhenStopped)


def _clear_scroll_anim(scrollarea) -> None:
    """Зняти попередню анімацію скролу, якщо є Й ЖИВА. shiboken6.isValid — бо
    DeleteWhenStopped міг уже знести C++ об'єкт, а Python-обгортка звисає:
    old.stop() на мертвому об'єкті кинув би RuntimeError (краш 21.07)."""
    old = getattr(scrollarea, "_scroll_anim", None)
    scrollarea._scroll_anim = None
    if old is not None and shiboken6.isValid(old):
        old.stop()                             # серія карток не б'ється сама з собою
        old.deleteLater()


def smooth_scroll_to_end(scrollarea) -> None:
    """ТЗ п.11: плавний скрол до нової картки — QPropertyAnimation по value
    скролбара, 200мс OutCubic. Заміна миттєвого sb.setValue(sb.maximum()).

    Життєвий цикл (фікс краху 21.07): анімація стартує з DeleteWhenStopped, тож
    коли добігає — Qt видаляє C++ об'єкт. Посилання scrollarea._scroll_anim
    ОБНУЛЯЄМО по finished, інакше наступний виклик дістане звисаючий wrapper і
    old.stop() кине RuntimeError (libshiboken: Internal C++ object already
    deleted) — лізло на КОЖНЕ додавання картки. Зняття старої додатково
    захищене shiboken6.isValid (див. _clear_scroll_anim)."""
    try:
        sb = scrollarea.verticalScrollBar()
        if not animations_enabled():
            sb.setValue(sb.maximum())
            return
        _clear_scroll_anim(scrollarea)

        def _run():
            try:
                # ПАСТКА: одразу після insertWidget maximum ще старий (layout не
                # перерахувався) — беремо ціль ліниво через singleShot(0)
                start = sb.value()
                target = sb.maximum()
                if target <= start:
                    sb.setValue(target)
                    return
                anim = QPropertyAnimation(sb, b"value", sb)   # батько = скролбар (ref)
                anim.setDuration(200)
                anim.setEasingCurve(QEasingCurve.OutCubic)
                anim.setStartValue(start)
                anim.setEndValue(target)

                def _done():
                    # Анімація добігла й Qt видалив C++ об'єкт (DeleteWhenStopped)
                    # — знімаємо звисаюче посилання, інакше наступний виклик кине
                    # RuntimeError на old.stop() (мертвий об'єкт).
                    if getattr(scrollarea, "_scroll_anim", None) is anim:
                        scrollarea._scroll_anim = None

                anim.finished.connect(_done)
                scrollarea._scroll_anim = anim
                anim.start(QAbstractAnimation.DeleteWhenStopped)
            except RuntimeError:
                pass                           # див. косметичний рубіж нижче

        QTimer.singleShot(0, _run)
    except RuntimeError:
        # Останній рубіж: плавний скрол — суто косметика (вставка тексту вже
        # успішна). Мертвий Qt-об'єкт тут НЕ повинен спливати в глобальний хук і
        # лякати користувача діалогом краху. Корінь усунено вище (обнулення по
        # finished + isValid); це страховка від інших мертвих-об'єктних випадків.
        pass


def expand_height(widget: QWidget) -> None:
    """ТЗ п.7: розгортання результату Файлів — maximumHeight від поточної висоти
    (шапка рядка) до sizeHint, 220мс OutCubic. По finished — скинути maximumHeight
    у QWIDGETSIZE_MAX, інакше пізніший контент/ресайз обріже рядок.
    ПАСТКА: sizeHint рахувати ПІСЛЯ додавання body+btns — інакше ціль замала."""
    if not animations_enabled():
        return
    start = widget.height()
    target = widget.sizeHint().height()
    if target <= start:
        return
    widget.setMaximumHeight(start)
    anim = QPropertyAnimation(widget, b"maximumHeight", widget)  # батько row = ref
    anim.setDuration(220)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.setStartValue(start)
    anim.setEndValue(target)

    def _done():
        widget.setMaximumHeight(_QWIDGETSIZE_MAX)

    anim.finished.connect(_done)
    anim.start(QAbstractAnimation.DeleteWhenStopped)


def slide_fade_in(widget: QWidget) -> None:
    """ТЗ п.9: поява примітки в Налаштуваннях — чистий opacity-fade 0→1, 160мс
    OutCubic (у QGridLayout 6px-зсув геометрією б'ється з лейаутом, свідомо
    опущено). Idempotent: вже видиму примітку не переанімовуємо."""
    if widget.isVisible():
        return
    if not animations_enabled():
        widget.show()
        return
    eff = QGraphicsOpacityEffect(widget)        # лист-QLabel — безпечно
    eff.setOpacity(0.0)
    widget.setGraphicsEffect(eff)
    widget.show()
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(160)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))  # зняти ефект
    anim.start(QAbstractAnimation.DeleteWhenStopped)


def toast(parent: QWidget, text: str) -> None:
    """ТЗ п.5: in-window Toast — QLabel унизу по центру parent: підйом 12px + fade-in
    200мс OutCubic → пауза 2.5с → fade-out 220мс OutCubic. Ревізія Kimi K3: overshoot
    (OutBack) і 24px-підйом прибрано — тост це службове повідомлення, а не жест
    «подивись на мене». Лист-QLabel, прозорий для миші; deleteLater по finished;
    новий гасить попередній."""
    if parent is None:
        return
    old = getattr(parent, "_toast", None)
    if old is not None:
        parent._toast = None
        # Попередній тост міг уже померти (закрили сторінку, Qt прибрав дитину) —
        # deleteLater по мертвому C++-об'єкту валить програму. Це і був краш
        # живого тесту 25.07: дві дії підряд, другий тост чіпав мертвий перший.
        if shiboken6.isValid(old):
            old.deleteLater()
    lbl = QLabel(text, parent)

    def _toast_style(w):
        w.setStyleSheet(
            f"QLabel {{ background: {theme.DEEP}; color: {theme.TEXT_STRONG};"
            f" border: 1px solid {theme._LINE_SOFT};"
            " border-radius: 8px; padding: 9px 16px; }}")

    _toast_style(lbl)
    theme.register_restyle_call(lbl, _toast_style)   # тост живе довше миттєвого показу
    lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
    # канон §3: довгий toast переноситься в межах вікна, а не вилазить/обрізається
    lbl.setWordWrap(True)
    lbl.setMaximumWidth(max(240, parent.width() - 64))
    lbl.adjustSize()
    x = max(0, (parent.width() - lbl.width()) // 2)
    y_final = parent.height() - lbl.height() - 28
    parent._toast = lbl

    def _drop():
        if not shiboken6.isValid(lbl):    # підпис міг померти раніше за таймер
            return
        if getattr(parent, "_toast", None) is lbl:
            parent._toast = None
        lbl.deleteLater()

    # Анімації вимкнені (конфіг або системний перемикач) — Toast це НОТИФІКАЦІЯ про
    # дію («Скопійовано» / «Слово додано»), а не декор, тож показуємо СТАТИЧНО (без
    # руху) і прибираємо через 2.5с. Глушити фідбек разом із рухом не можна.
    if not animations_enabled():
        lbl.move(x, y_final)
        lbl.raise_()
        lbl.show()
        t = QTimer(lbl)              # таймер — дитина lbl → авто-скасування при destroy
        t.setSingleShot(True)
        t.timeout.connect(_drop)
        t.start(2500)
        return

    y_start = y_final + 12
    lbl.move(x, y_start)
    lbl.raise_()
    lbl.show()
    eff = QGraphicsOpacityEffect(lbl)
    eff.setOpacity(0.0)
    lbl.setGraphicsEffect(eff)

    rise = QPropertyAnimation(lbl, b"pos")
    rise.setDuration(200)
    rise.setEasingCurve(QEasingCurve.OutCubic)   # без overshoot: службове, не жест
    rise.setStartValue(QPoint(x, y_start))
    rise.setEndValue(QPoint(x, y_final))
    fade_in = QPropertyAnimation(eff, b"opacity")
    fade_in.setDuration(200)
    fade_in.setEasingCurve(QEasingCurve.OutCubic)
    fade_in.setStartValue(0.0)
    fade_in.setEndValue(1.0)
    appear = QParallelAnimationGroup()
    appear.addAnimation(rise)
    appear.addAnimation(fade_in)

    fade_out = QPropertyAnimation(eff, b"opacity")
    fade_out.setDuration(220)
    fade_out.setEasingCurve(QEasingCurve.OutCubic)
    fade_out.setStartValue(1.0)
    fade_out.setEndValue(0.0)

    seq = QSequentialAnimationGroup(parent)     # батько parent = ref (GC)
    seq.addAnimation(appear)
    seq.addPause(2500)                          # виняток ТЗ: hold-фаза Toast
    seq.addAnimation(fade_out)

    def _done():
        if not shiboken6.isValid(lbl):    # finished прийшов до мертвого підпису
            return
        if getattr(parent, "_toast", None) is lbl:
            parent._toast = None
        lbl.deleteLater()

    seq.finished.connect(_done)
    seq.start(QAbstractAnimation.DeleteWhenStopped)


def undo_toast(parent: QWidget, text: str, on_undo, *, undo_label: str = "Скасувати",
               seconds: int = 10) -> None:
    """feature/selflearn-dict: тост-підтвердження з дією «Скасувати» на ~seconds
    секунд. На відміну від toast() — КЛІКАБЕЛЬНИЙ (кнопка undo), тож окремий слот
    _undo_toast (щоб звичайні тости його не гасили) і без WA_TransparentForMouse.
    on_undo викликається один раз при натисканні; далі тост зникає. Статичний (без
    анімації) — службове повідомлення про виконану дію, а не декор."""
    if parent is None:
        return
    old = getattr(parent, "_undo_toast", None)
    if old is not None:
        parent._undo_toast = None
        if shiboken6.isValid(old):        # той самий клас, що в toast()
            old.deleteLater()

    def _frame_style(w):
        w.setStyleSheet(
            f"QFrame {{ background: {theme.DEEP};"
            f" border: 1px solid {theme._LINE_SOFT}; border-radius: 8px; }}")

    def _label_style(w):
        w.setStyleSheet(
            f"QLabel {{ color: {theme.TEXT_STRONG}; background: transparent; border: none; }}")

    def _btn_style(w):
        w.setStyleSheet(
            f"QPushButton {{ color: {theme.GOLD_EYEBROW}; background: transparent; border: none;"
            " font-weight: 600; padding: 2px 4px; }"
            " QPushButton:hover { text-decoration: underline; }")

    frame = QFrame(parent)
    _frame_style(frame)
    theme.register_restyle_call(frame, _frame_style)   # тост живе довше миттєвого показу
    lay = QHBoxLayout(frame)
    lay.setContentsMargins(16, 8, 12, 8)
    lay.setSpacing(14)
    lbl = QLabel(text, frame)
    _label_style(lbl)
    theme.register_restyle_call(lbl, _label_style)
    lbl.setWordWrap(True)
    lay.addWidget(lbl, 1)
    btn = QPushButton(undo_label, frame)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setAccessibleName(undo_label)
    _btn_style(btn)
    theme.register_restyle_call(btn, _btn_style)
    lay.addWidget(btn, 0)
    frame.setMaximumWidth(max(280, parent.width() - 64))
    frame.adjustSize()
    x = max(0, (parent.width() - frame.width()) // 2)
    y = parent.height() - frame.height() - 28
    frame.move(x, y)
    frame.raise_()
    frame.show()
    parent._undo_toast = frame

    def _drop():
        if not shiboken6.isValid(frame):  # рамку могли прибрати раніше за таймер
            return
        if getattr(parent, "_undo_toast", None) is frame:
            parent._undo_toast = None
        frame.deleteLater()

    t = QTimer(frame)
    t.setSingleShot(True)
    t.timeout.connect(_drop)
    t.start(int(seconds * 1000))

    def _clicked():
        try:
            on_undo()
        finally:
            _drop()

    btn.clicked.connect(_clicked)
