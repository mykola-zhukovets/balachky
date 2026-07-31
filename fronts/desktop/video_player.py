"""Вбудований відеоплеєр «Balachky» — перегляд запису екрана й відео наради.

feature/video-player-mvp. Технологія — Qt Multimedia (QMediaPlayer + QVideoWidget
+ QAudioOutput, FFmpeg-бекенд; уже в колесі PySide6 6.11.1 — нуль нових
залежностей). Швидкість 0,5-2× зі збереженням тону через ``pitchCompensation``
(Qt >= 6.10 — у нас 6.11.1); якщо бекенд не вміє, властивість — тихий no-op.
Якщо модуль QtMultimedia недоступний (екзотична збірка) — фолбек на кнопку
«Відкрити у системному плеєрі», як в InlinePlayer.

Рідний брат аудіо-плеєра (fronts/desktop/player.py): та сама ідіома контролів —
``_Slider`` (золота смуга позиції/гучності), ``_IconButton`` (play/pause, mute),
кнопка-цикл швидкості, тайм-код «поточний / загальний». Різниця — окремий
модальний діалог із полотном ``QVideoWidget``, бо відео потребує великої поверхні,
а не тонкого ряду в картці.

ВАЖЛИВО (Windows/ffmpeg-бекенд): QMediaPlayer тримає файл ВІДКРИТИМ, поки джерело
завантажене. Діалог звільняє його при закритті (stop + setSource(QUrl())),
інакше видалення/переміщення запису падало б із WinError 32 «файл зайнятий».
"""
import logging
import os

from PySide6.QtCore import QEvent, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QSplitter, QVBoxLayout, QWidget,
)

from .crash import anonymize_path
from .glass import GlassButton
from .i18n import tr
from .meeting_transcript_panel import TranscriptPanel
from .player import _IconButton, _Slider, fmt_time
from .player_tracks import FollowerGroup, TrackChannel, TrackMixerPanel

try:                                   # QtMultimedia є у колесі PySide6 (перевірено)
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    _HAVE_QTMM = True
except Exception:                      # pragma: no cover — лише коли модуль відсутній
    QAudioOutput = QMediaPlayer = QVideoWidget = None
    _HAVE_QTMM = False

# Швидкості циклічної кнопки: 1× → 1,25× → 1,5× → 2× → 0,5× → 0,75× → 1×.
# Той самий ідіом, що аудіо-плеєр, плюс 0,5× (повільний перегляд наради).
_VIDEO_SPEEDS = (1.0, 1.25, 1.5, 2.0, 0.5, 0.75)


def fmt_speed(rate: float) -> str:
    """Швидкість → підпис кнопки з українською комою: 1×, 1,25×, 0,5×."""
    return (f"{rate:.0f}×" if rate == int(rate) else f"{rate:g}×").replace(".", ",")


class VideoPlayerDialog(QDialog):
    """Модальний перегляд одного відеофайлу: полотно ``QVideoWidget`` + ряд
    контролів (play/pause, перемотка, тайм-код, швидкість 0,5-2×, гучність).
    ``open_for(parent, path)`` — показати й прибрати за собою (звільнити файл).

    ``audio_tracks`` (список (key, label, path)) вмикає режим наради: відео —
    майстер (екран, зазвичай німий mp4), аудіодоріжки mic/sys грають синхронно
    «відомими» плеєрами з панеллю мікшера (увімк/гучність/соло на доріжку).
    Рядок «Звук екрана» додаємо ЛИШЕ якщо відео реально має власний звук —
    інакше це був би мертвий чекбокс (запис екрана наради без аудіодоріжки)."""

    @staticmethod
    def _default_size():
        """Стартовий розмір діалогу: 85% доступного екрана (не менш ніж 760×540).
        Раніше діалог завжди відкривався фіксованим 760×540 — на записі 2560×1600
        це давало крихітний прямокутник посеред величезної сторінки, і дрібний
        текст (документ/таблиця/презентація) був нечитабельним без перетягування
        рамки вручну. Тепер плеєр одразу займає майже весь екран."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return 760, 540
        avail = screen.availableGeometry()
        w = max(760, int(avail.width() * 0.85))
        h = max(540, int(avail.height() * 0.85))
        return w, h

    def __init__(self, path=None, parent=None, audio_tracks=None,
                 utterances=None, speaker_names=None):
        super().__init__(parent)
        self._path = str(path) if path else None
        self._audio_tracks = list(audio_tracks or [])
        self._group = None
        self._screen_protection_controller = None
        self._panel = None
        self._started_once = False
        self._is_fullscreen = False
        self._status_was_visible = False
        self._transcript_panel = None
        self._transcript_was_visible = False
        self.setWindowTitle(
            os.path.basename(self._path) if self._path else tr("video_title"))
        self.setModal(True)
        self.resize(*self._default_size())

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(12)

        if not _HAVE_QTMM:
            # Фолбек без QtMultimedia — одна кнопка системного плеєра (як InlinePlayer)
            self._player = self._audio = self._video = None
            self._status = self._play_btn = self._fs_btn = None
            note = QLabel(tr("video_unavailable"))
            note.setWordWrap(True)
            note.setProperty("muted", True)
            root.addWidget(note)
            open_row = QHBoxLayout()
            open_row.addStretch()
            open_btn = GlassButton(tr("player_open_external"))
            open_btn.setAccessibleName(tr("player_open_external"))
            open_btn.clicked.connect(self._open_external)
            open_row.addWidget(open_btn)
            root.addLayout(open_row)
            return

        self._video = QVideoWidget(self)
        self._video.setMinimumSize(640, 360)
        self._video.setAccessibleName(tr("video_surface"))
        # Подвійний клац по відео перемикає повний екран (вхід і вихід) —
        # фільтр подій, бо QVideoWidget не має свого mouseDoubleClickEvent-гака.
        self._video.installEventFilter(self)

        # Розшифровка ПОРУЧ із відео (feature/meeting-video-text, етап 2):
        # QSplitter — людина може перетягнути межу; лівий контейнер тримає
        # ВСЕ, що раніше йшло прямо в root (відео/банер/контроли/мікшер), тож
        # без реплік поведінка діалогу лишається БУКВАЛЬНО тою самою (жоден
        # наявний тест видеоплеєра без уттерансів не бачить різниці).
        self._splitter = None
        body = root
        if utterances:
            self._splitter = QSplitter(Qt.Horizontal, self)
            left_widget = QWidget(self._splitter)
            body = QVBoxLayout(left_widget)
            body.setContentsMargins(0, 0, 0, 0)
            body.setSpacing(12)
            self._splitter.addWidget(left_widget)
            self._transcript_panel = TranscriptPanel(
                utterances, speaker_names, self._splitter)
            self._transcript_panel.seekRequested.connect(self._on_transcript_seek)
            self._splitter.addWidget(self._transcript_panel)
            self._splitter.setStretchFactor(0, 7)     # ~70% відео / 30% текст
            self._splitter.setStretchFactor(1, 3)
            self._transcript_was_visible = True
            root.addWidget(self._splitter, stretch=1)
        self._body = body
        body.addWidget(self._video, stretch=1)

        # Банер людської помилки (кодек/битий файл) — прихований, поки все добре.
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setProperty("muted", True)
        self._status.setVisible(False)
        body.addWidget(self._status)

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._audio.setVolume(0.9)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._video)
        # Тон при швидкості: pitchCompensation (Qt >= 6.10) тримає голос природним
        # на 0,5-2×. Якщо бекенд не підтримує — властивість тихо ігнорується.
        try:
            self._player.setPitchCompensation(True)
        except Exception:                          # pragma: no cover
            pass
        self._speed_idx = 0
        self._player.setPlaybackRate(_VIDEO_SPEEDS[self._speed_idx])
        self._muted_prev = 0.9

        controls = QHBoxLayout()
        controls.setSpacing(10)

        self._play_btn = _IconButton("fa6s.play", tr("player_play"))
        self._play_btn.clicked.connect(self._toggle)
        controls.addWidget(self._play_btn)

        self._time = QLabel("0:00 / 0:00")
        self._time.setProperty("kbd", True)
        controls.addWidget(self._time)

        self._seek = _Slider()
        self._seek.setAccessibleName(tr("video_position"))
        self._seek.moved.connect(self._on_seek)
        controls.addWidget(self._seek, stretch=1)

        self._speed_btn = GlassButton(self._speed_label())
        self._speed_btn.setToolTip(tr("player_speed"))
        self._speed_btn.setAccessibleName(tr("player_speed"))
        self._speed_btn.clicked.connect(self._cycle_speed)
        _sfm = self._speed_btn.fontMetrics()
        _widest = max(_sfm.horizontalAdvance(fmt_speed(s)) for s in _VIDEO_SPEEDS)
        self._speed_btn.setFixedWidth(_widest + 24)
        controls.addWidget(self._speed_btn)

        self._vol_btn = _IconButton("fa6s.volume-high", tr("player_volume"),
                                    size=16, icon_w=20)
        self._vol_btn.clicked.connect(self._toggle_mute)
        controls.addWidget(self._vol_btn)

        self._vol = _Slider(eyebrow=True)
        self._vol.setAccessibleName(tr("player_volume"))
        self._vol.setFixedWidth(72)
        self._vol.set_fraction(0.9)
        self._vol.moved.connect(self._on_volume)
        controls.addWidget(self._vol)

        self._fs_btn = _IconButton("fa6s.expand", tr("video_fullscreen_enter"),
                                    size=16, icon_w=18)
        self._fs_btn.clicked.connect(self._toggle_fullscreen)
        controls.addWidget(self._fs_btn)

        if self._transcript_panel is not None:
            # Праву панель розшифровки можна згорнути окремо від повного
            # екрана — QSplitter дає їй 0 ширини, коли вона схована.
            self._transcript_btn = _IconButton(
                "fa6s.message", tr("meeting_transcript_panel_toggle"),
                size=16, icon_w=18)
            self._transcript_btn.clicked.connect(self._toggle_transcript_panel)
            controls.addWidget(self._transcript_btn)
        else:
            self._transcript_btn = None

        body.addLayout(controls)

        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_playback_state)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_error)

        if self._audio_tracks:
            self._build_track_panel(body)

    def _build_track_panel(self, root):
        """Режим наради: відомі аудіоплеєри під відео-майстром + панель мікшера.

        Власні контроли гучності відео ховаємо — увесь звук керується панеллю:
        рядок «Звук екрана» (вихід відео, прихований поки відео без звуку) плюс
        рядок на кожну аудіодоріжку наради."""
        # Гучність екрана тепер у панелі — ховаємо дубльні контроли ряду.
        self._vol_btn.setVisible(False)
        self._vol.setVisible(False)

        self._group = FollowerGroup(self._player, self)
        channels = [TrackChannel("screen", tr("player_screen_audio"),
                                 self._audio, hidden=True)]
        for key, label, path in self._audio_tracks:
            follower = self._group.add(path)
            follower.set_rate(self._player.playbackRate())
            channels.append(TrackChannel(key, label, follower.audio))
        self._panel = TrackMixerPanel(channels, self)
        root.addWidget(self._panel)

        # «Звук екрана» показуємо лише коли відео дійсно несе аудіодоріжку.
        try:
            self._player.hasAudioChanged.connect(self._on_has_audio)
        except Exception:                              # pragma: no cover
            pass

    def _on_has_audio(self, has_audio: bool) -> None:
        if has_audio and self._panel is not None:
            self._panel.reveal_channel("screen")

    # ---- показ / життєвий цикл ----
    @classmethod
    def open_for(cls, parent, path, audio_tracks=None, utterances=None,
                 speaker_names=None):
        """Показати модальний плеєр для ``path`` і прибрати за собою. Гарантовано
        звільняє файловий хендл на закритті (закриття → closeEvent → release).
        ``audio_tracks`` — синхронні аудіодоріжки наради (режим мікшера).
        ``utterances``/``speaker_names`` — розшифровка наради поруч із відео
        (feature/meeting-video-text, етап 2); без них діалог лишається чистим
        відеоплеєром, як і раніше."""
        top = parent.window() if parent is not None else None
        dlg = cls(path, top, audio_tracks=audio_tracks,
                  utterances=utterances, speaker_names=speaker_names)
        dlg.exec()
        dlg.deleteLater()

    def showEvent(self, event):
        super().showEvent(event)
        # Захист від захоплення екрана: вікно відтворення наради теж ховаємо від
        # Recall/скріншотів/трансляцій, якщо тумблер увімкнено. winId дійсний лише
        # після показу, тож застосовуємо саме тут (реєстрація → тумблер зніме WDA
        # з живого вікна при вимкненні).
        try:
            controller = getattr(self.window(), "controller", None)
            if controller is None and self.parentWidget() is not None:
                controller = getattr(
                    self.parentWidget().window(), "controller", None)
            self._screen_protection_controller = controller
            apply_protection = getattr(
                controller, "apply_screen_protection_to_window", None)
            if apply_protection is not None:
                apply_protection(self)
            else:
                from whisper_core.win_hardening import protect_window
                protect_window(self)
        except Exception:
            pass
        if self._started_once or self._player is None:
            return
        self._started_once = True
        if self._path and os.path.exists(self._path):
            self._player.setSource(QUrl.fromLocalFile(self._path))
            self._player.play()
        elif self._path:                # шлях є, але файл зник
            self._show_error(tr("video_not_found"))

    def closeEvent(self, event):
        try:
            remove_protection = getattr(
                self._screen_protection_controller,
                "remove_screen_protection_from_window", None)
            if remove_protection is not None:
                remove_protection(self)
            else:
                from whisper_core.win_hardening import unprotect_window
                unprotect_window(self)
        except Exception:
            pass
        self._release_source()
        super().closeEvent(event)

    def _release_source(self):
        """Стоп + відпустити файловий хендл бекенда (setSource(QUrl()))."""
        if self._group is not None:
            self._group.stop()
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())

    # ---- повний екран ----
    # ПАСТКА Qt: QVideoWidget не можна переносити між батьківськими контейнерами
    # (реперентинг руйнує поверхню відтворення QVideoSink — після виходу з
    # повного екрана відео чорніє й більше не грає). Тому тут НІКОЛИ не
    # робимо self._video.setParent(...) чи layout.addWidget(self._video) вдруге —
    # відео лишається дитиною ``self`` завжди. Повний екран — це лише зміна
    # СТАНУ ВІКНА (showFullScreen()/showNormal() на самому діалозі) плюс
    # ховання сусідніх панелей (банер помилки, мікшер), а не перебудова дерева.
    def eventFilter(self, obj, event):
        if obj is self._video and event.type() == QEvent.MouseButtonDblClick:
            self._toggle_fullscreen()
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_F and event.modifiers() == Qt.ControlModifier:
            if self._transcript_panel is not None:
                self._transcript_panel.open_search()
                event.accept()
                return
        if key == Qt.Key_F11 or (
                key == Qt.Key_F and event.modifiers() == Qt.NoModifier):
            self._toggle_fullscreen()
            event.accept()
            return
        if key == Qt.Key_Escape and self._is_fullscreen:
            self._toggle_fullscreen()
            event.accept()
            return
        super().keyPressEvent(event)

    def _toggle_fullscreen(self):
        if self._video is None:
            return
        if self._is_fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        """Розгорнути ВІКНО на весь екран — відео й контроли лишаються, батько
        відеовіджета не міняється. Сусідні панелі (банер помилки, мікшер наради,
        панель розшифровки) ховаємо, щоб «видно тільки відео й керування»."""
        self._is_fullscreen = True
        if self._status is not None:
            self._status_was_visible = self._status.isVisible()
            self._status.setVisible(False)
        if self._panel is not None:
            self._panel.setVisible(False)
        if self._transcript_panel is not None:
            self._transcript_was_visible = self._transcript_panel.isVisible()
            self._transcript_panel.setVisible(False)
        if self._fs_btn is not None:
            self._fs_btn.set_icon_name("fa6s.compress")
            self._fs_btn.setToolTip(tr("video_fullscreen_exit"))
            self._fs_btn.setAccessibleName(tr("video_fullscreen_exit"))
        self.showFullScreen()

    def _exit_fullscreen(self):
        """Повернути звичайне вікно. Той самий батько відеовіджета — жодного
        reparent-у — тому відтворення триває без чорного екрана."""
        self._is_fullscreen = False
        if self._status is not None and self._status_was_visible:
            self._status.setVisible(True)
        if self._panel is not None:
            self._panel.setVisible(True)
        if self._transcript_panel is not None and self._transcript_was_visible:
            self._transcript_panel.setVisible(True)
        if self._fs_btn is not None:
            self._fs_btn.set_icon_name("fa6s.expand")
            self._fs_btn.setToolTip(tr("video_fullscreen_enter"))
            self._fs_btn.setAccessibleName(tr("video_fullscreen_enter"))
        self.showNormal()

    # ---- керування ----
    def _toggle(self):
        if self._player is None:
            return
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
            return
        if self._player.source().isEmpty() and self._path:
            # джерело звільнене (після кінця чи помилки) — перезавантажити з початку
            self._player.setSource(QUrl.fromLocalFile(self._path))
        self._player.play()

    def _on_seek(self, frac: float):
        dur = self._player.duration()
        if dur > 0:
            ms = int(frac * dur)
            self._player.setPosition(ms)
            if self._group is not None:
                self._group.broadcast_seek(ms)

    def _on_transcript_seek(self, ms: int):
        """Клацання по репліці в панелі розшифровки → перемотати відео (і
        аудіодоріжки наради, якщо режим мікшера) на її час."""
        if self._player is None:
            return
        self._player.setPosition(ms)
        if self._group is not None:
            self._group.broadcast_seek(ms)

    def _toggle_transcript_panel(self):
        panel = self._transcript_panel
        if panel is None:
            return
        show = not panel.isVisible()
        panel.setVisible(show)
        if self._transcript_btn is not None:
            self._transcript_btn.set_icon_name(
                "fa6s.message" if show else "fa6s.chevron-left")

    def _cycle_speed(self):
        self._speed_idx = (self._speed_idx + 1) % len(_VIDEO_SPEEDS)
        rate = _VIDEO_SPEEDS[self._speed_idx]
        self._player.setPlaybackRate(rate)
        if self._group is not None:
            self._group.broadcast_rate(rate)
        self._speed_btn.setText(self._speed_label())

    def _speed_label(self) -> str:
        return fmt_speed(_VIDEO_SPEEDS[self._speed_idx])

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
        dur = self._player.duration()
        if dur > 0:
            self._seek.set_fraction(pos / dur)
        self._time.setText(f"{fmt_time(pos)} / {fmt_time(dur)}")
        if self._transcript_panel is not None:
            self._transcript_panel.set_active_ms(pos)

    def _on_duration(self, dur: int):
        self._time.setText(f"{fmt_time(self._player.position())} / {fmt_time(dur)}")

    def _on_playback_state(self, state):
        playing = state == QMediaPlayer.PlayingState
        self._play_btn.set_icon_name("fa6s.pause" if playing else "fa6s.play")
        self._play_btn.setToolTip(tr("player_pause") if playing else tr("player_play"))

    def _on_media_status(self, status):
        # Аудит чесності (31.07, знахідка 3): без цього обробника кінець
        # відео НЕ звільняв джерело (той самий ідіом, що аудіо-плеєр —
        # player.py._on_media_status), тож `_toggle`'ів `source().isEmpty()`
        # ніколи не спрацьовував і кнопка «відтворити» після кінця мовчки
        # не робила нічого. Тепер кінець → звільнити джерело й повернути
        # повзунок на початок; наступний play() у `_toggle` перезавантажить
        # і почне з нуля.
        if status == QMediaPlayer.EndOfMedia:
            self._release_source()
            self._seek.set_fraction(0.0)

    def _on_error(self, _error, error_string):
        """Кодек відсутній / файл битий: людське повідомлення замість краху."""
        logging.warning("Відеоплеєр не зміг відтворити «%s»: %s",
                        anonymize_path(self._path), error_string)
        self._show_error(tr("video_error"))

    def _show_error(self, message: str):
        """Показати банер помилки, вимкнути play (нема чого відтворювати)."""
        self._release_source()
        if self._status is not None:
            self._status.setText(message)
            self._status.setVisible(True)
        if self._play_btn is not None:
            self._play_btn.setEnabled(False)
            self._play_btn.setToolTip(message)

    def _open_external(self):
        if self._path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._path))
