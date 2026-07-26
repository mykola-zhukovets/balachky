"""Синхронне відтворення кількох звукових доріжок наради + панель мікшера.

feature/player-tracks. Нарада пише мікрофон і системний звук ОКРЕМИМИ доріжками
(історія clean-mix: mic.wav / sys.wav). Раніше вбудований плеєр грав ЛИШЕ одну
доріжку з перемикачем-сегментом; тут — усі доріжки разом, синхронно, з панеллю:
рядок на доріжку (чекбокс-увімкнути, слайдер гучності, кнопка «Соло»).

Модель синхронізації (рекомендація оркестратора, звірена кодом): N незалежних
QMediaPlayer+QAudioOutput. Один — «майстер»: тримає позицію/стан, транспорт
(play/pause/перемотка/швидкість) віддає слухачам-«відомим». Кожні 2 с таймер
ресинку звіряє позицію кожного відомого з майстром і за дрейфу понад поріг
робить setPosition. Кожна доріжка — власний QAudioOutput, тож гучність/mute/соло
незалежні. Той самий рушій обслуговує і відеоплеєр: відео-майстер (екран) +
аудіодоріжки наради відомими.

ПОРІГ РЕСИНКУ (живий тест на 440/880 Гц, АУДІО-майстер): реальний дрейф
синхронного відтворення ≈ 0 мс, АЛЕ position() двох незалежних плеєрів звітує з
квантом ~кадр буфера — навколо перемотки миттєвий репорт розходиться до ~86 мс,
хоча звук синхронний. Порогом 60 мс (первісна рекомендація) ресинк спрацьовував
би на цьому шумі репорту — зайві setPosition під час синхронного відтворення
(ризик клацань на відомому). Тому поріг = 120 мс: вище шуму репорту, тож ресинк
втручається лише за СПРАВЖНЬОГО розсинхрону (відомий підвис/відстав).

СТАЛИЙ ЗСУВ ВІДЕО-МАЙСТРА (живий тест, відео 320×240 30fps і 15fps-allkey): коли
майстер — ВІДЕОплеєр (кнопка «Дивитися відео»), а не аудіо, position() відео-
конвеєра СИСТЕМАТИЧНО звітує на 120-250 мс ПОПЕРЕДУ аудіодоріжок — це не реальний
розсинхрон звуку, а властивість власне репорту position() відео проти аудіо
(відтворюється й на розрідженому, і на суцільно-ключовому GOP, тобто не артефакт
кодування). Абсолютний поріг 120 мс той сталий зсув реГУЛЯРНО перевищує, тож
сліпий ресинк смикав би аудіо на майстрову позицію ЩОДВА такти — чутні клацання.
Тому синхронізуємо не за АБСОЛЮТНОЮ різницею, а за дрейфом ПОНАД вивчений сталий
зсув: FollowerGroup оцінює зсув майстер↔відомий у сталому русі (аудіо ≈0, відео
~180) і ресинкить лише коли відомий відхиляється від ОЧІКУВАНОЇ позиції
(майстер − зсув) понад поріг. Так аудіо-шлях лишається як був (зсув ≈0), а відео-
шлях більше не смикається на сталому конвеєрному зсуві.

Панель показується ЛИШЕ коли керованих доріжок >= 2 (одна доріжка чи файл-мікс —
поведінка як раніше, тонкий ряд InlinePlayer). Файлові хендли звільняються так
само, як в InlinePlayer (ffmpeg-бекенд тримає WAV відкритим — інакше видалення
наради падало б із WinError 32): джерело ледаче, stop() відпускає його.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtWidgets import (
    QCheckBox, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from . import motion
from .glass import GlassButton
from .i18n import tr
from .player import (
    _IconButton, _Slider, _LIVE_PLAYERS, _SPEEDS, fmt_time,
    resume_position_ms,
)

try:                                   # QtMultimedia є у колесі PySide6 (перевірено)
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    _HAVE_QTMM = True
except Exception:                      # pragma: no cover — лише коли модуль відсутній
    QAudioOutput = QMediaPlayer = None
    _HAVE_QTMM = False


# ─────────────────────── чисті хелпери (тестуються без Qt) ───────────────────────
# 120 мс, а не 60: живий тест показав, що position() двох плеєрів звітує з
# квантом буфера (~86 мс шуму навколо перемотки) при реально синхронному звуку;
# нижчий поріг спричиняв би зайві коригувальні setPosition (див. докстрінг модуля).
RESYNC_TOLERANCE_MS = 120     # дрейф відомого проти ОЧІКУВАНОЇ позиції (майстер−зсув)
RESYNC_INTERVAL_MS = 2000     # період звіряння позицій
# Розрив |майстер−відомий| понад цю межу на першому такті — це НЕ сталий зсув
# конвеєра (аудіо ≈0, відео ~180 мс), а справжня різниця позицій (майстер стартував
# не з нуля: resume/перемотка, або відомий підвис) — його жорстко вирівнюємо, а не
# приймаємо за baseline. Вище виміряного відео-зсуву (~250 мс) із запасом.
OFFSET_SEED_MAX_MS = 800
_OFFSET_BLEND = 0.34          # EMA-згладжування оцінки зсуву (сходиться за ~3 такти)


def resync_needed(master_ms: int, follower_ms: int, offset_ms: float = 0,
                  tol_ms: int = RESYNC_TOLERANCE_MS) -> bool:
    """Чи відомий відхилився від ОЧІКУВАНОЇ позиції (майстер − сталий зсув) понад
    поріг. ``offset_ms`` — вивчений сталий зсув репорту майстра проти відомого
    (аудіо-майстер ≈0 → зводиться до простого |майстер−відомий|; відео-майстер
    ~180 мс, бо його position() звітує попереду аудіо — коригуємо лише дрейф понад
    цей зсув, інакше ресинк смикав би звук на кожному такті; див. докстрінг модуля)."""
    target = int(master_ms) - int(round(offset_ms))
    return abs(target - int(follower_ms)) > int(tol_ms)


def blend_offset(prev_offset: float, raw_diff: float,
                 alpha: float = _OFFSET_BLEND) -> float:
    """Плавно (EMA) підтягуємо оцінку сталого зсуву до свіжого виміру raw_diff
    (= майстер−відомий), поки дрейф у межах порога. Стабільний зсув конвеєра так
    тримається біля середнього, не даючи шуму репорту зривати ресинк."""
    return (1.0 - float(alpha)) * float(prev_offset) + float(alpha) * float(raw_diff)


def plan_resync(master_ms: int, follower_ms: int, offset_ms,
                tol_ms: int = RESYNC_TOLERANCE_MS,
                seed_max_ms: int = OFFSET_SEED_MAX_MS,
                *, audio_master: bool = False):
    """Рішення ресинку для ОДНОГО відомого (чиста логіка, тестується без Qt).

    Повертає ``(new_offset, seek_to)``: ``new_offset`` — оновлена оцінка сталого
    зсуву (може лишитись None, поки не вивчено), ``seek_to`` — позиція setPosition
    відомого або None, якщо чіпати не треба.

    ``audio_master=True`` — майстер АУДІО (немає відео): конвеєр НЕ дає сталого
    зсуву репорту, тож базлайн = 0. Будь-який розбіг понад поріг — справжня
    десинхронізація → жорстко вирівнюємо на майстра; офсет лишається 0 (без
    seed-зсуву й EMA, які тут лише маскували б розсинхрон звуку).

    ``offset_ms is None`` (відео-майстер) — baseline ще не вивчено:
      • невеликий розрив (|raw| ≤ seed_max) → приймаємо raw за сталий зсув, без
        перемотки (природний зсув конвеєра: аудіо ≈0, відео ~180);
      • великий розрив → майстер стартував не з нуля / відомий підвис: жорстко
        вирівнюємо на майстра, зсув лишаємо невивченим (довчиться, коли зійдуться).
    Інакше (зсув вивчено): якщо дрейф понад зсув перевищує поріг — перемотати на
    (майстер − зсув), зберігши природний зсув; інакше плавно уточнити оцінку."""
    raw = int(master_ms) - int(follower_ms)
    if audio_master:
        if abs(raw) > tol_ms:
            return 0.0, int(master_ms)
        return 0.0, None
    if offset_ms is None:
        if abs(raw) <= seed_max_ms:
            return float(raw), None
        return None, int(master_ms)
    if resync_needed(master_ms, follower_ms, offset_ms, tol_ms):
        return offset_ms, int(round(master_ms - offset_ms))
    return blend_offset(offset_ms, raw), None


def should_show_track_panel(track_count: int) -> bool:
    """Панель доріжок має сенс лише коли керованих доріжок >= 2."""
    return int(track_count) >= 2


def effective_volume(user_volume: float, enabled: bool,
                     solo: bool, any_solo: bool) -> float:
    """Підсумкова гучність доріжки з урахуванням увімкнення й соло.

    Якщо десь активне соло — чутні ЛИШЕ соло-доріжки; інакше — усі ввімкнені.
    Вимкнена доріжка (знятий чекбокс) завжди німа."""
    audible = enabled and (solo if any_solo else True)
    return float(user_volume) if audible else 0.0


# ─────────────────────── одна відома доріжка ───────────────────────
class _FollowerTrack:
    """Відомий: QMediaPlayer+QAudioOutput, що дзеркалить транспорт майстра.

    Джерело ледаче (перший play), звільняється у release() — та сама дисципліна
    файлових хендлів, що в InlinePlayer. Перемотка на «холодному» (ще не
    seekable) джерелі відкладається до seekableChanged/LoadedMedia."""

    def __init__(self, path, parent):
        self.path = str(path)
        self.player = QMediaPlayer(parent)
        self.audio = QAudioOutput(parent)
        self.audio.setVolume(0.9)
        self.player.setAudioOutput(self.audio)
        self._loaded = False
        self._pending_ms = None
        self.sync_offset_ms = None      # вивчений сталий зсув майстер↔цей відомий
        self.player.seekableChanged.connect(self._apply_pending)
        self.player.mediaStatusChanged.connect(self._on_status)

    def ensure_source(self) -> None:
        if not self._loaded and self.path:
            self.player.setSource(QUrl.fromLocalFile(self.path))
            self._loaded = True

    def release(self) -> None:
        self.player.stop()
        if self._loaded:
            self.player.setSource(QUrl())
            self._loaded = False
        self._pending_ms = None

    def set_rate(self, rate: float) -> None:
        self.player.setPlaybackRate(float(rate))

    def play(self) -> None:
        self.ensure_source()
        self.player.play()

    def pause(self) -> None:
        self.player.pause()

    def seek(self, ms: int) -> None:
        self.ensure_source()
        ms = max(0, int(ms))
        if self.player.isSeekable():
            self.player.setPosition(ms)
        else:
            self._pending_ms = ms

    def _apply_pending(self, *_args) -> None:
        if self._pending_ms is not None and self.player.isSeekable():
            ms, self._pending_ms = self._pending_ms, None
            self.player.setPosition(ms)

    def _on_status(self, status) -> None:
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self._apply_pending()


# ─────────────────────── група відомих під майстром ───────────────────────
class FollowerGroup:
    """Набір відомих, синхронних до майстра, з ресинком дрейфу кожні 2 с.

    Слухає ``playbackStateChanged`` майстра: Playing → play усіх + старт таймера;
    Paused → pause; Stopped → release. Перемотку/швидкість транслює власник плеєра
    напряму (broadcast_seek/broadcast_rate). Не QObject: живе, поки живий майстер
    (з'єднання тримає посилання)."""

    def __init__(self, master_player, parent, *, audio_master: bool = False):
        self._master = master_player
        self._parent = parent
        # Майстер — АУДІО (немає відео): ресинк вирівнює будь-який дрейф на 0,
        # без seed-зсуву конвеєра (той стосується лише відео-майстра).
        self._audio_master = bool(audio_master)
        self._followers: list[_FollowerTrack] = []
        self._resync = QTimer(parent)
        self._resync.setInterval(RESYNC_INTERVAL_MS)
        self._resync.timeout.connect(self._resync_tick)
        master_player.playbackStateChanged.connect(self._on_master_state)

    @property
    def followers(self) -> list:
        return self._followers

    def add(self, path) -> _FollowerTrack:
        follower = _FollowerTrack(path, self._parent)
        follower.set_rate(self._master.playbackRate())
        self._followers.append(follower)
        return follower

    def broadcast_seek(self, ms: int) -> None:
        for follower in self._followers:
            follower.seek(ms)
            follower.sync_offset_ms = None      # після перемотки зсув переоцінити наново

    def broadcast_rate(self, rate: float) -> None:
        for follower in self._followers:
            follower.set_rate(rate)
            # Зсув, вивчений на попередній швидкості, стає хибним на новій
            # (репорт position() масштабується) → скидаємо, хай перевчиться.
            follower.sync_offset_ms = None

    def stop(self) -> None:
        self._resync.stop()
        for follower in self._followers:
            follower.release()

    def _on_master_state(self, state) -> None:
        try:
            if state == QMediaPlayer.PlayingState:
                for follower in self._followers:
                    follower.play()
                    follower.sync_offset_ms = None   # зсув учиться заново на цей старт
                self._resync.start()
                # cold-seek: майстер стартує не з нуля — швидко підтягуємо відомих
                QTimer.singleShot(150, self._cold_align)
            elif state == QMediaPlayer.PausedState:
                self._resync.stop()
                for follower in self._followers:
                    follower.pause()
            else:                                   # StoppedState
                self.stop()
        except RuntimeError:                        # майстра/віджет уже знищено
            pass

    def _cold_align(self) -> None:
        """Швидке вирівнювання холодного старту з НЕНУЛЬОВОЇ позиції (resume/сік):
        підтягуємо лише ВЕЛИКИЙ розрив. Природний сталий зсув конвеєра (аудіо ≈0,
        відео ~180 мс) НЕ чіпаємо тут — його вчить _resync_tick, інакше seed на
        150-й мс (зсув ще не розвинувся) недооцінив би його й спричинив зайвий
        ресинк пізніше."""
        try:
            master_ms = self._master.position()
        except RuntimeError:
            return
        for follower in self._followers:
            try:
                if abs(master_ms - follower.player.position()) > OFFSET_SEED_MAX_MS:
                    follower.seek(master_ms)
            except RuntimeError:
                continue

    def _resync_tick(self) -> None:
        try:
            master_ms = self._master.position()
        except RuntimeError:
            return
        for follower in self._followers:
            try:
                follower_ms = follower.player.position()
            except RuntimeError:
                continue
            new_offset, seek_to = plan_resync(
                master_ms, follower_ms, follower.sync_offset_ms,
                audio_master=self._audio_master)
            follower.sync_offset_ms = new_offset
            if seek_to is not None:
                try:
                    follower.seek(seek_to)
                except RuntimeError:
                    continue


# ─────────────────────── дані одного рядка панелі ───────────────────────
class TrackChannel:
    """Керований звуковий вихід (рядок панелі): підпис + його QAudioOutput.

    Для аудіоплеєра — доріжка mic/sys; для відео — «Звук екрана» (вихід відео-
    майстра) плюс аудіодоріжки наради. ``hidden=True`` будує рядок, але ховає
    його, поки не з'ясується, що канал реальний (напр. екран без аудіодоріжки —
    німий mp4 наради; рядок «Звук екрана» показуємо лише коли відео дійсно має
    звук, інакше це мертвий чекбокс)."""

    def __init__(self, key: str, label: str, audio_output, *, base_volume=0.9,
                 hidden=False):
        self.key = key
        self.label = label
        self.audio = audio_output
        self.user_volume = float(base_volume)
        self.enabled = True
        self.solo = False
        self.hidden = bool(hidden)


# ─────────────────────── панель мікшера ───────────────────────
class TrackMixerPanel(QWidget):
    """Панель доріжок: рядок на кожен канал — чекбокс-увімкнути з підписом,
    слайдер гучності, кнопка «Соло». Соло глушить решту; знятий чекбокс — німо.
    Обчислення гучності — чистий effective_volume, тож логіка тестується без Qt."""

    def __init__(self, channels, parent=None):
        super().__init__(parent)
        self.channels = list(channels)
        self.setAccessibleName(tr("player_volume"))
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 2)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        self._rows = {}                    # key -> (checkbox, slider, solo-кнопка)
        for row, ch in enumerate(self.channels):
            chk = QCheckBox(ch.label)
            chk.setChecked(True)
            chk.setAccessibleName(ch.label)
            chk.setCursor(Qt.PointingHandCursor)
            chk.toggled.connect(lambda on, c=ch: self._set_enabled(c, on))
            grid.addWidget(chk, row, 0)

            vol = _Slider(eyebrow=True)
            vol.setMinimumWidth(90)
            vol.set_fraction(ch.user_volume)
            vol.setAccessibleName(f'{tr("player_volume")}: {ch.label}')
            vol.moved.connect(lambda frac, c=ch: self._set_volume(c, frac))
            grid.addWidget(vol, row, 1)

            solo = GlassButton(tr("player_solo"))
            solo.setCheckable(True)
            solo.setAccessibleName(f'{tr("player_solo")}: {ch.label}')
            _fm = solo.fontMetrics()
            solo.setFixedWidth(_fm.horizontalAdvance(tr("player_solo")) + 28)
            solo.toggled.connect(lambda on, c=ch: self._set_solo(c, on))
            grid.addWidget(solo, row, 2)

            self._rows[ch.key] = (chk, vol, solo)
            if ch.hidden:                  # напр. «Звук екрана» до підтвердження звуку
                for w in (chk, vol, solo):
                    w.setVisible(False)
        self._apply()

    def reveal_channel(self, key: str) -> None:
        """Показати раніше прихований рядок (відео виявило власну аудіодоріжку)."""
        widgets = self._rows.get(key)
        if not widgets:
            return
        for ch in self.channels:
            if ch.key == key:
                ch.hidden = False
        for w in widgets:
            w.setVisible(True)

    def _set_enabled(self, ch, on: bool) -> None:
        ch.enabled = bool(on)
        self._apply()

    def _set_volume(self, ch, frac: float) -> None:
        ch.user_volume = float(frac)
        self._apply()

    def _set_solo(self, ch, on: bool) -> None:
        ch.solo = bool(on)
        self._apply()

    def _apply(self) -> None:
        any_solo = any(c.solo for c in self.channels)
        for c in self.channels:
            c.audio.setVolume(
                effective_volume(c.user_volume, c.enabled, c.solo, any_solo))


# ─────────────────────── багатодоріжковий аудіоплеєр ───────────────────────
class MultiTrackPlayer(QWidget):
    """Синхронний плеєр кількох доріжок наради з панеллю мікшера.

    ``track_specs`` — впорядкований список (key, label, path); перша доріжка =
    майстер. Сумісний із рештою картки наради: віддає ``_player`` (майстер-
    QMediaPlayer), ``seek_ms``, ``play_from`` і сигнал ``position_changed``, тож
    хвиля/транскрипт/навігація репліками працюють без змін."""

    position_changed = Signal(int)     # для waveform-редактора й підсвітки транскрипту

    def __init__(self, track_specs, parent=None):
        super().__init__(parent)
        specs = list(track_specs)
        self._path0 = str(specs[0][2]) if specs else None
        self._fragment_end = None
        self._pending_position = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self._master = QMediaPlayer(self)
        self._master_audio = QAudioOutput(self)
        self._master_audio.setVolume(0.9)
        self._master.setAudioOutput(self._master_audio)
        self._player = self._master        # сумісність: картка читає .position()
        self._loaded = False
        import fronts.desktop.player as _p
        self._speed_idx = _p._session_speed_idx    # відновити швидкість сесії
        self._master.setPlaybackRate(_SPEEDS[self._speed_idx])
        _LIVE_PLAYERS.add(self)

        # відомі доріжки (усі, крім майстра) + панель мікшера. Майстер —
        # АУДІО (немає відео-конвеєра), тож ресинк вирівнює будь-який дрейф.
        self._group = FollowerGroup(self._master, self, audio_master=True)
        channels = [TrackChannel(specs[0][0], specs[0][1], self._master_audio)]
        for key, label, path in specs[1:]:
            follower = self._group.add(path)
            channels.append(TrackChannel(key, label, follower.audio))
        self._panel = TrackMixerPanel(channels, self)
        root.addWidget(self._panel)

        # транспорт (без глобальної гучності — вона в панелі по доріжках)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        self._play_btn = _IconButton("fa6s.play", tr("player_play"))
        self._play_btn.clicked.connect(self._toggle)
        row.addWidget(self._play_btn)

        self._time = QLabel("0:00 / 0:00")
        self._time.setProperty("kbd", True)
        row.addWidget(self._time)

        self._seek = _Slider()
        self._seek.setAccessibleName(tr("player_play"))
        self._seek.moved.connect(self._on_seek)
        row.addWidget(self._seek, stretch=1)

        self._speed_btn = GlassButton(self._speed_label())
        self._speed_btn.setToolTip(tr("player_speed"))
        self._speed_btn.setAccessibleName(tr("player_speed"))
        self._speed_btn.clicked.connect(self._cycle_speed)
        _sfm = self._speed_btn.fontMetrics()
        _labels = [(f"{s:.0f}×" if s == int(s) else f"{s:g}×").replace(".", ",")
                   for s in _SPEEDS]
        self._speed_btn.setFixedWidth(
            max(_sfm.horizontalAdvance(t) for t in _labels) + 24)
        row.addWidget(self._speed_btn)
        root.addLayout(row)

        self._master.positionChanged.connect(self._on_position)
        self._master.durationChanged.connect(self._on_duration)
        self._master.playbackStateChanged.connect(self._on_playback_state)
        self._master.mediaStatusChanged.connect(self._on_media_status)
        self._master.errorOccurred.connect(self._on_error)
        self._master.seekableChanged.connect(self._apply_pending)

    # ---- джерело майстра (ледаче, звільняється при stop) ----
    def _ensure_source(self) -> None:
        if not self._loaded and self._path0:
            self._master.setSource(QUrl.fromLocalFile(self._path0))
            self._loaded = True

    def _release_source(self) -> None:
        self._master.stop()               # → StoppedState → group.stop()
        if self._loaded:
            self._master.setSource(QUrl())
            self._loaded = False

    # ---- публічний API (сумісність із карткою наради) ----
    def stop(self) -> None:
        """Зупинити всі доріжки й відпустити файли (перед видаленням/ховані)."""
        self._release_source()
        self._group.stop()
        self._pending_position = None
        self._fragment_end = None

    def seek_ms(self, ms: int) -> None:
        """Перемотати всі доріжки (клік по хвилі/таймкоду транскрипту)."""
        self._ensure_source()
        ms = max(0, int(ms))
        if self._master.isSeekable():
            self._master.setPosition(ms)
        else:
            self._pending_position = ms
        self._group.broadcast_seek(ms)

    def play_from(self, seconds: float, until: float | None = None) -> None:
        """Почати з таймкоду (розділ/репліка); ``until`` — короткий фрагмент."""
        self._fragment_end = int(until * 1000) if until is not None else None
        self._stop_other_players()
        self._ensure_source()
        self._pending_position = max(0, int(seconds * 1000))
        self._apply_pending()

    # ---- керування ----
    def _stop_other_players(self) -> None:
        for other in list(_LIVE_PLAYERS):
            if other is not self:
                try:
                    other.stop()
                except RuntimeError:
                    pass

    def _toggle(self) -> None:
        self._pending_position = None
        self._fragment_end = None
        if self._master.playbackState() == QMediaPlayer.PlayingState:
            self._master.pause()
        else:
            resuming = self._master.playbackState() == QMediaPlayer.PausedState
            self._stop_other_players()
            self._ensure_source()
            from .player import _resume_backstep_ms
            if resuming and _resume_backstep_ms > 0:
                back = resume_position_ms(self._master.position(),
                                          _resume_backstep_ms)
                self._master.setPosition(back)
                self._group.broadcast_seek(back)
            self._master.play()

    def _on_seek(self, frac: float) -> None:
        dur = self._master.duration()
        if dur > 0:
            ms = int(frac * dur)
            self._master.setPosition(ms)
            self._group.broadcast_seek(ms)

    def _apply_pending(self, *_args) -> None:
        if self._pending_position is None or not self._master.isSeekable():
            return
        position, self._pending_position = self._pending_position, None
        self._master.setPosition(position)
        self._group.broadcast_seek(position)
        self._master.play()

    def _cycle_speed(self) -> None:
        import fronts.desktop.player as _p
        self._speed_idx = (self._speed_idx + 1) % len(_SPEEDS)
        _p._session_speed_idx = self._speed_idx        # запам'ятати на сесію
        rate = _SPEEDS[self._speed_idx]
        self._master.setPlaybackRate(rate)
        self._group.broadcast_rate(rate)
        self._speed_btn.setText(self._speed_label())

    def _speed_label(self) -> str:
        s = _SPEEDS[self._speed_idx]
        return (f"{s:.0f}×" if s == int(s) else f"{s:g}×").replace(".", ",")

    # ---- сигнали майстра ----
    def _on_position(self, pos: int) -> None:
        if self._fragment_end is not None and pos >= self._fragment_end:
            self._fragment_end = None
            self._master.pause()
        self.position_changed.emit(int(pos))
        dur = self._master.duration()
        if dur > 0:
            self._seek.set_fraction(pos / dur)
        self._time.setText(f"{fmt_time(pos)} / {fmt_time(dur)}")

    def _on_duration(self, dur: int) -> None:
        self._time.setText(f"{fmt_time(self._master.position())} / {fmt_time(dur)}")

    def _on_playback_state(self, state) -> None:
        playing = state == QMediaPlayer.PlayingState
        self._play_btn.set_icon_name("fa6s.pause" if playing else "fa6s.play")
        self._play_btn.setToolTip(tr("player_pause") if playing else tr("player_play"))

    def _on_media_status(self, status) -> None:
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self._apply_pending()
        if status == QMediaPlayer.EndOfMedia:
            self._release_source()
            self._seek.set_fraction(0.0)

    def _on_error(self, _error, _error_string) -> None:
        self._release_source()
        self._group.stop()
        self._play_btn.setEnabled(False)
        self._play_btn.setToolTip(tr("player_error"))
        motion.toast(self.window() or self, tr("player_error"))
