"""UI Command Mode (feature/voice-edit-selection): голосове редагування виділеного.

Користувач виділив текст (у редакторі транскрипту АБО в довільному застосунку),
викликав Command Mode, ПРОДИКТУВАВ команду («зроби офіційніше», «скороти вдвічі»,
«переклади англійською», «виправ помилки») — локальна LLM переписує виділене, а
результат після ОГЛЯДУ-ПЕРЕД-ДІЄЮ (diff «було → стало») заміняє виділення.

Qt-обгортка над whisper_core.protocol.service.CommandEditGenerator (уся важка
робота — у фоні QThread, UI лишається чуйним). Модель не завантажена → чесний стан
із кнопкою «Завантажити модель» (НЕ заглушка). LLM недоступна або відповіла сміттям
→ помилка, виділення НЕ чіпаємо (гейт «тихої заглушки» на боці service).

Команда надходить одним із двох шляхів:
  • голосом — кнопка мікрофона кличе інжектований ``mic_toggle_fn`` (контролер
    керує спільним рекордером і повертає розшифрований текст у ``set_command_text``);
  • набором — те саме поле вводу (fallback / Scenario B без активного мікрофона).
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QTextEdit, QLineEdit, QMessageBox)

from ..diff_review import DiffReviewDialog
from ..i18n import tr
from ..onboarding import _reap_worker


# --- фонове редагування -----------------------------------------------------
class CommandEditWorker(QThread):
    """Фонове голосове редагування. Тримає CommandEditGenerator для cancel()."""
    finished_ok = Signal(str)
    failed = Signal(str)
    was_cancelled = Signal()

    def __init__(self, generator, selected_text, command, parent=None):
        super().__init__(parent)
        self._gen = generator
        self._selected = selected_text
        self._command = command

    def cancel(self):
        self._gen.cancel()

    def run(self):
        from whisper_core.protocol.service import CommandEditCancelled
        try:
            text = self._gen.run(self._selected, self._command)
            self.finished_ok.emit(text)
        except CommandEditCancelled:
            self.was_cancelled.emit()
        except Exception as exc:                # noqa: BLE001
            self.failed.emit(str(exc))


class CommandEditDialog(QDialog):
    """Огляд виділеного → диктування/набір команди → перегляд diff → заміна.

    apply_fn(new_text) — застосувати результат (вставка назад у зовнішній застосунок
    або заміна виділення в редакторі транскрипту). mic_toggle_fn (опційно) —
    контролер починає/зупиняє запис голосової команди; коли розшифрував, кличе
    set_command_text(). preset_id/model_root — модель мовної генерації."""

    def __init__(self, selected_text, preset_id, apply_fn, *, model_root=None,
                 custom_models=None, mic_toggle_fn=None, parent=None):
        super().__init__(parent)
        self._selected = selected_text or ""
        self._preset_id = preset_id
        self._apply_fn = apply_fn
        self._model_root = model_root
        self._custom_models = custom_models   # llm-picker: власні моделі теж доступні
        self._mic_toggle_fn = mic_toggle_fn
        self._worker = None
        self._recording = False
        self.setWindowTitle(tr("cmdedit_title"))
        self.setModal(True)
        self.setMinimumSize(560, 440)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(12)

        intro = QLabel(tr("cmdedit_intro"))
        intro.setProperty("muted", True)
        intro.setWordWrap(True)
        lay.addWidget(intro)

        sel_caption = QLabel(tr("cmdedit_selection_label"))
        sel_caption.setProperty("muted", True)
        lay.addWidget(sel_caption)
        self._sel_view = QTextEdit()
        self._sel_view.setReadOnly(True)
        self._sel_view.setAcceptRichText(False)
        self._sel_view.setPlainText(self._selected)
        self._sel_view.setAccessibleName(tr("cmdedit_selection_label"))
        self._sel_view.setMaximumHeight(150)
        lay.addWidget(self._sel_view)

        self._status = QLabel()
        self._status.setProperty("muted", True)
        self._status.setWordWrap(True)
        self._status.hide()
        lay.addWidget(self._status)
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setRange(0, 0)                 # тривалість невідома (хвилини на CPU)
        self._bar.hide()
        lay.addWidget(self._bar)

        # --- ряд команди: мікрофон + поле вводу + «Виконати» ---
        cmd_row = QHBoxLayout()
        cmd_row.setSpacing(8)
        if self._mic_toggle_fn is not None:
            self._mic = QPushButton(tr("cmdedit_dictate"))
            self._mic.setAccessibleName(tr("cmdedit_dictate"))
            self._mic.setCheckable(True)
            self._mic.clicked.connect(self._on_mic)
            cmd_row.addWidget(self._mic)
        else:
            self._mic = None
        self._input = QLineEdit()
        self._input.setPlaceholderText(tr("cmdedit_command_placeholder"))
        self._input.setAccessibleName(tr("cmdedit_command_placeholder"))
        self._input.returnPressed.connect(self._on_run)
        cmd_row.addWidget(self._input, stretch=1)
        self._run = QPushButton(tr("cmdedit_run"))
        self._run.setAccessibleName(tr("cmdedit_run"))
        self._run.setProperty("accent", True)
        self._run.clicked.connect(self._on_run)
        cmd_row.addWidget(self._run)
        lay.addLayout(cmd_row)

        # --- нижній ряд: «Завантажити модель» (лише коли треба) + «Закрити» ---
        btns = QHBoxLayout()
        self._download = QPushButton(tr("cmdedit_download_model"))
        self._download.setAccessibleName(tr("cmdedit_download_model"))
        self._download.clicked.connect(self._on_download)
        self._download.hide()
        btns.addWidget(self._download)
        btns.addStretch()
        self._close = QPushButton(tr("common_close"))
        self._close.clicked.connect(self.reject)
        btns.addWidget(self._close)
        lay.addLayout(btns)

        self._input.setFocus()
        self._check_ready()

    # ------------------------------------------------------------ готовність
    def _check_ready(self):
        """Проактивно: без бекенда llama або без завантаженої моделі — чесний стан
        із кнопкою завантаження, а не мовчазна заглушка при спробі виконати."""
        from whisper_core.protocol import service
        if not service.backend_available():
            self._set_blocked(tr("cmdedit_backend_missing"), download=False)
            return
        if not service.model_available(self._preset_id, self._model_root,
                                       self._custom_models):
            self._set_blocked(tr("cmdedit_model_missing"), download=True)
            return
        self._set_ready()

    def _set_blocked(self, message, *, download: bool):
        self._status.setText(message); self._status.show()
        self._run.setEnabled(False)
        self._input.setEnabled(False)
        if self._mic is not None:
            self._mic.setEnabled(False)
        self._download.setVisible(download)

    def _set_ready(self):
        self._status.hide()
        self._run.setEnabled(True)
        self._input.setEnabled(True)
        if self._mic is not None:
            self._mic.setEnabled(True)
        self._download.hide()
        self._input.setFocus()

    def _on_download(self):
        from whisper_core.protocol import model_manager as mm
        from whisper_core import paths
        from .protocol_ui import ProtocolModelDownloadDialog, _human_size
        pid = self._preset_id
        size = _human_size(mm.PRESETS[pid].approx_size_bytes)
        if QMessageBox.question(
                self, tr("protocol_model_consent_title"),
                tr("protocol_model_consent_body", size=size)) != QMessageBox.Yes:
            return
        dlg = ProtocolModelDownloadDialog(paths.protocol_model_dir(pid), pid, self)
        dlg.exec()
        self._check_ready()

    # -------------------------------------------------------------- диктування
    def _on_mic(self):
        if self._mic_toggle_fn is None:
            return
        # Тумблер: перший клік — почати запис, другий — зупинити. Стан вертає
        # контролер через set_recording()/set_command_text().
        self._mic_toggle_fn()

    def set_recording(self, on: bool):
        """Контролер повідомляє стан запису команди (оновити вигляд кнопки)."""
        self._recording = bool(on)
        if self._mic is not None:
            self._mic.setChecked(self._recording)
            self._mic.setText(tr("cmdedit_dictate_stop") if self._recording
                              else tr("cmdedit_dictate"))

    def set_command_text(self, text: str):
        """Контролер віддає розшифровану голосову команду в поле вводу."""
        self.set_recording(False)
        if text:
            self._input.setText(text.strip())
            self._input.setFocus()

    # ------------------------------------------------------------------- запуск
    def _on_run(self):
        if self._worker is not None:
            return
        command = self._input.text().strip()
        if not command:
            self._status.setText(tr("cmdedit_need_command")); self._status.show()
            return
        if not self._selected.strip():
            self._status.setText(tr("cmdedit_need_selection")); self._status.show()
            return
        self._status.setText(tr("cmdedit_working")); self._status.show()
        self._bar.show()
        self._set_busy(True)

        from whisper_core.protocol.service import CommandEditGenerator
        from whisper_core import paths
        root = self._model_root if self._model_root is not None else paths.protocol_models_dir()
        generator = CommandEditGenerator(self._preset_id, model_root=root,
                                         custom_models=self._custom_models)
        self._worker = CommandEditWorker(generator, self._selected, command)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.was_cancelled.connect(self._on_cancelled)
        self._worker.start()

    def _set_busy(self, busy: bool):
        self._input.setEnabled(not busy)
        self._run.setEnabled(not busy)
        if self._mic is not None:
            self._mic.setEnabled(not busy)

    def _on_done(self, text: str):
        self._detach()
        self._bar.hide()
        self._set_busy(True)                     # блок під час огляду
        new_text = text or ""
        # Огляд-перед-дією: показуємо diff «було → стало». Застосовуємо ЛИШЕ за
        # явним «Застосувати» — інакше виділення лишається недоторканим.
        chosen = DiffReviewDialog.review(self, self._selected, new_text)
        if chosen == new_text and new_text != self._selected:
            try:
                self._apply_fn(new_text)
            except Exception as exc:             # noqa: BLE001
                self._status.setText(tr("cmdedit_apply_failed", error=str(exc)))
                self._status.show()
                self._set_busy(False)
                return
            self.accept()                        # застосовано — закриваємо
            return
        # «Лишити як було» — вертаємось, даємо змінити команду й спробувати знову
        self._status.setText(tr("cmdedit_kept")); self._status.show()
        self._set_busy(False)
        self._input.setFocus()

    def _on_failed(self, msg: str):
        self._detach()
        self._bar.hide()
        self._set_busy(False)
        self._status.setText(msg); self._status.show()

    def _on_cancelled(self):
        self._detach()
        self._bar.hide()
        self._set_busy(False)
        self._status.setText(tr("cmdedit_cancelled")); self._status.show()

    def _detach(self):
        w = self._worker
        self._worker = None
        if w is None:
            return
        try:
            w.finished_ok.disconnect(); w.failed.disconnect(); w.was_cancelled.disconnect()
        except (RuntimeError, TypeError):
            pass
        w.cancel()
        _reap_worker(w)

    def reject(self):
        self._detach()
        super().reject()
