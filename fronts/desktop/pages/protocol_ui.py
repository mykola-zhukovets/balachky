"""UI AI-протоколу наради (feature/ai-protocol): завантаження моделі з прогресом,
фонова генерація зі скасуванням, перегляд результату + копіювати/зберегти md/docx.

Qt-обгортка над whisper_core.protocol.* (service/model_manager). Уся важка робота —
у фоні (QThread), UI лишається чуйним; шлях без моделі (кнопка вимкнена, consent)
працює без завантажених гігабайтів.
"""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QTextEdit, QLineEdit, QFileDialog, QApplication, QComboBox, QPlainTextEdit)

from ..i18n import tr, human_size
from ..onboarding import _reap_worker

_MB = 1024 * 1024


_human_size = human_size



# --- завантаження моделі --------------------------------------------------
class ProtocolModelDownloadWorker(QThread):
    """Тягне GGUF-модель у фоні. Пресет (preset_id) або власна інтернет-модель
    (custom=CustomModel). progress(done, total) у байтах."""
    progress = Signal(object, object)
    finished_ok = Signal()
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, target_dir, *, preset_id=None, custom=None, force=False,
                 parent=None):
        super().__init__(parent)
        self._target = target_dir
        self._preset_id = preset_id
        self._custom = custom
        self._force = force
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        from whisper_core.protocol import model_manager as mm
        try:
            progress = lambda done, total: self.progress.emit(done, total)
            if self._custom is not None:
                mm.download_custom_hf(self._target, self._custom,
                                      progress_cb=progress,
                                      cancel_check=self._cancel.is_set,
                                      force=self._force)
            else:
                mm.download_and_install(self._target, self._preset_id,
                                        progress_cb=progress,
                                        cancel_check=self._cancel.is_set,
                                        force=self._force)
            self.finished_ok.emit()
        except InterruptedError:
            self.cancelled.emit()
        except Exception as exc:                # noqa: BLE001
            self.failed.emit(str(exc))


class ProtocolModelDownloadDialog(QDialog):
    """Модальна докачка моделі протоколу (пресет або власна інтернет-модель) з
    прогресом і скасуванням. exec() == Accepted → модель готова."""

    def __init__(self, target_dir, *, preset_id=None, custom=None, force=False,
                 parent=None):
        super().__init__(parent)
        self._target = target_dir
        self._preset_id = preset_id
        self._custom = custom
        self._force = force
        self._worker = None
        self.setWindowTitle(tr("protocol_model_consent_title"))
        self.setModal(True)
        self.setMinimumWidth(440)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(14)
        self._status = QLabel(tr("protocol_model_downloading", done=0))
        self._status.setProperty("strong", True)
        self._status.setWordWrap(True)
        lay.addWidget(self._status)
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        lay.addWidget(self._bar)
        btns = QHBoxLayout()
        btns.addStretch()
        self._cancel = QPushButton(tr("common_cancel"))
        self._cancel.clicked.connect(self.reject)
        btns.addWidget(self._cancel)
        lay.addLayout(btns)
        self._start()

    def _start(self):
        self._bar.setRange(0, 0)
        self._worker = ProtocolModelDownloadWorker(
            self._target, preset_id=self._preset_id, custom=self._custom,
            force=self._force)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self.accept)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self.reject)
        self._worker.start()

    def _on_progress(self, done, total):
        if total:
            self._bar.setRange(0, 1000)
            self._bar.setValue(min(1000, int(done * 1000 / total)))
        self._status.setText(tr("protocol_model_downloading", done=done // _MB))

    def _on_failed(self, msg: str):
        logging.warning("Докачка моделі протоколу не вдалась: %s", msg)
        self._status.setText(tr("protocol_model_failed", error=msg))
        self._cancel.setText(tr("common_close"))

    def _detach(self):
        w = self._worker
        self._worker = None
        if w is None:
            return
        try:
            w.progress.disconnect(); w.finished_ok.disconnect()
            w.failed.disconnect(); w.cancelled.disconnect()
        except (RuntimeError, TypeError):
            pass
        w.cancel()
        _reap_worker(w)

    def reject(self):
        self._detach()
        super().reject()

    def accept(self):
        self._detach()
        super().accept()


# --- додавання власної моделі з інтернету ----------------------------------
class HFModelDialog(QDialog):
    """Додати власну модель з інтернету за ідентифікатором репозиторію
    (власник/назва) та ім'ям GGUF-файлу. Після Accepted — .result_data =
    (repo_id, filename). Чесно попереджає, що якість чужих моделей не гарантована."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.result_data = None
        self.setWindowTitle(tr("protocol_model_add_hf"))
        self.setModal(True)
        self.setMinimumWidth(480)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(10)
        warn = QLabel(tr("protocol_model_hf_warning"))
        warn.setProperty("muted", True)
        warn.setWordWrap(True)
        lay.addWidget(warn)

        repo_cap = QLabel(tr("protocol_model_hf_repo"))
        repo_cap.setWordWrap(True)
        lay.addWidget(repo_cap)
        self._repo = QLineEdit()
        self._repo.setPlaceholderText(tr("protocol_model_hf_repo_ph"))
        self._repo.setAccessibleName(tr("protocol_model_hf_repo"))
        lay.addWidget(self._repo)

        file_cap = QLabel(tr("protocol_model_hf_file"))
        file_cap.setWordWrap(True)
        lay.addWidget(file_cap)
        self._file = QLineEdit()
        self._file.setPlaceholderText(tr("protocol_model_hf_file_ph"))
        self._file.setAccessibleName(tr("protocol_model_hf_file"))
        lay.addWidget(self._file)

        self._err = QLabel()
        self._err.setProperty("badge", "error")
        self._err.setWordWrap(True)
        self._err.hide()
        lay.addWidget(self._err)

        btns = QHBoxLayout()
        btns.addStretch()
        ok = QPushButton(tr("protocol_model_add_confirm"))
        ok.setAccessibleName(tr("protocol_model_add_confirm"))
        ok.clicked.connect(self._on_ok)
        cancel = QPushButton(tr("common_cancel"))
        cancel.clicked.connect(self.reject)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)
        self._repo.setFocus()

    def _on_ok(self):
        from whisper_core.protocol import model_manager as mm
        repo = self._repo.text().strip()
        fname = self._file.text().strip()
        if not mm.is_repo_id(repo) or not mm.is_gguf_name(fname):
            self._err.setText(tr("protocol_model_hf_invalid"))
            self._err.show()
            return
        self.result_data = (repo, fname)
        self.accept()


# --- генерація протоколу --------------------------------------------------
class ProtocolGenWorker(QThread):
    """Фонова генерація протоколу. Тримає ProtocolGenerator для cancel()."""
    finished_ok = Signal(str)
    failed = Signal(str)
    was_cancelled = Signal()

    def __init__(self, generator, utterances, labels, parent=None):
        super().__init__(parent)
        self._gen = generator
        self._utterances = utterances
        self._labels = labels

    def cancel(self):
        self._gen.cancel()

    def run(self):
        from whisper_core.protocol.service import ProtocolCancelled
        try:
            text = self._gen.run(self._utterances, **self._labels)
            self.finished_ok.emit(text)
        except ProtocolCancelled:
            self.was_cancelled.emit()
        except Exception as exc:                # noqa: BLE001
            self.failed.emit(str(exc))


class ProtocolDialog(QDialog):
    """Створення й перегляд протоколу наради. Показує прогрес зі скасуванням,
    потім результат + Копіювати / Зберегти .md / .docx. protocol.md зберігається
    автоматично поруч із записом одразу після генерації."""

    def __init__(self, session_dir, utterances, preset_id, labels, parent=None,
                 *, custom_models=None):
        super().__init__(parent)
        self._session_dir = session_dir
        self._utterances = utterances
        self._labels = labels
        self._custom_models = custom_models
        self._worker = None
        self._text = ""
        self.setWindowTitle(tr("protocol_dialog_title"))
        self.setModal(True)
        self.setMinimumSize(560, 460)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(12)

        self._status = QLabel(tr("protocol_generating"))
        self._status.setProperty("strong", True)
        self._status.setWordWrap(True)
        lay.addWidget(self._status)
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setRange(0, 0)                 # тривалість невідома (хвилини на CPU)
        lay.addWidget(self._bar)

        self._view = QTextEdit()
        self._view.setReadOnly(True)
        self._view.setAcceptRichText(False)
        self._view.hide()
        lay.addWidget(self._view, stretch=1)

        self._saved = QLabel()
        self._saved.setProperty("muted", True)
        self._saved.setWordWrap(True)
        self._saved.hide()
        lay.addWidget(self._saved)

        btns = QHBoxLayout()
        self._copy = QPushButton(tr("protocol_copy")); self._copy.clicked.connect(self._on_copy)
        self._save_md = QPushButton(tr("protocol_save_md")); self._save_md.clicked.connect(self._on_save_md)
        self._save_docx = QPushButton(tr("protocol_save_docx")); self._save_docx.clicked.connect(self._on_save_docx)
        for b in (self._copy, self._save_md, self._save_docx):
            b.hide(); btns.addWidget(b)
        btns.addStretch()
        self._retry = QPushButton(tr("onb_retry")); self._retry.clicked.connect(self._on_retry)
        self._retry.hide(); btns.addWidget(self._retry)
        self._cancel = QPushButton(tr("common_cancel")); self._cancel.clicked.connect(self.reject)
        btns.addWidget(self._cancel)
        lay.addLayout(btns)

        self._preset_id = preset_id
        self._start(preset_id)

    def _on_retry(self):
        self._detach()
        self._retry.hide()
        self._status.setText(tr("protocol_generating")); self._status.show()
        self._bar.setRange(0, 0); self._bar.show()
        self._cancel.setText(tr("common_cancel"))
        self._start(self._preset_id)

    def _start(self, preset_id):
        from whisper_core.protocol.service import ProtocolGenerator
        from whisper_core import paths
        generator = ProtocolGenerator(
            preset_id, model_root=paths.protocol_models_dir(),
            custom_models=self._custom_models)
        self._worker = ProtocolGenWorker(generator, self._utterances, self._labels)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.was_cancelled.connect(self._on_cancelled)
        self._worker.start()

    def _on_done(self, text: str):
        self._text = text or ""
        self._bar.hide()
        if not self._text.strip():
            self._status.setText(tr("protocol_empty"))
            self._cancel.setText(tr("common_close"))
            return
        # автозбереження protocol.md поруч із записом
        try:
            from whisper_core.protocol import service
            dest = service.save_protocol(self._session_dir, self._text)
            self._saved.setText(tr("protocol_saved", path=str(dest)))
            self._saved.show()
        except OSError:
            pass
        self._status.hide()
        self._view.setPlainText(self._text)
        self._view.show()
        for b in (self._copy, self._save_md, self._save_docx):
            b.show()
        self._cancel.setText(tr("common_close"))

    def _on_failed(self, msg: str):
        self._bar.hide()
        self._status.setText(msg); self._status.show()
        self._retry.show()                       # E5: дати повторити після збою
        self._cancel.setText(tr("common_close"))

    def _on_cancelled(self):
        self._bar.hide()
        self._status.setText(tr("protocol_cancelled"))
        self._cancel.setText(tr("common_close"))

    def _on_copy(self):
        QApplication.clipboard().setText(self._text)

    def _on_save_md(self):
        path, _ = QFileDialog.getSaveFileName(
            self, tr("protocol_save_md"), "protocol.md", "Markdown (*.md)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self._text)
            self._saved.setText(tr("protocol_saved", path=path)); self._saved.show()
        except OSError as exc:
            self._saved.setText(str(exc)); self._saved.show()

    def _on_save_docx(self):
        path, _ = QFileDialog.getSaveFileName(
            self, tr("protocol_save_docx"), "protocol.docx", "Word (*.docx)")
        if not path:
            return
        try:
            from whisper_core import export
            export.protocol_to_docx(self._text, path, title=tr("protocol_dialog_title"))
            self._saved.setText(tr("protocol_saved", path=path)); self._saved.show()
        except Exception as exc:                # noqa: BLE001
            self._saved.setText(str(exc)); self._saved.show()

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


# --- Q&A по нараді ---------------------------------------------------------
class QAGenWorker(QThread):
    """Фонова відповідь на питання по нараді. Тримає QAGenerator для cancel()."""
    finished_ok = Signal(str)
    failed = Signal(str)
    was_cancelled = Signal()

    def __init__(self, generator, question, utterances, labels, parent=None):
        super().__init__(parent)
        self._gen = generator
        self._question = question
        self._utterances = utterances
        self._labels = labels

    def cancel(self):
        self._gen.cancel()

    def run(self):
        from whisper_core.protocol.service import QACancelled
        try:
            text = self._gen.run(self._question, self._utterances, **self._labels)
            self.finished_ok.emit(text)
        except QACancelled:
            self.was_cancelled.emit()
        except Exception as exc:                # noqa: BLE001
            self.failed.emit(str(exc))


class QADialog(QDialog):
    """Q&A-чат по завершеній нараді: питання → відповідь із таймкод-цитатами.
    Клік по цитаті стрибає вбудований плеєр картки на момент (як Smart Chapters).
    Фонова генерація зі скасуванням; бекенд перевірено ПЕРЕД відкриттям (у картці)."""

    def __init__(self, utterances, preset_id, labels, seek=None, parent=None,
                 *, custom_models=None):
        super().__init__(parent)
        self._utterances = utterances
        self._preset_id = preset_id
        self._labels = labels
        self._custom_models = custom_models
        self._seek = seek                        # callable(seconds) → плеєр стрибає
        self._worker = None
        self._answer = ""
        self.setWindowTitle(tr("qa_dialog_title"))
        self.setModal(True)
        self.setMinimumSize(560, 420)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(12)

        self._view = QTextEdit()
        self._view.setReadOnly(True)
        self._view.setAcceptRichText(False)
        self._view.setAccessibleName(tr("qa_dialog_title"))
        self._view.hide()
        lay.addWidget(self._view, stretch=1)

        # ряд клікабельних цитат (динамічний): чіпи-таймкоди відповіді
        self._cites_row = QHBoxLayout()
        self._cites_row.setSpacing(8)
        self._cites_caption = QLabel(tr("qa_citations"))
        self._cites_caption.setProperty("muted", True)
        self._cites_caption.hide()
        self._cites_row.addWidget(self._cites_caption)
        self._cites_row.addStretch()
        lay.addLayout(self._cites_row)
        self._cite_buttons = []

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

        ask = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText(tr("qa_question_placeholder"))
        self._input.setAccessibleName(tr("qa_question_placeholder"))
        self._input.returnPressed.connect(self._on_ask)
        ask.addWidget(self._input, stretch=1)
        self._ask = QPushButton(tr("qa_send"))
        self._ask.setAccessibleName(tr("qa_send"))
        self._ask.clicked.connect(self._on_ask)
        ask.addWidget(self._ask)
        lay.addLayout(ask)

        btns = QHBoxLayout()
        btns.addStretch()
        self._cancel = QPushButton(tr("common_close"))
        self._cancel.clicked.connect(self.reject)
        btns.addWidget(self._cancel)
        lay.addLayout(btns)
        self._input.setFocus()

    def _on_ask(self):
        question = self._input.text().strip()
        if not question or self._worker is not None:
            return
        self._clear_citations()
        self._view.hide()
        self._answer = ""
        self._status.setText(tr("qa_generating")); self._status.show()
        self._bar.show()
        self._set_asking(True)

        from whisper_core.protocol.service import QAGenerator
        from whisper_core import paths
        generator = QAGenerator(
            self._preset_id, model_root=paths.protocol_models_dir(),
            custom_models=self._custom_models)
        self._worker = QAGenWorker(generator, question, self._utterances, self._labels)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.was_cancelled.connect(self._on_cancelled)
        self._worker.start()

    def _set_asking(self, busy: bool):
        self._input.setEnabled(not busy)
        self._ask.setEnabled(not busy)

    def _on_done(self, text: str):
        self._detach()
        self._bar.hide()
        self._set_asking(False)
        self._answer = text or ""
        if not self._answer.strip():
            self._status.setText(tr("qa_empty")); self._status.show()
            return
        self._status.hide()
        self._view.setPlainText(self._answer)
        self._view.show()
        self._build_citations(self._answer)
        self._input.clear(); self._input.setFocus()

    def _on_failed(self, msg: str):
        self._detach()
        self._bar.hide()
        self._set_asking(False)
        self._status.setText(msg); self._status.show()

    def _on_cancelled(self):
        self._detach()
        self._bar.hide()
        self._set_asking(False)
        self._status.setText(tr("qa_cancelled")); self._status.show()

    # --- клікабельні цитати ---
    def _clear_citations(self):
        for b in self._cite_buttons:
            self._cites_row.removeWidget(b)
            b.deleteLater()
        self._cite_buttons = []
        self._cites_caption.hide()

    def _build_citations(self, answer: str):
        from whisper_core.protocol import qa as _qa
        self._clear_citations()
        cites = _qa.parse_citations(answer)
        if not cites:
            return
        self._cites_caption.show()
        for secs, tc in cites:
            b = QPushButton(tc)
            b.setToolTip(tr("qa_citation_seek"))
            b.setAccessibleName(f"{tr('qa_citations')} {tc}")
            b.setEnabled(self._seek is not None)
            if self._seek is not None:
                b.clicked.connect(lambda _=False, t=float(secs): self._seek(t))
            # вставляємо перед фінальним stretch
            self._cites_row.insertWidget(self._cites_row.count() - 1, b)
            self._cite_buttons.append(b)

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


# --- AI-переформатування надиктованого тексту (feature/output-formats) --------

class RewriteWorker(QThread):
    """Фонове AI-переформатування тексту. Тримає RewriteGenerator для cancel()."""
    finished_ok = Signal(str)
    failed = Signal(str)
    was_cancelled = Signal()

    def __init__(self, generator, text, template_id, custom_prompt, parent=None):
        super().__init__(parent)
        self._gen = generator
        self._text = text
        self._template_id = template_id
        self._custom_prompt = custom_prompt

    def cancel(self):
        self._gen.cancel()

    def run(self):
        from whisper_core.protocol.service import RewriteCancelled
        try:
            text = self._gen.run(self._text, self._template_id,
                                 custom_prompt=self._custom_prompt)
            self.finished_ok.emit(text)
        except RewriteCancelled:
            self.was_cancelled.emit()
        except Exception as exc:                # noqa: BLE001
            self.failed.emit(str(exc))


class RewriteDialog(QDialog):
    """«Переформатувати…» надиктований текст локальною LLM: вибір шаблону
    (лист/задачі/рапорт/стисло) + опційний власний промт → переписаний текст із
    оглядом «Було/Стало» і кнопкою «Застосувати». Фонова генерація зі скасуванням;
    бекенд перевірено ПЕРЕД відкриттям (у картці). on_apply(new_text) — колбек
    застосування (оновлює картку + буфер обміну)."""

    def __init__(self, text, preset_id, on_apply=None, parent=None,
                 *, custom_models=None):
        super().__init__(parent)
        self._text = text
        self._preset_id = preset_id
        self._on_apply = on_apply
        self._custom_models = custom_models
        self._worker = None
        self._result = ""
        self.setWindowTitle(tr("rewrite_dialog_title"))
        self.setModal(True)
        self.setMinimumSize(560, 460)

        from whisper_core.protocol import rewrite as _rw
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(10)

        row = QHBoxLayout()
        cap = QLabel(tr("rewrite_mode_label"))
        row.addWidget(cap)
        self._mode = QComboBox()
        self._mode.setAccessibleName(tr("rewrite_mode_label"))
        for tid in _rw.TEMPLATE_IDS:
            self._mode.addItem(tr(f"rewrite_mode_{tid}"), tid)
        row.addWidget(self._mode, stretch=1)
        lay.addLayout(row)

        self._custom = QLineEdit()
        self._custom.setPlaceholderText(tr("rewrite_custom_placeholder"))
        self._custom.setAccessibleName(tr("rewrite_custom_placeholder"))
        lay.addWidget(self._custom)

        # огляд «Було / Стало»
        self._before_cap = QLabel(tr("rewrite_before"))
        self._before_cap.setProperty("muted", True)
        lay.addWidget(self._before_cap)
        self._before = QPlainTextEdit()
        self._before.setReadOnly(True)
        self._before.setPlainText(text or "")
        self._before.setAccessibleName(tr("rewrite_before"))
        self._before.setMaximumHeight(120)
        lay.addWidget(self._before)

        self._after_cap = QLabel(tr("rewrite_after"))
        self._after_cap.setProperty("muted", True)
        self._after_cap.hide()
        lay.addWidget(self._after_cap)
        self._after = QPlainTextEdit()
        self._after.setReadOnly(True)
        self._after.setAccessibleName(tr("rewrite_after"))
        self._after.hide()
        lay.addWidget(self._after, stretch=1)

        self._status = QLabel()
        self._status.setProperty("muted", True)
        self._status.setWordWrap(True)
        self._status.hide()
        lay.addWidget(self._status)
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setRange(0, 0)
        self._bar.hide()
        lay.addWidget(self._bar)

        btns = QHBoxLayout()
        self._run = QPushButton(tr("rewrite_run"))
        self._run.setAccessibleName(tr("rewrite_run"))
        self._run.clicked.connect(self._on_run)
        btns.addWidget(self._run)
        btns.addStretch()
        self._apply = QPushButton(tr("rewrite_apply"))
        self._apply.setAccessibleName(tr("rewrite_apply"))
        self._apply.setEnabled(False)
        self._apply.clicked.connect(self._on_apply_clicked)
        btns.addWidget(self._apply)
        self._close = QPushButton(tr("common_cancel"))
        self._close.clicked.connect(self.reject)
        btns.addWidget(self._close)
        lay.addLayout(btns)

    def _on_run(self):
        if self._worker is not None:
            return
        self._after.hide(); self._after_cap.hide()
        self._apply.setEnabled(False)
        self._result = ""
        self._status.setText(tr("rewrite_generating")); self._status.show()
        self._bar.show()
        self._run.setEnabled(False)

        from whisper_core.protocol.service import RewriteGenerator
        from whisper_core import paths
        generator = RewriteGenerator(
            self._preset_id, model_root=paths.protocol_models_dir(),
            custom_models=self._custom_models)
        self._worker = RewriteWorker(
            generator, self._text, self._mode.currentData(),
            self._custom.text().strip() or None)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.was_cancelled.connect(self._on_cancelled)
        self._worker.start()

    def _on_done(self, text: str):
        self._detach()
        self._bar.hide()
        self._run.setEnabled(True)
        self._result = text or ""
        self._status.hide()
        self._after.setPlainText(self._result)
        self._after_cap.show(); self._after.show()
        self._apply.setEnabled(bool(self._result.strip()))

    def _on_failed(self, msg: str):
        self._detach()
        self._bar.hide()
        self._run.setEnabled(True)
        self._status.setText(msg); self._status.show()

    def _on_cancelled(self):
        self._detach()
        self._bar.hide()
        self._run.setEnabled(True)
        self._status.setText(tr("rewrite_cancelled")); self._status.show()

    def _on_apply_clicked(self):
        if not self._result.strip():
            return
        if self._on_apply is not None:
            self._on_apply(self._result)
        self.accept()

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
