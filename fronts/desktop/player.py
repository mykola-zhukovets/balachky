"""Вбудований аудіо-плеєр «Balachky» — картка наради й запис диктофона.

feature/player-recordings. Технологія відтворення — QtMultimedia (QMediaPlayer +
QAudioOutput): є у встановленому колесі PySide6, декодує WAV сам, дає перемотку,
швидкість і гучність без власного мікшера. Якщо модуль колись недоступний
(екзотична збірка) — тихий фолбек на одну кнопку «Відкрити у системному плеєрі».

Канон «Мундір»: золото — єдиний акцент, скло замість тіней. Смуга прогресу й
гучності — та сама ідіома, що LevelMeter (золота заливка на глибокій підложці),
з перемоткою кліком/тягненням. Іконки — qtawesome (play/pause/volume).

ВАЖЛИВО (Windows/ffmpeg-бекенд): QMediaPlayer тримає файл ВІДКРИТИМ, поки
джерело завантажене — навіть на паузі/стопі. Тому джерело вантажиться ЛЕДАЧЕ
(лише на перший play), а stop() ЗВІЛЬНЯЄ його (setSource(QUrl())) — інакше
видалення запису/наради падало б із WinError 32 «файл зайнятий». Будь-який
шлях видалення файлів мусить спершу викликати stop() плеєрів.
"""
import weakref

from PySide6.QtCore import QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QSizePolicy, QToolButton, QWidget,
)
from PySide6.QtGui import QDesktopServices

import qtawesome as qta

from . import motion
from .glass import GlassButton
from .i18n import tr
from . import theme   # кольори читаємо при малюванні (нічний режим свопає палітру)

try:                                   # QtMultimedia є у колесі PySide6 (перевірено)
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    _HAVE_QTMM = True
except Exception:                      # pragma: no cover — лише коли модуль відсутній
    QAudioOutput = QMediaPlayer = None
    _HAVE_QTMM = False

# швидкості відтворення, які циклічно перемикає кнопка (стандарт плеєрів):
# 1× → 1,25× → 1,5× → 2× → 0,75× → 1×
_SPEEDS = (1.0, 1.25, 1.5, 2.0, 0.75)

# Обрана швидкість пам'ятається на СЕСІЮ: новий InlinePlayer (інша нарада/запис/
# аудіофайл) стартує з тією ж швидкістю, що користувач виставив останньою.
# Модульна змінна — як _SPEEDS/_resume_backstep_ms, плеєр не має доступу до cfg.
_session_speed_idx = 0

# авто-відкат після паузи: при відновленні відмотати на стільки назад (мс).
# Значення задає застосунок із Налаштувань через set_resume_backstep_ms; 0 —
# без відкату. Модульна змінна (як _SPEEDS) — плеєр не має доступу до cfg.
_resume_backstep_ms = 1500


def set_resume_backstep_ms(ms: int) -> None:
    """Скільки відмотати назад при відновленні після паузи (мс, ≥0)."""
    global _resume_backstep_ms
    _resume_backstep_ms = max(0, int(ms))


def resume_position_ms(current_ms: int, backstep_ms: int) -> int:
    """Позиція відновлення: поточна мінус відкат, не нижче нуля."""
    return max(0, int(current_ms) - max(0, int(backstep_ms)))

# реєстр живих плеєрів: play() одного спиняє інші (два плеєри на сторінці не
# мають грати одночасно). Слабкі посилання — знищений Qt-об'єкт зникає сам.
_LIVE_PLAYERS = weakref.WeakSet()


def fmt_time(ms: int) -> str:
    """Мілісекунди → «M:SS» (або «H:MM:SS» для запису понад годину)."""
    total = max(0, int(ms)) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class _Slider(QWidget):
    """Горизонтальна смуга 0..1 із золотою заливкою й круглою ручкою. Клік або
    тягнення ставить позицію (перемотка). Значення ззовні — set_fraction (без
    сигналу); дію користувача віддає сигнал moved(float)."""

    moved = Signal(float)

    #: крок перемотки/гучності на Key_Left/Key_Right (5% діапазону)
    _KEY_STEP = 0.05

    def __init__(self, *, eyebrow=False, height=16, parent=None):
        super().__init__(parent)
        self._value = 0.0
        self._eyebrow = eyebrow      # True → GOLD_EYEBROW (гучність), інакше GOLD
        self._dragging = False
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)

    def set_fraction(self, frac: float) -> None:
        """Оновити позицію ззовні (плеєр рухається) — без сигналу moved.
        Під час тягнення ігноруємо, щоб ручка не смикалась проти пальця."""
        if self._dragging:
            return
        v = 0.0 if frac < 0 else 1.0 if frac > 1 else float(frac)
        if v != self._value:
            self._value = v
            self.update()

    def fraction(self) -> float:
        return self._value

    def _value_at(self, x: float) -> float:
        w = max(1, self.width())
        v = x / w
        return 0.0 if v < 0 else 1.0 if v > 1 else v

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._value = self._value_at(event.position().x())
            self.update()
            self.moved.emit(self._value)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._value = self._value_at(event.position().x())
            self.update()
            self.moved.emit(self._value)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self._value = self._value_at(event.position().x())
            self.update()
            self.moved.emit(self._value)

    def keyPressEvent(self, event):
        """Клавіатурна перемотка: ←/→ — крок _KEY_STEP; Home/End — краї."""
        key = event.key()
        if key == Qt.Key_Left:
            self._set_value(self._value - self._KEY_STEP)
        elif key == Qt.Key_Right:
            self._set_value(self._value + self._KEY_STEP)
        elif key == Qt.Key_Home:
            self._set_value(0.0)
        elif key == Qt.Key_End:
            self._set_value(1.0)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def _set_value(self, v: float) -> None:
        v = 0.0 if v < 0 else 1.0 if v > 1 else float(v)
        self._value = v
        self.update()
        self.moved.emit(self._value)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        track_h = 6.0
        top = (h - track_h) / 2.0
        r = QRectF(1.0, top, self.width() - 2.0, track_h)
        radius = track_h / 2.0
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        accent = QColor(theme.GOLD_EYEBROW if self._eyebrow else theme.GOLD)
        p.fillPath(path, QColor(*theme.PANEL_RGB, 180))    # підложка у тон полів
        p.setClipPath(path)
        fill_w = r.width() * self._value
        if fill_w > 0:
            p.fillRect(QRectF(r.left(), r.top(), fill_w, r.height()), accent)
        p.setClipping(False)
        p.setPen(QPen(QColor(*theme.LIGHT_RGB, 40), 1))
        p.drawPath(path)
        # ручка: золоте коло на позиції
        knob_r = h / 2.0 - 2.0
        cx = r.left() + fill_w
        cx = min(max(cx, r.left() + knob_r), r.right() - knob_r)
        p.setPen(Qt.NoPen)
        p.setBrush(accent)
        p.drawEllipse(QRectF(cx - knob_r, top + track_h / 2.0 - knob_r,
                             knob_r * 2, knob_r * 2))


class _IconButton(QToolButton):
    """Дрібна іконкова кнопка (play/pause, mute) у тон канону: прозорий фон,
    focus-рамка з QSS. Активується Enter/Space (як TipToolButton).

    ``icon_w`` — окрема ширина ІКОНКИ (не кнопки). qtawesome вписує гліф у
    квадрат за ВИСОТОЮ, тож ширші за квадрат гліфи (fa6s.volume-high, viewBox
    640×512 — співвідношення 1,25) різались праворуч у квадратному боксі 16×16
    (звук-хвилі зникали). Прямокутний бокс за пропорцією гліфа рендерить його
    повністю. Для решти (play/pause) лишається квадрат."""

    def __init__(self, icon_name: str, tip: str = "", *, size=18, icon_w=None,
                 parent=None):
        super().__init__(parent)
        self._icon_name = None
        self._icon_w = int(icon_w or size)
        self._icon_h = int(size)
        self.set_icon_name(icon_name)
        self.setIconSize(QSize(self._icon_w, self._icon_h))
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFixedSize(max(self._icon_w, self._icon_h) + 14, self._icon_h + 12)
        if tip:
            self.setToolTip(tip)
            self.setAccessibleName(tip)
        self._apply_focus_style()
        theme.register_restyle(self._restyle)   # нічний режим

    def _apply_focus_style(self) -> None:
        self.setStyleSheet(
            "QToolButton { border: 2px solid transparent; background: transparent; }"
            f"QToolButton:focus {{ border-color: {theme.GOLD}; border-radius: 5px; }}")

    def _restyle(self) -> None:
        self._apply_focus_style()
        name, self._icon_name = self._icon_name, None
        self.set_icon_name(name)                # перебудувати іконку в новій гамі

    def set_icon_name(self, icon_name: str) -> None:
        if icon_name != self._icon_name:
            self._icon_name = icon_name
            self.setIcon(qta.icon(icon_name, color=theme.TEXT_STRONG,
                                  color_active=theme.GOLD_EYEBROW))

    def keyPressEvent(self, event):
        if (self.isEnabled()
                and event.key() in (Qt.Key_Enter, Qt.Key_Return, Qt.Key_Space)):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)


class InlinePlayer(QWidget):
    """Вбудований плеєр одного аудіофайлу: play/pause, перемотка, час/тривалість,
    швидкість 1×/1,25×/1,5×/2×/0,75× (циклічна кнопка, стан на сесію), гучність.
    Вставляється у картку наради чи запису — НЕ
    окреме вікно. `set_source(path)` міняє файл (напр. доріжки mic/sys)."""

    position_changed = Signal(int)  # для синхронізації waveform-редактора

    def __init__(self, path=None, parent=None):
        super().__init__(parent)
        self._path = str(path) if path else None

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        if not _HAVE_QTMM:
            # фолбек: без QtMultimedia лишаємо одну кнопку системного плеєра
            self._player = self._audio = None
            open_btn = GlassButton(tr("player_open_external"))
            open_btn.clicked.connect(self._open_external)
            row.addWidget(open_btn)
            row.addStretch()
            return

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._audio.setVolume(0.9)
        self._player.setAudioOutput(self._audio)
        self._speed_idx = _session_speed_idx   # відновити швидкість сесії
        self._player.setPlaybackRate(_SPEEDS[self._speed_idx])
        self._loaded = False           # джерело завантажене (ледаче, на перший play)
        self._pending_position = None   # cold-source seek чекає LoadedMedia
        _LIVE_PLAYERS.add(self)

        self._play_btn = _IconButton("fa6s.play", tr("player_play"))
        self._play_btn.clicked.connect(self._toggle)
        row.addWidget(self._play_btn)

        self._time = QLabel("0:00 / 0:00")
        self._time.setProperty("kbd", True)
        row.addWidget(self._time)

        self._seek = _Slider()
        self._seek.setAccessibleName(tr("player_position"))
        self._seek.moved.connect(self._on_seek)
        row.addWidget(self._seek, stretch=1)

        self._speed_btn = GlassButton(self._speed_label())
        self._speed_btn.setToolTip(tr("player_speed"))
        self._speed_btn.clicked.connect(self._cycle_speed)
        # Стала ширина під найширший підпис (щоб ряд не «стрибав» при циклі), але
        # рахована від fontMetrics — фікс 62px різав «1,25×»/«0,75×» (~65-70px).
        _sfm = self._speed_btn.fontMetrics()
        _labels = [(f"{s:.0f}×" if s == int(s) else f"{s:g}×").replace(".", ",")
                   for s in _SPEEDS]
        self._speed_btn.setFixedWidth(max(_sfm.horizontalAdvance(t) for t in _labels) + 24)
        row.addWidget(self._speed_btn)

        self._vol_btn = _IconButton("fa6s.volume-high", tr("player_volume"),
                                    size=16, icon_w=20)
        self._vol_btn.clicked.connect(self._toggle_mute)
        row.addWidget(self._vol_btn)
        self._vol = _Slider(eyebrow=True)
        self._vol.setAccessibleName(tr("player_volume"))
        self._vol.setFixedWidth(72)
        self._vol.set_fraction(0.9)
        self._vol.moved.connect(self._on_volume)
        self._muted_prev = 0.9
        row.addWidget(self._vol)

        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_playback_state)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_error)
        self._player.seekableChanged.connect(self._apply_pending_position)
        # джерело НЕ вантажимо тут: ffmpeg-бекенд тримає файл відкритим —
        # ледаче завантаження на перший play (див. шапку модуля)

    # ---- публічний API ----
    def set_source(self, path) -> None:
        """Перемкнути файл (напр. mic ⇄ sys). Звільняє старе джерело; нове
        завантажиться ледаче на перший play. Скидає позицію."""
        self._path = str(path) if path else None
        if self._player is None:
            return
        self._release_source()
        self._play_btn.setEnabled(True)          # новий файл — новий шанс
        self._seek.set_fraction(0.0)
        self._time.setText("0:00 / 0:00")

    def play_from(self, seconds: float, until: float | None = None) -> None:
        """Почати з таймкоду; ``until`` зупиняє короткий фрагмент."""
        if self._player is None:
            self._open_external()
            return
        self._fragment_end = int(until * 1000) if until is not None else None
        self._pending_position = max(0, int(seconds * 1000))
        for other in list(_LIVE_PLAYERS):
            if other is not self:
                try:
                    other.stop()
                except RuntimeError:
                    pass
        self._ensure_source()
        self._apply_pending_position()

    def _apply_pending_position(self, *_args) -> None:
        if self._pending_position is None or not self._player.isSeekable():
            return
        position, self._pending_position = self._pending_position, None
        self._player.setPosition(position)
        self._player.play()

    def seek_ms(self, ms: int) -> None:
        """Перемотати ззовні (waveform клік)."""
        if self._player is None:
            return
        self._ensure_source()
        self._player.setPosition(max(0, int(ms)))

    def stop(self) -> None:
        """Зупинити відтворення І звільнити файл (виклик при ховані сторінки,
        оновленні стрічки та ПЕРЕД будь-яким видаленням файлів)."""
        if self._player is not None:
            self._release_source()
            self._pending_position = None
            self._fragment_end = None

    def _release_source(self) -> None:
        """Стоп + відпустити файловий хендл бекенда (setSource(QUrl()))."""
        self._player.stop()
        if self._loaded:
            self._player.setSource(QUrl())
            self._loaded = False

    def _ensure_source(self) -> None:
        """Ледаче завантаження джерела (перший play або play після stop)."""
        if not self._loaded and self._path:
            self._player.setSource(QUrl.fromLocalFile(self._path))
            self._loaded = True

    # ---- керування ----
    def _toggle(self):
        if self._player is None:
            return
        self._pending_position = None
        self._fragment_end = None
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
        else:
            # відновлення саме після паузи (не свіжий play/стоп) — відмотати назад,
            # щоб підхопити контекст; на паузі позиція й джерело живі.
            resuming = self._player.playbackState() == QMediaPlayer.PausedState
            # один плеєр на сторінці: інші живі — стоп (і звільнення файлів)
            for other in list(_LIVE_PLAYERS):
                if other is not self:
                    try:
                        other.stop()
                    except RuntimeError:
                        pass             # C++-обʼєкт уже знищено
            self._ensure_source()
            if resuming and _resume_backstep_ms > 0:
                self._player.setPosition(
                    resume_position_ms(self._player.position(), _resume_backstep_ms))
            self._player.play()

    def _on_seek(self, frac: float):
        dur = self._player.duration()
        if dur > 0:
            self._player.setPosition(int(frac * dur))

    def _cycle_speed(self):
        global _session_speed_idx
        self._speed_idx = (self._speed_idx + 1) % len(_SPEEDS)
        _session_speed_idx = self._speed_idx     # запам'ятати на сесію
        # УВАГА (тон): бекенд ffmpeg Qt у цій збірці PySide6 має swresample, але
        # НЕ має avfilter (atempo) чи soundtouch — швидкість реалізується
        # ресемплом, тож на 1,5×/2× тон, найпевніше, підвищується («бурундук»).
        # Збереження тону = окремий time-stretch (QAudioDecoder+SoundTouch) —
        # тут НЕ реалізовано (див. звіт). Потребує живої перевірки на слух.
        self._player.setPlaybackRate(_SPEEDS[self._speed_idx])
        self._speed_btn.setText(self._speed_label())

    def _speed_label(self) -> str:
        s = _SPEEDS[self._speed_idx]
        return (f"{s:.0f}×" if s == int(s) else f"{s:g}×").replace(".", ",")

    def _on_volume(self, frac: float):
        self._audio.setVolume(frac)
        self._vol_btn.set_icon_name(
            "fa6s.volume-xmark" if frac <= 0.001 else "fa6s.volume-high")

    def _toggle_mute(self):
        if self._audio.volume() > 0.001:
            self._muted_prev = self._audio.volume()
            self._audio.setVolume(0.0)
            self._vol.set_fraction(0.0)
            self._vol_btn.set_icon_name("fa6s.volume-xmark")
        else:
            self._audio.setVolume(self._muted_prev)
            self._vol.set_fraction(self._muted_prev)
            self._vol_btn.set_icon_name("fa6s.volume-high")

    # ---- сигнали плеєра ----
    def _on_position(self, pos: int):
        if getattr(self, "_fragment_end", None) is not None and pos >= self._fragment_end:
            self._fragment_end = None
            self._player.pause()
        self.position_changed.emit(int(pos))
        dur = self._player.duration()
        if dur > 0:
            self._seek.set_fraction(pos / dur)
        self._time.setText(f"{fmt_time(pos)} / {fmt_time(dur)}")

    def _on_duration(self, dur: int):
        self._time.setText(f"{fmt_time(self._player.position())} / {fmt_time(dur)}")

    def _on_playback_state(self, state):
        playing = state == QMediaPlayer.PlayingState
        self._play_btn.set_icon_name("fa6s.pause" if playing else "fa6s.play")
        self._play_btn.setToolTip(tr("player_pause") if playing else tr("player_play"))

    def _on_media_status(self, status):
        # кінець файлу → зупинити, ЗВІЛЬНИТИ файл і повернути на початок
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self._apply_pending_position()
        # (наступний play завантажить джерело заново — ледаче)
        if status == QMediaPlayer.EndOfMedia:
            self._release_source()
            self._seek.set_fraction(0.0)

    def _on_error(self, _error, error_string):
        """Файл зник/битий/кодек: чесна відмова замість тихого нічого —
        кнопка вимикається, тост пояснює. Новий set_source вмикає знову."""
        self._release_source()
        self._play_btn.setEnabled(False)
        self._play_btn.setToolTip(tr("player_error"))
        motion.toast(self.window() or self, tr("player_error"))

    def _open_external(self):
        if self._path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._path))
