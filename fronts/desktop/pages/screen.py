"""Окрема сторінка «Запис екрана» — не залежить від режиму Нарада."""
import logging
import shutil
import time
import qtawesome as qta
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QFileDialog, QFrame, QComboBox, QButtonGroup, QCheckBox,
    QScrollArea, QToolButton, QPushButton, QStackedWidget)
try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PySide6.QtMultimediaWidgets import QVideoWidget
except Exception:  # pragma: no cover
    QMediaPlayer = QAudioOutput = QVideoWidget = None
from ..chip_popover import ValueSliderChip
from ..glass import GlassButton, StatusTag
from ..empty_state import EmptyState
from ..i18n import tr
from .. import motion
from ..record_action_bar import RecordActionBar
from .. import theme   # нічний режим: сегмент-контрол читає палітру
from . import page_header


class ScreenPage(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self._started = 0
        self._recording = False
        # Аудит чесності (31.07, знахідка 4): чи вже показано КОНКРЕТНУ
        # причину збою через _error() цього запису. Скидається на старті
        # нового запису (_state("recording")).
        self._error_shown = False
        self._error_text = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 26, 32, 18)
        root.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(12)
        head.addLayout(page_header(tr("nav_screen"), tr("screen_subtitle")), 1)

        # Єдина текстова кнопка з ВІДЕО-іконкою (аудит 1.2.1). Плаский QPushButton
        # (НЕ GlassButton): GlassButton малює все сам у paintEvent і QSS
        # [accent="true"] на нього не лягає (лишається непомітним склом, як
        # сусідні кнопки) — головна дія сторінки ховалась (той самий дефект,
        # що знайдено на кнопці «Отримати текст наради», e092484).
        self._rec_action = QPushButton(tr("screen_start"))
        self._rec_action.setProperty("accent", True)
        self._rec_action.setCursor(Qt.PointingHandCursor)
        self._rec_action.setIcon(qta.icon("fa6s.video"))
        self._rec_action.setAccessibleName(tr("screen_start"))
        self._rec_action.clicked.connect(self._toggle)
        head.addWidget(self._rec_action)
        root.addLayout(head)

        root.addWidget(self._build_settings_disclosure())

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

        # порожній стан ⇄ список записів (аудит 31.07: критичний дефект —
        # без жодного запису нижні ~80% сторінки лишались голим полем без
        # тексту й іконки, як зламаний/незавантажений UI).
        self._empty = EmptyState("fa6s.video", tr("screen_empty_title"),
                                 tr("screen_empty_hint"),
                                 button_text=tr("screen_start"), on_click=self._toggle)
        self._stack = QStackedWidget()
        self._stack.addWidget(self._empty)   # 0 — порожній стан
        self._stack.addWidget(scroll)        # 1 — список записів
        root.addWidget(self._stack, 1)

        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._update_timer)

        controller.screen_record_state.connect(self._state)
        controller.screen_record_error.connect(self._error)
        controller.screen_record_finished.connect(self._finished)

        self._refresh_sources()
        self._refresh()

    def _build_settings_disclosure(self) -> QWidget:
        """Розкривна секція налаштувань запису екрана (канон побудови сторінок
        30.07 п.4): шість рядків налаштувань раніше стояли розгорнутими
        завжди й відсували стрічку записів вниз — той самий дефект, що на
        Нараді до аудиту 22.07. Згортаємо в один блок, як там; розкривач —
        та сама явна кнопка з рамкою й стрілкою (п.3, property("disclosure")
        у theme.py), не дрібний рядок-підпис."""
        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        toggle = QToolButton()
        toggle.setText(tr("screen_settings"))
        toggle.setCheckable(True)
        toggle.setChecked(False)
        toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        toggle.setArrowType(Qt.RightArrow)
        toggle.setCursor(Qt.PointingHandCursor)
        toggle.setAccessibleName(tr("screen_settings"))
        toggle.setProperty("disclosure", True)
        panel = self._build_controls()
        panel.setVisible(False)
        toggle.toggled.connect(self._on_settings_toggled)
        v.addWidget(toggle, alignment=Qt.AlignLeft)
        v.addWidget(panel)
        self._settings_toggle = toggle
        self._settings_panel = panel
        return host

    def _on_settings_toggled(self, opened: bool):
        self._settings_panel.setVisible(opened)
        self._settings_toggle.setArrowType(Qt.DownArrow if opened else Qt.RightArrow)

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

        # style_segment_frame прибирає власну рамку підкладки: [glasspanel]
        # QSS-правило дає border 1px, а активна кнопка-«таблетка» малює СВОЮ
        # золоту рамку поверх — дві лінії поспіль (діагноз 2026-07-30 №2).
        theme.style_segment_frame(segment_frame)
        theme.register_restyle_call(segment_frame, theme.style_segment_frame)
        kinds_lay = QHBoxLayout(segment_frame)
        kinds_lay.setContentsMargins(2, 2, 2, 2)
        kinds_lay.setSpacing(2)

        self._kind = QButtonGroup(self)
        for ident, key in (("monitor", "screen_source_monitor"),
                           ("window", "screen_source_window")):
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
            self._empty.button.setEnabled(True)
            return
        if not has_source:
            self._rec_action.setEnabled(False)
            self._rec_action.setToolTip(tr("screen_no_source_tooltip"))
            self._empty.button.setEnabled(False)
            self._empty.button.setToolTip(tr("screen_no_source_tooltip"))
        else:
            self._rec_action.setEnabled(True)
            self._rec_action.setToolTip(tr("screen_start"))
            self._empty.button.setEnabled(True)
            self._empty.button.setToolTip("")

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
        if not source:
            # Кнопка зазвичай неактивна без джерела (_update_start_button_state),
            # але сигнал currentIndexChanged може відставати — не мовчати.
            logging.warning("Спроба почати запис екрана без обраного джерела")
            self._error(tr("screen_no_source_tooltip"))
            return
        if self.controller.screen_record_start(source, self._options()):
            self._state("recording")
        # інакше screen_record_start уже залогував причину й надіслав
        # screen_record_error → _error() виставить бейдж "error" з поясненням

    def _state(self, state):
        if state == "recording":
            self._error_shown = False   # новий запис — забуваємо стару причину
        self._recording = (state == "recording")
        label = tr("screen_stop") if self._recording else tr("screen_start")
        self._rec_action.setText(label)
        self._rec_action.setAccessibleName(label)
        self._rec_action.setIcon(qta.icon("fa6s.square" if self._recording else "fa6s.video"))
        self._empty.button.setText(label)
        self._empty.button.setAccessibleName(label)
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
        self._error_shown = True
        self._error_text = tr("screen_error", error=message)
        self._badge.set_state("error", self._error_text)

    def _finished(self, _path, ok):
        # Аудит чесності (31.07, знахідка 4): _state("idle") нижче сам
        # перемальовує бейдж на "queued"/screen_idle — якщо конкретну
        # причину вже показав _error() РАНІШЕ за цей сигнал, після ідле-
        # скидання повертаємо саме її, а не загальний "screen_failed".
        preserve_detail = not ok and self._error_shown
        self._state("idle")
        self._refresh()
        if preserve_detail:
            self._badge.set_state("error", self._error_text)
            return
        self._badge.set_state("done" if ok else "error", tr("screen_saved") if ok else tr("screen_failed"))

    def _refresh(self):
        while self._list.count() > 1:
            item = self._list.takeAt(0)
            w = item.widget()
            w and w.deleteLater()
        recordings = self.controller.list_screen_recordings()
        for path in recordings:
            self._list.insertWidget(0, self._build_recording_card(path))
        self._stack.setCurrentIndex(1 if recordings else 0)

    def _build_recording_card(self, path):
        """Картка одного відеозапису: назва + RecordActionBar —
        «Дивитися відео» як специфічна дія сторінки, решта
        (перейменувати/показати в теці/надіслати/видалити) — спільний бар,
        той самий, що застосується на Аудіофайлах. `state["path"]` тримає
        актуальний шлях: після перейменування наступні дії (видалити, показати
        в теці…) мусять цілитись у НОВИЙ файл, не в застарілий захоплений
        лямбдою на момент побудови картки."""
        state = {"path": path}
        card = QFrame()
        card.setProperty("card", True)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 13, 18, 13)
        lay.setSpacing(8)

        name = QLabel(path.name)
        name.setProperty("strong", True)
        lay.addWidget(name)

        watch = GlassButton(tr("screen_play"))
        watch.setIcon(qta.icon("fa6s.circle-play"))
        watch.setAccessibleName(tr("screen_play"))
        watch.clicked.connect(lambda _=False: self._watch(state["path"]))

        bar = RecordActionBar(path.stem, str(path), extra_widget=watch)

        def _on_rename(new_stem):
            new_path = self.controller.rename_screen_recording(state["path"], new_stem)
            if new_path is None:
                motion.toast(self, tr("recact_rename_failed"))
                return
            state["path"] = new_path
            name.setText(new_path.name)
            bar.set_display_name(new_path.stem)
            bar.set_path_text(str(new_path))

        bar.rename_requested.connect(_on_rename)
        bar.show_in_folder_requested.connect(
            lambda: self.controller.show_screen_recording_in_folder(state["path"]))
        bar.save_as_requested.connect(lambda: self._save_recording_as(state["path"]))
        bar.copy_path_requested.connect(
            lambda: QApplication.clipboard().setText(str(state["path"])))
        bar.delete_requested.connect(lambda: self._delete_recording(state["path"]))
        lay.addWidget(bar)
        return card

    def _save_recording_as(self, path):
        out, _sel = QFileDialog.getSaveFileName(self, tr("recact_save_as"), path.name)
        if not out:
            return
        try:
            shutil.copy2(path, out)
        except OSError:
            motion.toast(self, tr("recact_save_as_failed"))

    def _delete_recording(self, path):
        if self.controller.delete_screen_recording(path):
            self._refresh()
        else:
            motion.toast(self, tr("recact_delete_failed"))

    def _watch(self, path):
        from ..video_player import VideoPlayerDialog
        VideoPlayerDialog.open_for(self, path)
