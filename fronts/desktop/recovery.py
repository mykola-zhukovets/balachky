"""Відновлення старту, коли пінованої ревізії моделі нема локально.

Замість краху з сирим HF-трейсбеком — маленький модальний діалог, що ПОКАЗУЄ
стан і чекає СВІДОМОГО кліку (той самий довірчий контракт, що онбординг):
  • ABSENT → єдина кнопка «Завантажити»; докачка — існуючим DownloadWorker.
  • OTHER_REVISION_PRESENT → вибір двома кнопками: офлайн «Використати наявну»
    (передаємо фактичний локальний sha у рушій) АБО «Завантажити піновану».

Жодної авто-докачки/QTimer: мережа лише по кліку, у видимому QThread. Воркер
від'єднуємо без блокуючого очікування (cancel + reaping), як FirstRunWizard.
"""
import logging

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar,
    QFrame, QFileDialog,
)

from whisper_core.models import (
    resolve_model_state, repo_for, revision_for, resolve_cache_dir,
    model_present, model_all_real, dereference_snapshot,
    PINNED_OK, OTHER_REVISION_PRESENT,
)
from .i18n import tr
from .onboarding import (DownloadWorker, _MB, _reap_worker,
                         _model_search_dirs)


class RecoveryDialog(QDialog):
    """exec() == Accepted → будувати Engine(cfg, revision_override=self.revision_override);
    Rejected → користувач відмовився, застосунок виходить (як скасований онбординг)."""

    def __init__(self, cfg, err, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("rec_title"))
        self.setMinimumWidth(540)
        self._cfg = cfg
        self._worker = None
        # None → рушій візьме пінований коміт (після докачки); sha → офлайн-старт
        # наявної ревізії (revision=None+local_files_only може не знайти без refs/main)
        self.revision_override = None

        state = resolve_model_state(cfg)
        self._local_sha = (state.revision
                           if state.state == OTHER_REVISION_PRESENT else None)

        outer = QVBoxLayout(self)
        card = QFrame()
        card.setProperty("card", True)
        lay = QVBoxLayout(card)
        lay.setSpacing(10)

        eyebrow = QLabel(tr("rec_eyebrow"))
        eyebrow.setProperty("eyebrow", True)
        lay.addWidget(eyebrow)
        title = QLabel(tr("rec_title"))
        title.setProperty("title", True)
        title.setWordWrap(True)
        lay.addWidget(title)
        body = QLabel(tr("rec_other_body") if self._local_sha
                      else tr("rec_absent_body"))
        body.setWordWrap(True)
        lay.addWidget(body)
        note = QLabel(tr("rec_offline_note"))
        note.setProperty("muted", True)
        note.setWordWrap(True)
        lay.addWidget(note)

        # прогрес докачки — прихований, доки не натиснуть «Завантажити»
        self._status = QLabel("")
        self._status.setProperty("strong", True)
        self._status.setWordWrap(True)
        self._status.hide()
        lay.addWidget(self._status)
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.hide()
        lay.addWidget(self._bar)
        self._info = QLabel("")
        self._info.setProperty("muted", True)
        self._info.setWordWrap(True)
        self._info.hide()
        lay.addWidget(self._info)

        # результат «обрати теку» / «перевірити комп'ютер»: коротка приглушена
        # нотатка («у теці моделі немає» / «не знайшли — можна завантажити»)
        self._pick_note = QLabel("")
        self._pick_note.setProperty("muted", True)
        self._pick_note.setWordWrap(True)
        self._pick_note.hide()
        lay.addWidget(self._pick_note)

        self._use_btn = QPushButton(tr("rec_use_existing"))
        self._use_btn.clicked.connect(self._use_existing)
        # офлайн-альтернативи докачці: вказати теку з моделлю АБО пошукати її
        # у стандартних місцях цього комп'ютера (та сама логіка, що в майстрі)
        self._pick_btn = QPushButton(tr("rec_pick_folder"))
        self._pick_btn.clicked.connect(self._pick_folder)
        self._scan_btn = QPushButton(tr("rec_scan"))
        self._scan_btn.clicked.connect(self._scan_computer)
        self._dl_btn = QPushButton(tr("rec_download_pinned"))
        self._dl_btn.setProperty("accent", True)
        self._dl_btn.clicked.connect(self._start_download)
        self._cancel = QPushButton(tr("common_cancel"))
        self._cancel.clicked.connect(self._cancel_download)
        self._cancel.hide()
        actions = QVBoxLayout()
        actions.setSpacing(8)
        if self._local_sha:
            actions.addWidget(self._use_btn)
        offline_row = QHBoxLayout()
        offline_row.setSpacing(8)
        offline_row.addWidget(self._pick_btn)
        offline_row.addWidget(self._scan_btn)
        actions.addLayout(offline_row)
        actions.addWidget(self._cancel)
        actions.addWidget(self._dl_btn)
        lay.addLayout(actions)

        outer.addWidget(card)
        outer.addStretch()

    # --- само-лікування знімка перед прийняттям ---
    def _heal_if_symlinks(self, revision):
        """Перед accept: замінити символьні лінки у знімку РЕАЛЬНИМИ копіями.
        На встановленому (без підпису) exe модель із лінків HF-кешу не
        відкривається (WinError 448 «untrusted mount point») — і «Обрати теку →
        D:\\hf-cache (лінки)» знову показало б цей самий діалог. Дереференс це
        знімає БЕЗ мережі. revision: None → пінований коміт; sha → наявна ревізія.
        Ідемпотентно (не-лінки пропускає) і безпечно: будь-який збій лишає знімок
        як є — далі спрацює звичайна докачка/повтор Engine у app.py."""
        rev = (revision if revision is not None
               else revision_for(self._cfg.model_name))
        try:
            dereference_snapshot(resolve_cache_dir(self._cfg.model_dir),
                                 repo_for(self._cfg.model_name), rev)
        except Exception:
            logging.exception("Дереференс у відновленні впав — лишаємо знімок як є")

    # --- офлайн: узяти наявну локальну ревізію ---
    def _use_existing(self):
        self.revision_override = self._local_sha
        self._heal_if_symlinks(self._local_sha)   # лінки → реальні файли, щоб відкрилось
        logging.info("Відновлення: старт із наявною локальною ревізією %s",
                     self._local_sha)
        self.accept()

    # --- офлайн: вказати теку, де вже лежить модель ---
    def _pick_folder(self):
        """Обрати теку з уже завантаженою моделлю. Знайшли там пінований знімок
        → старт без докачки; інший повний знімок → офлайн-старт цієї ревізії;
        нічого → лагідно кажемо, що моделі там немає (конфіг не чіпаємо)."""
        path = QFileDialog.getExistingDirectory(
            self, tr("common_model_folder"), self._cfg.model_dir or "")
        if not path:
            return
        prev = self._cfg.model_dir
        self._cfg.model_dir = path
        state = resolve_model_state(self._cfg)
        if state.state == PINNED_OK:
            self._persist_and_accept(None)
        elif state.state == OTHER_REVISION_PRESENT:
            self._persist_and_accept(state.revision)
        else:
            self._cfg.model_dir = prev        # тека без моделі — конфіг лишаємо
            self._note(tr("rec_not_in_folder"))

    # --- офлайн: пошук моделі у стандартних місцях цього комп'ютера ---
    def _scan_computer(self):
        """Пошук уже завантаженої моделі поточного типу у стандартних місцях
        (тека Балачок, кеш HuggingFace) — та сама логіка, що в майстрі
        (_find_existing/_model_search_dirs). Знайшли → старт без докачки.
        Кандидати з РЕАЛЬНИМИ файлами мають пріоритет над symlink-знімками
        HF-кешу (frozen exe лінки не читає, WinError 448) — той самий вибір,
        що онбординг (_find_existing)."""
        repo, rev = repo_for(self._cfg.model_name), revision_for(self._cfg.model_name)
        found = [d for d in _model_search_dirs() if model_present(d, repo, rev)]
        if not found:
            self._note(tr("rec_scan_none"))
            return
        d = next((c for c in found if model_all_real(c, repo, rev)), found[0])
        self._cfg.model_dir = d
        self._persist_and_accept(None)

    def _persist_and_accept(self, revision):
        """Записати нову теку моделі в конфіг і прийняти діалог: застосунок
        завантажить модель звідти, без докачки. self._cfg — той самий об'єкт,
        що бере Engine у app.py, тож нова тека діє одразу і переживе перезапуск."""
        self.revision_override = revision
        self._cfg.save()
        self._heal_if_symlinks(revision)  # лінки → реальні файли, щоб точно відкрилось
        logging.info("Відновлення: модель у теці %s (revision_override=%s)",
                     self._cfg.model_dir, revision)
        self.accept()

    def _note(self, text: str):
        """Коротка приглушена нотатка під кнопками (без закриття діалогу)."""
        self._pick_note.setText(text)
        self._pick_note.show()

    # --- докачка пінованої ревізії (єдиний мережевий вихід, по кліку) ---
    def _start_download(self):
        self._status.setText(tr("onb_dl_intro"))
        self._status.show()
        self._info.setText(tr("onb_dl_connecting"))
        self._info.show()
        self._bar.setRange(0, 0)          # обсяг ще невідомий
        self._bar.show()
        self._pick_note.hide()
        self._use_btn.hide()
        self._pick_btn.hide()
        self._scan_btn.hide()
        self._dl_btn.hide()
        self._cancel.setText(tr("common_cancel"))
        self._cancel.setEnabled(True)
        self._cancel.show()
        self._detach_worker()             # retry завжди будує СВІЖИЙ воркер
        self._worker = DownloadWorker(repo_for(self._cfg.model_name),
                                      resolve_cache_dir(self._cfg.model_dir),
                                      revision_for(self._cfg.model_name))
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.start()

    def _detach_worker(self):
        """Від'єднати воркер без блокуючого очікування (як у FirstRunWizard):
        від'єднати сигнали, попросити скасування, віддати потік у reaping."""
        w = self._worker
        self._worker = None
        if w is None:
            return
        try:
            w.progress.disconnect()
            w.finished_ok.disconnect()
            w.failed.disconnect()
            w.cancelled.disconnect()
        except (RuntimeError, TypeError):
            pass
        w.cancel()
        _reap_worker(w)

    def _cancel_download(self):
        if self._worker is not None:
            # оптимістично: одразу у стан «скасовано», не блокуючи GUI-потік
            self._detach_worker()
            self._bar.setRange(0, 1000)
            self._status.setText(tr("onb_dl_cancelled"))
            self._cancel.hide()
            self._restore_buttons()

    def _on_progress(self, done, total):
        if total:
            self._bar.setRange(0, 1000)
            self._bar.setValue(min(1000, int(done * 1000 / total)))
            self._info.setText(tr("onb_dl_progress",
                                  done=done // _MB, total=total // _MB))
        else:
            self._info.setText(tr("onb_dl_progress_indet", done=done // _MB))

    def _on_done(self):
        self._bar.setRange(0, 1000)
        self._bar.setValue(1000)
        self.revision_override = None     # тепер пінований знімок на місці
        self.accept()

    def _on_failed(self, msg: str):
        logging.warning("Відновлення моделі завершилось помилкою: %s", msg)
        self._bar.setRange(0, 1000)
        self._status.setText(tr("onb_dl_failed"))
        self._info.setText(tr("onb_dl_failed_detail"))
        self._cancel.hide()
        self._restore_buttons()

    def _on_cancelled(self):
        self._bar.setRange(0, 1000)
        self._status.setText(tr("onb_dl_cancelled"))
        self._cancel.hide()
        self._restore_buttons()

    def _restore_buttons(self):
        """Після збою/скасування — знову дати вибір (не закривати діалог)."""
        if self._local_sha:
            self._use_btn.show()
        self._pick_btn.show()
        self._scan_btn.show()
        self._dl_btn.setText(tr("onb_retry"))
        self._dl_btn.show()

    def reject(self):
        """Закриття (X / Esc / «Скасувати» без докачки): від'єднати воркер і
        вийти негайно — без блокуючого wait() у GUI-потоці (потік догорить
        сам у reaping-реєстрі)."""
        self._detach_worker()
        super().reject()
