"""Тонкий адаптер локальної діаризації системної доріжки через sherpa-onnx.

Межа модуля: БЕЗ Qt, БЕЗ мережі. ``sherpa_onnx`` НІКОЛИ не імпортується на рівні
модуля — лише ліниво всередині ``load_runtime``/``diarize``. Тому весь конвеєр
(вікна, зведення, binding, публікація) стартує й проходить QA-гейт навіть без
встановленого пакета: фіча просто вимкнена (graceful degradation).

Глобальна кластеризація свідками (``cluster_embeddings``) — ЧИСТИЙ numpy:
повнозв'язна (complete-link) агломерація за косинусною ВІДСТАННЮ
``1 - cos_sim``. Це та сама семантика, що документує sherpa для
``FastClusteringConfig.threshold=0.5`` (менший поріг → більше мовців), але
реалізована локально: pinned Python-біндинг sherpa 1.13.4 НЕ експонує метод
кластеризації у ``FastClustering`` (лише конструктор), тож глобальний прохід
робимо власним детермінованим кодом з ідентичною семантикою відстані.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import stat
import wave
from pathlib import Path

import numpy as np

SEGMENTATION_RELATIVE = Path("sherpa-onnx-pyannote-segmentation-3-0") / "model.onnx"
EMBEDDING_NAME = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"

# Косинусна ВІДСТАНЬ (dissimilarity = 1 - cos_sim), НЕ схожість. Менше значення —
# більше кластерів. 0.5 — дефолт sherpa, а не відкалібрована якість (див. дизайн).
DISTANCE_THRESHOLD = 0.5
DEFAULT_THRESHOLD = DISTANCE_THRESHOLD  # сумісність назад (виправлено з 0.7)
LOCAL_NUM_CLUSTERS = -1

# Ліміт лишається ЛИШЕ у façade diarize()/load_wav_f32_16k для ручного smoke;
# продакшн-конвеєр читає доріжку вікнами через WavTrackReader без стелі.
MAX_DIARIZATION_SECONDS = 30 * 60

# Розмір/хеш точно перевіряються з immutable HF-ревізій (див. diarization_models).
MODEL_MANIFEST = {
    SEGMENTATION_RELATIVE: (5992913, "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"),
    Path(EMBEDDING_NAME): (28281164, "aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2"),
}

LEDGER_RATE = 16000


class DiarizationUnavailable(RuntimeError):
    """Діаризація недоступна через відсутній рушій або моделі."""


def validate_speaker_count(count: int | str | None) -> int | None:
    """Валідатор кількості мовців для діаризації: 2..10 inclusively, інакше None (auto)."""
    if count is None:
        return None
    try:
        val = int(count)
        if 2 <= val <= 10:
            return val
    except (TypeError, ValueError):
        pass
    return None


# ── доступність рушія і моделей ─────────────────────────────────────────────

def runtime_available() -> bool:
    """Єдина import-time проба: чи встановлено пакет sherpa_onnx.

    НЕ імпортує сам пакет — лише перевіряє наявність, щоб решта модуля лишалась
    безпечною для збірок без sherpa.
    """
    return importlib.util.find_spec("sherpa_onnx") is not None


def model_dir(configured_dir=None) -> Path:
    """Тека моделей: env для live-тесту/розгортання > конфіг > локальна тека."""
    from whisper_core import paths
    return Path(os.environ.get("BALACHKY_DIAR_MODELS") or configured_dir
                or paths.diarization_models_dir())


def model_paths(configured_dir=None) -> tuple[Path, Path]:
    root = model_dir(configured_dir)
    return root / SEGMENTATION_RELATIVE, root / EMBEDDING_NAME


def _is_reparse_point(path: Path) -> bool:
    """True для symlink/junction та інших Windows reparse points."""
    try:
        info = path.lstat()
    except OSError:
        return True
    return (path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) &
                                      getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _verified_regular_file(path: Path, size: int, digest: str) -> bool:
    if _is_reparse_point(path):
        return False
    try:
        if not path.is_file() or path.stat().st_size != size:
            return False
        checksum = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                checksum.update(block)
        return checksum.hexdigest() == digest
    except OSError:
        return False


def models_available(configured_dir=None) -> bool:
    root = model_dir(configured_dir)
    if _is_reparse_point(root):
        return False
    return all(_verified_regular_file(root / relative, size, digest)
               for relative, (size, digest) in MODEL_MANIFEST.items())


def models_present_fast(configured_dir=None) -> bool:
    """Дешева проба для UI: реальні файли з точним розміром, БЕЗ SHA.

    Повний SHA-хеш (34 МБ×2) робимо лише перед побудовою рантайму у
    ``require_models``; на кожен клік чекбокса він морозив би вкладку.
    """
    root = model_dir(configured_dir)
    if _is_reparse_point(root):
        return False
    for relative, (size, _digest) in MODEL_MANIFEST.items():
        path = root / relative
        if _is_reparse_point(path):
            return False
        try:
            if not path.is_file() or path.stat().st_size != size:
                return False
        except OSError:
            return False
    return True


def require_models(configured_dir=None) -> tuple[Path, Path]:
    segmentation, embedding = model_paths(configured_dir)
    root = model_dir(configured_dir)
    missing = [str(p) for p in (segmentation, embedding)
               if not _verified_regular_file(p, *MODEL_MANIFEST[p.relative_to(root)])]
    if missing:
        raise DiarizationUnavailable("Моделі розпізнавання мовців не завантажено: "
                                    + ", ".join(missing))
    return segmentation, embedding


# ── DLL search-order фікс для frozen/dev на Windows ─────────────────────────

def prepare_windows_dlls() -> None:
    """Виставити пошук DLL так, щоб онтайм ішов зі sherpa_onnx/lib, а не System32.

    Windows може підхопити старий ``onnxruntime.dll`` із System32 і впасти з
    «requested API version [23]». Тримаємо ``os.add_dll_directory`` на теці
    ``sherpa_onnx/lib`` і додаємо її на початок PATH. У frozen це робить
    рантайм-хук; тут — dev-еквівалент. Дешево й ідемпотентно.
    """
    if os.name != "nt":
        return
    spec = importlib.util.find_spec("sherpa_onnx")
    if spec is None or not spec.submodule_search_locations:
        return
    lib = Path(list(spec.submodule_search_locations)[0]) / "lib"
    if not lib.is_dir():
        return
    try:
        os.add_dll_directory(str(lib))
    except (OSError, AttributeError):
        pass
    current = os.environ.get("PATH", "")
    if str(lib) not in current.split(os.pathsep):
        os.environ["PATH"] = str(lib) + os.pathsep + current


# ── глобальна кластеризація свідків (чистий numpy) ──────────────────────────

def cluster_embeddings(features, *, num_speakers: "int | None",
                       distance_threshold: float = DISTANCE_THRESHOLD) -> list[int]:
    """Повнозв'язна агломерація ембедингів за косинусною відстанню.

    ``features`` — послідовність L2-нормалізованих векторів (одна на локального
    мовця/вікно). Повертає мітку кластера для кожного рядка у вхідному порядку.

    Семантика ідентична документованій для sherpa FastClustering:
      distance = 1 - cos_sim; менший поріг → більше кластерів.
    ``num_speakers``:
      * None → auto: зливаємо, доки мінімальна complete-link відстань ≤ поріг;
      * ціле K → зливаємо, доки лишиться K кластерів (або менше, якщо рядків < K).
    Порожній вхід → ``[]``. Один рядок → ``[0]``.
    """
    mat = np.asarray(features, dtype=np.float64)
    if mat.ndim != 2 or mat.shape[0] == 0:
        return []
    n = mat.shape[0]
    if n == 1:
        return [0]
    # Косинусна схожість на нормалізованих векторах = скалярний добуток. Ще раз
    # нормалізуємо тут — стійко до ненормалізованого входу з тестів.
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = mat / norms
    sim = np.clip(unit @ unit.T, -1.0, 1.0)
    dist = 1.0 - sim

    clusters = [[i] for i in range(n)]
    target_k = None
    if num_speakers is not None:
        target_k = max(1, min(int(num_speakers), n))

    def complete_link(a, b):
        return max(dist[i][j] for i in clusters[a] for j in clusters[b])

    while len(clusters) > 1:
        if target_k is not None and len(clusters) <= target_k:
            break
        # найближча пара за complete-link
        best = None
        best_pair = None
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                d = complete_link(a, b)
                if best is None or d < best:
                    best, best_pair = d, (a, b)
        if target_k is None and best > distance_threshold:
            break
        a, b = best_pair
        clusters[a] = clusters[a] + clusters[b]
        del clusters[b]

    labels = [0] * n
    for label, members in enumerate(clusters):
        for i in members:
            labels[i] = label
    return labels


# ── рантайм sherpa (ліниво) ─────────────────────────────────────────────────

def _num_threads(explicit=None) -> int:
    if explicit:
        return max(1, int(explicit))
    cpu = os.cpu_count() or 2
    return min(4, max(1, cpu // 2))


class SherpaRuntime:
    """Один сегментаційний рушій + один екстрактор ембедингів на нараду.

    ``diarize_window`` віддає ЛОКАЛЬНІ спани у семплах 16 кГц з локальними
    мітками (auto-кластеризація в межах вікна). ``embed`` рахує один
    нормалізований CampPlus-вектор для кліпу-свідка. Глобальна кластеризація
    відбувається окремо через ``cluster_embeddings`` (чистий numpy).
    """

    def __init__(self, diarizer, extractor):
        self._diarizer = diarizer
        self._extractor = extractor

    @property
    def embedding_dim(self) -> int:
        # Читаємо з рантайму (CampPlus=192), НЕ хардкодимо у схемі.
        return int(self._extractor.dim)

    def diarize_window(self, samples, *, progress=None, cancel_check=None):
        """→ список (start_sample, end_sample, local_speaker_id:str)."""
        audio = np.asarray(samples, dtype=np.float32)

        def callback(done, total):
            if progress is not None:
                progress(int(done), int(total))
            if cancel_check is not None and cancel_check():
                return 1  # ненульове = перервати
            return 0

        result = self._diarizer.process(audio, callback=callback)
        spans = []
        for seg in result.sort_by_start_time():
            start = int(round(float(seg.start) * LEDGER_RATE))
            end = int(round(float(seg.end) * LEDGER_RATE))
            if end > start:
                spans.append((start, end, str(seg.speaker)))
        return spans

    def embed(self, samples):
        """Один нормалізований ембединг клипу-свідка або None, якщо не готово."""
        audio = np.asarray(samples, dtype=np.float32)
        if audio.size == 0:
            return None
        stream = self._extractor.create_stream()
        stream.accept_waveform(LEDGER_RATE, audio)
        if not self._extractor.is_ready(stream):
            return None
        vector = np.asarray(self._extractor.compute(stream), dtype=np.float32)
        if vector.size != self.embedding_dim:
            return None
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            return None
        return vector / norm

    def cluster(self, features, *, num_speakers,
                distance_threshold: float = DISTANCE_THRESHOLD) -> list[int]:
        return cluster_embeddings(
            features, num_speakers=num_speakers,
            distance_threshold=distance_threshold)


def load_runtime(configured_dir=None, *, num_threads=None) -> SherpaRuntime:
    """Створити рантайм sherpa. Кидає ``DiarizationUnavailable``, якщо пакета або
    моделей нема — викликач деградує до звичайного транскрипта."""
    if not runtime_available():
        raise DiarizationUnavailable(
            "Компонент розпізнавання мовців не встановлено. Встановіть sherpa-onnx.")
    prepare_windows_dlls()
    segmentation, embedding = require_models(configured_dir)
    import importlib
    sherpa_onnx = importlib.import_module("sherpa_onnx")
    threads = _num_threads(num_threads)
    diar_config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(segmentation)),
            num_threads=threads),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(embedding), num_threads=threads),
        # Локальні вікна ЗАВЖДИ auto: учасник може бути відсутній локально.
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=LOCAL_NUM_CLUSTERS, threshold=DISTANCE_THRESHOLD),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    diarizer = sherpa_onnx.OfflineSpeakerDiarization(diar_config)
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(embedding), num_threads=threads))
    return SherpaRuntime(diarizer, extractor)


# ── façade для ручного/живого smoke (не для продакшн-конвеєра) ───────────────

def load_wav_f32_16k(path) -> np.ndarray:
    """Прочитати mono PCM WAV 16 kHz як float32; інші формати відхиляємо чесно."""
    with wave.open(str(path), "rb") as source:
        if source.getframerate() != 16000 or source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise DiarizationUnavailable("Для розпізнавання мовців потрібен mono WAV 16 кГц")
        if source.getnframes() > MAX_DIARIZATION_SECONDS * source.getframerate():
            raise DiarizationUnavailable(f"Façade-діаризація доступна лише для записів до {MAX_DIARIZATION_SECONDS // 60} хвилин")
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def diarize(audio_f32_16k, num_speakers=None, threshold=DISTANCE_THRESHOLD,
            *, configured_dir=None) -> list[tuple[float, float, str]]:
    """Façade суцільної діаризації одного масиву (ручний smoke). Продакшн іде
    вікнами через diarization_pipeline. ``threshold`` — косинусна ВІДСТАНЬ."""
    if not runtime_available():
        raise DiarizationUnavailable("Компонент розпізнавання мовців не встановлено. "
                                    "Встановіть sherpa-onnx.")
    prepare_windows_dlls()
    segmentation, embedding = require_models(configured_dir)
    clusters = int(num_speakers) if num_speakers else -1
    if clusters == 0 or clusters < -1:
        raise ValueError("Кількість співрозмовників має бути додатним числом або порожньою")
    import importlib
    sherpa_onnx = importlib.import_module("sherpa_onnx")
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(segmentation))),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(embedding)),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=clusters, threshold=float(threshold)),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    engine = sherpa_onnx.OfflineSpeakerDiarization(config)
    result = engine.process(np.asarray(audio_f32_16k, dtype=np.float32))
    segments = result.sort_by_start_time()
    return [(float(x.start), float(x.end), str(x.speaker)) for x in segments]


__all__ = [
    "DISTANCE_THRESHOLD", "DEFAULT_THRESHOLD", "LOCAL_NUM_CLUSTERS",
    "MODEL_MANIFEST", "SEGMENTATION_RELATIVE", "EMBEDDING_NAME",
    "DiarizationUnavailable", "validate_speaker_count", "runtime_available", "prepare_windows_dlls",
    "cluster_embeddings", "SherpaRuntime", "load_runtime",
    "model_dir", "model_paths", "models_available", "models_present_fast",
    "require_models",
    "load_wav_f32_16k", "diarize",
]
