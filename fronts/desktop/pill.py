"""Плаваючий індикатор диктування — маленька пілюля поверх усіх вікон, яку видно
під час запису/розпізнавання навіть коли головне вікно сховане (feature/ux-center).

Її можна перетягнути мишею в будь-яке місце; позиція запам'ятовується в config.
Подвійний клік або пункт контекстного меню — «Скинути позицію». При зникненні
монітора позиція відкочується до типової (whisper_core.pill_pos).

Канон «Мундір»: без нових кольорів — REC-червоний під час запису, золото під час
розпізнавання (ті самі стани, що й у треї/смужці рівня).

«З душею» (feature/status-tags-soul): пілюля живе, але стримано —
- запис: мʼякий пульс-німб під крапкою (дозволений виняток ТЗ: REC-пульс);
- розпізнавання: shimmer — світлова смуга ковзає пілюлею (світло, не колір);
- зміна стану: мʼякий колірний морф крапки + fade-in при появі.
Рух жене ОДИН таймер (~30fps), що спиняється, щойно пілюля idle/схована або
системні анімації вимкнені → 0% CPU у спокої.
"""
import math

from PySide6.QtCore import (
    QAbstractAnimation, QEasingCurve, QElapsedTimer, QPoint, QPropertyAnimation,
    QRectF, Qt, QTimer, QVariantAnimation,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QGuiApplication, QLinearGradient, QPainter,
    QPainterPath, QRadialGradient,
)
from PySide6.QtWidgets import QMenu, QWidget

from . import motion
from . import theme   # кольори читаємо при малюванні (нічний режим свопає палітру)
from .i18n import tr

_H = 34                     # висота пілюлі
# Геометрія тексту в paintEvent: кольорова крапка + підпис від _TEXT_LEFT px
# зліва, _TEXT_RIGHT px відступ справа. Ширину пілюлі рахуємо від напису
# (fontMetrics) під ОБИДВІ мови, щоб «Розшифровую…»/«Transcribing…» не різались.
_TEXT_LEFT = 34            # відступ підпису від лівого краю (за крапкою)
_TEXT_RIGHT = 12          # відступ підпису від правого краю
_W_FALLBACK = 132         # запасна ширина, якщо метрики недоступні
_MARGIN_BOTTOM = 90        # відступ від низу типової позиції (над панеллю задач)

_STATE_KEY = {"recording": "pill_recording", "busy": "pill_busy",
              "loading": "pill_loading"}


def _state_color(state: str) -> str:
    """Колір крапки стану з ПОТОЧНОЇ палітри (нічний режим свопає її на льоту).
    REC — червоний, розпізнавання/завантаження — акцент."""
    return theme.ALERT if state == "recording" else theme.GOLD


def _is_color_state(state: str) -> bool:
    return state in ("recording", "busy", "loading")

_PULSE_MS = 1400           # період дихання REC-німба
_SHIMMER_MS = 1500         # період shimmer-проходу (recon: ~1.5с — «природно»)
_TICK_MS = 33              # ~30fps: гладко для мʼякого руху, легше за 60fps


class FloatingPill(QWidget):
    def __init__(self, *, on_moved, on_reset):
        super().__init__(None)
        self._on_moved = on_moved      # (x, y) — зберегти позицію
        self._on_reset = on_reset      # скинути позицію
        self._state = "idle"
        self._queue_count = 0          # feature/dictation-queue: скільки фраз ЩЕ
                                       # чекає розшифровки (бейдж «+N» на пілюлі)
        self._drag_from = None         # зсув курсора всередині пілюлі під час drag
        self._accent = QColor(theme.GOLD)  # колір крапки (морфиться при зміні стану)
        self._morph_anim = None
        self._fade_anim = None
        self._clock = QElapsedTimer()  # безперервна фаза пульсу/shimmer
        self._timer = QTimer(self)     # батько = self → ref (GC)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._w = self._measure_width()
        self.setFixedSize(self._w, _H)
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setToolTip(tr("pill_tip"))

    def _label_text(self) -> str:
        """Підпис стану, з бейджем «+N» коли за поточною фразою чекають ще N
        (feature/dictation-queue)."""
        base = tr(_STATE_KEY.get(self._state, "pill_recording"))
        if self._queue_count > 0:
            return base + tr("pill_queue_badge", n=self._queue_count)
        return base

    def _measure_width(self):
        """Ширина пілюлі від найширшого підпису стану (обидві мови однакові —
        активна tr()), щоб напис ніколи не обрізався. Коли є хвіст черги —
        враховуємо ще й бейдж «+N». Шрифт — той самий 13px pixelSize, що й у
        paintEvent."""
        f = QFont(self.font())
        f.setPixelSize(13)
        fm = QFontMetrics(f)
        badge = tr("pill_queue_badge", n=self._queue_count) if self._queue_count > 0 else ""
        text_w = max(fm.horizontalAdvance(tr("pill_recording") + badge),
                     fm.horizontalAdvance(tr("pill_busy") + badge),
                     fm.horizontalAdvance(tr("pill_loading") + badge))
        if text_w <= 0:
            return _W_FALLBACK
        return _TEXT_LEFT + text_w + _TEXT_RIGHT + 6   # +6 — запас під край

    def set_queue_count(self, n: int):
        """feature/dictation-queue: скільки фраз ЩЕ чекає розшифровки. Пілюля
        показує бейдж «+N» і за потреби підростає праворуч (від збереженого
        лівого краю), щоб напис не обрізався."""
        n = max(0, int(n))
        if n == self._queue_count:
            return
        self._queue_count = n
        neww = self._measure_width()
        if neww != self._w:
            self._w = neww
            self.setFixedSize(self._w, _H)
        self.update()

    # --- позиція ---
    def _screen_rects(self):
        return [(g.x(), g.y(), g.width(), g.height())
                for g in (s.geometry() for s in QGuiApplication.screens())]

    def _default_pos(self):
        scr = QGuiApplication.primaryScreen()
        geo = scr.availableGeometry() if scr else None
        if geo is None:
            return QPoint(200, 200)
        x = geo.x() + (geo.width() - self._w) // 2
        y = geo.y() + geo.height() - _H - _MARGIN_BOTTOM
        return QPoint(x, y)

    def apply_saved_position(self, saved):
        """saved=(x, y)|None з config. Показати у збереженій позиції, якщо вона ще
        видима на якомусь моніторі; інакше — у типовій."""
        from whisper_core import pill_pos
        default = self._default_pos()
        x, y = pill_pos.resolved_position(saved, self._screen_rects(),
                                          (default.x(), default.y()))
        self.move(x, y)

    def reset_to_default(self):
        self.move(self._default_pos())

    # --- стан (від rec_state контролера) ---
    def set_state(self, state: str):
        """recording/busy → показати пілюлю відповідного кольору; idle → сховати.
        Перехід між кольоровими станами — мʼякий колірний морф крапки."""
        prev = self._state
        self._state = state
        if _is_color_state(state):
            target = QColor(_state_color(state))
            if not self.isVisible():
                self._stop_morph()
                self._accent = target
                self.show()                # showEvent → fade-in + старт таймера
            else:
                if (prev != state and motion.animations_enabled()
                        and self._accent != target):
                    self._start_morph(QColor(self._accent), target)
                else:
                    self._stop_morph()
                    self._accent = target
            self._sync_timer()
            self.update()
        else:
            self.hide()                    # hideEvent спиняє таймер

    def sync_animations(self):
        """Негайно застосувати живий перемикач анімацій (з Налаштувань)."""
        if not motion.animations_enabled():
            self._stop_morph()             # доморфити миттєво до цільового кольору
            if _is_color_state(self._state):
                self._accent = QColor(_state_color(self._state))
        self._sync_timer()
        self.update()

    # --- дисципліна таймера: крутиться ЛИШЕ коли є що анімувати й видно ---
    def _sync_timer(self):
        live = (_is_color_state(self._state) and self.isVisible()
                and motion.animations_enabled())
        if live:
            if not self._clock.isValid():
                self._clock.start()
            if not self._timer.isActive():
                self._timer.start()
        elif self._timer.isActive():
            self._timer.stop()

    def _tick(self):
        if not motion.animations_enabled() or not self.isVisible():
            self._timer.stop()
            return
        self.update()

    # --- переходи станів ---
    def _fade_in(self):
        """Мʼяка поява вікна (windowOpacity 0→1). Вимкнені анімації → одразу 1."""
        if self._fade_anim is not None:
            self._fade_anim.stop()
            self._fade_anim = None
        if not motion.animations_enabled():
            self.setWindowOpacity(1.0)
            return
        self.setWindowOpacity(0.0)
        a = QPropertyAnimation(self, b"windowOpacity", self)   # батько = ref (GC)
        a.setDuration(150)
        a.setEasingCurve(QEasingCurve.OutCubic)
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        a.finished.connect(lambda: setattr(self, "_fade_anim", None))
        self._fade_anim = a
        a.start(QAbstractAnimation.DeleteWhenStopped)

    def _start_morph(self, start_c, end_c):
        if self._morph_anim is not None:
            self._morph_anim.stop()
        a = QVariantAnimation(self)        # батько = ref (GC)
        a.setDuration(200)                 # у каноні руху 120–250мс
        a.setEasingCurve(QEasingCurve.OutCubic)
        a.setStartValue(start_c)
        a.setEndValue(end_c)               # QVariantAnimation інтерполює QColor
        a.valueChanged.connect(self._on_morph)
        a.finished.connect(lambda: setattr(self, "_morph_anim", None))
        self._morph_anim = a
        a.start(QAbstractAnimation.DeleteWhenStopped)

    def _on_morph(self, v):
        self._accent = QColor(v)
        self.update()

    def _stop_morph(self):
        if self._morph_anim is not None:
            self._morph_anim.stop()        # DeleteWhenStopped сам знищить
            self._morph_anim = None

    # --- життєвий цикл ---
    def showEvent(self, event):
        super().showEvent(event)
        self._fade_in()
        self._sync_timer()

    def hideEvent(self, event):
        if self._timer.isActive():
            self._timer.stop()
        super().hideEvent(event)

    # --- перетягування ---
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag_from = ev.globalPosition().toPoint() - self.pos()
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._drag_from is not None and (ev.buttons() & Qt.LeftButton):
            self.move(ev.globalPosition().toPoint() - self._drag_from)
            ev.accept()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._drag_from is not None:
            self._drag_from = None
            p = self.pos()
            self._on_moved(p.x(), p.y())
            ev.accept()

    def mouseDoubleClickEvent(self, ev):
        # подвійний клік — скинути позицію (як у PillFloat)
        self._on_reset()
        ev.accept()

    def contextMenuEvent(self, ev):
        menu = QMenu(self)
        act = menu.addAction(tr("pill_reset"))
        act.triggered.connect(self._on_reset)
        menu.exec(ev.globalPos())

    # --- малювання ---
    def paintEvent(self, _ev):
        if not _is_color_state(self._state):
            return
        color = self._accent
        # фаза руху активна лише поки таймер справді крутиться (не в idle,
        # не при вимкнених анімаціях) — інакше статичний кадр
        live = self._timer.isActive() and self._clock.isValid()
        elapsed = self._clock.elapsed() if live else 0

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, _H / 2.0, _H / 2.0)
        light = theme.LIGHT_RGB
        # тіло: глибока панель у тон полів вводу (напівпрозора)
        p.fillPath(path, QColor(*theme.PANEL_RGB, 235))

        # shimmer при розпізнаванні: світлова смуга ковзає пілюлею (світло,
        # не колір — канон). Кліп по тілу пілюлі, щоб не вилазила за краї.
        if self._state in ("busy", "loading") and live:
            sweep = (elapsed % _SHIMMER_MS) / _SHIMMER_MS      # 0..1
            band = 44.0
            cx = -band + sweep * (self.width() + 2 * band)
            grad = QLinearGradient(cx - band / 2.0, 0.0, cx + band / 2.0, 0.0)
            grad.setColorAt(0.0, QColor(*light, 0))
            grad.setColorAt(0.5, QColor(*light, 26))
            grad.setColorAt(1.0, QColor(*light, 0))
            p.save()
            p.setClipPath(path)
            p.fillPath(path, grad)
            p.restore()

        # кольоровий кружок стану ліворуч
        dot_r = 6.0
        cx = r.left() + 16
        cy = r.center().y()

        # пульс при записі: мʼякий німб-дихання під крапкою (виняток ТЗ:
        # REC-пульс — єдиний дозволений нескінченний рух)
        if self._state == "recording" and live:
            breathe = 0.5 + 0.5 * math.sin(2.0 * math.pi * elapsed / _PULSE_MS)
            halo_r = dot_r + 3.0 + 3.0 * breathe
            halo_a = int(30 + 55 * breathe)
            glow = QRadialGradient(cx, cy, halo_r)
            c0 = QColor(color); c0.setAlpha(halo_a)
            c1 = QColor(color); c1.setAlpha(0)
            glow.setColorAt(0.0, c0)
            glow.setColorAt(1.0, c1)
            p.setBrush(glow)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QRectF(cx - halo_r, cy - halo_r, 2 * halo_r, 2 * halo_r))

        p.setBrush(QColor(color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(cx - dot_r, cy - dot_r, 2 * dot_r, 2 * dot_r))

        # напис стану
        p.setPen(QColor(theme.TEXT_STRONG))
        f = p.font()
        f.setPixelSize(13)   # пікселі (не pt!): pt масштабується інакше за решту DPI
        p.setFont(f)
        text_rect = r.adjusted(_TEXT_LEFT, 0, -_TEXT_RIGHT, 0)
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self._label_text())
        # тонкий бордюр (канон: бордюр замість тіні)
        p.setPen(QColor(*light, 40))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
