"""Конвеєр діаризації системної доріжки: обмежені читання, вікна, свідки,
глобальна кластеризація, стабільні спани мовців (Slice 3).

Межа модуля: БЕЗ Qt, БЕЗ мережі, БЕЗ import sherpa на рівні модуля. Рантайм
(``diarize.SherpaRuntime`` або фейк у тестах) інжектиться ззовні. Пам'ять
обмежена одним вікном ≤520 с float32 (≤33.3 МБ) — НІКОЛИ не тримаємо годину в RAM.

Алгоритм (див. дизайн §3.2):
  1. читаємо sys-доріжку вікнами 480 с core + 20 с гало (не склеюємо нараду);
  2. локальна auto-діаризація кожного вікна → локальні спани; лишаємо сегмент,
     чий центр у core, і обрізаємо до core (детерміноване прибирання дублів гало);
  3. на кожного локального мовця — один нормалізований CampPlus-вектор зі спанів
     свідка (≥1.5 с, до 12 с сумарно);
  4. ОДНА глобальна кластеризація всіх векторів (auto=поріг 0.5 або фікс. K);
  5. канонізуємо кластери у speaker_01, speaker_02… за найранішим початком;
  6. зливаємо сусідні спани того ж мовця (розрив <100 мс), сортуємо.
Жодного ембединга не серіалізуємо.
"""
from __future__ import annotations

import json
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .word_binding import DiarizationSpan

LEDGER_RATE = 16000
_MERGE_GAP_SAMPLES = int(0.1 * LEDGER_RATE)      # 100 мс
_MIN_EVIDENCE_SAMPLES = int(1.5 * LEDGER_RATE)   # ≥1.5 с на спан-свідок
_PREF_EVIDENCE_SAMPLES = int(3.0 * LEDGER_RATE)  # ідеал 2–4 с, орієнтир 3 с
_MAX_EVIDENCE_SAMPLES = int(12.0 * LEDGER_RATE)  # до 12 с сумарно на мовця/вікно
_MAX_LOCAL_SPEAKERS = 32                          # запобіжник від абсурду/квадрату
_MAX_EVIDENCE_ROWS = 256


@dataclass(frozen=True)
class DiarizationSettings:
    enabled: bool
    num_speakers: "int | None"     # None=auto; інакше 2..10
    distance_threshold: float = 0.5
    core_seconds: int = 480
    halo_seconds: int = 20
    voice_memory_enabled: bool = False
    profile: Any = None


@dataclass(frozen=True)
class DiarizationResult:
    status: str                    # complete|partial|unavailable|failed|cancelled
    spans: tuple                   # tuple[DiarizationSpan, ...]
    speaker_ids: tuple             # tuple[str, ...]
    diagnostics: dict = field(default_factory=dict)
    # Центроїди мовців (БІОМЕТРІЯ). Лише в пам'яті, заповнюються ТІЛЬКИ за згоди.
    # НІКОЛИ не серіалізуються у diarization.final.json — конвеєр складає їх у
    # per-профільне voice_pending/ (поза текою сесії) для бутстрапу голосу.
    voice_centroids: dict = field(default_factory=dict)


class DiarizationInputError(ValueError):
    """Sys-WAV не mono PCM16 16 кГц — конвеєр деградує до звичайного транскрипта."""


class WavTrackReader:
    """Кілька послідовних sys-WAV як одна віртуальна доріжка семплів 16 кГц.

    Валідує кожен файл (mono, 16-біт, 16 кГц) на конструюванні; ``read`` віддає
    вузьке вікно як float32 [-1, 1], не тримаючи всю доріжку в пам'яті.
    """

    def __init__(self, paths, expected_rate: int = 16000):
        self._files = []           # (path, nframes)
        self._offsets = []         # кумулятивний старт кожного файлу у семплах
        total = 0
        for path in paths:
            path = Path(path)
            with wave.open(str(path), "rb") as wav:
                if (wav.getframerate() != expected_rate or wav.getnchannels() != 1
                        or wav.getsampwidth() != 2):
                    raise DiarizationInputError(
                        f"Sys-доріжка має бути mono PCM16 {expected_rate} Гц: {path.name}")
                frames = wav.getnframes()
            self._offsets.append(total)
            self._files.append((path, frames))
            total += frames
        self._total = total

    @property
    def total_samples(self) -> int:
        return self._total

    def read(self, start_sample: int, end_sample: int) -> np.ndarray:
        start = max(0, int(start_sample))
        end = min(self._total, int(end_sample))
        if end <= start:
            return np.empty(0, dtype=np.float32)
        out = []
        for (path, frames), offset in zip(self._files, self._offsets):
            file_end = offset + frames
            if file_end <= start:
                continue
            if offset >= end:
                break
            local_start = max(0, start - offset)
            local_end = min(frames, end - offset)
            with wave.open(str(path), "rb") as wav:
                wav.setpos(local_start)
                raw = wav.readframes(local_end - local_start)
            out.append(np.frombuffer(raw, dtype="<i2"))
        if not out:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(out).astype(np.float32) / 32768.0


def _select_evidence_spans(spans):
    """Обрати спани-свідки: ≥1.5 с, ближче до 3 с — раніше, до 12 с сумарно.

    Якщо жоден спан не дотягує до 1.5 с — беремо найдовший ненульовий (робастність:
    краще слабкий свідок, ніж втратити мовця цілком). Порожньо → ``[]``.
    """
    eligible = [(s, e) for (s, e) in spans if e - s >= _MIN_EVIDENCE_SAMPLES]
    if not eligible:
        longest = max(spans, key=lambda p: p[1] - p[0], default=None)
        eligible = [longest] if longest and longest[1] > longest[0] else []
    eligible.sort(key=lambda p: abs((p[1] - p[0]) - _PREF_EVIDENCE_SAMPLES))
    chosen, total = [], 0
    for s, e in eligible:
        if total >= _MAX_EVIDENCE_SAMPLES:
            break
        chosen.append((s, e))
        total += e - s
    chosen.sort()
    return chosen


def _embed_local_speaker(reader, runtime, spans):
    clips = _select_evidence_spans(spans)
    if not clips:
        return None
    pieces = [reader.read(s, e) for s, e in clips]
    pieces = [p for p in pieces if p.size]
    if not pieces:
        return None
    return runtime.embed(np.concatenate(pieces))


def _merge_and_sort(spans):
    """Злити сусідні спани того ж мовця з розривом <100 мс; відсортувати."""
    ordered = sorted(spans, key=lambda x: (x.start_sample, x.end_sample, x.speaker_id))
    merged: list[DiarizationSpan] = []
    for span in ordered:
        if (merged and merged[-1].speaker_id == span.speaker_id
                and span.start_sample - merged[-1].end_sample < _MERGE_GAP_SAMPLES):
            prev = merged[-1]
            merged[-1] = DiarizationSpan(
                prev.start_sample, max(prev.end_sample, span.end_sample),
                prev.speaker_id)
        else:
            merged.append(span)
    merged.sort(key=lambda x: (x.start_sample, x.end_sample, x.speaker_id))
    return tuple(merged)


def run_system_diarization(paths, settings: DiarizationSettings, *, runtime,
                           cancel=None, progress=None) -> DiarizationResult:
    """Прогнати діаризацію ЛИШЕ по sys-WAV. Ніколи не кидає у продакшн-шлях:
    внутрішні збої стають статусом ``failed`` з порожніми спанами, тож викликач
    публікує звичайний транскрипт."""
    wall_start = time.perf_counter()
    try:
        reader = WavTrackReader(paths)
    except DiarizationInputError as exc:
        return DiarizationResult("failed", (), (), {"error": str(exc)})
    total = reader.total_samples
    if total == 0:
        return DiarizationResult("unavailable", (), (), {"reason": "empty_audio"})

    core = max(1, settings.core_seconds) * LEDGER_RATE
    halo = max(0, settings.halo_seconds) * LEDGER_RATE
    windows = []
    s = 0
    while s < total:
        windows.append((s, min(s + core, total)))
        s += core
    n_windows = len(windows)
    warnings: list[str] = []
    evidence = []   # {"window", "local", "vector", "spans"}
    peak_window = 0

    try:
        for wi, (cs, ce) in enumerate(windows):
            if cancel is not None and cancel():
                return DiarizationResult("cancelled", (), (), {
                    "reason": "cancelled", "windows_done": wi})
            rs = max(0, cs - halo)
            re_ = min(total, ce + halo)
            window_samples = reader.read(rs, re_)
            peak_window = max(peak_window, int(window_samples.size))
            local_spans = runtime.diarize_window(
                window_samples,
                cancel_check=(cancel if cancel is not None else None))
            by_local: dict[str, list] = {}
            for ls, le, spk in local_spans:
                gs, ge = rs + int(ls), rs + int(le)
                mid = (gs + ge) // 2
                if not (cs <= mid < ce):
                    continue                    # дубль гало — прибираємо детерміновано
                cgs, cge = max(gs, cs), min(ge, ce)
                if cge > cgs:
                    by_local.setdefault(str(spk), []).append((cgs, cge))
            if len(by_local) > _MAX_LOCAL_SPEAKERS:
                warnings.append(
                    f"вікно {wi}: {len(by_local)} локальних мовців > "
                    f"{_MAX_LOCAL_SPEAKERS} — пропущено")
                if progress is not None:
                    progress(wi + 1, n_windows)
                continue
            for spk, spk_spans in by_local.items():
                if len(evidence) >= _MAX_EVIDENCE_ROWS:
                    warnings.append("перевищено ліміт рядків свідків — решту пропущено")
                    break
                vector = _embed_local_speaker(reader, runtime, spk_spans)
                if vector is None:
                    warnings.append(f"вікно {wi}/{spk}: нема валідного ембединга")
                    continue
                evidence.append({
                    "window": wi, "local": spk,
                    "vector": np.asarray(vector, dtype=np.float64),
                    "spans": spk_spans})
            if progress is not None:
                progress(wi + 1, n_windows)

        audio_seconds = round(total / LEDGER_RATE, 3)
        if not evidence:
            return DiarizationResult("unavailable", (), (), {
                "reason": "no_evidence", "windows": n_windows,
                "audio_seconds": audio_seconds, "warnings": warnings})

        labels = runtime.cluster(
            [e["vector"] for e in evidence],
            num_speakers=settings.num_speakers,
            distance_threshold=settings.distance_threshold)

        cluster_spans: dict[int, list] = {}
        cluster_key: dict[int, tuple] = {}
        cluster_vectors: dict[int, list] = {}
        for e, lab in zip(evidence, labels):
            for gs, ge in e["spans"]:
                cluster_spans.setdefault(lab, []).append((gs, ge))
            cluster_vectors.setdefault(lab, []).append(e["vector"])
            key = (min(s for s, _ in e["spans"]), e["window"], e["local"])
            if lab not in cluster_key or key < cluster_key[lab]:
                cluster_key[lab] = key
        ordered = sorted(cluster_spans, key=lambda l: cluster_key[l])
        speaker_id_for = {lab: f"speaker_{i + 1:02d}" for i, lab in enumerate(ordered)}

        # Центроїди голосів — БІОМЕТРІЯ. Обчислюємо ЛИШЕ за явної згоди
        # (voice_memory_enabled) і ТІЛЬКИ в пам'яті: зіставляємо зі збереженими
        # голосами (voices.json) та оновлюємо їх. У diarization.final.json і в
        # diagnostics центроїди НЕ потрапляють — інакше вектори течуть у доказовий
        # пакет (evidence._session_files архівує всю теку сесії). Зіставлення
        # наступних нарад іде через voices.json, не через артефакт.
        matched_speaker_names: dict[str, str] = {}
        voice_centroids: dict[str, list[float]] = {}
        if getattr(settings, "voice_memory_enabled", False) and getattr(settings, "profile", None) is not None:
            for lab, vecs in cluster_vectors.items():
                spk_id = speaker_id_for[lab]
                mean_vec = np.mean(vecs, axis=0)
                norm = float(np.linalg.norm(mean_vec))
                if norm > 0.0:
                    mean_vec = mean_vec / norm
                voice_centroids[spk_id] = mean_vec.tolist()
            try:
                from . import voice_memory
                saved_voices = voice_memory.load_voices(settings.profile)
                if saved_voices:
                    for spk_id, centroid in voice_centroids.items():
                        m_name, m_sim = voice_memory.match_voice(centroid, saved_voices)
                        if m_name:
                            matched_speaker_names[spk_id] = m_name
                            voice_memory.add_or_update_voice(settings.profile, m_name, centroid)
            except Exception:
                warnings.append("Помилка зіставлення збережених голосів")

        raw_spans = [
            DiarizationSpan(gs, ge, speaker_id_for[lab])
            for lab, spans in cluster_spans.items()
            for gs, ge in spans]
        spans_out = _merge_and_sort(raw_spans)
        speaker_ids = tuple(speaker_id_for[lab] for lab in ordered)

        wall_seconds = round(time.perf_counter() - wall_start, 3)
        rtf = round(wall_seconds / audio_seconds, 4) if audio_seconds else 0.0
        diagnostics = {
            "audio_seconds": audio_seconds,
            "wall_seconds": wall_seconds,
            "rtf": rtf,
            "windows": n_windows,
            "evidence_rows": len(evidence),
            "peak_window_samples": peak_window,
            "num_speakers_setting": settings.num_speakers,
            "distance_threshold": settings.distance_threshold,
            "overlap_supported": False,
            "matched_speaker_names": matched_speaker_names,
            "warnings": warnings,
        }
        return DiarizationResult("complete", spans_out, speaker_ids, diagnostics,
                                 voice_centroids)
    except Exception as exc:  # діаризація — опційне збагачення, ніколи не валимо ASR
        return DiarizationResult("failed", (), (), {
            "error": str(exc)[:300], "warnings": warnings})


def write_diarization_artifact(path, result: DiarizationResult, provenance: dict) -> None:
    """Записати ``diarization.final.json`` (schema 1) БЕЗ ембедингів.

    Артефакт архівується у доказовий пакет (evidence._session_files), тож
    біометричних векторів (``speaker_centroids``) тут бути НЕ повинно —
    зберігаємо лише мітки (``speaker_ids``), спани та імена зіставлених мовців.
    """
    payload = {
        "schema": 1,
        "status": result.status,
        "overlap_supported": False,
        "speaker_ids": list(result.speaker_ids),
        "spans": [
            {"start_sample": s.start_sample, "end_sample": s.end_sample,
             "speaker_id": s.speaker_id}
            for s in result.spans],
        "matched_speaker_names": result.diagnostics.get("matched_speaker_names", {}),
        "diagnostics": result.diagnostics,
        "provenance": dict(provenance or {}),
    }
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = [
    "DiarizationSettings", "DiarizationResult", "DiarizationInputError",
    "WavTrackReader", "run_system_diarization", "write_diarization_artifact",
    "DiarizationSpan",
]
