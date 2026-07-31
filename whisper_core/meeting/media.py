"""Експорт і фрагменти аудіо наради: stdlib WAV + PyAV, без Qt."""
from __future__ import annotations

import os
import tempfile
import wave
from pathlib import Path
import numpy as np

_FORMATS = {"mp3": ("libmp3lame", "mp3"), "m4a": ("aac", "ipod")}

def available_formats() -> dict:
    """{extension: codec}; лише реально доступні PyAV-кодеки."""
    try:
        import av
    except ImportError:
        return {}
    out = {}
    for ext, (codec, _container) in _FORMATS.items():
        try:
            av.codec.Codec(codec, "w")
        except Exception:
            continue
        out[ext] = codec
    return out

def timestamp_range(start: float, end: float, sample_rate: int, total_frames: int) -> tuple[int, int]:
    """Секунди → безпечний напіввідкритий діапазон кадрів [start, end)."""
    total = max(0, int(total_frames))
    first = min(total, max(0, int(round(float(start) * sample_rate))))
    last = min(total, max(first, int(round(float(end) * sample_rate))))
    return first, last

def read_wav(path) -> tuple[np.ndarray, int]:
    """PCM WAV → mono float32 [-1; 1]. Формат WAV наради — 16-bit PCM."""
    with wave.open(str(path), "rb") as src:
        rate, channels, width, frames = src.getframerate(), src.getnchannels(), src.getsampwidth(), src.getnframes()
        if width != 2:
            raise ValueError("Підтримується лише 16-bit PCM WAV")
        raw = src.readframes(frames)
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio[:(audio.size // channels) * channels].reshape(-1, channels).mean(axis=1)
    return audio, rate

def write_wav(path, audio: np.ndarray, sample_rate: int) -> Path:
    """Mono float32 → 16-bit PCM WAV із saturation як останнім захистом."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(np.asarray(audio, dtype=np.float32), -1, 1) * 32767.0).astype("<i2")
    fd, staged_name = tempfile.mkstemp(
        prefix=f".{out.stem}.", suffix=out.suffix, dir=str(out.parent))
    staged = Path(staged_name)
    published = False
    try:
        os.close(fd)
        with wave.open(str(staged), "wb") as dst:
            dst.setnchannels(1); dst.setsampwidth(2); dst.setframerate(int(sample_rate)); dst.writeframes(pcm.tobytes())
        with staged.open("r+b") as durable:
            os.fsync(durable.fileno())
        os.replace(staged, out)
        published = True
    finally:
        if not published:
            staged.unlink(missing_ok=True)
    return out

# feature/clean-mix: анти-кліпінг лімітер міксу.
# Пряма сума доріжок зберігає рівень тихих/поодиноких ділянок, але двоє гучних
# голосів одночасно дають суму > 1.0 → кліпінг при записі в 16-bit PCM. М'який
# tanh-лімітер лишає все нижче порога лінійним (тиха пара не змінюється), а над
# порогом плавно стискає піки у стелю — без різкого «зрізу» й без залежностей.
_LIMIT_THRESHOLD = 0.8   # нижче порога — лінійно, піки не чіпаємо
_LIMIT_CEILING = 0.999   # стеля |виходу|; трохи < 1, щоб 16-bit не саморувало

def soft_limit(mixed: np.ndarray, threshold: float = _LIMIT_THRESHOLD,
               ceiling: float = _LIMIT_CEILING) -> np.ndarray:
    """М'який лімітер: |x|<=threshold лишаємо як є, вище — tanh-стиск у ceiling.
    Гарантує |вихід| < ceiling; поодинокі/тихі піки лишаються незмінними."""
    mixed = np.asarray(mixed, dtype=np.float32)
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    if peak <= threshold:
        return mixed                      # тихо/помірно — не чіпаємо (лінійно)
    room = ceiling - threshold
    mag = np.abs(mixed)
    over = np.maximum(mag - threshold, 0.0)
    shaped = threshold + room * np.tanh(over / room)
    return np.where(mag > threshold, np.sign(mixed) * shaped, mixed).astype(np.float32)

def mix_tracks(tracks: list[np.ndarray], weights: list[float] | None = None) -> np.ndarray:
    """Вирівняти доріжки від нуля, звести зваженою сумою та обмежити піки
    м'яким лімітером — щоб гучні одночасні голоси не спотворювались кліпінгом.

    ``weights[i]`` масштабує ``tracks[i]`` перед сумою (баланс мікшера плеєра:
    гучність доріжки/mute=0/соло). ``None`` або коротший за ``tracks`` список —
    відсутні ваги трактуються як 1.0, тож старі виклики без балансу (рівна
    сума) поводяться як раніше."""
    pairs = []
    for i, a in enumerate(tracks):
        arr = np.asarray(a, dtype=np.float32).reshape(-1)
        if not arr.size:
            continue
        w = float(weights[i]) if weights is not None and i < len(weights) else 1.0
        pairs.append((arr, w))
    if not pairs:
        return np.empty(0, dtype=np.float32)
    n = max(arr.size for arr, _w in pairs)
    mixed = np.zeros(n, dtype=np.float32)
    for arr, w in pairs:
        if w != 0.0:               # вимкнена/заглушена соло доріжка (w=0) не додає нічого
            mixed[:arr.size] += arr * w
    return soft_limit(mixed)

def mix_wavs(paths, weights: list[float] | None = None) -> tuple[np.ndarray, int]:
    loaded = [read_wav(p) for p in paths]
    if not loaded:
        return np.empty(0, dtype=np.float32), 16000
    rates = {rate for _audio, rate in loaded}
    if len(rates) != 1:
        raise ValueError("Доріжки мають різну частоту дискретизації")
    return mix_tracks([audio for audio, _rate in loaded], weights), rates.pop()

def export_balanced_wav(source_paths, output, weights: list[float] | None = None) -> Path:
    """Звести доріжки наради з балансом мікшера плеєра (гучність/mute/соло) у
    НОВИЙ WAV-файл — «Зберегти зведення». Читає лише ``source_paths``, оригінали
    не чіпає. Без PyAV/кодеків (на відміну від ``export_audio``): WAV завжди
    доступний, тож ця кнопка не залежить від наявних кодеків стиснення.

    Ваги йдуть парами до ``source_paths`` за індексом, тому відсутній файл НЕ
    відкидається мовчки: інакше ваги з'їхали б на чужі доріжки, а журнал
    цілісності записав би коефіцієнти, яких у файлі насправді немає (рецензія 31.07)."""
    paths = [Path(p) for p in source_paths]
    if not paths:
        raise ValueError("Немає аудіодоріжки для зведення")
    missing = next((p for p in paths if not p.is_file()), None)
    if missing is not None:
        raise ValueError(f"Доріжка відсутня: {missing}")
    audio, rate = mix_wavs(paths, weights)
    if not audio.size:
        raise ValueError("Доріжки порожні — нічого зводити")
    return write_wav(output, audio, rate)

def export_audio(source_paths, output, fmt: str, bitrate_kbps: int = 128, *, mix: bool = False, weights: list[float] | None = None, start: float | None = None, end: float | None = None) -> Path:
    """Експортувати першу доріжку або мікс у MP3/M4A через PyAV."""
    fmt = (fmt or "").lower()
    if fmt not in available_formats():
        raise RuntimeError(f"Кодек для .{fmt} недоступний у цьому PyAV")
    if weights is not None:
        # З вагами фільтрувати не можна — індекси ваг з'їдуть (див. export_balanced_wav).
        paths = [Path(p) for p in source_paths]
        missing = next((p for p in paths if not p.is_file()), None)
        if missing is not None:
            raise ValueError(f"Доріжка відсутня: {missing}")
    else:
        paths = [Path(p) for p in source_paths if Path(p).is_file()]
    if not paths:
        raise ValueError("Немає аудіодоріжки для експорту")
    audio, rate = mix_wavs(paths, weights) if mix else read_wav(paths[0])
    if start is not None or end is not None:
        first, last = timestamp_range(start or 0.0, end if end is not None else audio.size / rate, rate, audio.size)
        audio = audio[first:last]
    if not audio.size:
        raise ValueError("Обраний фрагмент не містить аудіо")
    import av
    codec, container_fmt = _FORMATS[fmt]
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    fd, staged_name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=output.suffix,
        dir=str(output.parent))
    staged = Path(staged_name)
    published = False
    try:
        os.close(fd)
        container = av.open(str(staged), mode="w", format=container_fmt)
        try:
            stream = container.add_stream(codec, rate=rate)
            stream.layout = "mono"; stream.bit_rate = max(8, int(bitrate_kbps)) * 1000
            frame = av.AudioFrame.from_ndarray(audio.reshape(1, -1), format="fltp", layout="mono")
            frame.sample_rate = rate
            for packet in stream.encode(frame): container.mux(packet)
            for packet in stream.encode(None): container.mux(packet)
        finally:
            container.close()
        with staged.open("r+b") as durable:
            os.fsync(durable.fileno())
        os.replace(staged, output)
        published = True
    finally:
        if not published:
            staged.unlink(missing_ok=True)
    return output
