"""Рушій транскрипції: єдиний власник WhisperModel.

Приймає аудіо як шлях / BytesIO / ndarray — faster-whisper декодує сам через
вбудований PyAV (системний ffmpeg НЕ потрібен; перевірено на Telegram .ogg).
На Етапі 2 екземпляр Engine живе в одному inference-потоці за чергою.
"""
import ctypes
import gc
import os
import sys
from pathlib import Path

import ctranslate2
from faster_whisper import WhisperModel

try:
    from huggingface_hub.errors import LocalEntryNotFoundError
except Exception:                    # hf завжди є (залежність faster_whisper)
    LocalEntryNotFoundError = FileNotFoundError

from . import cuda_runtime
from .config import (
    VAD_THRESHOLD_DEFAULT, VAD_MIN_SILENCE_MS_DEFAULT, VAD_MIN_SPEECH_MS_DEFAULT,
    NO_REPEAT_NGRAM_DEFAULT,
)
from .languages import transcribe_language_arg
from .terms import Terms, apply_glossary


# ctranslate2 4.7.2 потребує на CUDA рівно ці дві cuBLAS-бібліотеки; cudnn не
# потрібен (згортки whisper-енкодера йдуть через GEMM) — валідовано на RTX 4070.
# Той самий набір докачує cuda_runtime (feature/gpu).
_CUDA_RUNTIME_DLLS = cuda_runtime.RUNTIME_DLLS


def _load_windows_dll(name: str):
    """Спробувати завантажити DLL зі штатного пошуку та типових тек застосунку."""
    candidates = [name]
    roots = [
        Path(sys.executable).resolve().parent,
        Path(getattr(sys, "_MEIPASS", "")),
        Path(ctranslate2.__file__).resolve().parent,
        cuda_runtime.cuda_dir(),          # докачаний рантайм (feature/gpu)
    ]
    roots.extend(Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p)
    for root in roots:
        candidate = root / name
        if candidate.is_file():
            candidates.append(str(candidate))
    last_error = None
    for candidate in dict.fromkeys(candidates):
        try:
            return ctypes.WinDLL(candidate)
        except OSError as exc:
            last_error = exc
    raise last_error or OSError(f"DLL not found: {name}")


def cuda_runtime_available() -> bool:
    """Чи є і NVIDIA-пристрій, і повний runtime, потрібний цій збірці.

    ``get_cuda_device_count`` сам по собі бачить драйвер/GPU, але не перевіряє
    delay-loaded cuBLAS. Через це GPU виглядав доступним у Налаштуваннях,
    а перша транскрипція падала на відсутньому ``cublas64_12.dll``.

    Джерела рантайму два: докачаний нами (cuda_runtime.runtime_ready) або
    системний/поруч з exe (пошук _load_windows_dll). Досить будь-якого.
    """
    try:
        if ctranslate2.get_cuda_device_count() < 1:
            return False
        if sys.platform != "win32":
            return True
        if cuda_runtime.runtime_ready():          # докачаний рантайм (feature/gpu)
            return True
        # системний CUDA Toolkit / DLL поруч з exe: тримаємо хендли до кінця
        # функції — cublasLt може залежати від раніше завантаженої cublas.
        handles = [_load_windows_dll(name) for name in _CUDA_RUNTIME_DLLS]
        return len(handles) == len(_CUDA_RUNTIME_DLLS)
    except (OSError, RuntimeError):
        return False


def is_cuda_runtime_error(exc: BaseException) -> bool:
    """Вузька класифікація помилок, після яких можна безпечно спробувати CPU."""
    message = str(exc).lower()
    return any(token in message for token in ("cuda", "cublas", "cudnn", "nvrtc"))

# Пін ревізій репозиторіїв моделей (supply-chain): вантажимо саме ці коміти, а не
# «плаваючий» main — навіть якщо upstream переллє ваги, ми лишаємось на звіреній
# версії. Ті самі хеші качає онбординг (fronts/desktop/onboarding.py), тож на
# старті модель береться з локального кешу без жодного мережевого запиту.
# Оновлюючи модель — синхронно міняй хеш ТУТ і в онбордингу.
MODEL_REVISIONS = {
    # Systran/faster-whisper-small @ main (звірено 2026-07-19) — пресет слабких ПК
    "small": "536b0662742c02347bc0e980a01041f333bce120",
    # Systran/faster-whisper-medium @ main (звірено 2026-07-19) — пресет слабких ПК
    "medium": "08e178d48790749d25932bbc082711ddcfdfbc4f",
    # mobiuslabsgmbh/faster-whisper-large-v3-turbo @ main (звірено 2026-07-12)
    "large-v3-turbo": "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
    # Systran/faster-whisper-large-v3 @ main (звірено 2026-07-12)
    "large-v3": "edaa852ec7e145841d8ffdb056a99866b5f0a478",
}


class ModelRevisionUnavailable(Exception):
    """Пінованого знімка моделі нема локально, а старт лишається офлайн
    (local_files_only). Не сирий HF-трейсбек, а типізований сигнал: desktop
    показує відновлення, cli/telegram — читабельний меседж. НЕ вмикаємо мережу —
    лише перетворюємо виняток. has_other_revision: чи є в кеші ІНШИЙ повний
    знімок тієї ж моделі (тоді можливий офлайн-старт наявної ревізії)."""

    def __init__(self, model_name, model_dir, revision, has_other_revision):
        self.model_name = model_name
        self.model_dir = model_dir
        self.revision = revision
        self.has_other_revision = has_other_revision
        super().__init__(
            f"Пінованої ревізії моделі {model_name} "
            f"({revision or 'main'}) нема в локальному кеші "
            f"({model_dir or 'стандартний кеш HuggingFace'}); "
            "старт залишається офлайн — потрібна свідома докачка."
        )


class TranscriptionCancelled(Exception):
    """Розпізнавання урвано на вимогу користувача (кнопка «Скасувати»).

    Не помилка: сигнал, що результату не буде і показувати помилку не треба.
    Кидається з тіла циклу сегментів — генератор faster-whisper припиняє
    декодування на найближчому сегменті, тож робота справді спиняється."""


class ModelAbsentError(Exception):
    """Спроба розпізнати мовлення, коли мовний пакет ще не завантажено
    (NullEngine). Типізований сигнал — фронти показують чесне пояснення,
    а не сирий traceback."""


class NullEngine:
    """Заглушка рушія з тим самим інтерфейсом, що й Engine, але без
    завантаженого мовного пакета розпізнавання (feature/no-model-state).

    Дозволяє застосунку стартувати й жити (вікно, плеєр, історія, запис
    наради, налаштування) без ~3 ГБ моделі: усе, що дійсно потребує
    розпізнавання, ловить ModelAbsentError на виклику .transcribe()."""
    is_available = False

    def __init__(self, cfg=None):
        self.cfg = cfg

    def transcribe(self, *args, **kwargs):
        raise ModelAbsentError(
            "Мовний пакет розпізнавання не завантажено — розпізнавання недоступне.")

    def close(self):
        pass


class Engine:
    is_available = True

    def __init__(self, cfg, revision_override=None):
        self.cfg = cfg
        # revision: за замовчуванням пінований коміт із MODEL_REVISIONS; при
        # само-лікуванні «використати наявну версію» desktop передає фактичний
        # локальний sha (revision=None+local_files_only може не знайти знімок без
        # refs/main). Мережу це НЕ вмикає — local_files_only лишається True.
        revision = (revision_override if revision_override is not None
                    else MODEL_REVISIONS.get(cfg.model_name))
        from .models import (model_snapshot_integrity, repo_for,
                             resolve_cache_dir, resolve_model_state,
                             OTHER_REVISION_PRESENT)
        cache_dir = resolve_cache_dir(cfg.model_dir)
        # local_files_only=True → модель береться ВИКЛЮЧНО з локального кешу, БЕЗ
        # запиту до huggingface.co на старті (приватність: застосунок офлайн за
        # замовчуванням). Наявність кешу гарантує онбординг; там-таки — єдина
        # дозволена докачка з мережі. revision пінує конкретний коміт: докачка
        # за хешем не пише refs/main, тож вантажити теж треба за хешем.
        if (revision == MODEL_REVISIONS.get(cfg.model_name)
                and not model_snapshot_integrity(
                    cache_dir, repo_for(cfg.model_name), revision)):
            state = resolve_model_state(cfg)
            raise ModelRevisionUnavailable(
                cfg.model_name, cache_dir, revision,
                state.state == OTHER_REVISION_PRESENT)
        try:
            self.model = WhisperModel(
                cfg.model_name, device=cfg.device,
                compute_type=cfg.compute_type, download_root=cache_dir,
                local_files_only=True, revision=revision,
            )
        except (LocalEntryNotFoundError, FileNotFoundError) as e:
            # Ці типи однозначно означають, що локального asset моделі бракує,
            # навіть якщо короткий snapshot-check бачить основні файли.
            state = resolve_model_state(cfg)
            raise ModelRevisionUnavailable(
                cfg.model_name, cache_dir,
                MODEL_REVISIONS.get(cfg.model_name),
                state.state == OTHER_REVISION_PRESENT,
            ) from e
        except (RuntimeError, OSError) as e:
            # RuntimeError/OSError також означають CUDA OOM, несумісний compute
            # type, драйвер або DLL. Діалогом моделі їх маскувати не можна:
            # конвертуємо виняток лише коли файлова перевірка цільового snapshot
            # справді показала missing/incomplete/unreadable.
            from .models import model_snapshot_usable
            if model_snapshot_usable(cache_dir, repo_for(cfg.model_name), revision):
                raise
            state = resolve_model_state(cfg)
            raise ModelRevisionUnavailable(
                cfg.model_name, cache_dir,
                MODEL_REVISIONS.get(cfg.model_name),
                state.state == OTHER_REVISION_PRESENT,
            ) from e

    def transcribe(self, audio, terms: Terms | None = None, *,
                   include_word_timestamps=False, should_cancel=None):
        """audio: шлях | BytesIO | ndarray. → (raw, final, duration_s, words, segments).

        words: [(слово, ймовірність), ...] по всіх сегментах — для підсвітки
        непевних слів у стрічці розшифровок (word_timestamps=True вмикає
        пословні ймовірності у faster-whisper).
        segments: [(start, end, text), ...] — таймкоди сегментів для експорту
        в субтитри/docx (текст уже з виправленими за словником термінами)."""
        terms = terms or Terms()
        # VAD (Silero) відсікає тишу/шум перед розпізнаванням. threshold і
        # min_silence — з конфігу (feature/audio-qol: керуються в Налаштуваннях,
        # діють з наступної транскрипції без перезапуску). getattr — сумісність зі
        # старими мок-конфігами тестів, як у решті рушія.
        # feature/multilang-asr: cfg.language може бути "auto" (визначати мову) —
        # для faster-whisper це language=None. Нормалізація в одному місці
        # (whisper_core.languages), тож усі фронти поводяться однаково.
        segments, info = self.model.transcribe(
            audio, language=transcribe_language_arg(self.cfg.language),
            beam_size=self.cfg.beam_size,
            hotwords=terms.hotwords or None, initial_prompt=terms.initial_prompt or None,
            vad_filter=True,
            vad_parameters=dict(
                threshold=getattr(self.cfg, "vad_threshold", VAD_THRESHOLD_DEFAULT),
                min_speech_duration_ms=getattr(
                    self.cfg, "vad_min_speech_ms", VAD_MIN_SPEECH_MS_DEFAULT),
                min_silence_duration_ms=getattr(
                    self.cfg, "vad_min_silence_ms", VAD_MIN_SILENCE_MS_DEFAULT),
            ),
            # feature/model-bottlenecks (під-хвиля 7): анти-повтор проти циклів-
            # галюцинацій на довгих аудіо. getattr — сумісність зі старими
            # мок-конфігами тестів, як у VAD-параметрах вище.
            no_repeat_ngram_size=getattr(
                self.cfg, "no_repeat_ngram_size", NO_REPEAT_NGRAM_DEFAULT),
            word_timestamps=include_word_timestamps,
        )
        texts, words, segs, timed_words = [], [], [], []
        for seg in segments:                     # генератор: один прохід збирає все
            # Скасування: перевіряємо перед кожним сегментом — вихід із циклу
            # припиняє декодування генератора, а не лише відкидає результат.
            if should_cancel is not None and should_cancel():
                raise TranscriptionCancelled()
            txt = seg.text.strip()
            texts.append(txt)
            segs.append((seg.start, seg.end, apply_glossary(txt, terms)))
            for w in (seg.words or []):
                words.append((w.word.strip(), w.probability))
                if include_word_timestamps:
                    timed_words.append({"start": float(w.start), "end": float(w.end),
                                        "word": w.word.strip()})
        raw = " ".join(texts).strip()
        final = apply_glossary(raw, terms)
        result = (raw, final, info.duration, words, segs)
        return result + (timed_words,) if include_word_timestamps else result

    def close(self):
        """Детерміновано відпустити CTranslate2-модель і CUDA-алокації.

        У різних версіях CTranslate2 низькорівнева модель може мати
        ``unload_model``; getattr лишає сумісність із поточною та тестовими
        двійниками. ``torch`` не імпортуємо заради cleanup, але якщо Gemma-side
        вже підтягнув його в процес — спорожнюємо й torch CUDA allocator.
        """
        model, self.model = getattr(self, "model", None), None
        if model is not None:
            inner = getattr(model, "model", None)
            unload = getattr(inner, "unload_model", None)
            if callable(unload):
                try:
                    unload()
                except Exception:
                    pass
            del inner
            del model
        gc.collect()
        torch = sys.modules.get("torch")
        cuda = getattr(torch, "cuda", None) if torch is not None else None
        empty_cache = getattr(cuda, "empty_cache", None)
        if callable(empty_cache):
            try:
                empty_cache()
            except Exception:
                pass
