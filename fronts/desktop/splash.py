"""Splash-екран «Жук-медальйон» — заставка старту застосунку.

Композиція (затверджена Миколою по живому прев'ю): КОМПАКТНА картка, над якою
анімований жук-медальйон ВИХОДИТЬ ЗА МЕЖІ — половина кола висить над верхнім
краєм картки, навколо кола прозорість (видно робочий стіл). Медальйон грає
циклічно (animated WebP через QMovie, кругла альфа-маска вже вшита в кадри).

На картці — назва «Балачки / у Коростені», напис «Завантаження…», смужка
прогресу і рядок статусу.

Гейт руху (motion.animations_enabled()):
  • увімкнено — медальйон грає (QMovie); на ПЕРШОМУ запуску (greet=True) жук ще
    й один раз м'яко «прокидається» (fade + підйом 8px); якщо старт затягнувся
    (>_PROGRESS_GATE_MS) — знизу проявляється золоте заповнення смужки прогресу
    (дані, а не орнамент): не пульсує, не циклиться, не йде назад;
  • ВИМКНЕНО — медальйон СТАТИЧНИЙ (перший кадр із ресурсу), жодного руху,
    таймерів чи привітання: просто показати вікно й закрити splash.

На фіналі — тихе затихання всієї заставки 200мс (без золотого пульсу).

Фікс 9ea96fe (НЕ регресувати): _progress_anim обнуляється по finished
(_on_creep_done) — інакше пізніший finish_to().stop() на вже-видаленому Qt-об'єкті
кине RuntimeError і сплеш упаде на повільному старті.
"""
from PySide6.QtCore import (
    QAbstractAnimation, QEasingCurve, QPointF, QRect, QRectF, QSize, Qt, QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import (
    QColor, QCursor, QFont, QGuiApplication, QImage, QLinearGradient, QMovie,
    QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import QSplashScreen


from whisper_core.paths import asset_root
from . import motion
from .i18n import tr
from . import theme   # персоналізація кольору: заставка теж у активному тоні

# Рішення Миколи 25.07 (аудит мономи theme.py): сплеш РОЗШИРЕНО на всю
# персоналізацію кольору, не лише еталонний червоний. Три константи нижче —
# ЕТАЛОННИЙ червоний вигляд картки (hue точно 0° — перевірено math: G==B у
# кожній, тож colorsys дає H=0 для всіх трьох), той самий, що був хардкод-
# гілкою is_night() до 25.07. Для довільного hue їх ПЕРЕФАРБОВУЄ той самий
# theme._shift_token, що build_palette_for_hue — тон рухається, S/V (тобто
# насиченість і «наскільки темно») лишаються, тож 'red' (hue=0) виходить
# БАЙТ-У-БАЙТ тим самим, що й раніше (shift на 0° — тотожність), а класика
# (hue=None) узагалі не займається цими константами.
_NIGHT_CARD_BG     = "#1A0606"
_NIGHT_CARD_BORDER = "#5A2626"
_NIGHT_TRACK       = "#2A1010"


def _splash_hue():
    """Активний тон (0-359) для персоналізованого вигляду сплеша, або None —
    класика (day, оригінальні кольори картки й жука без змін)."""
    return theme._hue_for(theme.current_ui_color())


def _hue_tint(pm: QPixmap, hue) -> QPixmap:
    """Жук-медальйон — КАНОНІЧНИЙ бренд-маскот (пам'ять проєкту:
    balachky-mascot-canon; затверджений мультяшний стиль, векторні редизайни
    відхилені), не елемент інтерфейсу — персоналізація кольору його НЕ
    перефарбовує. Єдиний виняток — 'red' (hue=0): це не естетичний вибір, а
    вимога сумісності з приладами нічного бачення (жодного не-червоного
    світла), формула 70+175·lum / 28·lum / 28·lum лишається ТОЧНО такою, що
    була в _red_tint до узагальнення на персоналізацію (не рухаємо тон —
    hue тут завжди точно 0°, крутити нічого). Будь-який інший hue (teal/
    blue/... чи None-класика) повертає піксель незмінним."""
    if hue != 0:
        return pm
    img = pm.toImage().convertToFormat(QImage.Format_ARGB32)
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            a = c.alpha()
            if a == 0:
                continue
            lum = (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255.0
            img.setPixelColor(x, y, QColor(int(70 + 175 * lum),
                                           int(28 * lum), int(28 * lum), a))
    out = QPixmap.fromImage(img)
    out.setDevicePixelRatio(pm.devicePixelRatio())
    return out


def _card_bg() -> str:
    hue = _splash_hue()
    return _CARD_BG if hue is None else theme._shift_token(_NIGHT_CARD_BG, hue)


def _card_border() -> str:
    hue = _splash_hue()
    return _CARD_BORDER if hue is None else theme._shift_token(_NIGHT_CARD_BORDER, hue)


def _track_color() -> str:
    hue = _splash_hue()
    return _TRACK if hue is None else theme._shift_token(_NIGHT_TRACK, hue)

# --- ЄДИНЕ місце заміни ассетів медальйона -----------------------------------
# Анімований жук-медальйон Миколи: circular-alpha WebP (ffmpeg із Logo_no_Background)
# + перший кадр окремим PNG для reduce-motion. Обидва — 240×240, кругла альфа.
_MASCOT_WEBP = "splash-beetle.webp"
_MASCOT_STATIC = "splash-beetle-static.png"

# логічні розміри (px масштабує Qt під DPI — хардкоду фізичних пікселів немає)
_W, _H = 340, 350             # вікно: картка на всю ширину + прозорий верх під медальйон
_OVERHANG = 105               # на скільки медальйон висить над верхнім краєм картки
_MED = 210                    # діаметр медальйона (кола)
_CARD_TOP = _OVERHANG         # верх картки у координатах вікна
_CARD_RADIUS = 14.0

_CARD_BG = "#221D12"          # фон компактної картки (референс Миколи)
_CARD_BORDER = "#4A442F"      # тонка рамка картки
_TRACK = "#3B3526"            # жолоб смужки прогресу

_BEETLE_MS = 260              # привітальне «прокидання» жука: fade + підйом (OutCubic)
_BEETLE_RISE = 8              # підйом жука знизу під час привітання (логічні px)
_FADE_MS = 200                # фінальне затихання заставки у вікно
# Поріг появи заповнення смужки. МАЄ перевищувати мінімальний час показу splash
# (~800мс у app._splash_min_remaining) + побудову головного вікна, інакше на
# теплому кеші заповнення блимне під час штучного утримання. Головна гарантія —
# таймер гейта скасовується у finish_to/_stop_motion: якщо старт завершився
# раніше, заповнення не з'явиться взагалі. Поріг лише страхує від блимання на межі.
_PROGRESS_GATE_MS = 1200
_PROGRESS_CREEP_MS = 4000     # уповільнена асимптота до 90% — «йде, майже готово»

_OUTCUBIC = QEasingCurve(QEasingCurve.OutCubic)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


class SplashScreen(QSplashScreen):
    """QSplashScreen-нащадок: компактна картка + анімований жук-медальйон.

    Публічний контракт:
      splash = SplashScreen(greet=first_run)
      splash.show()
      splash.set_status(text)          # напис під заголовком (напр. «Готую модель…»)
      splash.finish_to(window)         # затихання заставки → показати вікно
    """

    def __init__(self, greet: bool = False):
        screen = (QGuiApplication.screenAt(QCursor.pos())
                  or QGuiApplication.primaryScreen())
        dpr = screen.devicePixelRatio() if screen is not None else 1.0
        self._dpr = dpr
        # пікселі малюнку = логічні × dpr; painter далі працює в логічних одиницях
        pm = QPixmap(int(_W * dpr), int(_H * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        super().__init__(screen, pm)
        self.setAttribute(Qt.WA_TranslucentBackground, True)   # прозорість навколо картки й кола
        self.setAccessibleName(tr("app_title"))                # a11y: вікно оголошує застосунок
        self._status = tr("app_loading_model")
        self._finished = False
        self._fade = 1.0            # непрозорість splash при фінальному затиханні (1→0)
        # привітання дозволене лише на першому запуску І коли рух увімкнено
        self._greet = bool(greet) and motion.animations_enabled()
        self._beetle_reveal = 0.0 if self._greet else 1.0   # 1.0 = жук повністю на місці
        self._progress = 0.0        # 0..1 — заповнення смужки прогресу
        self._line_alpha = 0.0      # 0..1 — м'яка поява золотого заповнення
        self._progress_shown = False
        # кешований фон (картка + світло + назва + напис), медальйон — динамічно
        self._bg = self._build_background(dpr)
        # медальйон: рух → QMovie (грає циклічно); статика → перший кадр PNG
        self._movie = None
        self._static_pm = None
        self._build_medallion(dpr)
        self._wake_anim = None
        self._progress_anim = None
        self._gate_timer = None
        self._fade_anim = None
        self._start_motion()

    # --- геометрія елементів (логічні одиниці) ---
    def _medallion_rect(self) -> QRect:
        return QRect((_W - _MED) // 2, 0, _MED, _MED)

    def _card_rect(self) -> QRectF:
        return QRectF(0.0, float(_CARD_TOP), float(_W), float(_H - _CARD_TOP))

    # --- медальйон: анімований QMovie або статичний перший кадр під DPI ---
    def _build_medallion(self, dpr: float) -> None:
        side = round(_MED * dpr)
        hue = _splash_hue()
        # Жук — канонічний бренд-маскот: грає ОРИГІНАЛЬНИМ кольоровим QMovie
        # для класики І для будь-якої персоналізації кольору (teal/blue/...) —
        # перефарбовується лише картка/рамка/смужка навколо нього. Виняток —
        # 'red' (hue=0): статичний тінт-жук, вимога нічного бачення, не
        # естетика (і per-frame тінт кольорового QMovie дорогий).
        if motion.animations_enabled() and hue != 0:
            mv = QMovie(str(asset_root() / "assets" / _MASCOT_WEBP), b"", self)
            if mv.isValid():
                mv.setCacheMode(QMovie.CacheAll)
                mv.setScaledSize(QSize(side, side))   # Qt масштабує при декоді
                mv.frameChanged.connect(self._on_frame)
                self._movie = mv
                return
        # reduce-motion / 'red' (нічне бачення) / битий WebP: статичний перший кадр
        self._static_pm = self._load_static(side, dpr, hue)

    def _load_static(self, side: int, dpr: float, hue) -> QPixmap:
        src = QPixmap(str(asset_root() / "assets" / _MASCOT_STATIC))
        if src.isNull():
            return src
        scaled = src.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        scaled = _hue_tint(scaled, hue)     # hue=None (класика) → без змін
        scaled.setDevicePixelRatio(dpr)
        return scaled

    def _current_medallion(self) -> QPixmap:
        """Поточний кадр медальйона (кадр QMovie або статичний перший кадр)."""
        if self._movie is not None:
            pm = self._movie.currentPixmap()
            if not pm.isNull():
                pm.setDevicePixelRatio(self._dpr)   # scaledSize = _MED×dpr → лог. _MED
            return pm
        return self._static_pm if self._static_pm is not None else QPixmap()

    def _on_frame(self, _n):
        self.update()

    def _start_medallion(self):
        if self._movie is not None:
            self._movie.start()

    # --- кешований фон: картка + світло + назва + напис ---
    def _build_background(self, dpr: float) -> QPixmap:
        pm = QPixmap(int(_W * dpr), int(_H * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        card = self._card_rect()
        path = QPainterPath()
        path.addRoundedRect(card, _CARD_RADIUS, _CARD_RADIUS)
        # тіло картки: темний тон референсу
        p.fillPath(path, QColor(_card_bg()))
        # м'яке світло зверху — ефект опуклості (світло, не колір), ідіома glass.py
        p.setClipPath(path)
        _lt = theme.LIGHT_RGB
        g = QLinearGradient(0, card.top(), 0, card.bottom())
        g.setColorAt(0.0, QColor(*_lt, 16))
        g.setColorAt(0.5, QColor(*_lt, 3))
        g.setColorAt(1.0, QColor(0, 0, 0, 26))
        p.fillPath(path, g)
        p.setClipping(False)
        # тонка рамка картки
        p.setPen(QPen(QColor(_card_border()), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5),
                          _CARD_RADIUS, _CARD_RADIUS)
        # назва: «Балачки» + «у Коростені» (єдиний ключ app_title, перше слово / решта)
        title = tr("app_title")
        brand, sub = (title.split(" ", 1) + [""])[:2]
        p.setPen(QColor(theme.TEXT_STRONG))
        f = QFont("Segoe UI")
        f.setPixelSize(20)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRect(0, 214, _W, 28), Qt.AlignHCenter, brand)
        p.setPen(QColor(theme.TEXT_MUTED))
        f2 = QFont("Segoe UI")
        f2.setPixelSize(13)
        p.setFont(f2)
        p.drawText(QRect(0, 246, _W, 18), Qt.AlignHCenter, sub)
        # напис «ЗАВАНТАЖЕННЯ…» (splash_eyebrow, uppercase-стиль лейбла)
        f3 = QFont("Segoe UI")
        f3.setPixelSize(11)
        f3.setBold(True)
        f3.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        p.setFont(f3)
        p.drawText(QRect(0, 276, _W, 16), Qt.AlignHCenter,
                   tr("splash_eyebrow").upper())
        p.end()
        return pm

    # --- запуск руху (медальйон + привітання + гейт заповнення) ---
    def _start_motion(self):
        if not motion.animations_enabled():
            return                       # абсолютна статика (медальйон = перший кадр)
        self._start_medallion()          # медальйон грає циклічно
        if self._greet:
            self._start_wake()
        # заповнення смужки — лише якщо старт затягнеться довше за поріг
        self._gate_timer = QTimer(self)
        self._gate_timer.setSingleShot(True)
        self._gate_timer.timeout.connect(self._reveal_progress)
        self._gate_timer.start(_PROGRESS_GATE_MS)

    def _start_wake(self):
        anim = QVariantAnimation(self)   # батько self = ref (правило GC)
        anim.setDuration(_BEETLE_MS)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.valueChanged.connect(self._on_wake)
        anim.finished.connect(self._on_wake_done)
        self._wake_anim = anim
        anim.start(QAbstractAnimation.DeleteWhenStopped)

    def _on_wake(self, v):
        self._beetle_reveal = _clamp01(float(v))
        self.update()

    def _on_wake_done(self):
        self._beetle_reveal = 1.0
        self._wake_anim = None
        self.update()

    def _reveal_progress(self):
        if self._finished:
            return                       # старт уже завершився — заповнення не потрібне
        self._progress_shown = True
        anim = QVariantAnimation(self)
        anim.setDuration(_PROGRESS_CREEP_MS)
        anim.setEasingCurve(QEasingCurve.OutCubic)   # уповільнення = чесна невизначеність
        anim.setStartValue(0.0)
        anim.setEndValue(0.9)            # до 100% дожимаємо лише на фіналі
        anim.valueChanged.connect(self._on_creep)
        anim.finished.connect(self._on_creep_done)   # обнулити ref (фікс 9ea96fe)
        self._progress_anim = anim
        anim.start(QAbstractAnimation.DeleteWhenStopped)

    def _on_creep(self, v):
        self._progress = float(v)
        self._line_alpha = min(1.0, self._progress / 0.05)   # м'яка поява ~перші 150мс
        self.update()

    def _on_creep_done(self):
        # Анімація природно добігла (creep до 0.9) і Qt видалив C++ об'єкт
        # (DeleteWhenStopped) — знімаємо звисаюче посилання, інакше пізніший
        # finish_to().stop() на вже-видаленому об'єкті кине RuntimeError (9ea96fe).
        self._progress_anim = None

    def _stop_motion(self):
        if self._movie is not None:
            self._movie.stop()
        for name in ("_wake_anim", "_progress_anim", "_fade_anim"):
            a = getattr(self, name, None)
            if a is not None:
                try:
                    a.stop()
                except RuntimeError:
                    pass                     # C++ об'єкт уже видалено (DeleteWhenStopped)
                setattr(self, name, None)
        if self._gate_timer is not None:
            self._gate_timer.stop()
            self._gate_timer = None

    def set_status(self, text: str):
        """Оновити напис статусу під смужкою (напр. «Готую модель…» у гілці відновлення)."""
        self._status = text
        self.update()

    # --- малювання (перевизначений paintEvent, НЕ drawContents) ---
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        if self._fade < 1.0:
            p.setOpacity(self._fade)     # затихання: уся заставка тане у вікно
        p.drawPixmap(0, 0, self._bg)     # кешована картка + назва + напис
        self._draw_progress(p)           # смужка прогресу (жолоб + золоте заповнення)
        self._draw_medallion(p)          # медальйон-breakout поверх верху картки
        # статус під смужкою
        p.setOpacity(self._fade)
        p.setPen(QColor(theme.TEXT_MUTED))
        f = QFont("Segoe UI")
        f.setPixelSize(12)
        p.setFont(f)
        p.drawText(QRect(0, 314, _W, 18), Qt.AlignHCenter, self._status)
        p.end()

    def _draw_medallion(self, p):
        pm = self._current_medallion()
        if pm.isNull():
            return
        reveal = self._beetle_reveal
        if reveal <= 0.001:
            return
        mr = self._medallion_rect()
        dpr = pm.devicePixelRatio() or 1.0
        w = pm.width() / dpr
        h = pm.height() / dpr
        rise = _BEETLE_RISE * (1.0 - reveal)      # підйом знизу під час привітання
        x = mr.x() + (mr.width() - w) / 2.0
        y = mr.y() + (mr.height() - h) / 2.0 + rise
        p.save()
        p.setOpacity(self._fade * _clamp01(reveal))
        p.drawPixmap(QPointF(x, y), pm)
        p.restore()

    def _draw_progress(self, p):
        # смужка: тьмяний жолоб (завжди) + золоте заповнення (лише після гейта).
        # Заповнення = self._progress; його поява = self._line_alpha.
        x0, x1, y = (_W - 240.0) / 2.0, (_W + 240.0) / 2.0, 300.0
        p.save()
        p.setOpacity(self._fade)
        pen = QPen(QColor(_track_color()), 4.0)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(x0, y), QPointF(x1, y))
        if self._progress_shown and self._line_alpha > 0.01:
            fill_x = x0 + (x1 - x0) * _clamp01(self._progress)
            if fill_x > x0 + 0.5:
                gold = QColor(theme.GOLD)
                gold.setAlpha(int(255 * self._line_alpha))
                pen = QPen(gold, 4.0)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.drawLine(QPointF(x0, y), QPointF(fill_x, y))
        p.restore()

    # --- фінал: затихання заставки у головне вікно ---
    def finish_to(self, window):
        """Готово: показати головне вікно й тихо розчинити заставку поверх нього.

        Анімації вимкнені → просто показати вікно й закрити splash (без руху)."""
        self._finished = True
        # гейт заповнення й привітання більше не потрібні
        if self._gate_timer is not None:
            self._gate_timer.stop()
            self._gate_timer = None
        for name in ("_wake_anim", "_progress_anim"):
            a = getattr(self, name, None)
            if a is not None:
                try:
                    a.stop()
                except RuntimeError:
                    pass                     # анімація вже добігла й Qt її видалив
                setattr(self, name, None)
        self._beetle_reveal = 1.0
        if not motion.animations_enabled():
            window.show()
            self.finish(window)          # штатне закриття QSplashScreen
            return
        window.show()                    # вікно зʼявляється ПІД заставкою
        if self._progress_shown:
            self._progress = 1.0         # заповнення «дожимається» до 100% на фіналі
            self._line_alpha = 1.0
        anim = QVariantAnimation(self)   # батько self = ref
        anim.setDuration(_FADE_MS)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.valueChanged.connect(self._on_fade)
        anim.finished.connect(self._on_fade_done)
        self._fade_anim = anim
        anim.start(QAbstractAnimation.DeleteWhenStopped)

    def _on_fade(self, v):
        self._fade = 1.0 - _OUTCUBIC.valueForProgress(float(v))
        self.update()

    def _on_fade_done(self):
        self._fade_anim = None
        if self._movie is not None:
            self._movie.stop()           # звільнити таймер декоду перед закриттям
        self.close()
