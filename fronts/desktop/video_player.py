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

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QVBoxLayout,
)

from .glass import GlassButton
from .i18n import tr
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

    def __init__(self, path=None, parent=None, audio_tracks=None):
        super().__init__(parent)
        self._path = str(path) if path else None
        self._audio_tracks = list(audio_tracks or [])
        self._group = None
        self._panel = None
        self._started_once = False
        self.setWindowTitle(
            os.path.basename(self._path) if self._path else tr("video_title"))
        self.setModal(True)
        self.resize(760, 540)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(12)

        if not _HAVE_QTMM:
            # Фолбек без QtMultimedia — одна кнопка системного плеєра (як InlinePlayer)
            self._player = self._audio = self._video = None
            self._status = self._play_btn = None
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
        root.addWidget(self._video, stretch=1)

        # Банер людської помилки (кодек/битий файл) — прихований, поки все добре.
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setProperty("muted", True)
        self._status.setVisible(False)
        root.addWidget(self._status)

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

        root.addLayout(controls)

        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_playback_state)
        self._player.errorOccurred.connect(self._on_error)

        if self._audio_tracks:
            self._build_track_panel(root)

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
    def open_for(cls, parent, path, audio_tracks=None):
        """Показати модальний плеєр для ``path`` і прибрати за собою. Гарантовано
        звільняє файловий хендл на закритті (закриття → closeEvent → release).
        ``audio_tracks`` — синхронні аудіодоріжки наради (режим мікшера)."""
        top = parent.window() if parent is not None else None
        dlg = cls(path, top, audio_tracks=audio_tracks)
        dlg.exec()
        dlg.deleteLater()

    def showEvent(self, event):
        super().showEvent(event)
        # Захист від захоплення екрана: вікно відтворення наради теж ховаємо від
        # Recall/скріншотів/трансляцій, якщо тумблер увімкнено. winId дійсний лише
        # після показу, тож застосовуємо саме тут (реєстрація → тумблер зніме WDA
        # з живого вікна при вимкненні).
        try:
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
            self._show_error(tr("video_error"))

    def closeEvent(self, event):
        self._release_source()
        super().closeEvent(event)

    def _release_source(self):
        """Стоп + відпустити файловий хендл бекенда (setSource(QUrl()))."""
        if self._group is not None:
            self._group.stop()
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())

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

    def _on_duration(self, dur: int):
        self._time.setText(f"{fmt_time(self._player.position())} / {fmt_time(dur)}")

    def _on_playback_state(self, state):
        playing = state == QMediaPlayer.PlayingState
        self._play_btn.set_icon_name("fa6s.pause" if playing else "fa6s.play")
        self._play_btn.setToolTip(tr("player_pause") if playing else tr("player_play"))

    def _on_error(self, _error, error_string):
        """Кодек відсутній / файл битий: людське повідомлення замість краху."""
        logging.warning("Відеоплеєр не зміг відтворити «%s»: %s",
                        self._path, error_string)
        self._show_error(tr("video_error"))

    def _show_error(self, message: str):
        """Показати банер помилки, вимкнути play (нема чого відтворювати)."""
        self._release_source()
        if self._status is not None:
            self._status.setText(message)
            self._status.setVisible(True)
        if self._play_btn is not None:
            self._play_btn.setEnabled(False)
            self._play_btn.setToolTip(tr("video_error"))

    def _open_external(self):
        if self._path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._path))
