"""Окрема сторінка «Запис екрана» — не залежить від режиму Нарада."""
import time
import qtawesome as qta
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QFrame, QComboBox, QButtonGroup, QCheckBox, QScrollArea)
try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PySide6.QtMultimediaWidgets import QVideoWidget
except Exception:  # pragma: no cover
    QMediaPlayer = QAudioOutput = QVideoWidget = None
from ..chip_popover import ValueSliderChip
from ..glass import GlassButton, StatusTag
from ..i18n import tr
from .. import theme   # нічний режим: сегмент-контрол читає палітру
from . import page_header


class ScreenPage(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self._started = 0
        self._recording = False

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 26, 32, 18)
        root.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(12)
        head.addLayout(page_header(tr("nav_screen"), tr("screen_subtitle")), 1)

        # Єдина текстова кнопка з ВІДЕО-іконкою (аудит 1.2.1)
        self._rec_action = GlassButton(tr("screen_start"))
        self._rec_action.setProperty("accent", True)
        self._rec_action.setIcon(qta.icon("fa6s.video"))
        self._rec_action.setAccessibleName(tr("screen_start"))
        self._rec_action.clicked.connect(self._toggle)
        head.addWidget(self._rec_action)
        root.addLayout(head)

        root.addWidget(self._build_controls())

        # Об'єднана статусна панель
        status_panel = QFrame()
        status_panel.setProperty("glasspanel", True)
        status_lay = QHBoxLayout(status_panel)
        status_lay.setContentsMargins(14, 10, 14, 10)
        status_lay.setSpacing(12)

        self._badge = StatusTag("queued", tr("screen_idle"))
        self._timer = QLabel("00:00")
        self._timer.setAccessibleName(tr("screen_idle"))
        self._timer.setStyleSheet(
            "font-family: monospace; font-size: 14px; font-weight: bold; "
            "background: transparent; border: none; padding: 0 4px;"
        )

        status_lay.addWidget(self._badge)
        status_lay.addWidget(self._timer)
        status_lay.addStretch()

        self._open = GlassButton(tr("screen_open_folder"))
        self._open.setAccessibleName(tr("screen_open_folder"))
        self._open.clicked.connect(controller.open_screen_recordings_folder)
        status_lay.addWidget(self._open)

        root.addWidget(status_panel)

        self._list = QVBoxLayout()
        self._list.setSpacing(8)
        self._list.addStretch()

        host = QWidget()
        host.setLayout(self._list)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._update_timer)

        controller.screen_record_state.connect(self._state)
        controller.screen_record_error.connect(self._error)
        controller.screen_record_finished.connect(self._finished)

        self._refresh_sources()
        self._refresh()

    def _build_controls(self):
        panel = QFrame()
        panel.setProperty("glasspanel", True)
        grid = QGridLayout(panel)
        grid.setContentsMargins(16, 12, 16, 12)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        grid.setColumnMinimumWidth(0, 150)

        # Єдиний сегмент-контрол для вибору джерела: підкладка ТРОХИ темніша за
        # оточуючий glasspanel (не повне скло — вкладений контрол), тож інлайн-
        # background свідомо перебиває глобальний [glasspanel] QSS; токен _WHITE_04
        # (не хардкод), щоб у 'red'/інших кольорах підсвіт теж був у тон теми.
        segment_frame = QFrame()
        segment_frame.setProperty("glasspanel", True)

        def _segment_style(w):
            w.setStyleSheet(
                f"QFrame {{ background: {theme._WHITE_04}; border-radius: 8px; padding: 2px; }}"
            )

        _segment_style(segment_frame)
        theme.register_restyle_call(segment_frame, _segment_style)
        kinds_lay = QHBoxLayout(segment_frame)
        kinds_lay.setContentsMargins(2, 2, 2, 2)
        kinds_lay.setSpacing(2)

        self._kind = QButtonGroup(self)
        for ident, key in (("monitor", "screen_source_monitor"),
                           ("window", "screen_source_window"),
                           ("rect", "screen_source_rect")):
            b = GlassButton(tr(key))
            b.setCheckable(True)
            b.setProperty("kind", ident)
            b.setAccessibleName(tr(key))
            b.setChecked(ident == "monitor")
            self._kind.addButton(b)
            kinds_lay.addWidget(b)

        self._kind.buttonClicked.connect(self._refresh_sources)
        lbl_source = QLabel(tr("screen_source"))
        lbl_source.setAccessibleName(tr("screen_source"))
        grid.addWidget(lbl_source, 0, 0)
        grid.addWidget(segment_frame, 0, 1)

        self._source = QComboBox()
        self._source.setAccessibleName(tr("screen_select"))
        self._source.currentIndexChanged.connect(self._on_source_changed)

        refresh = GlassButton(tr("screen_refresh"))
        refresh.setAccessibleName(tr("screen_refresh"))
        refresh.clicked.connect(self._refresh_sources)

        srcrow = QHBoxLayout()
        srcrow.addWidget(self._source, 1)
        srcrow.addWidget(refresh)

        lbl_select = QLabel(tr("screen_select"))
        lbl_select.setAccessibleName(tr("screen_select"))
        grid.addWidget(lbl_select, 1, 0)
        grid.addLayout(srcrow, 1, 1)

        self._fps = ValueSliderChip(
            5, 60,
            int(getattr(self.controller.cfg, "screen_record_fps", 30)),
            label_key="screen_fps_chip", name_key="screen_fps"
        )
        fps_cap = QLabel(tr("screen_fps"))
        fps_cap.setObjectName("screenFpsCaption")
        fps_cap.setAccessibleName(tr("screen_fps"))
        fps_cap.setWordWrap(True)
        grid.addWidget(fps_cap, 2, 0)
        grid.addWidget(self._fps, 2, 1)

        self._resolution = QComboBox()
        self._resolution.setAccessibleName(tr("screen_resolution"))
        self._resolution.addItem(tr("screen_native"), "native")
        self._resolution.addItem("1080p", "1080p")
        self._resolution.addItem("720p", "720p")

        self._quality = QComboBox()
        self._quality.setAccessibleName(tr("screen_quality"))
        for k, v in (("screen_quality_low", "low"),
                     ("screen_quality_medium", "medium"),
                     ("screen_quality_high", "high")):
            self._quality.addItem(tr(k), v)

        self._format = QComboBox()
        self._format.setAccessibleName(tr("screen_format"))
        from whisper_core.screen.recorder import available_formats
        fmt_display = {"webm": "WebM", "mp4": "MP4", "mkv": "MKV"}
        for f in available_formats():
            self._format.addItem(fmt_display.get(f, f.upper()), f)

        for combo, value in ((self._resolution, getattr(self.controller.cfg, "screen_record_resolution", "native")),
                            (self._quality, getattr(self.controller.cfg, "screen_record_quality", "medium")),
                            (self._format, getattr(self.controller.cfg, "screen_record_format", "webm"))):
            i = combo.findData(value)
            combo.setCurrentIndex(max(0, i))

        lbl_res = QLabel(tr("screen_resolution"))
        lbl_res.setAccessibleName(tr("screen_resolution"))
        grid.addWidget(lbl_res, 3, 0)
        grid.addWidget(self._resolution, 3, 1)

        lbl_qual = QLabel(tr("screen_quality"))
        lbl_qual.setAccessibleName(tr("screen_quality"))
        grid.addWidget(lbl_qual, 4, 0)
        grid.addWidget(self._quality, 4, 1)

        lbl_fmt = QLabel(tr("screen_format"))
        lbl_fmt.setAccessibleName(tr("screen_format"))
        grid.addWidget(lbl_fmt, 5, 0)
        grid.addWidget(self._format, 5, 1)

        self._audio = QCheckBox(tr("screen_system_audio"))
        self._audio.setAccessibleName(tr("screen_system_audio"))
        self._audio.setChecked(bool(getattr(self.controller.cfg, "screen_record_system_audio", False)))
        grid.addWidget(self._audio, 6, 1)

        return panel

    def _refresh_sources(self, *_):
        kind = self._kind.checkedButton().property("kind")
        self._source.blockSignals(True)
        self._source.clear()
        if kind == "monitor":
            for mon in self.controller.list_screen_monitors():
                self._source.addItem(mon.label, {"kind": "monitor", "index": mon.index})
        elif kind == "window":
            for win in self.controller.list_screen_windows():
                self._source.addItem(win.label, {"kind": "window", "hwnd": win.hwnd})
        else:
            for mon in self.controller.list_screen_monitors()[:1]:
                self._source.addItem(mon.label, {"kind": "rect", "rect": (mon.left, mon.top, mon.width, mon.height)})
        if not self._source.count():
            self._source.addItem(tr("screen_no_source"), None)
        self._source.blockSignals(False)
        self._on_source_changed()

    def _on_source_changed(self, *_):
        has_source = bool(self._source.currentData() is not None)
        self._update_start_button_state(has_source=has_source)

    def _update_start_button_state(self, has_source: bool):
        if self._recording:
            self._rec_action.setEnabled(True)
            self._rec_action.setToolTip(tr("screen_stop"))
            return
        if not has_source:
            self._rec_action.setEnabled(False)
            self._rec_action.setToolTip(tr("screen_no_source_tooltip"))
        else:
            self._rec_action.setEnabled(True)
            self._rec_action.setToolTip(tr("screen_start"))

    def _options(self):
        cfg = self.controller.cfg
        cfg.screen_record_fps = self._fps.value()
        cfg.screen_record_resolution = self._resolution.currentData()
        cfg.screen_record_format = self._format.currentData()
        cfg.screen_record_quality = self._quality.currentData()
        cfg.screen_record_system_audio = self._audio.isChecked()
        self.controller.save_config()
        return {
            "fps": cfg.screen_record_fps,
            "resolution": cfg.screen_record_resolution,
            "format": cfg.screen_record_format,
            "quality": cfg.screen_record_quality,
            "system_audio": cfg.screen_record_system_audio,
        }

    def _toggle(self):
        if self._recording:
            self.controller.screen_record_stop()
            return
        source = self._source.currentData()
        if source and self.controller.screen_record_start(source, self._options()):
            self._state("recording")

    def _state(self, state):
        self._recording = (state == "recording")
        label = tr("screen_stop") if self._recording else tr("screen_start")
        self._rec_action.setText(label)
        self._rec_action.setAccessibleName(label)
        self._rec_action.setIcon(qta.icon("fa6s.square" if self._recording else "fa6s.video"))
        self._update_start_button_state(has_source=self._source.currentData() is not None)
        self._badge.set_state("busy" if self._recording else "queued",
                             tr("screen_recording") if self._recording else tr("screen_idle"))
        if self._recording:
            self._started = time.time()
            self._tick.start()
        else:
            self._tick.stop()

    def _update_timer(self):
        seconds = int(time.time() - self._started)
        self._timer.setText(f"{seconds // 60:02d}:{seconds % 60:02d}")

    def _error(self, message):
        self._badge.set_state("error", tr("screen_error", error=message))

    def _finished(self, _path, ok):
        self._state("idle")
        self._refresh()
        self._badge.set_state("done" if ok else "error", tr("screen_saved") if ok else tr("screen_failed"))

    def _refresh(self):
        while self._list.count() > 1:
            item = self._list.takeAt(0)
            w = item.widget()
            w and w.deleteLater()
        for path in self.controller.list_screen_recordings():
            card = QFrame()
            card.setProperty("card", True)
            lay = QHBoxLayout(card)
            lay.addWidget(QLabel(path.name), 1)
            watch = GlassButton(tr("screen_play"))
            watch.setAccessibleName(tr("screen_play"))
            watch.clicked.connect(lambda _=False, p=path: self._watch(p))
            lay.addWidget(watch)
            self._list.insertWidget(0, card)

    def _watch(self, path):
        from ..video_player import VideoPlayerDialog
        VideoPlayerDialog.open_for(self, path)
