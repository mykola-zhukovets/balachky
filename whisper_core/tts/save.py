"""«Зберегти озвучення у файл» — санітизація імені, вибір формату, обʼєднання WAV (§8.7).

Стандартний Save As (QFileDialog) живе в GUI; тут — чисте ядро БЕЗ Qt: безпечне ім'я
за назвою наради/дати, перевірка кодера MP3 у нашому `av`, склейка WAV речень в один
файл. Формат MP3 показуємо ЛИШЕ якщо av має mp3-кодер (без нової залежності на lame)."""
from __future__ import annotations

import os
import re

# недопустимі у Windows-іменах символи (+ керівні) — прибираємо
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MAX_STEM = 120
_DEFAULT_STEM = "озвучення"


def sanitize_filename(name: str) -> str:
    """Запропоноване ім'я → безпечний stem (без розширення): прибрати недопустимі
    Windows-символи, обрізати довжину, порожнє/крапки → «озвучення»."""
    stem = _BAD_CHARS.sub("", str(name or "")).strip().strip(".")
    stem = re.sub(r"\s+", " ", stem)
    if len(stem) > _MAX_STEM:
        stem = stem[:_MAX_STEM].rstrip()
    return stem or _DEFAULT_STEM


def mp3_encoder_available() -> bool:
    """Чи має наш `av` (PyAV у стеку) mp3-кодер. Перевірка на старті; інакше формат
    MP3 не показуємо (правило «жодної нової залежності на lame»)."""
    try:
        import av
        for codec in ("libmp3lame", "mp3", "mp3_mf"):
            try:
                av.codec.Codec(codec, "w")
                return True
            except Exception:                    # noqa: BLE001
                continue
    except Exception:                            # noqa: BLE001
        pass
    return False


def enough_free_space(directory: str, needed_bytes: int) -> bool:
    """Перевірка вільного місця перед повним синтезом великого аудіо."""
    try:
        usage = __import__("shutil").disk_usage(directory)
        return usage.free >= max(0, int(needed_bytes))
    except OSError:
        return True                              # не змогли виміряти — не блокуємо


def combine_wavs(wav_paths, out_path: str) -> str:
    """Обʼєднати WAV речень (§3.2) у один WAV. Усі мають однакову частоту/ширину.
    Повертає out_path. Порожній список → порожній валідний WAV."""
    import wave
    wav_paths = [p for p in (wav_paths or []) if p and os.path.isfile(p)]
    params = None
    frames = []
    for p in wav_paths:
        with wave.open(p, "rb") as w:
            if params is None:
                params = w.getparams()
            frames.append(w.readframes(w.getnframes()))
    with wave.open(out_path, "wb") as out:
        if params is not None:
            out.setnchannels(params.nchannels)
            out.setsampwidth(params.sampwidth)
            out.setframerate(params.framerate)
        else:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(24000)
        for chunk in frames:
            out.writeframes(chunk)
    return out_path


def default_stem_from(name: str = "", when=None) -> str:
    """Запропонований stem: назва наради, інакше дата. Завжди санітизований."""
    if name and str(name).strip():
        return sanitize_filename(name)
    import datetime
    when = when or datetime.datetime.now()
    return sanitize_filename(when.strftime("озвучення-%Y-%m-%d"))
