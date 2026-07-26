"""Неруйнівне редагування PCM WAV для панелі аудіо-редактора."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from .audiodsp import AGC_TARGET_DB_DEFAULT, agc


def read_wav(path):
    """Прочитати PCM WAV як ``(audio, sample_rate)``; audio має форму N×канали."""
    with wave.open(str(path), "rb") as wf:
        channels, width = wf.getnchannels(), wf.getsampwidth()
        rate, raw = wf.getframerate(), wf.readframes(wf.getnframes())
    if channels < 1 or width not in (1, 2, 3, 4):
        raise wave.Error("Непідтримуваний PCM WAV")
    if width == 1:
        values = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        ints = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8)
                | (b[:, 2].astype(np.int32) << 16))
        ints[ints & 0x800000 != 0] -= 1 << 24
        values = ints.astype(np.float32) / float(1 << 23)
    else:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float32) / float(1 << 31)
    return values.reshape(-1, channels), int(rate)


def write_wav(path, audio, sample_rate: int) -> Path:
    """Записати float audio без кліпінгу як 16-біт PCM WAV у ``path``."""
    out = Path(path)
    a = np.asarray(audio, dtype=np.float32)
    if a.ndim == 1:
        a = a[:, None]
    if a.ndim != 2 or a.shape[1] < 1:
        raise ValueError("Аудіо мусить мати хоча б один канал")
    out.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.round(np.clip(a, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(a.shape[1])
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm.tobytes())
    return out


def sample_range(sample_rate: int, start_s: float, end_s: float, total: int) -> tuple[int, int]:
    """Часовий інтервал → безпечні індекси семплів [start, end)."""
    start = max(0, min(total, int(round(max(0.0, start_s) * sample_rate))))
    end = max(start, min(total, int(round(max(0.0, end_s) * sample_rate))))
    return start, end


def trim_to_range(audio, sample_rate: int, start_s: float, end_s: float):
    """Залишити лише виділення; вхідний масив не змінюється."""
    a = np.asarray(audio, dtype=np.float32)
    start, end = sample_range(sample_rate, start_s, end_s, len(a))
    return a[start:end].copy()


def cut_range(audio, sample_rate: int, start_s: float, end_s: float):
    """Вирізати виділення й зшити частини в новий масив."""
    a = np.asarray(audio, dtype=np.float32)
    start, end = sample_range(sample_rate, start_s, end_s, len(a))
    return np.concatenate((a[:start], a[end:]), axis=0).copy()


def redact_range(audio, sample_rate: int, start_s: float, end_s: float, *,
                 mode: str = "silence", freq: float = 1000.0, amplitude: float = 0.2):
    """Заглушити виділення тишею (``mode="silence"``) або 1 кГц-біпом (``mode="beep"``).

    Повертає КОПІЮ тієї ж довжини й форми; поза виділенням семпли недоторкані,
    а вхідний масив не змінюється (оригінал завжди лишається цілим)."""
    a = np.array(audio, dtype=np.float32)   # copy=True за замовчуванням
    start, end = sample_range(sample_rate, start_s, end_s, len(a))
    if end > start:
        if mode == "beep":
            t = np.arange(end - start, dtype=np.float32) / float(sample_rate)
            tone = (float(amplitude) * np.sin(2.0 * np.pi * float(freq) * t)).astype(np.float32)
            a[start:end] = tone[:, None] if a.ndim == 2 else tone
        else:
            a[start:end] = 0.0
    return a


def remove_silence(audio, sample_rate: int, *, threshold_db: float = -45.0,
                   frame_ms: int = 20, padding_ms: int = 80):
    """Прибрати тихі RMS-рамки, зберігши невеликі відступи біля мовлення."""
    a = np.asarray(audio, dtype=np.float32)
    if len(a) == 0:
        return a.copy()
    frame = max(1, int(sample_rate * frame_ms / 1000))
    count = (len(a) + frame - 1) // frame
    active = np.zeros(count, dtype=bool)
    limit = float(10.0 ** (threshold_db / 20.0))
    for i in range(count):
        seg = a[i * frame:(i + 1) * frame]
        rms = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2))) if len(seg) else 0.0
        active[i] = rms >= limit
    pad = max(0, int(np.ceil(padding_ms / frame_ms)))
    if active.any() and pad:
        idx = np.flatnonzero(active)
        keep = np.zeros_like(active)
        for i in idx:
            keep[max(0, i - pad):min(count, i + pad + 1)] = True
        active = keep
    parts = [a[i * frame:min(len(a), (i + 1) * frame)]
             for i, on in enumerate(active) if on]
    return np.concatenate(parts, axis=0).copy() if parts else a[:0].copy()


def normalize_archive(audio, *, target_db: float = AGC_TARGET_DB_DEFAULT,
                      limit: float = 0.99):
    """RMS-нормалізація для експорту з обмеженням піку проти кліпінгу."""
    a = np.asarray(audio, dtype=np.float32)
    if a.ndim == 1:
        return agc(a, target_db, limit=limit)
    flat = a.reshape(-1)
    rms = float(np.sqrt(np.mean(flat.astype(np.float64) ** 2))) if len(flat) else 0.0
    if rms <= 1e-10:
        return a.copy()
    gain = float(10.0 ** ((target_db - 20.0 * np.log10(rms)) / 20.0))
    out = a * gain
    peak = float(np.max(np.abs(out)))
    return (out * (limit / peak) if peak > limit else out).astype(np.float32)


def queue_range(source, output, start_s: float, end_s: float, enqueue):
    """Створити окремий WAV виділення та передати його існуючій черзі."""
    audio, rate = read_wav(source)
    written = write_wav(output, trim_to_range(audio, rate, start_s, end_s), rate)
    enqueue(str(written))
    return written
