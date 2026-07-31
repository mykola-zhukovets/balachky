"""Диктофон: збереження, перелік і видалення простих записів голосу.

feature/player-recordings. Це ЯДРО — БЕЗ Qt і БЕЗ аудіо-заліза: сама робота з
диском і формат файлу. Запис (float32 моно, який дає recorder.Recorder) пишемо
16-бітним PCM WAV — стандартний контейнер, який відкриє і вбудований плеєр
(QtMultimedia), і будь-який системний. Ім'я файлу = локальний час старту
"РРРР-ММ-ДД_гг-хх-сс.wav" (як id сесії наради) — сортується і читається людиною.

Сховище — user_dir()/"recordings" за замовчуванням (frozen —
%LOCALAPPDATA%\\Balachky\\recordings; dev — <репо>/recordings), або тека з
cfg.recordings_dir. ЛОКАЛЬНЕ, поза синхронізованими теками.

Захист від traversal при видаленні — той самий трирубіжний, що в
whisper_core.meeting.session (безпечне ім'я + realpath під коренем).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .paths import anonymize_path

# Безпечне ім'я запису = рівно те, що генерує save_recording: локальний час
# старту "РРРР-ММ-ДД_гг-хх-сс" з опційним суфіксом колізії "-N", розширення .wav.
# Один компонент шляху, без роздільників / ".." — не дає видалити щось поза текою.
_SAFE_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:-\d+)?\.wav$")

_SAMPWIDTH = 2                       # 16-біт PCM


def is_safe_recording_name(name) -> bool:
    """Ім'я безпечне для побудови filesystem-шляху (формат save_recording)."""
    return bool(_SAFE_NAME_RE.match(str(name)))


def _float_to_pcm16(mono: np.ndarray) -> bytes:
    """Float32 [-1, 1] → 16-біт PCM. Кліп-захист: значення поза [-1, 1]
    обрізаються, а не перекручуються при приведенні до int16."""
    clipped = np.clip(mono, -1.0, 1.0)
    return np.round(clipped * 32767.0).astype("<i2").tobytes()


def _alloc_path(root: Path) -> Path:
    """Шлях запису = локальний час старту; колізія (два записи за секунду) →
    суфікс -1, -2… (як у session._alloc_dir)."""
    root.mkdir(parents=True, exist_ok=True)
    base = time.strftime("%Y-%m-%d_%H-%M-%S")
    p = root / f"{base}.wav"
    n = 1
    while p.exists():
        p = root / f"{base}-{n}.wav"
        n += 1
    return p


def save_recording(root, audio, sample_rate: int) -> "Path | None":
    """Записати float32-моно `audio` як 16-біт PCM WAV у теку `root`.
    Порожнє/None аудіо → None (нічого не пишемо). Повертає шлях до файлу."""
    if audio is None or len(audio) == 0:
        return None
    root = Path(root)
    mono = np.asarray(audio, dtype=np.float32).flatten()
    if mono.size == 0:
        return None
    out = _alloc_path(root)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(_SAMPWIDTH)
        w.setframerate(int(sample_rate))
        w.writeframes(_float_to_pcm16(mono))
    return out


#: коротший запис вважаємо випадковим кліком/тишею — файл не зберігаємо
MIN_SECONDS = 0.3


class RecordingWriter:
    """Інкрементальний запис диктофона ПРЯМО на диск (рішення проти росту RAM:
    буферизація всього запису в пам'яті давала б ~500 МБ/год піку — натомість
    кожен блок float32 з audio-callback одразу конвертується у 16-біт PCM і
    дописується у WAV; wave дозволяє дописувати кадри до close, який сам
    виправляє довжини у заголовку). Замок — write кличе audio-потік, а
    close/abort — GUI (та сама межа, що у meeting.session).

    close() → шлях до файлу або None (закоротший за MIN_SECONDS запис —
    файл видаляється: випадковий клік не сміє смітити в теці).
    abort() → файл видаляється завжди (скасування)."""

    def __init__(self, root, sample_rate: int):
        self._path = _alloc_path(Path(root))
        self._rate = int(sample_rate)
        self._wf = wave.open(str(self._path), "wb")
        self._wf.setnchannels(1)
        self._wf.setsampwidth(_SAMPWIDTH)
        self._wf.setframerate(self._rate)
        self._frames = 0
        self._closed = False
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def seconds(self) -> float:
        return self._frames / self._rate if self._rate else 0.0

    def write(self, chunk) -> None:
        """Дописати блок float32 (audio-callback). Після close/abort — no-op."""
        mono = np.asarray(chunk, dtype=np.float32).flatten()
        if mono.size == 0:
            return
        with self._lock:
            if self._closed:
                return
            self._wf.writeframes(_float_to_pcm16(mono))
            self._frames += mono.size

    def _finish(self) -> bool:
        """Закрити WAV (ідемпотентно). True — закрили ми, False — уже закрито."""
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            self._wf.close()
            return True

    def close(self) -> "Path | None":
        """Фіналізувати запис. Закоротший за MIN_SECONDS → видалити, None."""
        self._finish()
        if self._frames < self._rate * MIN_SECONDS:
            self._unlink_quiet()
            return None
        return self._path

    def abort(self) -> None:
        """Скасування: закрити й видалити файл."""
        self._finish()
        self._unlink_quiet()

    def _unlink_quiet(self) -> None:
        try:
            self._path.unlink()
        except OSError:
            pass


@dataclass
class Recording:
    path: Path
    name: str
    created: float          # mtime (секунди epoch)
    duration: float         # секунди
    size: int               # байти


def _wav_duration(path: Path) -> float:
    """Тривалість WAV у секундах; битий/нечитний файл → 0.0."""
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            frames = w.getnframes()
        return round(frames / rate, 3) if rate else 0.0
    except (wave.Error, OSError, EOFError):
        return 0.0


def list_recordings(root) -> list:
    """Усі *.wav у теці, новіші першими (за mtime; тай-брейк — ім'я).
    Тека відсутня → []."""
    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    for p in root.glob("*.wav"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out.append(Recording(path=p, name=p.name, created=st.st_mtime,
                             duration=_wav_duration(p), size=st.st_size))
    out.sort(key=lambda r: (r.created, r.name), reverse=True)
    return out


def _within_root(target: Path, root: Path) -> bool:
    """target ФІЗИЧНО під root (realpath резолвить "..", симлінки, регістр тому)."""
    try:
        rt = os.path.realpath(target)
        rr = os.path.realpath(root)
        return rt != rr and os.path.commonpath([rt, rr]) == rr
    except (ValueError, OSError):
        return False


def delete_recording(root, name) -> bool:
    """Видалити один запис за ІМЕНЕМ у теці `root`. True — видалено; False —
    небезпечне ім'я, поза коренем, або файлу вже нема (повторний виклик безпечний).

    Рубіж 2 — ім'я мусить бути безпечним (формат save_recording); рубіж 3 —
    realpath файлу мусить лежати під root. Інакше відмова + лог, БЕЗ видалення."""
    root = Path(root)
    if not is_safe_recording_name(name):
        logging.warning("delete_recording: небезпечне ім'я %r — відмова", name)
        return False
    target = root / name
    if not _within_root(target, root):
        logging.warning("delete_recording: %r поза сховищем %r — відмова",
                        anonymize_path(target), anonymize_path(root))
        return False
    if not target.exists():
        return False
    try:
        target.unlink()
    except OSError:
        logging.exception("Не вдалося видалити запис %s", anonymize_path(target))
        return False
    return True
