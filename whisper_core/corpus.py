"""Збирач корпусу точності українського розпізнавання.

Зберігає пари «аудіо-кліп + розпізнаний текст + виправлений текст + метадані»
локально у paths.corpus_dir(): manifest.jsonl (по рядку JSON на зразок) + WAV-
кліпи поруч. Джерело для dev/ab_test.py — A/B кількох моделей проти виправленого
людиною тексту (WER/CER).

Приватність — канон: нічого нікуди не відправляється, усе лишається на диску
користувача. Модуль — ЧИСТЕ ЯДРО: без Qt і без аудіо-заліза, лише робота з диском.

Звідки береться аудіо-кліп:
  • Диктування — float32-буфер останнього запису (recorder.to_audio) передається
    у save_sample(audio=..., sample_rate=...). WAV диктування на диск НЕ
    персиститься сам по собі, тож кліп пишемо саме в момент позначення «погано».
  • Аудіофайли / наради — на диску вже є WAV: передаємо src_wav=<шлях>, копіюємо.
  • Немає ні буфера, ні файлу — зразок пишеться текстовий (wav=None); A/B такий
    зразок чесно пропускає (нема що прогнати через модель).
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import wave
from pathlib import Path

from . import paths, recordings

MANIFEST_NAME = "manifest.jsonl"

# Один процес-локальний замок на дописування manifest.jsonl (запис із GUI-потоку,
# рідкісний). Той самий формат імені кліпу, що й recordings.save_recording.
_LOCK = threading.Lock()


def _manifest_path(root: Path) -> Path:
    return Path(root) / MANIFEST_NAME


def _alloc_wav(root: Path) -> Path:
    """Ім'я кліпу = локальний час; колізія (два за секунду) → суфікс -1, -2…
    Той самий формат, що recordings.save_recording (РРРР-ММ-ДД_гг-хх-сс.wav)."""
    root.mkdir(parents=True, exist_ok=True)
    base = time.strftime("%Y-%m-%d_%H-%M-%S")
    p = root / f"{base}.wav"
    n = 1
    while p.exists():
        p = root / f"{base}-{n}.wav"
        n += 1
    return p


def _wav_duration(path: Path) -> "float | None":
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            frames = w.getnframes()
        return round(frames / rate, 3) if rate else None
    except (wave.Error, OSError, EOFError):
        return None


def save_sample(recognized: str, corrected: str, *,
                audio=None, sample_rate: "int | None" = None,
                src_wav=None, model: str = "", source: str = "",
                profile: str = "", root=None) -> "dict | None":
    """Записати один зразок корпусу. Повертає збережений dict або None (помилка).

    Аудіо-кліп резолвиться в такому порядку:
      audio+sample_rate → пишемо новий WAV (float32-моно, як recordings);
      src_wav           → копіюємо наявний WAV у корпус;
      нічого            → wav=None (текстовий зразок).

    corrected обов'язковий (без нього зразок марний для A/B) — порожній → None.

    profile — ім'я активного словника на момент виправлення (feature/selflearn-dict).
    Прив'язує зразок до словника, щоб щоденник помилок і підказки фільтрувались за
    профілем і НЕ пропонували чужу пару в іншому словнику (спека: «never become
    one-click suggestions for a selected profile»). Порожнє → зразок без прив'язки
    (legacy/глобальний): у профіль-фільтрованому перегляді він не з'явиться.
    """
    corrected = (corrected or "").strip()
    if not corrected:
        return None
    root = Path(root) if root is not None else paths.corpus_dir()
    root.mkdir(parents=True, exist_ok=True)

    wav_name = None
    duration = None
    sr = None
    if audio is not None and sample_rate:
        out = recordings.save_recording(root, audio, int(sample_rate))
        if out is not None:
            wav_name = out.name
            sr = int(sample_rate)
            duration = _wav_duration(out)
    elif src_wav is not None:
        src = Path(src_wav)
        if src.is_file():
            dst = _alloc_wav(root)
            try:
                shutil.copy2(src, dst)
                wav_name = dst.name
                duration = _wav_duration(dst)
                try:
                    with wave.open(str(dst), "rb") as w:
                        sr = w.getframerate()
                except (wave.Error, OSError, EOFError):
                    sr = None
            except OSError:
                wav_name = None

    rec = {
        "ts": round(time.time()),
        "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "wav": wav_name,
        "recognized": (recognized or "").strip(),
        "corrected": corrected,
        "model": model or "",
        "source": source or "",
        "profile": profile or "",
        "duration": duration,
        "sample_rate": sr,
    }
    try:
        with _LOCK:
            with _manifest_path(root).open("a", encoding="utf-8",
                                           newline="\n") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
    except OSError:
        return None
    return rec


def load_samples(root=None, *, profile=None) -> list:
    """Усі зразки з manifest.jsonl (найстаріші першими — порядок дописування).
    До кожного dict додає ключ "wav_path" (Path або None). Биті/порожні рядки
    пропускає; файлу нема → [].

    profile — якщо задано (не None), лишає лише зразки саме цього словника (точний
    збіг поля "profile"). Legacy-зразки без прив'язки під фільтр не потрапляють."""
    root = Path(root) if root is not None else paths.corpus_dir()
    path = _manifest_path(root)
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if profile is not None and (rec.get("profile") or "") != profile:
            continue
        wav = rec.get("wav")
        rec["wav_path"] = (root / wav) if wav else None
        out.append(rec)
    return out


def count(root=None) -> int:
    """Скільки зразків у корпусі (валідних рядків manifest.jsonl)."""
    return len(load_samples(root))
