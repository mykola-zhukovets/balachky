"""Майстер першого запуску: привітання → модель → мова → докачка моделі.

Головне для нових користувачів: докачка 1.6-3 ГБ не має бути мовчазним фризом —
тут вона з прогресом у мегабайтах, скасуванням і повтором після збою мережі.

Repo моделі береться з faster_whisper.utils._MODELS (у встановленій 1.2.1
large-v3-turbo → mobiuslabsgmbh/faster-whisper-large-v3-turbo, НЕ Systran):
качаємо саме те і саме туди (cache_dir → models--org--name/snapshots/...),
що потім шукатиме WhisperModel(download_root=...) — інакше друга докачка.

Дев-кейс: якщо задано env WHISPER_TYPER_MODELS, майстер не показується взагалі
(main() вважає onboarded) — моделі вже є у дев-кеші.
"""
import logging
import hashlib
import os
import shutil
import threading
import urllib.request
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QRadioButton,
    QButtonGroup, QPushButton, QProgressBar, QStackedWidget, QFileDialog,
    QFrame, QCheckBox, QMessageBox,
)

from .crash import anonymize_path
from .i18n import tr, human_size
from .hotkey import pretty
from .links import GITHUB_URL, SUPPORT_URL   # єдине джерело зовнішніх посилань автора
# Файлова детекція моделі живе в ядрі (whisper_core.models) — щоб engine міг
# нею користуватись без залежності від PySide6. Тут реекспорт для сумісності.
from whisper_core.models import (model_present, model_all_real,
                                 model_snapshot_usable,
                                 dereference_snapshot, repo_for, revision_for,
                                 resolve_cache_dir, model_download_manifest)
from whisper_core import netlog   # доказова офлайновість: журнал вихідних з'єднань
# Рушій озвучення є не в кожній збірці (полегшений інсталятор іде без нього) —
# імпорт відкладений у функцію, щоб майстер не тягнув TTS-стек на старті.


def _tts_engine_available() -> bool:
    from whisper_core.tts.sidecar import engine_available
    return engine_available()

# лише файли, потрібні рушію (як у faster_whisper.utils.download_model)
_ALLOW_PATTERNS = ["config.json", "preprocessor_config.json", "model.bin",
                   "tokenizer.json", "vocabulary.*"]
_MB = 1024 * 1024


def _has_network() -> bool:
    import socket
    try:
        netlog.record("1.1.1.1", kind=netlog.OTHER, allowed=True,
                      detail="connectivity-check")
        socket.create_connection(("1.1.1.1", 53), timeout=1.5).close()
        return True
    except Exception:
        return False



def default_model_dir() -> str:
    return str(Path(os.environ.get("LOCALAPPDATA") or Path.home())
               / "Balachky" / "models")


def _model_search_dirs():
    """Стандартні місця, де вже може лежати завантажена модель: тека Балачок і
    кеш HuggingFace (resolve_cache_dir(None) чтить HF_HOME/HF_HUB_CACHE, плюс
    типовий ~/.cache/huggingface/hub). Порядок збережено, дублі прибрано."""
    dirs = [default_model_dir(),
            resolve_cache_dir(None),
            str(Path.home() / ".cache" / "huggingface" / "hub")]
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def check_free_space(target_dir: str | Path, required_bytes: int):
    """Перевірка вільного місця на диску ДО початку завантаження.
    Якщо місця замало — кидаємо ранню помилку з числами «потрібно / є».
    """
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target)
    if usage.free < required_bytes:
        req = human_size(required_bytes)
        avail = human_size(usage.free)
        raise OSError(tr("onb_err_disk_full", required=req, available=avail))


def _sha256_of(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def resumable_download_file(url: str, destination_path: Path, *,
                            expected_size: int, expected_sha256: str,
                            progress_cb=None, cancel_check=None) -> None:
    """Автономне завантаження одного файла через HTTP з підтримкою Range (206)
    у стабільний тимчасовий файл <path>.incomplete БЕЗ підміни внутрішніх API.
    """
    if len(expected_sha256) != 64:
        raise ValueError("Expected SHA-256 must contain 64 hex characters")
    expected_sha256 = expected_sha256.lower()
    if destination_path.exists():
        if (destination_path.stat().st_size == expected_size
                and _sha256_of(destination_path) == expected_sha256):
            return
    incomplete_path = destination_path.parent / (destination_path.name + ".incomplete")
    incomplete_path.parent.mkdir(parents=True, exist_ok=True)
    done = incomplete_path.stat().st_size if incomplete_path.exists() else 0
    if done == expected_size:
        if _sha256_of(incomplete_path) == expected_sha256:
            os.replace(incomplete_path, destination_path)
            return
        done = 0
        incomplete_path.unlink(missing_ok=True)
    elif done > expected_size:
        done = 0
        incomplete_path.unlink(missing_ok=True)

    headers = {"User-Agent": "Balachky/1.0"}
    if done > 0:
        headers["Range"] = f"bytes={done}-"

    netlog.record_url(url, kind=netlog.MODEL, detail=destination_path.name)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = getattr(resp, "status", resp.getcode())
            append = False
            if done > 0 and status == 206:
                append = True
            elif done > 0:
                done = 0

            mode = "ab" if append else "wb"
            received = done
            with incomplete_path.open(mode) as out:
                while True:
                    if cancel_check is not None and cancel_check():
                        raise InterruptedError()
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    received += len(chunk)
                    if progress_cb:
                        progress_cb(received, expected_size)
        if incomplete_path.stat().st_size != expected_size:
            raise OSError(f"Downloaded file size mismatch: {incomplete_path.stat().st_size} != {expected_size}")
        got = _sha256_of(incomplete_path)
        if got != expected_sha256:
            incomplete_path.unlink(missing_ok=True)
            raise OSError(
                f"Downloaded file SHA-256 mismatch: {got} != {expected_sha256}")
        os.replace(incomplete_path, destination_path)
    except InterruptedError:
        raise
    except Exception as exc:
        raise OSError(f"Download failed for {url}: {exc}") from exc



# --- безпечний час життя від'єднаних воркерів ---
# Від'єднаний (detached) DownloadWorker не має Qt-parent, тож діалог може
# знищитись негайно. Щоб Qt не зруйнував QThread під час роботи
# («QThread: Destroyed while thread is still running»), тримаємо сильне
# посилання тут, доки потік не догорить, і лише тоді дозволяємо deleteLater.
_active_workers = set()


def _reap_worker(worker):
    if worker is None:
        return
    _active_workers.add(worker)

    def _cleanup():
        if worker in _active_workers:
            _active_workers.discard(worker)
            worker.deleteLater()

    worker.finished.connect(_cleanup)   # QThread.finished (run() повернувся)
    if worker.isFinished():             # уже догорів до під'єднання — прибрати
        _cleanup()


def drain_workers(wait_ms=400):
    """Вихід застосунку: не лишити жодного живого від'єднаного QThread до
    teardown Qt (інакше «QThread: Destroyed while thread is still running» →
    abort). Скасований воркер живе до ~30с read-timeout; на виході це неприйнятно
    — коротко чекаємо, потім terminate. Знімок set бо reaper (_cleanup через
    QThread.finished) мутує оригінал; guard на кожен воркер — teardown не має
    впасти сам."""
    for w in list(_active_workers):
        try:
            w.cancel()
        except Exception:
            pass
    for w in list(_active_workers):
        try:
            if w.isRunning() and not w.wait(wait_ms):
                w.terminate()
                w.wait(200)
        except Exception:
            pass


class DownloadWorker(QThread):
    """Докачує модель у кеш-теку в окремому потоці; прогрес — Qt-сигналами.

    progress несе (завантажено_байт, всього_байт) як object: Python-int,
    бо розміри моделей не влазять у 32-бітний int Qt-сигналу.
    Скасування: прапорець перевіряється у tqdm.update() на кожному шматку;
    недокачане не чистимо — і не дозволяємо чистити huggingface_hub
    (_keep_partial_downloads), щоб наступна спроба продовжила з місця обриву.
    """
    progress = Signal(object, object)
    finished_ok = Signal()
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, repo_id: str, cache_dir: str, revision=None, parent=None):
        super().__init__(parent)
        self._repo_id = repo_id
        self._cache_dir = cache_dir
        self._revision = revision            # пінований коміт (supply-chain)
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        try:
            approx_size = 3_100_000_000 if "large-v3" in self._repo_id and "turbo" not in self._repo_id else 1_600_000_000
            check_free_space(self._cache_dir, approx_size)
            # доказова офлайновість: єдиний легітимний вихід — завантаження моделі
            netlog.record("huggingface.co", kind=netlog.MODEL, allowed=True,
                          detail=self._repo_id)

            rev = self._revision
            if rev is None:
                for model_name in ("small", "medium", "large-v3-turbo",
                                   "large-v3"):
                    if repo_for(model_name) == self._repo_id:
                        rev = revision_for(model_name)
                        break
            manifest = model_download_manifest(self._repo_id, rev)
            snap_dir = Path(self._cache_dir) / ("models--" + self._repo_id.replace("/", "--")) / "snapshots" / rev
            snap_dir.mkdir(parents=True, exist_ok=True)

            for asset in manifest:
                if self._cancel.is_set():
                    raise InterruptedError()
                filename = asset.filename
                file_dest = snap_dir / filename
                url = f"https://huggingface.co/{self._repo_id}/resolve/{rev}/{filename}"
                resumable_download_file(
                    url,
                    file_dest,
                    expected_size=asset.size,
                    expected_sha256=asset.sha256,
                    progress_cb=lambda done, total: self.progress.emit(done, total),
                    cancel_check=self._cancel.is_set
                )

            refs_dir = snap_dir.parent.parent / "refs"
            refs_dir.mkdir(parents=True, exist_ok=True)
            (refs_dir / "main").write_text(rev, encoding="utf-8")
        except Exception as e:
            if self._cancel.is_set():
                logging.info("Докачку моделі %s скасовано", self._repo_id)
                self.cancelled.emit()
            else:
                logging.error("Не вдалося докачати модель %s: %s",
                              self._repo_id, e)
                self.failed.emit(str(e))
        else:
            logging.info("Модель %s готова у %s", self._repo_id,
                        anonymize_path(self._cache_dir))
            self.finished_ok.emit()



class ExtraComponentWorker(QThread):
    """Докачує додатковий компонент (діаризація, протокол, пунктуатор, TTS) послідовно."""
    progress = Signal(object, object)
    finished_ok = Signal()
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, comp_id: str, model_dir: str, voice_root=None, parent=None):
        super().__init__(parent)
        self.comp_id = comp_id
        self.model_dir = model_dir
        self.voice_root = voice_root
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        try:
            def _progress_cb(done, total):
                if self._cancel.is_set():
                    raise InterruptedError()
                self.progress.emit(done, total)

            if self.comp_id == "diarization":
                import whisper_core.meeting.diarization_models as diar_models
                check_free_space(self.model_dir, diar_models.TOTAL_DOWNLOAD_BYTES)
                diar_models.download_and_install(self.model_dir, progress_cb=_progress_cb, cancel_check=self._cancel.is_set)
            elif self.comp_id == "protocol":
                import whisper_core.protocol.model_manager as protocol_mm
                import whisper_core.paths as paths
                # УВАГА: якісна тека САМЕ пресета "fast" (root/fast), не
                # спільний корінь усіх пресетів — інакше файл лягав би туди,
                # де його ніколи не знайде mm.resolve()/model_available() з
                # решти програми, і модель мовчки лишалась би «не завантажена»
                # (знайдено аудитом 31.07.2026 разом зі спекою бекграунд-докачки).
                proto_dir = paths.protocol_model_dir("fast")
                sz = protocol_mm.PRESETS["fast"].approx_size_bytes
                check_free_space(proto_dir, sz)
                protocol_mm.download_and_install(proto_dir, "fast", progress_cb=_progress_cb, cancel_check=self._cancel.is_set)
            elif self.comp_id == "punctuator":
                import whisper_core.punctuator as punc
                import whisper_core.paths as paths
                punc_dir = paths.punctuator_model_dir()
                sz = getattr(punc, "APPROX_SIZE_BYTES", 234_000_000)
                check_free_space(punc_dir, sz)
                punc.download_and_install(punc_dir, progress_cb=_progress_cb, cancel_check=self._cancel.is_set)
            elif self.comp_id == "tts":
                import whisper_core.tts.voices as tts_voices
                sz = tts_voices.VOICE_PRESETS["styletts2_ua"].approx_size_bytes

                check_free_space(self.model_dir, sz)
                tts_voices.download_and_install("styletts2_ua", root=self.voice_root, progress_cb=_progress_cb, cancel_check=self._cancel.is_set)
        except InterruptedError:
            logging.info("Докачку компонента %s скасовано", self.comp_id)
            self.cancelled.emit()
        except Exception as e:
            if self._cancel.is_set():
                logging.info("Докачку компонента %s скасовано", self.comp_id)
                self.cancelled.emit()
            else:
                logging.error("Не вдалося докачати компонент %s: %s", self.comp_id, e)
                self.failed.emit(str(e))
        else:
            logging.info("Компонент %s готовий", self.comp_id)
            self.finished_ok.emit()


class GpuDownloadWorker(QThread):
    """Докачує CUDA-рантайм (cuBLAS) в окремому потоці; той самий API сигналів,
    що DownloadWorker моделі. Уся мережа/розпакування — у whisper_core.cuda_runtime;
    тут лише маршалінг прогресу і скасування у Qt. (feature/gpu)"""
    progress = Signal(object, object)        # (завантажено_байт, всього_байт)
    finished_ok = Signal()
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        from whisper_core import cuda_runtime
        try:
            cuda_runtime.download_and_install(
                progress_cb=lambda done, total: self.progress.emit(done, total),
                cancel_check=self._cancel.is_set)
        except cuda_runtime.CudaDownloadCancelled:
            logging.info("Докачку прискорення GPU скасовано")
            self.cancelled.emit()
        except Exception as e:
            if self._cancel.is_set():
                logging.info("Докачку прискорення GPU скасовано")
                self.cancelled.emit()
            else:
                logging.error("Не вдалося докачати прискорення GPU: %s", e)
                self.failed.emit(str(e))
        else:
            logging.info("Прискорення GPU готове")
            self.finished_ok.emit()


class VoiceDownloadWorker(QThread):
    """Докачує голос TTS у звичайну або тестову теку в окремому потоці; прогрес — Qt-сигналами."""
    progress = Signal(object, object)
    finished_ok = Signal()
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, voice_id: str, root=None, parent=None):
        super().__init__(parent)
        self._voice_id = voice_id
        self._root = root
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        from whisper_core.tts import voices as _v
        try:
            def _progress_cb(done, total):
                if self._cancel.is_set():
                    raise InterruptedError()
                self.progress.emit(done, total)

            _v.download_and_install(
                self._voice_id,
                root=self._root,
                progress_cb=_progress_cb,
                cancel_check=self._cancel.is_set,
            )
        except InterruptedError:
            logging.info("Докачку голосу %s скасовано", self._voice_id)
            self.cancelled.emit()
        except Exception as e:
            if self._cancel.is_set():
                logging.info("Докачку голосу %s скасовано", self._voice_id)
                self.cancelled.emit()
            else:
                logging.error("Не вдалося докачати голос %s: %s", self._voice_id, e)
                self.failed.emit(str(e))
        else:
            logging.info("Голос %s готовий", self._voice_id)
            self.finished_ok.emit()


def _step_page(eyebrow: str):
    """Сторінка майстра: картка з золотим лейблом кроку → (page, layout)."""
    page = QWidget()
    outer = QVBoxLayout(page)
    card = QFrame()
    card.setProperty("card", True)
    lay = QVBoxLayout(card)
    lay.setSpacing(10)
    lab = QLabel(eyebrow)
    lab.setProperty("eyebrow", True)
    lay.addWidget(lab)
    outer.addWidget(card)
    outer.addStretch()
    return page, lay


class FirstRunWizard(QDialog):
    """Перше налаштування. exec() == Accepted → читати model_name/model_dir/language."""

    def __init__(self, parent=None, *, model_name=None, model_dir=None,
                 language=None, ptt_key=None, voice_root=None, repeat=False):
        super().__init__(parent)
        self.setWindowTitle(tr("onb_title"))
        self.setMinimumSize(620, 460)
        # Передзаповнення з поточного cfg (кнопка «пройти ще раз», повторний
        # показ після оновлення). Якщо параметр не переданий (перший запуск) —
        # старі дефолти, поведінка незмінна.
        self.model_name = model_name or "large-v3-turbo"
        self.model_dir = model_dir or default_model_dir()
        self.language = language or "uk"
        self.ptt_key = ptt_key or "ctrl+shift+space"    # комбінація запису (крок 3)
        self.voice_root = voice_root
        self.use_gpu = False                 # feature/gpu: докачали прискорення → старт на GPU
        # repeat=True — майстер показано ПОВТОРНО (після оновлення версії, не
        # перший запуск): вітальний крок каже «що змінилося», а не «вітаємо»,
        # і дає одразу закрити майстер, не проходячи кроків.
        self.repeat = bool(repeat)
        # позначка «більше не показувати» на вітальному кроці повторного
        # режиму — читає _handle_onboarding_dismissed (app.py) при відхиленні
        self.dont_show_again = False
        # «Пропустити» на кроці завантаження: майстер завершується Accepted, але
        # моделі ще нема — app.py збереже налаштування й НЕ стартує рушій
        self.model_skipped = False
        self._worker = None
        self._gpu_worker = None
        self._voice_worker = None
        self._extra_worker = None
        self.selected_extras = []
        self._gpu_done = False               # GPU-крок показуємо щонайбільше раз
        # Чесна нумерація (рішення власника 31.07, варіант б): крок «Завантаження»
        # реально показаний лише якщо щось справді довелось качати. Якщо ні —
        # total_steps на кроці GPU зменшується на 1, щоб «Крок N з M» не обіцяв
        # екран, якого людина так і не побачила.
        self._download_shown = False

        # Лічильник кроків: Welcome(1), Model(2), Language(3), Voice(4), Extra(5), Download(6), GPU(7 - умовний)
        self._gpu_possible = self._gpu_step_possible()
        self._total_steps = 7 if self._gpu_possible else 6

        self._stack = QStackedWidget()
        self._stack.addWidget(self._page_welcome())
        self._stack.addWidget(self._page_model())
        self._stack.addWidget(self._page_language())
        self._stack.addWidget(self._page_voice())
        self._stack.addWidget(self._page_extra())
        self._stack.addWidget(self._page_download())
        # Крок GPU — ПІСЛЯ докачки моделі; додаємо в кінець стека
        self._gpu_index = self._stack.addWidget(self._page_gpu())

        self._back = QPushButton(tr("common_back"))
        self._back.setAccessibleName(tr("common_back"))
        self._back.setToolTip(tr("common_back"))
        self._back.clicked.connect(self._go_back)
        self._next = QPushButton(tr("common_next"))
        self._next.setAccessibleName(tr("common_next"))
        self._next.setToolTip(tr("common_next"))
        self._next.setProperty("accent", True)
        self._next.clicked.connect(self._go_next)
        nav = QHBoxLayout()
        nav.addWidget(self._back)
        nav.addStretch()
        nav.addWidget(self._next)

        root = QVBoxLayout(self)
        root.addWidget(self._stack, stretch=1)
        root.addLayout(nav)
        self._sync_nav()


        self._tray = None
        try:
            from .tray import Tray
            # fronts.desktop.profiles НЕ існує — правильний модуль у whisper_core
            # (хибний імпорт мовчки вбивав трей на онбордингу, рецензія 24.07)
            from whisper_core import profiles
            from whisper_core import paths
            self._tray = Tray(
                profile_names=[p.name for p in profiles.list_profiles(paths.profiles_root())],
                active="default", memory_on=True,
                on_switch_profile=lambda p: None, on_toggle_memory=lambda m: None,
                on_reset_memory=lambda: None, on_reload_terms=lambda: None,
                on_quit=self.reject, on_open_window=None,
            )
            self._tray.icon.show()
        except Exception:
            self._tray = None

    def accept(self):
        """Перед справжнім закриттям майстра — чесний підсумок (пункт 3
        завдання власника 31.07, мінімум-варіант «в» поверх а+б): що готово
        до роботи, що не завантажено/недоступне і де це ввімкнути пізніше.
        accept() викликається з кількох гілок (GPU-крок, «Пропустити»
        завантаження, фініш без GPU) — показуємо підсумок лише один раз."""
        if not getattr(self, "_summary_shown", False):
            self._summary_shown = True
            self._show_finish_summary()
        super().accept()

    def _show_finish_summary(self):
        from whisper_core import cuda_runtime
        from whisper_core.tts import voices as _v
        import whisper_core.meeting.diarization_models as diar_models
        import whisper_core.protocol.model_manager as protocol_mm
        import whisper_core.punctuator as punc
        import whisper_core.paths as paths

        lines = []

        repo = repo_for(self.model_name)
        rev = revision_for(self.model_name)
        stt_ready = (model_present(self.model_dir, repo, rev)
                    and model_snapshot_usable(self.model_dir, repo, rev))
        lines.append(tr("onb_summary_stt_ready") if stt_ready
                     else tr("onb_summary_stt_missing"))

        if cuda_runtime.gpu_present():
            gpu_on = self.use_gpu or cuda_runtime.runtime_ready()
            lines.append(tr("onb_summary_gpu_on") if gpu_on
                         else tr("onb_summary_gpu_off"))

        if not _tts_engine_available():
            lines.append(tr("onb_summary_voice_build_missing"))
        else:
            voice_id = _v.default_voice_for(self.language) or "styletts2_ua"
            voice_ready = _v.voice_available(voice_id, root=self.voice_root)
            lines.append(tr("onb_summary_voice_on") if voice_ready
                         else tr("onb_summary_voice_off"))

        extra_checks = [
            (tr("onb_extra_diar_title"), diar_models.models_available(self.model_dir)),
            (tr("onb_extra_proto_title"),
             protocol_mm.model_available(paths.protocol_models_dir(), "fast")),
            (tr("onb_extra_punc_title"), punc.model_available(paths.punctuator_model_dir())),
        ]
        for title, ready in extra_checks:
            status = tr("onb_summary_ready") if ready else tr("onb_summary_not_ready")
            lines.append(f"{title}: {status}")

        lines.append("")
        lines.append(tr("onb_summary_footer"))

        QMessageBox.information(self, tr("onb_summary_title"), "\n".join(lines))

    def done(self, result):
        self._detach_voice_worker()
        if getattr(self, "_tray", None) and getattr(self._tray, "icon", None):
            try:
                self._tray.icon.hide()
            except Exception:
                pass
        super().done(result)

    def _close_repeat_wizard(self):
        """Кнопка «Закрити» на вітальному кроці повторного показу: жодних
        кроків, жодних змін cfg. Прапорець «більше не показувати» лишається
        на self для app.py._handle_onboarding_dismissed — сама версія
        запам'ятовується там, а не тут (одне місце істини)."""
        self.dont_show_again = self._repeat_dont_show_chk.isChecked()
        self.reject()

    def _open_standard_folders(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        d = self.model_dir or default_model_dir()
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    # --- лічильник кроків ---
    def _gpu_step_possible(self) -> bool:
        """Чи буде показано умовний крок прискорення на відеокарті (є NVIDIA і
        рантайму ще нема). Та сама умова, що у _finish_or_gpu; збій → False."""
        try:
            from whisper_core import cuda_runtime
            return bool(cuda_runtime.gpu_present()
                        and not cuda_runtime.runtime_ready())
        except Exception:
            return False

    def _eyebrow(self, n: int, section_key: str) -> str:
        """Золотий лейбл кроку «КРОК n З total · РОЗДІЛ» з динамічним total."""
        return tr("onb_step_fmt", n=n, total=self._total_steps,
                  section=tr(section_key))

    # --- сторінки ---
    def _page_welcome(self):
        page, lay = _step_page(self._eyebrow(1, "onb_sec_welcome"))
        # картку привітання розтягуємо на всю висоту, щоб підпис автора ліг
        # делікатно ВНИЗУ, окремо від основного тексту (фідбек Миколи)
        outer = page.layout()
        outer.setStretch(0, 1)          # картка (індекс 0) забирає вільний простір
        outer.setStretch(1, 0)          # нижній розпір прибрано — картка на всю висоту

        # Повторний показ (після оновлення) має іншу вітальну мову: не
        # «вітаємо», а «що змінилося» — людина вже налаштовувала програму.
        title_key = "onb_welcome_title_repeat" if self.repeat else "onb_welcome_title"
        body_key = "onb_welcome_body_repeat" if self.repeat else "onb_welcome_body"
        steps_key = "onb_welcome_steps_repeat" if self.repeat else "onb_welcome_steps"

        title = QLabel(tr(title_key))
        title.setProperty("level", "h1")   # заголовок сторінки візарда — канон h1
        title.setWordWrap(True)   # заголовок вітання — ризик обрізання при 1000px
        lay.addWidget(title)
        body = QLabel(tr(body_key))
        body.setWordWrap(True)
        lay.addWidget(body)
        steps = QLabel(tr(steps_key))
        steps.setWordWrap(True)
        lay.addWidget(steps)

        if self.repeat:
            # Пункт 4 завдання: у повторному режимі закрити майстер можна
            # одразу на ПЕРШОМУ ж екрані (у першому запуску так само зробити
            # можна лише з кроку «Озвучення»/Esc) — плюс «більше не показувати»,
            # що запам'ятовує поточну версію без проходу решти кроків.
            close_row = QHBoxLayout()
            close_row.setSpacing(8)
            self._repeat_dont_show_chk = QCheckBox(tr("onb_repeat_dont_show"))
            self._repeat_dont_show_chk.setToolTip(tr("onb_repeat_dont_show"))
            self._repeat_dont_show_chk.setAccessibleName(tr("onb_repeat_dont_show"))
            close_row.addWidget(self._repeat_dont_show_chk)
            close_row.addStretch(1)
            self._repeat_close_btn = QPushButton(tr("onb_repeat_close"))
            self._repeat_close_btn.setToolTip(tr("onb_repeat_close_tip"))
            self._repeat_close_btn.setAccessibleName(tr("onb_repeat_close"))
            self._repeat_close_btn.clicked.connect(self._close_repeat_wizard)
            close_row.addWidget(self._repeat_close_btn)
            lay.addLayout(close_row)

        lay.addStretch()                # відсунути підпис автора до низу картки
        line = QFrame()
        line.setProperty("divider", True)   # тонка волосяна лінія-розділювач
        lay.addWidget(line)
        # підпис автора + два значки-посилання праворуч (GitHub і «Підтримати»).
        # round_social імпортуємо ліниво — settings уже імпортує onboarding на
        # рівні модуля, тож зустрічний імпорт лишаємо всередині методу.
        from .pages.settings import round_social
        author_row = QHBoxLayout()
        author_row.setSpacing(8)
        author = QLabel(tr("onb_author"))
        author.setObjectName("authorLabel")
        author.setProperty("level", "body")   # ОДИН рядок, крупніший шрифт (level body з theme.py)
        author.setWordWrap(False)
        author.setAccessibleName(tr("onb_author"))
        author_row.addWidget(author)
        author_row.addStretch(1)
        gh = round_social("fa6b.github", GITHUB_URL,
                          name=tr("author_github_name"),
                          tooltip=tr("author_github_hint"))
        gh.setObjectName("authorGithubLink")
        support = round_social("fa6s.heart", SUPPORT_URL,
                               name=tr("about_support_link"),
                               tooltip=tr("author_support_hint"))
        support.setObjectName("authorSupportLink")
        author_row.addWidget(gh)
        author_row.addWidget(support)
        lay.addLayout(author_row)
        return page

    def _page_model(self):
        page, lay = _step_page(self._eyebrow(2, "onb_sec_model"))
        self._rb_fast = QRadioButton(tr("onb_model_fast"))
        self._rb_fast.setAccessibleName(tr("onb_model_fast"))
        self._rb_precise = QRadioButton(tr("onb_model_precise"))
        self._rb_precise.setAccessibleName(tr("onb_model_precise"))
        # передзаповнення: «precise» лише для large-v3, інакше турбо (дефолт)
        if self.model_name == "large-v3":
            self._rb_precise.setChecked(True)
        else:
            self._rb_fast.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self._rb_fast)
        group.addButton(self._rb_precise)
        lay.addWidget(self._rb_fast)
        lay.addWidget(self._rb_precise)

        self._dir_label = QLabel(tr("onb_model_dir"))
        self._dir_label.setWordWrap(True)
        view_folders = QPushButton(tr("onb_model_view_folders"))
        view_folders.setAccessibleName(tr("onb_model_view_folders"))
        view_folders.clicked.connect(self._open_standard_folders)
        pick = QPushButton(tr("common_change"))
        pick.setAccessibleName(tr("common_change"))
        pick.clicked.connect(self._pick_dir)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self._dir_label, stretch=1)
        row.addWidget(view_folders)
        row.addWidget(pick)
        lay.addLayout(row)

        # «Я не памʼятаю, де модель» — пошук уже завантаженої моделі обраного
        # типу у стандартних місцях (тека Балачок, кеш HuggingFace)
        find = QPushButton(tr("onb_model_find"))
        find.setAccessibleName(tr("onb_model_find"))
        find.clicked.connect(self._find_existing)
        find_row = QHBoxLayout()
        find_row.setSpacing(12)
        find_row.addWidget(find)
        find_row.addStretch()
        lay.addLayout(find_row)

        # результат пошуку: «знайдено, качати не треба» / «не знайдено — завантажимо»
        self._found_note = QLabel("")
        self._found_note.setProperty("muted", True)
        self._found_note.setWordWrap(True)
        lay.addWidget(self._found_note)
        # зміна типу моделі робить попередній результат неактуальним
        group.buttonToggled.connect(lambda *_: self._found_note.clear())

        hint = QLabel(tr("onb_model_hint"))
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        lay.addWidget(hint)
        return page

    def _page_language(self):
        page, lay = _step_page(self._eyebrow(3, "onb_sec_lang"))
        self._rb_uk = QRadioButton(tr("common_ukrainian"))
        self._rb_en = QRadioButton(tr("common_english"))
        # передзаповнення мови з поточного cfg (дефолт — українська)
        if self.language == "en":
            self._rb_en.setChecked(True)
        else:
            self._rb_uk.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self._rb_uk)
        group.addButton(self._rb_en)
        lay.addWidget(self._rb_uk)
        lay.addWidget(self._rb_en)

        # комбінація клавіш для запису: пояснення + поточна комбінація + зміна
        key = QLabel(tr("onb_key"))
        key.setWordWrap(True)
        lay.addWidget(key)

        keyrow = QHBoxLayout()
        keyrow.setSpacing(12)
        self._key_label = QLabel(pretty(self.ptt_key))
        self._key_label.setProperty("kbd", True)
        change = QPushButton(tr("onb_key_change"))
        change.clicked.connect(self._change_key)
        keyrow.addWidget(self._key_label)
        keyrow.addWidget(change)
        keyrow.addStretch()
        lay.addLayout(keyrow)

        how = QLabel(tr("onb_how"))
        how.setProperty("muted", True)
        how.setWordWrap(True)
        lay.addWidget(how)

        # Ревізія наявних налаштувань: автозапуск Windows
        from . import autostart
        self._autostart_chk = QCheckBox(tr("onb_autostart_chk"))
        self._autostart_chk.setAccessibleName(tr("onb_autostart_chk"))
        try:
            self._autostart_chk.setChecked(autostart.is_enabled())
        except Exception:
            pass
        lay.addWidget(self._autostart_chk)
        return page

    def _page_voice(self):
        """НОВИЙ КРОК «Озвучення» (крок 4 з 5 / 6): людське пояснення голосу TTS,
        кнопки «Завантажити зараз» / «Пропустити», прогрес і м'яка обробка мережі."""
        page, lay = _step_page(self._eyebrow(4, "onb_sec_voice"))
        title = QLabel(tr("onb_sec_voice"))
        title.setProperty("level", "h1")
        title.setWordWrap(True)
        lay.addWidget(title)

        intro = QLabel(tr("onb_voice_intro"))
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self._voice_status = QLabel("")
        self._voice_status.setProperty("strong", True)
        self._voice_status.setWordWrap(True)
        lay.addWidget(self._voice_status)

        self._voice_bar = QProgressBar()
        self._voice_bar.setTextVisible(False)
        self._voice_bar.hide()
        lay.addWidget(self._voice_bar)

        self._voice_info = QLabel("")
        self._voice_info.setProperty("muted", True)
        self._voice_info.setWordWrap(True)
        lay.addWidget(self._voice_info)

        self._voice_dl_btn = QPushButton("")
        self._voice_dl_btn.setProperty("accent", True)
        self._voice_dl_btn.clicked.connect(self._start_voice_download)

        self._voice_skip_btn = QPushButton(tr("onb_voice_skip"))
        self._voice_skip_btn.setAccessibleName(tr("onb_voice_skip"))
        self._voice_skip_btn.clicked.connect(self._advance_from_voice)

        self._voice_next_btn = QPushButton(tr("common_next"))
        self._voice_next_btn.setAccessibleName(tr("common_next"))
        self._voice_next_btn.setProperty("accent", True)
        self._voice_next_btn.clicked.connect(self._advance_from_voice)
        self._voice_next_btn.hide()

        self._voice_cancel_btn = QPushButton(tr("common_cancel"))
        self._voice_cancel_btn.setAccessibleName(tr("common_cancel"))
        self._voice_cancel_btn.clicked.connect(self._cancel_voice_download)
        self._voice_cancel_btn.hide()

        self._voice_retry_btn = QPushButton(tr("onb_retry"))
        self._voice_retry_btn.setAccessibleName(tr("onb_retry"))
        self._voice_retry_btn.setProperty("accent", True)
        self._voice_retry_btn.clicked.connect(self._start_voice_download)
        self._voice_retry_btn.hide()

        btn_box = QVBoxLayout()
        btn_box.setSpacing(10)
        btn_box.addWidget(self._voice_dl_btn)
        btn_box.addWidget(self._voice_skip_btn)
        btn_box.addWidget(self._voice_next_btn)
        btn_box.addWidget(self._voice_retry_btn)
        btn_box.addWidget(self._voice_cancel_btn)
        lay.addLayout(btn_box)
        return page

    def _page_extra(self):
        page, lay = _step_page(self._eyebrow(5, "onb_sec_extra"))

        title = QLabel(tr("onb_extra_title"))
        title.setProperty("title", True)
        title.setWordWrap(True)
        title.setToolTip(tr("onb_extra_title"))
        title.setAccessibleName(tr("onb_extra_title"))
        lay.addWidget(title)

        sub_card = QFrame()
        sub_card.setProperty("card", True)
        sub_lay = QVBoxLayout(sub_card)
        sub_lay.setContentsMargins(10, 8, 10, 8)
        subtitle = QLabel(tr("onb_extra_subtitle"))
        subtitle.setProperty("muted", True)
        subtitle.setWordWrap(True)
        subtitle.setToolTip(tr("onb_extra_subtitle"))
        subtitle.setAccessibleName(tr("onb_extra_subtitle"))
        sub_lay.addWidget(subtitle)
        lay.addWidget(sub_card)

        # Розміри беруться З КОДУ через human_size
        import whisper_core.meeting.diarization_models as diar_models
        import whisper_core.protocol.model_manager as protocol_mm
        import whisper_core.punctuator as punc
        import whisper_core.tts.voices as tts_voices
        import whisper_core.paths as paths

        diar_sz = diar_models.TOTAL_DOWNLOAD_BYTES
        proto_sz = protocol_mm.PRESETS["fast"].approx_size_bytes
        punc_sz = getattr(punc, "APPROX_SIZE_BYTES", 234_000_000)
        tts_sz = tts_voices.VOICE_PRESETS["styletts2_ua"].approx_size_bytes


        diar_avail = diar_models.models_available(self.model_dir)
        # root/"fast" — та сама тека, куди її кладе download_and_install нижче
        # (аудит 31.07.2026: спільний корінь тут завжди читався б як «нема»).
        proto_avail = protocol_mm.model_available(paths.protocol_model_dir("fast"), "fast")
        punc_avail = punc.model_available(paths.punctuator_model_dir())
        tts_avail = tts_voices.voice_available("styletts2_ua", root=self.voice_root)

        self._extra_items = [
            ("diarization", "onb_extra_diar_title", "onb_extra_diar_row", "onb_extra_diar_info", diar_sz, diar_avail),
            ("protocol", "onb_extra_proto_title", "onb_extra_proto_row", "onb_extra_proto_info", proto_sz, proto_avail),
            ("punctuator", "onb_extra_punc_title", "onb_extra_punc_row", "onb_extra_punc_info", punc_sz, punc_avail),
            ("tts", "onb_extra_tts_title", "onb_extra_tts_row", "onb_extra_tts_info", tts_sz, tts_avail),
        ]

        from .pages.settings import info_hint
        from PySide6.QtWidgets import QScrollArea

        net_online = _has_network()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("background: transparent;")
        scroll_content = QWidget()
        scroll_content.setStyleSheet("background: transparent;")
        cards_lay = QVBoxLayout(scroll_content)
        cards_lay.setContentsMargins(0, 0, 0, 0)
        cards_lay.setSpacing(6)

        self._extra_chks = {}
        for comp_id, title_k, row_k, info_k, sz_bytes, is_downloaded in self._extra_items:
            item_box = QFrame()
            item_box.setProperty("card", True)
            item_lay = QVBoxLayout(item_box)
            item_lay.setContentsMargins(8, 6, 8, 6)
            item_lay.setSpacing(4)

            top_row = QHBoxLayout()
            top_row.setSpacing(8)

            sz_str = human_size(sz_bytes)
            chk_text = f"{tr(title_k)} ({sz_str})"
            chk = QCheckBox(chk_text)
            chk.setToolTip(chk_text)
            chk.setAccessibleName(chk_text)
            chk.setChecked(False)

            if is_downloaded:
                chk.setEnabled(False)
                badge = QLabel(tr("onb_extra_downloaded"))
                badge.setProperty("muted", True)
                badge.setToolTip(tr("onb_extra_downloaded"))
                badge.setAccessibleName(tr("onb_extra_downloaded"))
                top_row.addWidget(chk)
                top_row.addWidget(badge)
            else:
                if not net_online:
                    chk.setEnabled(False)
                chk.toggled.connect(self._update_extra_sum)
                top_row.addWidget(chk)

            top_row.addStretch()

            hint_btn = info_hint(info_k)
            hint_btn.setToolTip(tr(info_k))
            hint_btn.setAccessibleName(tr(info_k))
            top_row.addWidget(hint_btn)

            item_lay.addLayout(top_row)

            desc = QLabel(tr(row_k))
            desc.setProperty("muted", True)
            desc.setWordWrap(True)
            desc.setToolTip(tr(row_k))
            desc.setAccessibleName(tr(row_k))
            item_lay.addWidget(desc)

            cards_lay.addWidget(item_box)
            self._extra_chks[comp_id] = (chk, sz_bytes, is_downloaded)

        if not net_online:
            no_net = QLabel(tr("onb_extra_no_net"))
            no_net.setProperty("warning", True)
            no_net.setWordWrap(True)
            no_net.setToolTip(tr("onb_extra_no_net"))
            no_net.setAccessibleName(tr("onb_extra_no_net"))
            cards_lay.addWidget(no_net)

        scroll.setWidget(scroll_content)
        lay.addWidget(scroll, stretch=1)

        sum_box = QHBoxLayout()
        self._extra_sum_label = QLabel(tr("onb_extra_none_selected"))
        self._extra_sum_label.setProperty("strong", True)
        self._extra_sum_label.setToolTip(tr("onb_extra_none_selected"))
        self._extra_sum_label.setAccessibleName(tr("onb_extra_none_selected"))
        sum_box.addWidget(self._extra_sum_label)
        sum_box.addStretch()

        self._extra_skip_btn = QPushButton(tr("onb_extra_skip"))
        self._extra_skip_btn.setToolTip(tr("onb_extra_skip_tip"))
        self._extra_skip_btn.setAccessibleName(tr("onb_extra_skip"))
        self._extra_skip_btn.clicked.connect(self._skip_extra)
        sum_box.addWidget(self._extra_skip_btn)

        lay.addLayout(sum_box)
        return page


    def _update_extra_sum(self):
        total = 0
        for comp_id, (chk, sz_bytes, is_downloaded) in self._extra_chks.items():
            if chk.isChecked() and not is_downloaded:
                total += sz_bytes
        if total == 0:
            txt = tr("onb_extra_none_selected")
        else:
            txt = tr("onb_extra_sum", size=human_size(total))
        self._extra_sum_label.setText(txt)
        self._extra_sum_label.setToolTip(txt)
        self._extra_sum_label.setAccessibleName(txt)

    def _skip_extra(self):
        for comp_id, (chk, sz_bytes, is_downloaded) in self._extra_chks.items():
            if not is_downloaded:
                chk.setChecked(False)
        self._advance_from_extra()

    def _page_download(self):
        page, lay = _step_page(self._eyebrow(6, "onb_sec_download"))
        self._dl_status = QLabel(tr("onb_dl_status"))
        self._dl_status.setProperty("strong", True)
        self._dl_status.setWordWrap(True)
        self._dl_status.setToolTip(tr("onb_dl_status"))
        self._dl_status.setAccessibleName(tr("onb_dl_status"))
        lay.addWidget(self._dl_status)

        self._dl_bar = QProgressBar()
        self._dl_bar.setTextVisible(False)
        lay.addWidget(self._dl_bar)

        self._dl_info = QLabel("")
        self._dl_info.setProperty("muted", True)
        self._dl_info.setWordWrap(True)
        lay.addWidget(self._dl_info)

        # слабкий інтернет: людина має знати, що обрив не втрачає завантажене
        self._dl_resume_hint = QLabel(tr("onb_dl_resume_hint"))
        self._dl_resume_hint.setProperty("muted", True)
        self._dl_resume_hint.setWordWrap(True)
        self._dl_resume_hint.setToolTip(tr("onb_dl_resume_hint"))
        self._dl_resume_hint.setAccessibleName(tr("onb_dl_resume_hint"))
        lay.addWidget(self._dl_resume_hint)

        # ліцензія моделі розпізнавання (Whisper — MIT) з посиланням на сторінку;
        # текст із конкретним репозиторієм ставиться у _start_download
        self._dl_license = QLabel("")
        self._dl_license.setProperty("muted", True)
        self._dl_license.setOpenExternalLinks(True)
        self._dl_license.setWordWrap(True)
        lay.addWidget(self._dl_license)

        self._dl_cancel = QPushButton(tr("common_cancel"))
        self._dl_cancel.setAccessibleName(tr("common_cancel"))
        self._dl_cancel.setToolTip(tr("common_cancel"))
        self._dl_cancel.clicked.connect(self._cancel_download)
        self._dl_retry = QPushButton(tr("onb_retry"))
        self._dl_retry.setAccessibleName(tr("onb_retry"))
        self._dl_retry.setToolTip(tr("onb_retry"))
        self._dl_retry.setProperty("accent", True)
        self._dl_retry.clicked.connect(self._start_download)
        self._dl_retry.hide()
        # «Пропустити» — єдиний вихід уперед зі слабким інтернетом
        self._dl_skip = QPushButton(tr("onb_dl_skip"))
        self._dl_skip.setAccessibleName(tr("onb_dl_skip"))
        self._dl_skip.setToolTip(tr("onb_dl_skip"))
        self._dl_skip.clicked.connect(self._skip_download)
        row = QHBoxLayout()
        row.addWidget(self._dl_cancel)
        row.addWidget(self._dl_retry)
        row.addWidget(self._dl_skip)
        row.addStretch()
        lay.addLayout(row)
        return page


    def _page_gpu(self):
        """Опційний крок: докачка прискорення на відеокарті (feature/gpu).
        Показується лише коли є NVIDIA і рантайму ще нема. Скасування/відмова =
        працювати на процесорі (майстер не блокується)."""
        # Останній крок у загальному рахунку (5 з 5, коли показується)
        page, lay = _step_page(self._eyebrow(self._total_steps, "onb_sec_gpu"))
        # єдиний QLabel на сторінці станом на цей момент — золотий лейбл кроку;
        # зберігаємо посилання, щоб _finish_or_gpu міг чесно оновити «N з M»,
        # якщо крок «Завантаження» так і не був показаний (варіант б)
        self._gpu_eyebrow_lab = page.findChild(QLabel)
        self._gpu_intro = QLabel(tr("onb_gpu_intro"))
        self._gpu_intro.setWordWrap(True)
        lay.addWidget(self._gpu_intro)

        self._gpu_bar = QProgressBar()
        self._gpu_bar.setTextVisible(False)
        self._gpu_bar.hide()
        lay.addWidget(self._gpu_bar)

        self._gpu_info = QLabel("")
        self._gpu_info.setProperty("muted", True)
        self._gpu_info.setWordWrap(True)
        lay.addWidget(self._gpu_info)

        self._gpu_yes = QPushButton(tr("onb_gpu_yes"))
        self._gpu_yes.setAccessibleName(tr("onb_gpu_yes"))
        self._gpu_yes.setProperty("accent", True)
        self._gpu_yes.clicked.connect(self._start_gpu_download)
        self._gpu_no = QPushButton(tr("onb_gpu_no"))
        self._gpu_no.setAccessibleName(tr("onb_gpu_no"))
        self._gpu_no.clicked.connect(self.accept)   # продовжити на процесорі
        self._gpu_cancel = QPushButton(tr("common_cancel"))
        self._gpu_cancel.setAccessibleName(tr("common_cancel"))
        self._gpu_cancel.clicked.connect(self._cancel_gpu_download)
        self._gpu_cancel.hide()

        # рантайм уже на місці → жодного докачування, лише «Далі» (на GPU).
        # Зʼявляється лише у стані «готове» (_update_gpu_page_state).
        self._gpu_next_btn = QPushButton(tr("common_next"))
        self._gpu_next_btn.setAccessibleName(tr("common_next"))
        self._gpu_next_btn.setProperty("accent", True)
        self._gpu_next_btn.clicked.connect(self.accept)
        self._gpu_next_btn.hide()

        btn_box = QVBoxLayout()
        btn_box.setSpacing(10)
        btn_box.addWidget(self._gpu_yes)
        btn_box.addWidget(self._gpu_no)
        btn_box.addWidget(self._gpu_cancel)
        btn_box.addWidget(self._gpu_next_btn)
        lay.addLayout(btn_box)
        return page

    # --- навігація ---
    def _sync_nav(self):
        i = self._stack.currentIndex()
        on_gpu = (i == self._gpu_index)
        self._back.setVisible(not on_gpu)
        self._back.setEnabled(i > 0 and not on_gpu)
        self._next.setVisible(i < 5 and i != 4)  # Крок 4 має власні кнопки переходу / пропуску

    def _go_back(self):
        i = self._stack.currentIndex()
        if i > 0:
            if i == 3:
                self._detach_voice_worker()
            elif i == 5:
                self._detach_worker()
                self._detach_extra_worker()
            self._stack.setCurrentIndex(i - 1)
            if i - 1 == 3:
                # _update_voice_page_state сама чесно розводить обидва стани
                # (рушія нема / голос не завантажено) — окремого дублюючого
                # розгалуження тут більше не треба (рецензія-3 24.07 боявся саме
                # старої версії _update_voice_page_state, що сліпо пропонувала
                # завантажити голос без рушія; тепер вона сама це враховує).
                self._update_voice_page_state()
        self._sync_nav()

    def _go_next(self):
        i = self._stack.currentIndex()
        if i < 2:
            self._stack.setCurrentIndex(i + 1)
        elif i == 2:
            self._collect_choices()
            # Крок «Озвучення» показуємо ЗАВЖДИ (рішення власника 31.07,
            # варіант а): раніше за відсутності рушія сторінка перемикалась і
            # тієї ж миті пропускалась (_advance_from_voice відразу після
            # setCurrentIndex) — людина її просто не встигала побачити і не
            # дізнавалась, що озвучення взагалі існує. _update_voice_page_state
            # сама показує чесне пояснення, коли рушія нема.
            self._stack.setCurrentIndex(3)
            self._update_voice_page_state()
        elif i == 3:
            self._advance_from_voice()
        elif i == 4:
            self._advance_from_extra()
        self._sync_nav()


    def _update_voice_page_state(self):
        if not _tts_engine_available():
            # Полегшена збірка без рушія синтезу мовлення (variant а рішення
            # власника 31.07): чесно кажемо про це замість мовчазного стрибка
            # через сторінку. Єдина дія — «Далі»; кнопку завантаження голосу
            # 700+ МБ, який нема чим відтворити, не показуємо взагалі.
            self._voice_status.setText(tr("onb_voice_engine_missing"))
            self._voice_info.setText("")
            self._voice_bar.hide()
            self._voice_dl_btn.hide()
            self._voice_retry_btn.hide()
            self._voice_cancel_btn.hide()
            self._voice_skip_btn.hide()
            self._voice_next_btn.show()
            return

        from whisper_core.tts import voices as _v
        voice_id = _v.default_voice_for(self.language) or "styletts2_ua"
        preset = _v.VOICE_PRESETS.get(voice_id)
        size_bytes = preset.approx_size_bytes if preset else 749 * _MB
        size_str = f"~{human_size(size_bytes)}"

        self._voice_dl_btn.setText(tr("onb_voice_dl_now", size=size_str))
        self._voice_dl_btn.setAccessibleName(tr("onb_voice_dl_now", size=size_str))

        if _v.voice_available(voice_id, root=self.voice_root):
            self._voice_status.setText(tr("onb_voice_ready"))
            self._voice_info.setText("")
            self._voice_bar.hide()
            self._voice_dl_btn.hide()
            self._voice_skip_btn.hide()
            self._voice_retry_btn.hide()
            self._voice_cancel_btn.hide()
            self._voice_next_btn.show()
            return

        # голосу нема. Єдина канонічна перевірка — voice_available; власний
        # огляд тек (isdir/listdir) поверх неї лише здогадувався про
        # «пошкоджено», не додаючи істини, — його прибрано (рецензія-3, п.6).
        self._voice_bar.hide()
        self._voice_retry_btn.hide()
        self._voice_cancel_btn.hide()
        self._voice_next_btn.hide()
        self._voice_dl_btn.show()
        self._voice_skip_btn.show()
        self._voice_status.setText("")
        self._voice_info.setText("")

    def _update_gpu_page_state(self):
        """Стан кроку прискорення на відеокарті за фактичним рантаймом:
        готове (усе на місці) / відсутнє (теки нема) / пошкоджене (тека є,
        але runtime_ready() False). Готові перевірки — з whisper_core.cuda_runtime
        (gpu_present / runtime_ready / cuda_dir); власних перевірок DLL не пишемо."""
        from whisper_core import cuda_runtime
        if cuda_runtime.gpu_present() and cuda_runtime.runtime_ready():
            self._gpu_intro.setText(tr("onb_gpu_ready"))
            self._gpu_info.setText(tr("onb_gpu_ready_detail"))
            self._gpu_bar.hide()
            self._gpu_yes.hide()
            self._gpu_no.hide()
            self._gpu_cancel.hide()
            self._gpu_next_btn.show()
            self.use_gpu = True
            return

        # рантайм не готовий — єдина канонічна перевірка runtime_ready; власний
        # огляд тек (isdir/listdir) поверх неї лише здогадувався про «пошкоджені
        # DLL», не додаючи істини, — його прибрано (рецензія-3, п.6).
        self._gpu_next_btn.hide()
        self._gpu_cancel.hide()
        self._gpu_bar.hide()
        self._gpu_intro.setText(tr("onb_gpu_intro"))
        self._gpu_info.setText("")
        if cuda_runtime.gpu_present():
            self._gpu_yes.show()
            self._gpu_no.show()
        else:
            self._gpu_yes.hide()
            self._gpu_no.show()

    def _start_voice_download(self):
        from whisper_core.tts import voices as _v
        self._detach_voice_worker()
        voice_id = _v.default_voice_for(self.language) or "styletts2_ua"

        self._voice_status.setText(tr("onb_voice_dl_connecting"))
        self._voice_info.setText("")
        self._voice_bar.setRange(0, 0)
        self._voice_bar.show()
        self._voice_dl_btn.hide()
        self._voice_skip_btn.hide()
        self._voice_retry_btn.hide()
        self._voice_next_btn.hide()
        self._voice_cancel_btn.show()
        self._voice_cancel_btn.setEnabled(True)

        self._voice_worker = VoiceDownloadWorker(voice_id, root=self.voice_root)
        self._voice_worker.progress.connect(self._on_voice_progress)
        self._voice_worker.finished_ok.connect(self._on_voice_done)
        self._voice_worker.failed.connect(self._on_voice_failed)
        self._voice_worker.cancelled.connect(self._on_voice_cancelled)
        self._voice_worker.start()

    def _detach_voice_worker(self):
        w = self._voice_worker
        self._voice_worker = None
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

    def _cancel_voice_download(self):
        if self._voice_worker is not None:
            self._detach_voice_worker()
            self._voice_bar.hide()
            self._voice_status.setText("")
            self._voice_cancel_btn.hide()
            self._voice_dl_btn.show()
            self._voice_skip_btn.show()

    def _advance_from_voice(self):
        self._detach_voice_worker()
        self._collect_choices()
        repo = repo_for(self.model_name)
        rev = revision_for(self.model_name)
        if model_present(self.model_dir, repo, rev):
            # знімок є, але чи придатний? model_present перевіряє лише наявність
            # файлів за іменами; model_snapshot_usable відкриває кожен потрібний
            # файл — так відокремлюємо «качати нічого не треба» від «файли є, але
            # пошкоджені/непридатні» (напр. обірване докачування або symlink, що
            # не читається на frozen exe).
            if model_snapshot_usable(self.model_dir, repo, rev):
                # Модель уже готова — але крок «Додаткові можливості» ВСЕ ОДНО
                # показуємо (Дефект 1 аудиту 30.07: раніше тут стояв ранній
                # return у _finish_or_gpu, і людина з готовою моделлю ніколи не
                # дізнавалась про розрізнення голосів/протокол/пунктуацію/TTS).
                # Якщо якісь із них не обрано, а качати справді нічого — сама
                # сторінка «Додаткові можливості» (_advance_from_extra) піде
                # прямо на фініш, побачивши це вже там.
                logging.info("Модель %s знайдена і придатна у %s — докачка бази не потрібна",
                             self.model_name, self.model_dir)
                self._heal_if_symlinks()        # ідемпотентно, безпечно робити зараз
                self._stack.setCurrentIndex(4)  # _page_extra
                self._sync_nav()
                return
            # модель є, але непридатна — готуємо текст «пошкоджено» заздалегідь,
            # щоб він був на місці, коли людина дійде до кроку завантаження
            # (між «Озвученням» і завантаженням тепер завжди стоїть крок
            # «Додаткові можливості» — його не пропускаємо).
            self._dl_status.setText(tr("onb_model_corrupted"))
            self._dl_info.setText(tr("onb_model_corrupted_detail"))
            self._dl_bar.setRange(0, 1000)
            self._dl_bar.setValue(0)
            self._dl_cancel.hide()
            self._dl_retry.show()
        self._stack.setCurrentIndex(4)  # _page_extra
        self._sync_nav()

    def _advance_from_extra(self):
        self._collect_extra_choices()
        repo = repo_for(self.model_name)
        rev = revision_for(self.model_name)
        stt_needed = not (model_present(self.model_dir, repo, rev) and model_snapshot_usable(self.model_dir, repo, rev))
        extras_needed = len(getattr(self, "selected_extras", [])) > 0

        if not stt_needed and not extras_needed:
            logging.info("Модель %s знайдена і придатна, додаткових компонентів немає — завантаження не потрібне",
                         self.model_name)
            self._heal_if_symlinks()
            self._finish_or_gpu()
            return
        self._download_shown = True     # сторінку «Завантаження» реально показано
        self._stack.setCurrentIndex(5)  # _page_download
        self._start_download()

    def _collect_extra_choices(self):
        self.selected_extras = []
        if hasattr(self, "_extra_chks"):
            for comp_id, (chk, sz_bytes, is_downloaded) in self._extra_chks.items():
                if chk.isChecked() and not is_downloaded:
                    self.selected_extras.append(comp_id)


    def _on_voice_progress(self, done, total):
        if total:
            self._voice_bar.setRange(0, 1000)
            self._voice_bar.setValue(min(1000, int(done * 1000 / total)))
            self._voice_info.setText(tr("onb_voice_dl_progress",
                                        done=done // _MB, total=total // _MB))
        else:
            self._voice_info.setText(tr("onb_voice_dl_progress_indet", done=done // _MB))

    def _on_voice_done(self):
        self._voice_bar.setRange(0, 1000)
        self._voice_bar.setValue(1000)
        self._voice_status.setText(tr("onb_voice_ready"))
        self._voice_info.setText("")
        self._voice_cancel_btn.hide()
        self._advance_from_voice()

    def _on_voice_failed(self, msg: str):
        logging.warning("Завантаження голосу завершилось помилкою: %s", msg)
        self._voice_bar.hide()
        self._voice_status.setText(tr("onb_voice_dl_failed"))
        self._voice_info.setText(tr("onb_voice_dl_failed_detail"))
        self._voice_cancel_btn.hide()
        self._voice_retry_btn.show()
        self._voice_skip_btn.show()

    def _on_voice_cancelled(self):
        self._voice_bar.hide()
        self._voice_status.setText("")
        self._voice_info.setText("")
        self._voice_cancel_btn.hide()
        self._voice_dl_btn.show()
        self._voice_skip_btn.show()

    def _finish_or_gpu(self):
        """Модель готова → зберегти автозапуск, потім або запропонувати крок GPU,
        або завершити майстер. Крок показуємо раз."""
        from . import autostart
        try:
            if hasattr(self, "_autostart_chk") and self._autostart_chk.isChecked():
                autostart.enable()
            elif hasattr(self, "_autostart_chk"):
                autostart.disable()
        except Exception as e:
            logging.warning("Не вдалося оновити стан автозапуску: %s", e)

        from whisper_core import cuda_runtime
        if (not self._gpu_done and cuda_runtime.gpu_present()
                and not cuda_runtime.runtime_ready()):
            self._gpu_done = True
            # Чесна нумерація (варіант б): якщо крок «Завантаження» так і не
            # був показаний (модель вже готова, додаткових компонентів не
            # обрано), останній крок GPU не має вдавати, що перед ним був
            # крок, якого людина не бачила — total_steps зменшуємо на 1.
            if not self._download_shown:
                self._total_steps -= 1
            self._gpu_eyebrow_lab.setText(self._eyebrow(self._total_steps, "onb_sec_gpu"))
            self._stack.setCurrentIndex(self._gpu_index)
            self._update_gpu_page_state()
            self._sync_nav()
        else:
            self.accept()

    def _collect_choices(self):
        self.model_name = ("large-v3-turbo" if self._rb_fast.isChecked()
                           else "large-v3")
        self.language = "uk" if self._rb_uk.isChecked() else "en"

    def _heal_if_symlinks(self):
        """Перед accept зі знайденою моделлю: замінити символьні лінки у знімку
        РЕАЛЬНИМИ копіями (те саме лікування, що RecoveryDialog._heal_if_symlinks).
        На встановленому (без підпису) exe модель із лінків HF-кешу не
        відкривається (WinError 448 «untrusted mount point») — без дереференсу
        перший старт після онбордингу падав би у відновлення. Ідемпотентно
        (не-лінки пропускає); будь-який збій лишає знімок як є — далі спрацює
        звичайне само-лікування app.py."""
        try:
            dereference_snapshot(self.model_dir, repo_for(self.model_name),
                                 revision_for(self.model_name))
        except Exception:
            logging.exception("Дереференс в онбордингу впав — лишаємо знімок як є")

    def _pick_dir(self):
        path = QFileDialog.getExistingDirectory(self, tr("common_model_folder"),
                                                self.model_dir)
        if path:
            self.model_dir = path
            self._dir_label.setText(tr("onb_model_dir", dir=path))
            self._found_note.clear()          # вручну вибрана тека → скинути статус пошуку

    def _selected_model_name(self) -> str:
        """Тип моделі за поточним вибором радіо (до _collect_choices)."""
        return "large-v3-turbo" if self._rb_fast.isChecked() else "large-v3"

    def _find_existing(self):
        """Пошук уже завантаженої моделі обраного типу у стандартних місцях.
        Знайшли пінований знімок → вказуємо ту теку, і крок завантаження
        пропуститься сам (_go_next бачить model_present); ні — лагідно кажемо,
        що завантажимо на наступному кроці. Кандидати з РЕАЛЬНИМИ файлами
        мають пріоритет над symlink-знімками HF-кешу: frozen exe лінки не
        читає (WinError 448), і такий вибір потребував би дереференсу."""
        name = self._selected_model_name()
        repo, rev = repo_for(name), revision_for(name)
        found = [d for d in _model_search_dirs() if model_present(d, repo, rev)]
        if not found:
            self._found_note.setText(tr("onb_model_not_found"))
            return
        d = next((c for c in found if model_all_real(c, repo, rev)), found[0])
        self.model_dir = d
        self._dir_label.setText(tr("onb_model_dir", dir=d))
        self._found_note.setText(tr("onb_model_found"))

    def _change_key(self):
        """Задати власну комбінацію клавіш для запису (той самий діалог, що й у
        Налаштуваннях). Нічого не змінив — лишається стандартна Ctrl+Shift+Space."""
        from .pages.settings import KeyCaptureDialog
        dlg = KeyCaptureDialog(self)
        if dlg.exec() and dlg.result_key:
            self.ptt_key = dlg.result_key
            self._key_label.setText(pretty(self.ptt_key))

    # --- докачка ---
    # --- докачка ---
    def _start_download(self):
        self._detach_worker()
        self._detach_extra_worker()

        repo = repo_for(self.model_name)
        rev = revision_for(self.model_name)
        stt_needed = not (model_present(self.model_dir, repo, rev) and model_snapshot_usable(self.model_dir, repo, rev))

        if stt_needed:
            self._dl_status.setText(tr("onb_dl_intro"))
            self._dl_info.setText(tr("onb_dl_connecting"))
            self._dl_license.setText(tr("dl_consent_license", license="MIT",
                                         url="https://huggingface.co/"
                                             + repo_for(self.model_name)))
            self._dl_bar.setRange(0, 0)
            self._dl_retry.hide()
            self._dl_cancel.show()
            self._dl_cancel.setEnabled(True)
            self._dl_cancel.setText(tr("common_cancel"))
            self._worker = DownloadWorker(repo_for(self.model_name),
                                          self.model_dir,
                                          revision_for(self.model_name))
            self._worker.progress.connect(self._on_progress)
            self._worker.finished_ok.connect(self._on_done)
            self._worker.failed.connect(self._on_failed)
            self._worker.cancelled.connect(self._on_cancelled)
            self._worker.start()
        else:
            self._download_next_extra()
        self._sync_nav()

    def _detach_worker(self):
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

    def _detach_extra_worker(self):
        w = getattr(self, "_extra_worker", None)
        self._extra_worker = None
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

    def _download_next_extra(self):
        self._detach_extra_worker()
        extras = getattr(self, "selected_extras", [])
        if not extras:
            self._dl_bar.setRange(0, 1000)
            self._dl_bar.setValue(1000)
            self._finish_or_gpu()
            return

        comp_id = extras.pop(0)
        # E-бекграунд-докачка (аудит 31.07.2026): майстер може відкритись, коли
        # головне вікно вже якісно якраз якісно качає ту саму модель протоколу
        # (напр. з Наради) — не дублюємо друге якісне завантаження, переходимо
        # до наступного пункту (модель дообереться там, де вже почалась).
        if comp_id == "protocol":
            from .download_manager import DownloadManager
            import whisper_core.paths as paths
            if DownloadManager.instance().is_downloading(paths.protocol_model_dir("fast")):
                self._download_next_extra()
                return
        item_info = next((it for it in getattr(self, "_extra_items", []) if it[0] == comp_id), None)
        title_str = tr(item_info[1]) if item_info else comp_id
        self._dl_status.setText(f"{tr('onb_sec_extra')}: {title_str}")
        self._dl_info.setText(tr("onb_dl_connecting"))
        self._dl_license.setText("")
        self._dl_bar.setRange(0, 0)
        self._dl_retry.hide()
        self._dl_cancel.show()
        self._dl_cancel.setEnabled(True)

        self._extra_worker = ExtraComponentWorker(comp_id, self.model_dir, voice_root=self.voice_root)
        self._extra_worker.progress.connect(self._on_progress)
        self._extra_worker.finished_ok.connect(self._download_next_extra)
        self._extra_worker.failed.connect(lambda msg: self._download_next_extra())
        self._extra_worker.cancelled.connect(lambda: self._download_next_extra())
        self._extra_worker.start()

    def _cancel_download(self):
        if self._worker is not None or getattr(self, "_extra_worker", None) is not None:
            self._detach_worker()
            self._detach_extra_worker()
            self._dl_bar.setRange(0, 1000)
            self._dl_status.setText(tr("onb_dl_cancelled"))
            self._dl_cancel.hide()
            self._dl_retry.show()
            self._sync_nav()

    def _skip_download(self):
        self._detach_worker()
        self._detach_extra_worker()
        if getattr(self, "selected_extras", []):
            self._download_next_extra()
        else:
            self.model_skipped = True
            self.accept()

    def _on_progress(self, done, total):
        if total:
            self._dl_bar.setRange(0, 1000)
            self._dl_bar.setValue(min(1000, int(done * 1000 / total)))
            self._dl_info.setText(tr("onb_dl_progress",
                                     done=done // _MB, total=total // _MB))
        else:
            self._dl_info.setText(tr("onb_dl_progress_indet", done=done // _MB))

    def _on_done(self):
        self._dl_bar.setRange(0, 1000)
        self._dl_bar.setValue(1000)
        self._heal_if_symlinks()
        self._download_next_extra()


    def _on_failed(self, msg: str):
        logging.warning("Завантаження моделі завершилось помилкою: %s", msg)
        self._dl_bar.setRange(0, 1000)
        self._dl_status.setText(tr("onb_dl_failed"))
        self._dl_info.setText(tr("onb_dl_failed_detail"))
        self._dl_cancel.hide()
        self._dl_retry.show()
        self._sync_nav()

    def _on_cancelled(self):
        self._dl_bar.setRange(0, 1000)
        self._dl_status.setText(tr("onb_dl_cancelled"))
        self._dl_cancel.hide()
        self._dl_retry.show()
        self._sync_nav()

    # --- докачка прискорення GPU (feature/gpu) ---
    def _start_gpu_download(self):
        self._detach_gpu_worker()            # retry завжди свіжий воркер
        self._gpu_yes.hide()
        self._gpu_no.hide()
        self._gpu_cancel.show()
        self._gpu_cancel.setEnabled(True)
        self._gpu_intro.setText(tr("gpu_dl_status"))
        self._gpu_bar.show()
        self._gpu_bar.setRange(0, 0)         # обсяг ще невідомий — «невизначений»
        self._gpu_info.setText(tr("gpu_connecting"))
        self._gpu_worker = GpuDownloadWorker()
        self._gpu_worker.progress.connect(self._on_gpu_progress)
        self._gpu_worker.finished_ok.connect(self._on_gpu_done)
        self._gpu_worker.failed.connect(self._on_gpu_failed)
        self._gpu_worker.cancelled.connect(self._on_gpu_cancelled)
        self._gpu_worker.start()

    def _detach_gpu_worker(self):
        w = self._gpu_worker
        self._gpu_worker = None
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

    def _cancel_gpu_download(self):
        # скасування докачки GPU = продовжити на процесорі (майстер не блокуємо)
        self._detach_gpu_worker()
        self.accept()

    def _on_gpu_progress(self, done, total):
        if total:
            self._gpu_bar.setRange(0, 1000)
            self._gpu_bar.setValue(min(1000, int(done * 1000 / total)))
            self._gpu_info.setText(tr("onb_dl_progress",
                                      done=done // _MB, total=total // _MB))
        else:
            self._gpu_info.setText(tr("onb_dl_progress_indet", done=done // _MB))

    def _on_gpu_done(self):
        self._gpu_bar.setRange(0, 1000)
        self._gpu_bar.setValue(1000)
        self.use_gpu = True                  # app.main() застосує device=cuda
        self.accept()

    def _on_gpu_failed(self, msg: str):
        logging.warning("Докачка прискорення GPU завершилась помилкою: %s", msg)
        self._gpu_bar.setRange(0, 1000)
        self._gpu_bar.hide()
        self._gpu_intro.setText(tr("gpu_failed"))
        self._gpu_info.setText(tr("gpu_failed_detail"))
        self._gpu_cancel.hide()
        self._gpu_yes.setText(tr("onb_retry"))
        self._gpu_yes.show()
        self._gpu_no.show()

    def _on_gpu_cancelled(self):
        # штатне скасування самим воркером → на процесорі
        self.accept()

    def reject(self):
        """Закриття майстра (X / Esc): від'єднати докачки і вийти негайно.

        Жодного _worker.wait() у GUI-потоці — завислий потік догорить сам
        у reaping-реєстрі (див. _reap_worker)."""
        self._detach_worker()
        self._detach_gpu_worker()
        self._detach_voice_worker()
        super().reject()
