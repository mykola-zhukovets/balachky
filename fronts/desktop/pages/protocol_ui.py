"""UI AI-протоколу наради (feature/ai-protocol): завантаження моделі з прогресом,
фонова генерація зі скасуванням, перегляд результату + копіювати/зберегти md/docx.

Qt-обгортка над whisper_core.protocol.* (service/model_manager). Уся важка робота —
у фоні (QThread), UI лишається чуйним; шлях без моделі (кнопка вимкнена, consent)
працює без завантажених гігабайтів.
"""
from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QTextEdit, QLineEdit, QFileDialog, QApplication, QComboBox, QPlainTextEdit,
    QMessageBox, QCheckBox)

from ..i18n import tr, human_size, format_decimal, format_duration
from ..onboarding import _reap_worker
from .. import download_manager as dlmgr

_MB = 1024 * 1024

# Не перемальовувати напис поступу частіше, ніж раз на цей інтервал: на 4.6 ГБ
# файлі блок читання 256 КБ дає ~18 000 сигналів поступу; без дроселя кожен з
# них форсує setText()+relayout у GUI-потоці (аудит 30.07.2026).
_PROGRESS_UI_INTERVAL_S = 0.2


_human_size = human_size


def _progress_text(done: int, total: int, speed_bps) -> str:
    """Компонує рядок поступу з відомих частин. Ніколи не вигадує те, чого не
    можемо порахувати: без total — без відсотка; без виміряної швидкості —
    без часу, що лишився (аудит 30.07.2026, §5). Спільна для
    ProtocolModelDownloadDialog (сайдбар/модальне вікно) і
    ModelDownloadWaitDialog (§5 спеки — «Створити протокол» під час качання)."""
    if total:
        size_part = tr("protocol_model_downloading_of",
                       done=done // _MB, total=total // _MB)
        percent = min(100.0, done * 100 / total)
        size_part += " " + tr("protocol_model_downloading_percent",
                              percent=format_decimal(percent, 1))
    else:
        size_part = tr("protocol_model_downloading_done_only", done=done // _MB)

    parts = [tr("protocol_model_downloading_prefix") + " " + size_part]

    if speed_bps:
        speed_mb_s = speed_bps / _MB
        parts.append(tr("protocol_model_downloading_speed",
                        speed=format_decimal(speed_mb_s, 1)))
        if total and speed_bps > 0:
            eta_s = (total - done) / speed_bps
            parts.append(tr("protocol_model_downloading_eta",
                            eta=format_duration(eta_s)))
    else:
        parts.append(tr("protocol_model_downloading_speed_calc"))

    return " • ".join(parts)


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
    """Поступ докачки моделі протоколу (пресет або власна інтернет-модель).

    НЕмодальна (E-бекграунд-докачка, аудит 31.07.2026): закриття вікна («✕»,
    Esc чи кнопка «У фон») НЕ скасовує завантаження — воно триває через
    спільний DownloadManager, і людина продовжує диктувати чи гортати наради.
    Лишень явна кнопка «Скасувати» перериває докачку й прибирає частковий файл.

    Сам процес якісно якнайшвидше делегується DownloadManager.instance():
    якщо ця сама модель уже качається (ALREADY_THIS) — діалог просто
    приєднується до наявного прогресу; якщо качається ІНША модель (BUSY_OTHER)
    — питаємо користувача, чи скасувати те завантаження (одне одночасно, §3.2
    спеки, захист каналу мережі й диска)."""

    def __init__(self, target_dir, *, preset_id=None, custom=None, force=False,
                 label="", parent=None):
        super().__init__(parent)
        self._target = target_dir
        self._key = dlmgr.key_for(target_dir)
        self._preset_id = preset_id
        self._custom = custom
        self._force = force
        self._label = label
        self._manager = dlmgr.DownloadManager.instance()
        # Дросель перемальовування (throttling, §5.3 аудиту): останній момент
        # оновлення тексту/бару і опорна точка (час+байти) для виміру швидкості.
        self._last_ui_update = 0.0
        self._speed_ref_time = None
        self._speed_ref_bytes = 0
        self._speed_bps = None
        self.setWindowTitle(tr("protocol_model_consent_title"))
        self.setModal(False)
        self.setMinimumWidth(440)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(14)
        self._status = QLabel(self._format_status(0, 0))
        self._status.setProperty("strong", True)
        self._status.setWordWrap(True)
        lay.addWidget(self._status)
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        lay.addWidget(self._bar)
        hint = QLabel(tr("protocol_model_download_bg_hint"))
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        lay.addWidget(hint)
        btns = QHBoxLayout()
        btns.addStretch()
        self._background_btn = QPushButton(tr("protocol_model_download_background"))
        self._background_btn.setAccessibleName(tr("protocol_model_download_background"))
        # «У фон» = просто ховає вікно — воркер лишається під DownloadManager,
        # докачка триває (індикатор у сайдбарі показує стан далі).
        self._background_btn.clicked.connect(self.hide)
        btns.addWidget(self._background_btn)
        self._cancel = QPushButton(tr("common_cancel"))
        self._cancel.clicked.connect(self._on_cancel_clicked)
        btns.addWidget(self._cancel)
        lay.addLayout(btns)

        self._manager.progress.connect(self._on_manager_progress)
        self._manager.finished_ok.connect(self._on_manager_finished_ok)
        self._manager.failed.connect(self._on_manager_failed)
        self._manager.cancelled.connect(self._on_manager_cancelled)
        self._begin()

    # ------------------------------------------------------- запуск / приєднання
    def _begin(self):
        self._bar.setRange(0, 0)
        result = self._manager.start_download(
            self._target, preset_id=self._preset_id, custom=self._custom,
            force=self._force, label=self._label)
        if result == dlmgr.BUSY_OTHER:
            other = self._manager.active_label() or tr("protocol_model_other_label")
            if QMessageBox.question(
                    self, tr("protocol_model_consent_title"),
                    tr("protocol_model_download_busy_other", other=other)
            ) == QMessageBox.Yes:
                # Скасовуємо ЧУЖЕ завантаження явно (частковий файл того
                # пресета прибирається) — своє користувач запустить повторним
                # натиском «Завантажити», коли попереднє звільнить менеджер.
                self._manager.cancel_download()
            self._finalize_close()
            return
        if result == dlmgr.ALREADY_THIS:
            progress = self._manager.progress_for(self._target)
            if progress is not None:
                self._on_progress(*progress)
            return
        # STARTED — свіжий прогрес; текст «0 з ?» лишається, доки не прийде сигнал.

    def _on_cancel_clicked(self):
        self._manager.cancel_download(self._target)
        self._finalize_close()

    # ------------------------------------------------------- сигнали менеджера
    def _on_manager_progress(self, key, done, total):
        if key == self._key:
            self._on_progress(done, total)

    def _on_manager_finished_ok(self, key):
        if key == self._key:
            self.accept()

    def _on_manager_failed(self, key, msg):
        if key == self._key:
            self._on_failed(msg)

    def _on_manager_cancelled(self, key):
        if key == self._key:
            self._finalize_close()

    def _on_progress(self, done, total):
        now = time.monotonic()
        is_final = bool(total) and done >= total
        # Дросель: пропускаємо перемальовування, якщо не минуло достатньо часу
        # з попереднього — крім останнього сигналу (100%), який мусить дійти.
        if not is_final and (now - self._last_ui_update) < _PROGRESS_UI_INTERVAL_S:
            return
        self._last_ui_update = now

        if self._speed_ref_time is None:
            self._speed_ref_time = now
            self._speed_ref_bytes = done
        else:
            elapsed = now - self._speed_ref_time
            if elapsed >= _PROGRESS_UI_INTERVAL_S:
                self._speed_bps = (done - self._speed_ref_bytes) / elapsed
                self._speed_ref_time = now
                self._speed_ref_bytes = done

        if total:
            self._bar.setRange(0, 1000)
            self._bar.setValue(min(1000, int(done * 1000 / total)))
        self._status.setText(self._format_status(done, total))

    def _format_status(self, done: int, total: int) -> str:
        """Компонує рядок поступу з відомих частин. Ніколи не вигадує те, чого
        не можемо порахувати: без total — без відсотка; без виміряної
        швидкості — без часу, що лишився (аудит 30.07.2026, §5)."""
        return _progress_text(done, total, self._speed_bps)

    def _on_failed(self, msg: str):
        logging.warning("Докачка моделі протоколу не вдалась: %s", msg)
        self._status.setText(tr("protocol_model_failed", error=msg))
        self._cancel.setText(tr("common_close"))
        self._background_btn.hide()

    def _disconnect_manager(self):
        for sig, slot in (
                (self._manager.progress, self._on_manager_progress),
                (self._manager.finished_ok, self._on_manager_finished_ok),
                (self._manager.failed, self._on_manager_failed),
                (self._manager.cancelled, self._on_manager_cancelled)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _finalize_close(self):
        """Завантаження цього ключа справді завершилось (готово/збій/скасовано
        деінде) — вікно більше нема сенсу тримати живим, на відміну від
        Escape/«✕», які лише ховають його (докачка триває у фоні)."""
        self._disconnect_manager()
        super().reject()

    def reject(self):
        # Escape / «✕» / «У фон»: НЕ скасовує докачку (E-бекграунд-докачка,
        # аудит 31.07.2026) — лише ховає вікно, докачка триває через
        # DownloadManager і видно в індикаторі сайдбара.
        self.hide()

    def accept(self):
        self._disconnect_manager()
        super().accept()


class ModelDownloadWaitDialog(QDialog):
    """§5 спеки: «Створити протокол» / «Спитати про нараду» натиснуто, поки
    активна модель ще якраз якісно завантажується у фоні. Замість мовчазної
    відмови чи технічної помилки — чесний прогрес і вибір: скасувати качання,
    зачекати у фоні, чи автоматично почати дію одразу після завершення."""

    def __init__(self, target_dir, *, on_ready=None, parent=None):
        super().__init__(parent)
        self._key = dlmgr.key_for(target_dir)
        self._target = target_dir
        self._on_ready = on_ready
        self._manager = dlmgr.DownloadManager.instance()
        self._speed_bps = None
        self._speed_ref_time = None
        self._speed_ref_bytes = 0
        self.setWindowTitle(tr("protocol_model_wait_title"))
        self.setModal(False)
        self.setMinimumWidth(420)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(12)
        intro = QLabel(tr("protocol_model_wait_intro"))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self._status = QLabel()
        self._status.setProperty("strong", True)
        self._status.setWordWrap(True)
        lay.addWidget(self._status)
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        lay.addWidget(self._bar)
        self._autostart = QCheckBox(tr("protocol_model_wait_autostart"))
        self._autostart.setChecked(self._on_ready is not None)
        self._autostart.setEnabled(self._on_ready is not None)
        lay.addWidget(self._autostart)

        btns = QHBoxLayout()
        self._cancel_dl = QPushButton(tr("protocol_model_wait_cancel_download"))
        self._cancel_dl.clicked.connect(self._on_cancel_download)
        btns.addWidget(self._cancel_dl)
        btns.addStretch()
        self._close = QPushButton(tr("protocol_model_wait_background"))
        self._close.clicked.connect(self._finalize_close)
        btns.addWidget(self._close)
        lay.addLayout(btns)

        self._manager.progress.connect(self._on_manager_progress)
        self._manager.finished_ok.connect(self._on_manager_finished_ok)
        self._manager.failed.connect(self._on_manager_failed)
        self._manager.cancelled.connect(self._on_manager_cancelled)
        progress = self._manager.progress_for(self._target)
        self._on_progress(*(progress or (0, 0)))

    def _on_cancel_download(self):
        self._manager.cancel_download(self._target)
        self._finalize_close()

    def _on_manager_progress(self, key, done, total):
        if key == self._key:
            self._on_progress(done, total)

    def _on_progress(self, done, total):
        now = time.monotonic()
        if self._speed_ref_time is None:
            self._speed_ref_time = now
            self._speed_ref_bytes = done
        else:
            elapsed = now - self._speed_ref_time
            if elapsed >= _PROGRESS_UI_INTERVAL_S:
                self._speed_bps = (done - self._speed_ref_bytes) / elapsed
                self._speed_ref_time = now
                self._speed_ref_bytes = done
        if total:
            self._bar.setRange(0, 1000)
            self._bar.setValue(min(1000, int(done * 1000 / total)))
        else:
            self._bar.setRange(0, 0)
        self._status.setText(_progress_text(done, total, self._speed_bps))

    def _on_manager_finished_ok(self, key):
        if key != self._key:
            return
        autostart = self._autostart.isChecked() and self._on_ready is not None
        self._disconnect_manager()
        super().accept()
        if autostart:
            self._on_ready()

    def _on_manager_failed(self, key, _msg):
        if key == self._key:
            self._finalize_close()

    def _on_manager_cancelled(self, key):
        if key == self._key:
            self._finalize_close()

    def _disconnect_manager(self):
        for sig, slot in (
                (self._manager.progress, self._on_manager_progress),
                (self._manager.finished_ok, self._on_manager_finished_ok),
                (self._manager.failed, self._on_manager_failed),
                (self._manager.cancelled, self._on_manager_cancelled)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _finalize_close(self):
        self._disconnect_manager()
        super().reject()

    def reject(self):
        # «✕»/Escape — «Зачекати у фоні»: докачка триває, вікно просто ховаємо.
        self.hide()


# --- додавання власної моделі з інтернету ----------------------------------
class HFModelDialog(QDialog):
    """Додати власну модель з інтернету за ідентифікатором репозиторію
    (власник/назва), ім'ям GGUF-файлу, commit і SHA-256. Після Accepted —
    .result_data = (repo_id, filename, revision, sha256)."""

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

        revision_cap = QLabel(tr("protocol_model_hf_revision"))
        revision_cap.setWordWrap(True)
        lay.addWidget(revision_cap)
        self._revision = QLineEdit()
        self._revision.setPlaceholderText(
            tr("protocol_model_hf_revision_ph"))
        self._revision.setAccessibleName(
            tr("protocol_model_hf_revision"))
        lay.addWidget(self._revision)

        sha_cap = QLabel(tr("protocol_model_hf_sha256"))
        sha_cap.setWordWrap(True)
        lay.addWidget(sha_cap)
        self._sha256 = QLineEdit()
        self._sha256.setPlaceholderText(
            tr("protocol_model_hf_sha256_ph"))
        self._sha256.setAccessibleName(tr("protocol_model_hf_sha256"))
        lay.addWidget(self._sha256)

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
        revision = self._revision.text().strip().lower()
        sha256 = self._sha256.text().strip().lower()
        if (not mm.is_repo_id(repo) or not mm.is_gguf_name(fname)
                or not mm.is_commit_revision(revision)
                or not mm.is_sha256(sha256)):
            self._err.setText(tr("protocol_model_hf_invalid"))
            self._err.show()
            return
        self.result_data = (repo, fname, revision, sha256)
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
            # Аудит чесності (31.07, знахідка 2): тихий except ковтав збій
            # запису — людина бачила текст на екрані й вважала, що файл
            # збережено. Тепер помилка видима (з шляхом і дією) і в журналі.
            logging.exception("Не вдалося автозберегти protocol.md")
            expected_path = self._session_dir / service.PROTOCOL_FILENAME
            self._saved.setText(
                tr("protocol_autosave_failed", path=str(expected_path)))
            self._saved.show()
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
