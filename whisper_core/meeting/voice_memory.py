"""Пам'ять голосів співрозмовників (Т41).

Збереження та ідентифікація центроїдів ембедингів голосів між нарадами.
Біометрія = ЯВНА згода користувача (opt-in).
Сховище: per-профіль (voices.json у теці профілю).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

VOICE_MEMORY_SIMILARITY_THRESHOLD = 0.6
VOICE_SCHEMA = 1
_FILE_NAME = "voices.json"
_PENDING_DIR_NAME = "voice_pending"


def _voices_file(profile) -> Path:
    if hasattr(profile, "voice_memory_path"):
        return profile.voice_memory_path
    if hasattr(profile, "dir"):
        return Path(profile.dir) / _FILE_NAME
    return Path(profile) / _FILE_NAME


def load_voices(profile) -> dict[str, dict[str, Any]]:
    """Завантажити сховище голосів для даного профілю."""
    path = _voices_file(profile)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "voices" in data:
            return dict(data.get("voices", {}))
    except Exception:
        log.exception("Помилка читання voices.json з %s", path)
    return {}


def save_voices(profile, voices: dict[str, dict[str, Any]]) -> None:
    """Зберегти сховище голосів уVoices.json для профілю."""
    path = _voices_file(profile)
    payload = {
        "schema": VOICE_SCHEMA,
        "voices": voices,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        log.exception("Помилка збереження voices.json у %s", path)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _normalize(vector: np.ndarray) -> np.ndarray:
    mat = np.asarray(vector, dtype=np.float64)
    norm = float(np.linalg.norm(mat))
    if norm == 0.0:
        return mat
    return mat / norm


def add_or_update_voice(profile, name: str, embedding_vector: Any) -> dict[str, Any] | None:
    """Додати або оновити центроїд голосу для людини за іменем."""
    clean_name = (name or "").strip()
    if not clean_name:
        return None
    
    vec = _normalize(np.asarray(embedding_vector, dtype=np.float64))
    if vec.size == 0 or np.all(vec == 0):
        return None

    voices = load_voices(profile)
    now = int(time.time())

    if clean_name in voices:
        entry = voices[clean_name]
        old_centroid = np.asarray(entry["centroid"], dtype=np.float64)
        count = int(entry.get("sample_count", 1))
        new_count = count + 1
        # Ковзне середнє з накопиченням зразків
        updated_centroid = _normalize((old_centroid * count + vec) / new_count)
        entry["centroid"] = updated_centroid.tolist()
        entry["sample_count"] = new_count
        entry["updated_at"] = now
    else:
        entry = {
            "name": clean_name,
            "centroid": vec.tolist(),
            "sample_count": 1,
            "created_at": now,
            "updated_at": now,
        }
        voices[clean_name] = entry

    save_voices(profile, voices)
    return entry


def match_voice(
    embedding_vector: Any,
    voices: dict[str, dict[str, Any]],
    threshold: float = VOICE_MEMORY_SIMILARITY_THRESHOLD,
) -> tuple[str | None, float]:
    """Знайти найкращий збіг ембединга зі збереженими голосами."""
    if not voices:
        return None, 0.0

    vec = _normalize(np.asarray(embedding_vector, dtype=np.float64))
    if vec.size == 0 or np.all(vec == 0):
        return None, 0.0

    best_name = None
    best_sim = -1.0

    for name, entry in voices.items():
        centroid = np.asarray(entry.get("centroid", []), dtype=np.float64)
        if centroid.size != vec.size:
            continue
        sim = float(np.dot(vec, centroid))
        if sim > best_sim:
            best_sim = sim
            best_name = name

    if best_sim >= threshold and best_name is not None:
        return best_name, best_sim
    return None, max(0.0, best_sim)


def delete_voice(profile, name: str) -> bool:
    """Видалити один збережений голос за іменем."""
    clean_name = (name or "").strip()
    voices = load_voices(profile)
    if clean_name in voices:
        del voices[clean_name]
        save_voices(profile, voices)
        return True
    return False


def clear_voices(profile) -> int:
    """Видалити всі збережені голоси (і pending-центроїди сесій)."""
    voices = load_voices(profile)
    count = len(voices)
    if count > 0:
        save_voices(profile, {})
    clear_pending_centroids(profile)   # «Видалити всі голоси» чистить і voice_pending
    return count


# ── Pending-центроїди сесій (per-профіль, ПОЗА текою сесії) ───────────────────
# Біометричні центроїди мовців НЕ пишуться в diarization.final.json (той тече у
# доказовий пакет). Щоб бутстрап нового голосу з ренейму працював і після
# рестарту, конвеєр складає центроїди сюди — у теку профілю voice_pending/, яка
# НЕ входить у сесію (evidence чистий) і НЕ в allow-list profile-transfer (Т42).
# Ренейм забирає (take) потрібний запис, enroll-ить у voices.json і видаляє його.

def _pending_dir(profile) -> Path:
    return _voices_file(profile).parent / _PENDING_DIR_NAME


def _pending_file(profile, session_id: str) -> Path:
    return _pending_dir(profile) / f"{session_id}.json"


def save_pending_centroids(profile, session_id: str, centroids: dict[str, Any]) -> None:
    """Скласти центроїди мовців сесії у per-профільне pending-сховище.

    Викликається ЛИШЕ за увімкненої згоди (voice_memory_enabled). Порожній
    словник → файл не створюється взагалі."""
    if not centroids:
        return
    path = _pending_file(profile, session_id)
    now = int(time.time())
    payload = {
        "schema": VOICE_SCHEMA,
        "session_id": str(session_id),
        "created_at": now,
        "centroids": {
            str(spk): {"centroid": list(vec), "created_at": now}
            for spk, vec in centroids.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        log.exception("Помилка збереження voice_pending у %s", path)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def take_pending_centroid(profile, session_id: str, speaker_label: str) -> "list | None":
    """Прочитати центроїд мовця з pending-сховища і ВИДАЛИТИ використаний запис
    (enroll-once). Коли записів у файлі не лишається — файл видаляється."""
    path = _pending_file(profile, session_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Помилка читання voice_pending з %s", path)
        return None
    centroids = data.get("centroids", {})
    if not isinstance(centroids, dict):
        return None
    entry = centroids.pop(str(speaker_label), None)
    if not entry:
        return None
    vec = entry.get("centroid")
    try:
        if centroids:
            data["centroids"] = centroids
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            path.unlink()
    except OSError:
        log.exception("Помилка оновлення voice_pending %s", path)
    return vec


def delete_pending_centroids(profile, session_id: str) -> None:
    """Видалити pending-файл сесії (при видаленні наради)."""
    path = _pending_file(profile, session_id)
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        log.exception("Помилка видалення voice_pending %s", path)


def clear_pending_centroids(profile) -> None:
    """Прибрати всі pending-центроїди профілю."""
    d = _pending_dir(profile)
    if not d.is_dir():
        return
    for p in d.glob("*.json"):
        try:
            p.unlink()
        except OSError:
            log.exception("Помилка видалення voice_pending %s", p)


def list_voices(profile) -> list[dict[str, Any]]:
    """Повернути відсортований список збережених профілів голосів."""
    voices = load_voices(profile)
    items = []
    for name, data in voices.items():
        item = dict(data)
        item["name"] = name
        item["samples_count"] = data.get("sample_count", 1)
        items.append(item)
    items.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
    return items


__all__ = [
    "VOICE_MEMORY_SIMILARITY_THRESHOLD",
    "VOICE_SCHEMA",
    "load_voices",
    "save_voices",
    "add_or_update_voice",
    "match_voice",
    "delete_voice",
    "clear_voices",
    "list_voices",
    "save_pending_centroids",
    "take_pending_centroid",
    "delete_pending_centroids",
    "clear_pending_centroids",
]
