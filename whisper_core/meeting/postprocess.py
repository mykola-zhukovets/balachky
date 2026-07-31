"""Пост-обробка сесії наради: сирі сегменти доріжок → WAV для розшифровки,
транскрипти двох доріжок → єдиний зшитий текст із мітками мовців.

Межа модуля (спека §2.1): ЯДРО, БЕЗ Qt і БЕЗ мережі. Уся математика тут —
чиста (numpy + stdlib `wave`), тож покривається юнітами без реального аудіо.

Дві половини:

1. Аудіо (build_wav / build_session_wavs / build_segmented_wavs). Кожна доріжка сесії
   (`<session>/<track>/*.f32`, сирий float32 interleaved, БЕЗ заголовка —
   формат диску §2.2) склеюється, зводиться в моно (середнє каналів),
   ресемплиться 48к→16к і пишеться як 16-біт PCM WAV. Доріжки НЕ змішуються
   між собою (§2.5): змішати їх безповоротно = стерти розділення мовців.
   Фаза запису експортує кожну доріжку ОКРЕМИМИ 10-хв WAV-блоками; постановка
   у чергу розшифровки належить майбутній явній фазі обробки.

2. Транскрипти (stitch / to_transcript_*). Дві доріжки, розшифровані окремо
   (кожна — [(start, end, text), ...] від Engine.transcribe), зшиваються за
   таймкодами в хронологію Utterance з мітками «Я»/«Співрозмовники». Це
   діаризація без окремої моделі — головна структурна перевага над Meetily.
   Одна доріжка (очна розмова) → суцільний транскрипт без міток.

Ресемпл (рішення): scipy у залежностях НЕМА, тож ресемпл 48к→16к — це проста
децимація ×3 з усередненням блоків на numpy (спека §2.5: «ціле відношення»).
Для нетипової частоти пристрою (напр. 44100, не кратне 16000) — запасний
лінійний ресемпл через numpy.interp. Жодних нових важких залежностей.
"""
from __future__ import annotations

import json
import os
import wave
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from . import (DEFAULT_EXPORT_SEGMENT_SECONDS, NATIVE_CHANNELS, NATIVE_RATE,
               TRACK_MIC, TRACK_SYS)

OUT_RATE = 16000               # цільова частота для рушія (як cfg.sample_rate)

# Назви доріжок = імена підтек сесії на диску (контракт §2.2). ЄДИНЕ джерело —
# whisper_core.meeting (Б1): той самий формат диску, без дубля літералів
# (зведено інтегратором wave-2).
_TRACKS = (TRACK_MIC, TRACK_SYS)

# Запасні значення, коли meeting.json відсутній/неповний — з єдиного джерела
# whisper_core.meeting (Б1); рате/канали штатно читаються з meeting.json, а НЕ
# хардкодяться (спека §2.5).
_FALLBACK_RATE = NATIVE_RATE
_FALLBACK_CHANNELS = NATIVE_CHANNELS

# Коди мовців у Utterance.speaker (порівнюємо в коді, показуємо через мітки).
SPK_ME = "me"
SPK_OTHERS = "others"
SPK_SINGLE = "single"

# Нижче цього піку доріжку вважаємо тишею (≈ -80 dBFS) → WAV не пишемо.
_SILENCE_PEAK = 1e-4


# ── аудіо: сегменти → моно 16к WAV ──────────────────────────────────────────

def _read_track_f32(track_dir: Path) -> np.ndarray:
    """Склейка: усі `<track>/*.f32` за зростанням індексу → один float32-масив
    (interleaved, як на диску). Битий хвіст сегмента (довжина не кратна 4 байтам
    float32) відкидається до цілого числа семплів — сесія переживає краш під час
    запису останнього сегмента (спека §2.4: «відновлення після битого сегмента»).
    Порожньо/тека відсутня → масив нульової довжини."""
    if not track_dir.is_dir():
        return np.empty(0, dtype=np.float32)
    parts: list[np.ndarray] = []
    for seg in sorted(track_dir.glob("*.f32")):
        raw = seg.read_bytes()
        usable = len(raw) - (len(raw) % 4)      # ціле число float32
        if usable <= 0:
            continue
        parts.append(np.frombuffer(raw[:usable], dtype=np.float32))
    if not parts:
        return np.empty(0, dtype=np.float32)
    return np.concatenate(parts)


def _to_mono(interleaved: np.ndarray, channels: int) -> np.ndarray:
    """Interleaved float32 → моно. Канали (для стерео — «дві доріжки» L і R)
    вирівнюються по коротшій: хвіст, що не складає повний кадр із `channels`
    семплів, відкидається (frame-alignment). Далі мікс = середнє каналів
    (для стерео = 0.5·L + 0.5·R). channels ≤ 1 → повертаємо як є."""
    if interleaved.size == 0:
        return np.empty(0, dtype=np.float32)
    if channels <= 1:
        return interleaved.astype(np.float32, copy=False)
    frames = interleaved.size // channels           # вирівнювання по коротшій
    if frames == 0:
        return np.empty(0, dtype=np.float32)
    trimmed = interleaved[: frames * channels].reshape(frames, channels)
    return trimmed.mean(axis=1).astype(np.float32)


def _resample(mono: np.ndarray, in_rate: int, out_rate: int) -> np.ndarray:
    """Ресемпл моно-сигналу in_rate→out_rate.

    Кратне відношення (48000/16000 = 3) → децимація з усередненням блоків
    (простий анти-аліас box-фільтр, достатньо для мовлення). Некратне →
    запасний лінійний ресемпл через numpy.interp. in_rate == out_rate → без змін.
    """
    if mono.size == 0 or in_rate == out_rate:
        return mono.astype(np.float32, copy=False)
    if in_rate % out_rate == 0:
        factor = in_rate // out_rate
        frames = mono.size // factor                # вирівнюємо до цілих блоків
        if frames == 0:
            return np.empty(0, dtype=np.float32)
        blocks = mono[: frames * factor].reshape(frames, factor)
        return blocks.mean(axis=1).astype(np.float32)
    # некратне відношення — простий лінійний ресемпл (без scipy)
    out_len = int(round(mono.size * out_rate / in_rate))
    if out_len <= 0:
        return np.empty(0, dtype=np.float32)
    src_x = np.arange(mono.size, dtype=np.float64)
    dst_x = np.linspace(0.0, mono.size - 1, out_len, dtype=np.float64)
    return np.interp(dst_x, src_x, mono).astype(np.float32)


_STREAM_RAW_BYTES = 1024 * 1024
_STREAM_OUT_FRAMES = 64 * 1024


def _iter_mono_chunks(track_dir: Path, channels: int):
    """Потоково читати f32-сегменти й віддавати mono float32 невеликими блоками.

    Межі сегментів та read-блоків можуть розрізати frame; ``carry`` тримає лише
    до ``channels - 1`` семплів, тому результат збігається з old concatenate →
    _to_mono, але пам'ять не залежить від тривалості наради.
    """
    if not track_dir.is_dir():
        return
    carry = np.empty(0, dtype=np.float32)
    for seg in sorted(track_dir.glob("*.f32")):
        with open(seg, "rb") as f:
            while raw := f.read(_STREAM_RAW_BYTES):
                usable = len(raw) - (len(raw) % 4)
                if not usable:
                    continue
                values = np.frombuffer(raw[:usable], dtype=np.float32)
                if carry.size:
                    values = np.concatenate((carry, values))
                full = (values.size // channels) * channels if channels > 1 else values.size
                if full:
                    if channels <= 1:
                        yield values[:full].astype(np.float32, copy=False)
                    else:
                        yield values[:full].reshape(-1, channels).mean(axis=1).astype(np.float32)
                carry = values[full:].copy()
    # Незавершений interleaved frame відповідає поведінці _to_mono: відкинути.


def _track_float_count(track_dir: Path) -> int:
    """Кількість повних float32, не читаючи доріжку в RAM."""
    if not track_dir.is_dir():
        return 0
    total = 0
    for seg in sorted(track_dir.glob("*.f32")):
        try:
            size = seg.stat().st_size
        except OSError:
            continue
        total += size - (size % 4)
    return total // 4


def _read_track_float_range(track_dir: Path, start: int, count: int) -> np.ndarray:
    """Вузьке вікно concatenated float32-доріжки для рідкісного non-integer resample."""
    parts = []
    wanted_end = start + count
    offset = 0
    for seg in sorted(track_dir.glob("*.f32")):
        try:
            usable = seg.stat().st_size - (seg.stat().st_size % 4)
        except OSError:
            continue
        samples = usable // 4
        seg_end = offset + samples
        if seg_end <= start:
            offset = seg_end
            continue
        if offset >= wanted_end:
            break
        local_start = max(0, start - offset)
        local_end = min(samples, wanted_end - offset)
        with open(seg, "rb") as f:
            f.seek(local_start * 4)
            raw = f.read((local_end - local_start) * 4)
        parts.append(np.frombuffer(raw, dtype=np.float32))
        offset = seg_end
    return np.concatenate(parts) if parts else np.empty(0, dtype=np.float32)


def _iter_resampled_mono(track_dir: Path, channels: int, in_rate: int, out_rate: int):
    """Mono → цільова частота блоками; жодного повного треку в пам'яті."""
    if in_rate == out_rate:
        yield from _iter_mono_chunks(track_dir, channels)
        return
    if in_rate % out_rate == 0:
        factor = in_rate // out_rate
        carry = np.empty(0, dtype=np.float32)
        for mono in _iter_mono_chunks(track_dir, channels):
            values = np.concatenate((carry, mono)) if carry.size else mono
            usable = (values.size // factor) * factor
            if usable:
                yield values[:usable].reshape(-1, factor).mean(axis=1).astype(np.float32)
            carry = values[usable:].copy()
        return

    # Некратні rate трапляються рідко. Два індекси й одне вікно вихідного блоку
    # відтворюють linear interpolation без побудови величезного np.linspace.
    total_frames = _track_float_count(track_dir) // max(1, channels)
    out_len = int(round(total_frames * out_rate / in_rate))
    if total_frames <= 0 or out_len <= 0:
        return
    if out_len == 1:
        yield _to_mono(_read_track_float_range(track_dir, 0, channels), channels)[:1]
        return
    step = (total_frames - 1) / (out_len - 1)
    for out_start in range(0, out_len, _STREAM_OUT_FRAMES):
        out_end = min(out_len, out_start + _STREAM_OUT_FRAMES)
        positions = np.arange(out_start, out_end, dtype=np.float64) * step
        first = int(np.floor(positions[0]))
        last = int(np.ceil(positions[-1]))
        raw = _read_track_float_range(track_dir, first * channels,
                                      (last - first + 1) * channels)
        mono = _to_mono(raw, channels)
        source = np.arange(first, first + mono.size, dtype=np.float64)
        yield np.interp(positions, source, mono).astype(np.float32)


def _stream_wav(session_dir: Path, track: str, rate: int, channels: int,
                out_rate: int) -> "Path | None":
    """Потоково створити WAV; temporary output не підміняє готовий файл раніше часу."""
    out_path = session_dir / f"{track}.wav"
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    peak = 0.0
    wrote = False
    try:
        # Файл відкриваємо самі (не рядком-шляхом), щоб дістатись fileno() і
        # fsync-нути перед replace — wave.close() лише пише заголовок і не
        # чіпає диск. Пост-обробка йде вже після живого захоплення, тож один
        # fsync на весь файл (не на кожен блок) не додає затримки запису.
        with tmp_path.open("wb") as fh:
            with wave.open(fh, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(out_rate)
                for mono in _iter_resampled_mono(session_dir / track, channels, rate, out_rate):
                    if not mono.size:
                        continue
                    peak = max(peak, float(np.max(np.abs(mono))))
                    w.writeframes(_float_to_pcm16(mono))
                    wrote = True
            fh.flush()
            os.fsync(fh.fileno())
        if not wrote or peak < _SILENCE_PEAK:
            tmp_path.unlink(missing_ok=True)
            return None
        tmp_path.replace(out_path)
        return out_path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _float_to_pcm16(mono: np.ndarray) -> bytes:
    """Float32 [-1, 1] → 16-біт PCM. Кліп-захист: значення поза [-1, 1]
    (битий сегмент, накопичена сума) обрізаються, а не перекручуються при
    приведенні до int16. Множник 32767 тримає +1.0 у межах int16."""
    clipped = np.clip(mono, -1.0, 1.0)
    return np.round(clipped * 32767.0).astype("<i2").tobytes()


def _read_meta_rate_channels(session_dir: Path) -> tuple[int, int]:
    """rate/channels із meeting.json (§2.5: читаємо, не хардкодимо). Відсутній/
    битий json або поля → запасні NATIVE_RATE/CHANNELS."""
    meta_path = session_dir / "meeting.json"
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _FALLBACK_RATE, _FALLBACK_CHANNELS
    rate = data.get("rate") or _FALLBACK_RATE
    channels = data.get("channels") or _FALLBACK_CHANNELS
    return int(rate), int(channels)


def build_wav(session_dir: Path, track: str, *, out_rate: int = OUT_RATE) -> "Path | None":
    """Сегменти доріжки → mono PCM WAV, потоково й з обмеженою пам'яттю.

    На штатних 48 кГц/стерео у RAM одночасно лише ~1 MiB сирого входу та один
    вихідний блок; 2-годинний запис не масштабує споживання пам'яті.
    """
    session_dir = Path(session_dir)
    rate, channels = _read_meta_rate_channels(session_dir)
    return _stream_wav(session_dir, track, rate, channels, out_rate)


def _session_tracks(session_dir: Path) -> list[str]:
    """Імена доріжок із метаданих; старі/биті сесії мають старий fallback."""
    try:
        tracks = json.loads((session_dir / "meeting.json").read_text(encoding="utf-8")).get("sources")
    except (OSError, ValueError, TypeError):
        tracks = None
    valid = []
    for track in tracks or _TRACKS:
        track = str(track)
        if track and track not in valid and "/" not in track and "\\" not in track and track not in (".", ".."):
            valid.append(track)
    return valid


def build_session_wavs(session_dir: Path) -> dict:
    """Кожна іменована доріжка сесії → WAV, потоково й без змішування."""
    session_dir = Path(session_dir)
    result: dict = {}
    for track in _session_tracks(session_dir):
        wav = build_wav(session_dir, track)
        if wav is not None:
            result[track] = wav
    return result


def _meta_export_segment_seconds(session_dir: Path) -> int:
    try:
        data = json.loads((session_dir / "meeting.json").read_text(encoding="utf-8"))
        value = int(data.get("export_segment_seconds", DEFAULT_EXPORT_SEGMENT_SECONDS))
    except (OSError, TypeError, ValueError):
        value = DEFAULT_EXPORT_SEGMENT_SECONDS
    return max(1, value)


def _segmented_track_wavs(session_dir: Path, track: str, *, rate: int,
                          channels: int, segment_seconds: int,
                          out_rate: int) -> list[Path]:
    """Одна raw-доріжка → атомарні mono PCM16 WAV-блоки однакової довжини."""
    output_dir = session_dir / "audio" / track
    output_dir.mkdir(parents=True, exist_ok=True)
    frame_limit = max(1, int(segment_seconds) * int(out_rate))
    paths = []
    track_peak = 0.0
    writer = None
    writer_fh = None
    tmp_path = None
    frames_in_file = 0

    def open_writer(index):
        nonlocal writer, writer_fh, tmp_path, frames_in_file
        path = output_dir / f"{index:04d}.wav"
        tmp_path = path.with_suffix(".wav.tmp")
        # fileobj, не рядок-шлях: потрібен fileno() для fsync перед replace
        # (див. _stream_wav вище — той самий компроміс: fsync раз на готовий
        # експортний блок, не на кожен фрейм живого запису).
        writer_fh = tmp_path.open("wb")
        writer = wave.open(writer_fh, "wb")
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(out_rate)
        frames_in_file = 0
        return path

    def close_writer(path):
        nonlocal writer, writer_fh, tmp_path
        if writer is None:
            return
        writer.close()
        writer = None
        writer_fh.flush()
        os.fsync(writer_fh.fileno())
        writer_fh.close()
        writer_fh = None
        tmp_path.replace(path)
        tmp_path = None
        paths.append(path)

    current_path = None
    try:
        for mono in _iter_resampled_mono(session_dir / track, channels, rate, out_rate):
            offset = 0
            while offset < mono.size:
                if writer is None:
                    current_path = open_writer(len(paths) + 1)
                count = min(frame_limit - frames_in_file, mono.size - offset)
                part = mono[offset:offset + count]
                track_peak = max(track_peak, float(np.max(np.abs(part))))
                writer.writeframes(_float_to_pcm16(part))
                frames_in_file += count
                offset += count
                if frames_in_file == frame_limit:
                    close_writer(current_path)
                    current_path = None
        if writer is not None:
            close_writer(current_path)
        # Політику тиші вирішує викликач build_segmented_wavs (С2): тиху доріжку
        # серед звучних відкидаємо, але коли ВСІ доріжки тихі — WAV лишаємо, щоб
        # запис не перетворився на «done без аудіо». Тут лише повертаємо пік.
        keep = {path.name for path in paths}
        for stale in output_dir.glob("*.wav"):
            if stale.name not in keep:
                stale.unlink(missing_ok=True)
        return paths, track_peak
    except Exception:
        if writer is not None:
            writer.close()
        if writer_fh is not None:
            writer_fh.close()
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def build_segmented_wavs(session_dir: Path, *, segment_seconds: "int | None" = None,
                          out_rate: int = OUT_RATE) -> dict[str, list[Path]]:
    """Усі доріжки → окремі Whisper-ready WAV-файли, типово по 10 хв.

    Ресемпл потоковий: RAM не залежить від тривалості наради, доріжки ніколи не
    змішуються. Сирі crash-safe ``.f32`` сегменти лишаються джерелом істини.
    """
    session_dir = Path(session_dir)
    rate, channels = _read_meta_rate_channels(session_dir)
    duration = (max(1, int(segment_seconds)) if segment_seconds is not None
                else _meta_export_segment_seconds(session_dir))
    built = {}                     # track -> (paths, peak)
    for track in _session_tracks(session_dir):
        paths, peak = _segmented_track_wavs(
            session_dir, track, rate=rate, channels=channels,
            segment_seconds=duration, out_rate=out_rate)
        if paths:
            built[track] = (paths, peak)
    has_sound = any(peak >= _SILENCE_PEAK for _paths, peak in built.values())
    exports = {}
    for track, (paths, peak) in built.items():
        if has_sound and peak < _SILENCE_PEAK:
            # тиха доріжка серед звучних — WAV викидаємо (шум/зайвий мік)
            for path in paths:
                path.unlink(missing_ok=True)
            continue
        # звучна доріжка АБО (коли всі тихі) лишаємо тишу як чесний артефакт
        exports[track] = paths
    return exports

# ── діаризація: слова faster-whisper → сегменти мовців ─────────────────────

def speaker_segments_from_words(words, diarization_segments) -> tuple[list, dict]:
    """Призначити кожному слову мовця за найбільшим перекриттям.

    Повертає відрізки ``(start, end, text, speaker_id)`` і стабільні дефолтні
    назви ``speaker_id → speaker_1``. Слова без перекриття лишаємо без мітки:
    діаризація може лише додати мітку, але ніколи не має права прибрати текст.
    """
    diar = [(float(s), float(e), str(spk)) for s, e, spk in diarization_segments]
    labels, out, seen = {}, [], []
    for word in words or []:
        start, end, text = float(word["start"]), float(word["end"]), (word["word"] or "").strip()
        if not text:
            continue
        winner, overlap = None, 0.0
        for ds, de, spk in diar:
            amount = max(0.0, min(end, de) - max(start, ds))
            if amount > overlap:
                winner, overlap = spk, amount
        if winner is not None:
            if winner not in seen:
                seen.append(winner)
                labels[winner] = f"speaker_{len(seen)}"
            speaker = labels[winner]
        else:
            speaker = SPK_SINGLE
        # Зливаємо лише сусідні слова того самого мовця: це тримає репліки читабельними.
        if out and out[-1][3] == speaker and start - out[-1][1] < 0.8:
            old = out[-1]
            out[-1] = (old[0], end, (old[2] + " " + text).strip(), old[3])
        else:
            out.append((start, end, text, speaker))
    return out, labels

# ── транскрипт: зшивка двох доріжок за таймкодами ────────────────────────────

def _track_source(track: str) -> str:
    """Доріжка на диску → код джерела репліки: mic → «Я» (me), sys →
    «Співрозмовники» (others), окремий мікрофон multimic (mic1…) → його ж ключ.

    Джерело («хто фізично говорив») — це «діаризація для бідних»: мік і системний
    звук уже пишуться окремими доріжками, тож походження репліки відоме без моделі.
    Тримаємо його ОКРЕМО від ``speaker``: справжня діаризація перезаписує speaker
    мітками мовців, але джерело доріжки при цьому не має губитися."""
    if track == TRACK_MIC:
        return SPK_ME
    if track == TRACK_SYS:
        return SPK_OTHERS
    return track


@dataclass
class Utterance:
    start: float
    end: float
    speaker: str          # SPK_ME | SPK_OTHERS | SPK_SINGLE | speaker_N (діаризація)
    text: str
    # Код джерела доріжки (me/others/mic1…), незалежний від speaker. "" —
    # стара сесія без поля: transcript.json читається як раніше (сумісність назад).
    source: str = ""
    # Посилання на immutable ledger. Порожнє для legacy-транскриптів.
    word_ids: tuple[str, ...] = ()
    # Стабільний ідентифікатор мовця з діаризації (Slice 3): ``speaker_01``…
    # None — доріжка без діаризації (mic) або sys-слово без спану. Позиційні
    # виклики Utterance(start, end, speaker, text) лишаються сумісні.
    speaker_id: "str | None" = None


def _clean_segments(segments) -> list:
    """[(start, end, text), ...] → відкинути порожній текст, привести типи."""
    out = []
    for item in (segments or []):
        start, end, text = item[:3]
        text = (text or "").strip()
        if text:
            extra = (str(item[3]),) if len(item) > 3 else ()
            out.append((float(start), float(end), text, *extra))
    return out


def stitch(mic_segments, sys_segments) -> list:
    """Дві доріжки, розшифровані ОКРЕМО, → єдиний хронологічний список Utterance.
    Обидва WAV стартують з t=0 тієї ж сесії, тож таймкоди прямо порівнянні:
    mic → «Я» (me), sys → «Співрозмовники» (others). Сортуємо за start;
    сусідні репліки одного мовця НЕ зливаємо (MVP: простіше й чесніше).

    Мітки me/others лише коли ОБИДВІ доріжки мають репліки (§2.5: «зшивка
    потрібна лише для двох доріжок»). Якщо непорожня рівно одна доріжка (очна
    розмова або друга доріжка — тиша) → усі репліки speaker=SPK_SINGLE, без
    міток; фронт рендерить суцільним транскриптом. Обидві порожні → []."""
    mic = _clean_segments(mic_segments)
    sys = _clean_segments(sys_segments)

    named_sys = any(len(item) > 3 for item in sys)
    if mic and sys or named_sys:
        tagged = [Utterance(s, e, SPK_ME if mic and sys else SPK_SINGLE, t, source=SPK_ME)
                  for s, e, t, *_ in mic]
        tagged += [Utterance(s, e, (rest[0] if rest else SPK_OTHERS), t, source=SPK_OTHERS)
                   for s, e, t, *rest in sys]
    else:
        # Рівно одна непорожня доріжка: speaker=single (без мітки мовця), але
        # джерело доріжки лишаємо — щоб експорт міг за бажанням показати «Я».
        single, source = (mic, SPK_ME) if mic else (sys, SPK_OTHERS)
        tagged = [Utterance(s, e, SPK_SINGLE, t, source=source) for s, e, t, *_ in single]

    tagged.sort(key=lambda u: u.start)
    return tagged


def stitch_tracks(track_segments: dict[str, object]) -> list[Utterance]:
    """Зшити довільну кількість незалежно розшифрованих доріжок.

    Одна непорожня доріжка лишається суцільною. Для кількох мікрофонів ключ
    ``mic1``/``mic2`` стає кодом мовця й відображається через ``speaker_names``;
    системна доріжка зберігає legacy-мітку «Співрозмовники» або diarization id.
    """
    nonempty = [(track, _clean_segments(segments))
                for track, segments in track_segments.items()]
    nonempty = [(track, segments) for track, segments in nonempty if segments]
    if len(nonempty) <= 1:
        track = nonempty[0][0] if nonempty else ""
        segments = nonempty[0][1] if nonempty else []
        source = _track_source(track) if track else ""
        return [Utterance(s, e, SPK_SINGLE, text, source=source) for s, e, text, *_ in segments]
    utterances = []
    for track, segments in nonempty:
        source = _track_source(track)
        for s, e, text, *extra in segments:
            speaker = extra[0] if track == TRACK_SYS and extra else (
                SPK_ME if track == TRACK_MIC else SPK_OTHERS if track == TRACK_SYS else track)
            utterances.append(Utterance(s, e, speaker, text, source=source))
    utterances.sort(key=lambda u: u.start)
    return utterances

def _fmt_ts(seconds: float) -> str:
    """Секунди від старту → «MM:SS» (або «H:MM:SS» для нарад понад годину)."""
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _speaker_label(speaker: str, *, me_label: str, others_label: str,
                   speaker_names=None, show_source: bool = True) -> "str | None":
    """Код мовця → видима мітка; SPK_SINGLE → None (без мітки).

    ``show_source=False`` глушить лише мітки-джерела «Я»/«Співрозмовники» (чекбокс
    експорту). Справжні мітки мовців — діаризація ``speaker_N`` і власні імена з
    ``speaker_names`` (multimic) — мають ПРІОРИТЕТ і чекбоксом не вимикаються."""
    if speaker_names and speaker in speaker_names:
        return speaker_names[speaker]
    if speaker == SPK_ME:
        return me_label if show_source else None
    if speaker == SPK_OTHERS:
        return others_label if show_source else None
    if speaker.startswith("speaker_"):
        # Локалізацію дефолту передає фронт у speaker_names; ядро не підміняє
        # її жорстко українською для експортів англійського UI.
        return (speaker_names or {}).get(speaker)
    return None


def to_transcript_text(utterances, *, me_label: str, others_label: str,
                       speaker_names=None, show_source: bool = True) -> str:
    """Читабельний .txt: рядки «[MM:SS] Мітка: текст». Для суцільного транскрипту
    (одна доріжка, speaker=single) мітку опускаємо: «[MM:SS] текст».
    ``show_source=False`` прибирає мітки-джерела «Я»/«Співрозмовники» (чекбокс)."""
    lines = []
    for u in utterances:
        ts = _fmt_ts(u.start)
        label = _speaker_label(u.speaker, me_label=me_label, others_label=others_label,
                               speaker_names=speaker_names, show_source=show_source)
        if label:
            lines.append(f"[{ts}] {label}: {u.text}")
        else:
            lines.append(f"[{ts}] {u.text}")
    return "\n".join(lines)


def to_transcript_markdown(utterances, *, me_label: str, others_label: str,
                           speaker_names=None, meta: dict = None,
                           show_source: bool = True) -> str:
    """feature/markdown-export: транскрипт наради → Markdown для Obsidian.
    YAML-frontmatter (date/source/duration/tags) + секції за мітками мовців:
    «## Я» і «## Співрозмовники», під кожним — репліки цього мовця «[MM:SS]
    текст» у хронологічному порядку. Одна доріжка (speaker=single) → без секцій,
    суцільний список. Порожній транскрипт → лише frontmatter. duration беремо з
    останньої репліки, якщо meta його не задав."""
    from whisper_core import export
    m = dict(meta or {})
    if utterances and not m.get("duration"):
        m["duration"] = export.duration_str(max(u.end for u in utterances))
    front = export.build_frontmatter(m)
    if not utterances:
        return front + "\n\n"
    labelled = any(_speaker_label(u.speaker, me_label=me_label, others_label=others_label,
                                  speaker_names=speaker_names, show_source=show_source)
                   for u in utterances)
    parts = [front, ""]
    if not labelled:
        parts.extend(f"[{_fmt_ts(u.start)}] {u.text}" for u in utterances)
        return "\n".join(parts) + "\n"
    # С6: хронологічно, як TXT/JSON. Заголовок мовця з'являється при ЗМІНІ мовця,
    # а не групуванням усіх його реплік разом — інакше MD переставляв би репліки
    # й читач не відновив би, що співрозмовник говорив МІЖ двома моїми.
    prev_speaker = object()
    for u in utterances:
        if u.speaker != prev_speaker:
            label = _speaker_label(
                u.speaker, me_label=me_label, others_label=others_label,
                speaker_names=speaker_names, show_source=show_source)
            # show_source=False глушить джерело-мітку → без заголовка, суцільно.
            if label:
                if parts[-1] != "":
                    parts.append("")
                parts.append(f"## {label}")
            prev_speaker = u.speaker
        parts.append(f"[{_fmt_ts(u.start)}] {u.text}")
    return "\n".join(parts).rstrip("\n") + "\n"


def to_transcript_json(utterances) -> list:
    """[{start, end, speaker, source, text}, ...] — машинний формат transcript.json.
    ``source`` пишемо лише коли він є: старі читачі його ігнорують, а порожнє поле
    не засмічує файл (сумісність в обидва боки)."""
    out = []
    for u in utterances:
        item = {"start": u.start, "end": u.end, "speaker": u.speaker, "text": u.text}
        if getattr(u, "source", ""):
            item["source"] = u.source
        if getattr(u, "word_ids", ()):
            item["word_ids"] = list(u.word_ids)
        # speaker_id пишемо лише коли він є (діаризований sys): старі читачі його
        # ігнорують, а порожнє поле не засмічує файл.
        if getattr(u, "speaker_id", None):
            item["speaker_id"] = u.speaker_id
        out.append(item)
    return out


def write_transcript(session_dir, utterances, *, me_label: str, others_label: str,
                     speaker_names=None, show_source: bool = True,
                     stem: str = "transcript") -> tuple:
    """→ (<stem>.txt, <stem>.json). Мітки передає викликач (локалізація
    у фронті — ядро мов не знає). ``show_source`` керує мітками-джерела у .txt;
    transcript.json завжди несе повний ``source`` (структурне джерело правди).

    ``stem`` дозволяє писати В ОКРЕМИЙ файл (напр. «transcript-redacted») —
    редакція НЕ перезаписує оригінальні transcript.* сесії."""
    session_dir = Path(session_dir)
    txt_path = session_dir / f"{stem}.txt"
    json_path = session_dir / f"{stem}.json"
    # session.write_artifact is atomic for active plaintext sessions and rewrites
    # the authenticated container directly for completed encrypted sessions.
    from .session import write_artifact
    text = to_transcript_text(
        utterances, me_label=me_label, others_label=others_label,
        speaker_names=speaker_names, show_source=show_source)
    structured = json.dumps(to_transcript_json(utterances), ensure_ascii=False, indent=2)
    write_artifact(session_dir, txt_path.name, text.encode("utf-8"))
    write_artifact(session_dir, json_path.name, structured.encode("utf-8"))
    return txt_path, json_path


def write_transcript_text(session_dir, text: str, *, stem: str = "transcript"):
    """Перезаписати ЛИШЕ <stem>.txt відредагованим текстом (feature/
    transcript-editing). transcript.json — структурне джерело (raw) — НЕ чіпаємо.

    Повертає шлях до файлу при успіху, None — при помилці вводу (тека лише для
    читання тощо: некритично, правка лишається в пам'яті картки)."""
    txt_path = Path(session_dir) / f"{stem}.txt"
    try:
        from .session import write_artifact
        write_artifact(Path(session_dir), txt_path.name, text.encode("utf-8"))
    except OSError:
        return None
    return txt_path


def redact_utterances(utterances, start_s: float, end_s: float, *, marker: str = "[вилучено]",
                      source: "str | None" = None,
                      speaker_id: "str | None" = None) -> list:
    """Замінити текст реплік, що перетинають [start, end), на ``marker`` (redaction).

    ``source`` (код доріжки: me/others/mic1…) звужує редакцію до ОДНОГО джерела —
    редагування виділення на mic-доріжці не чіпає репліки sys (обіцянка UI «інші
    голоси залишаться без змін»). ``None`` — затерти всі перекриті репліки
    (одна доріжка / legacy без поля source).

    ``speaker_id`` (діаризований мовець sys: ``speaker_01``…) звужує ще далі —
    редакція одного мовця sys не чіпає іншого мовця sys у той самий момент. Обидва
    предикати діють РАЗОМ, коли задані. Source-only виклики поводяться як раніше.

    Повертає НОВИЙ список; таймкоди, мовці й джерела збережено, вхідні
    ``Utterance`` не змінюються (оригінал недоторканий). Порожній діапазон —
    без змін."""
    out = []
    for u in utterances:
        overlaps = end_s > start_s and float(u.end) > start_s and float(u.start) < end_s
        matches = (source is None or getattr(u, "source", "") == source) and (
            speaker_id is None or getattr(u, "speaker_id", None) == speaker_id)
        if overlaps and matches:
            out.append(replace(u, text=marker))
        else:
            out.append(u)
    return out


def append_transcript_note(session_dir, note: str, *, stem: str = "transcript"):
    """Дописати рядок-примітку про редагування в кінець <stem>.txt.

    Найкраще-зусилля: немає файлу / тека лише для читання → None, без краху."""
    session_dir = Path(session_dir)
    txt_path = session_dir / f"{stem}.txt"
    try:
        from .session import read_artifact
        try:
            existing = read_artifact(session_dir, txt_path.name).decode("utf-8")
        except FileNotFoundError:
            existing = ""
        sep = "" if not existing or existing.endswith("\n") else "\n"
        write_transcript_text(session_dir, f"{existing}{sep}{note}\n", stem=stem)
    except OSError:
        return None
    return txt_path
