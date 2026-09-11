"""Другий рушій розпізнавання: моделі NeMo (NVIDIA Parakeet-TDT) через sherpa-onnx.
feature/stt-sherpa-parakeet.

Контракт той самий, що й у ``engine.Engine`` (faster-whisper): ``transcribe()``
повертає ``(raw, final, duration_s, words, segments)`` і, за
``include_word_timestamps=True``, шостий елемент ``timed_words`` — тож решта
програми (PTT, файли, нарада, Telegram, живе прев'ю) не розрізняє рушіїв.

Що інакше під капотом:
  • аудіо будь-якої довжини ріжеться Silero-VAD з faster-whisper на мовні
    ділянки; сусідні ділянки з короткою паузою зливаються в чанк до 30 с, довша
    ділянка ріжеться вікнами — один прохід recognizer не отримує годинний запис;
  • модель сама визначає мову серед своїх 25; ``cfg.language`` не передається;
  • пословних ймовірностей модель не дає — ``words`` повертаються з 1.0, і UI
    для цього пресета чесно не малює «непевні слова»;
  • словник термінів діє як постобробка ``apply_glossary`` (як і ``final`` у
    Whisper); біасинг hotwords sherpa-onnx — окремий зріз.

Приватність: жодного виклику в мережу. Пакет моделі шукається лише на диску
(``stt_sherpa_models``); його нема — ``ModelRevisionUnavailable``, щоб чинний
шлях відновлення показав докачку, а не сирий traceback.
"""
from __future__ import annotations

import gc
import os
from pathlib import Path

import numpy as np

from . import stt_sherpa_models as sm
from .config import (VAD_THRESHOLD_DEFAULT, VAD_MIN_SILENCE_MS_DEFAULT,
                     VAD_MIN_SPEECH_MS_DEFAULT)
from .engine import ModelRevisionUnavailable, TranscriptionCancelled
from .terms import Terms, apply_glossary

SAMPLE_RATE = 16000
MAX_CHUNK_SECONDS = 30.0      # верхня межа одного проходу recognizer
MAX_MERGE_GAP_SECONDS = 3.0   # коротшу паузу між мовними ділянками не рвемо
_SPECIAL_PREFIX = "<|"        # службові токени NeMo: <|uk|>, <|pnc|>, <|nospeech|> …
# Початок слова: sherpa-onnx повертає токени NeMo/sentencepiece з ЛІТЕРНИМ пробілом
# спереду (" при", "віт", " світ"); сирий ▁ (U+2581) трапляється у tokens.txt і в інших
# експортах — приймаємо обидва (суд 07.09: з одним ▁ реальний рушій схлопував усі
# слова чанка в одне, а фейковий recognizer тестів цього не бачив).
_WORD_PREFIXES = ("▁", " ")


def _decode_audio(source):
    """Шлях або BytesIO → float32 моно 16 кГц тим самим PyAV-шляхом, що й Whisper."""
    from faster_whisper.audio import decode_audio
    return decode_audio(source, sampling_rate=SAMPLE_RATE)


def _to_waveform(audio) -> np.ndarray:
    if isinstance(audio, np.ndarray):
        data = np.asarray(audio, dtype=np.float32)
        if data.ndim > 1:
            data = data.mean(axis=1)
        return np.ascontiguousarray(data)
    return np.ascontiguousarray(np.asarray(_decode_audio(audio), dtype=np.float32))


def _default_vad(audio: np.ndarray, cfg) -> list:
    """Мовні ділянки у семплах — той самий Silero-VAD і ті самі параметри
    конфігу, що й у faster-whisper (Налаштування → Розпізнавання)."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    options = VadOptions(
        threshold=getattr(cfg, "vad_threshold", VAD_THRESHOLD_DEFAULT),
        min_speech_duration_ms=getattr(cfg, "vad_min_speech_ms", VAD_MIN_SPEECH_MS_DEFAULT),
        min_silence_duration_ms=getattr(cfg, "vad_min_silence_ms", VAD_MIN_SILENCE_MS_DEFAULT),
    )
    return [(int(s["start"]), int(s["end"])) for s in get_speech_timestamps(audio, options)]


def _default_recognizer_factory(paths: dict, cfg):
    """Живий recognizer sherpa-onnx (CPU). Імпорт лінивий: модуль рушія має
    імпортуватись і без sherpa-onnx (тести з фейком, легкі збірки)."""
    import sherpa_onnx
    threads = max(1, min(8, (os.cpu_count() or 2) // 2))
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(paths["encoder.int8.onnx"]),
        decoder=str(paths["decoder.int8.onnx"]),
        joiner=str(paths["joiner.int8.onnx"]),
        tokens=str(paths["tokens.txt"]),
        num_threads=threads,
        sample_rate=SAMPLE_RATE,
        provider="cpu",
        model_type="nemo_transducer",
    )


def _merge_chunks(segments, total_len: int) -> list:
    """[(start, end)] у семплах → чанки для recognizer.

    Сусідні мовні ділянки зливаються (разом із паузою між ними), поки пауза
    коротша за MAX_MERGE_GAP і загальна довжина не перевищує MAX_CHUNK; довша
    ділянка ріжеться вікнами по MAX_CHUNK. Порядок і покриття зберігаються."""
    max_len = int(MAX_CHUNK_SECONDS * SAMPLE_RATE)
    max_gap = int(MAX_MERGE_GAP_SECONDS * SAMPLE_RATE)
    merged = []
    for start, end in segments:
        start, end = max(0, int(start)), min(int(end), int(total_len))
        if end <= start:
            continue
        if merged and start - merged[-1][1] <= max_gap and end - merged[-1][0] <= max_len:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    chunks = []
    for start, end in merged:
        while end - start > max_len:
            chunks.append((start, start + max_len))
            start += max_len
        chunks.append((start, end))
    return chunks


def _words_from_tokens(tokens, timestamps, offset_s: float, chunk_end_s: float) -> list:
    """Токени sentencepiece + часові позначки → слова з початком і кінцем.

    Токен із ▁ або з пробілом спереду починає слово; решта (у т.ч. пунктуація)
    клеїться до поточного; службові <|...|> пропускаються. Кінець слова — початок наступного або кінець
    чанка (модель дає лише моменти появи токенів)."""
    words = []
    for token, stamp in zip(tokens or [], timestamps or []):
        token = str(token or "")
        if not token or token.startswith(_SPECIAL_PREFIX):
            continue
        if token.startswith(_WORD_PREFIXES):
            text = token[1:].lstrip("▁ ")
            if not text:
                continue
            words.append([text, offset_s + float(stamp)])
        elif words:
            words[-1][0] += token
        else:
            words.append([token, offset_s + float(stamp)])
    timed = []
    for i, (text, start) in enumerate(words):
        end = words[i + 1][1] if i + 1 < len(words) else chunk_end_s
        timed.append({"start": start, "end": max(end, start), "word": text})
    return timed


class SherpaEngine:
    """Рушій sherpa-onnx з інтерфейсом ``engine.Engine``."""
    is_available = True

    def __init__(self, cfg, *, recognizer_factory=None, vad_fn=None, model_dir=None):
        self.cfg = cfg
        name = str(getattr(cfg, "model_name", "") or "").strip()
        self._package = sm.package_for(name)
        if self._package is None:
            raise ValueError(f"Не sherpa-пресет: {name!r}")
        self._vad = vad_fn or _default_vad
        target = Path(model_dir) if model_dir else sm.model_dir(name)
        if recognizer_factory is None:
            # Живий рушій вантажиться ЛИШЕ з повністю звіреного пакета на диску.
            if not sm.models_available(target, self._package):
                raise ModelRevisionUnavailable(name, str(target), None, False)
            recognizer_factory = _default_recognizer_factory
        paths = {a.filename: target / a.filename for a in self._package.assets}
        self._recognizer = recognizer_factory(paths, cfg)

    def transcribe(self, audio, terms: Terms | None = None, *,
                   include_word_timestamps=False, should_cancel=None):
        """audio: шлях | BytesIO | ndarray(16 кГц, float32) → як Engine.transcribe."""
        if self._recognizer is None:
            raise RuntimeError("Рушій розпізнавання вже закрито")
        terms = terms or Terms()
        wave = _to_waveform(audio)
        duration = len(wave) / SAMPLE_RATE
        chunks = _merge_chunks(self._vad(wave, self.cfg), len(wave)) if len(wave) else []
        texts, words, segments, timed_words = [], [], [], []
        for start, end in chunks:
            # як у Engine: перевірка перед кожним шматком — вихід із циклу справді
            # припиняє роботу, а не лише відкидає результат
            if should_cancel is not None and should_cancel():
                raise TranscriptionCancelled()
            stream = self._recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, wave[start:end])
            self._recognizer.decode_stream(stream)
            result = stream.result
            text = str(getattr(result, "text", "") or "").strip()
            if not text:
                continue
            start_s, end_s = start / SAMPLE_RATE, end / SAMPLE_RATE
            chunk_words = _words_from_tokens(
                getattr(result, "tokens", None), getattr(result, "timestamps", None),
                start_s, end_s)
            texts.append(text)
            segments.append((start_s, end_s, apply_glossary(text, terms)))
            words.extend((w["word"], 1.0) for w in chunk_words)
            timed_words.extend(chunk_words)
        raw = " ".join(texts).strip()
        final = apply_glossary(raw, terms)
        result = (raw, final, duration, words, segments)
        return result + (timed_words,) if include_word_timestamps else result

    def close(self):
        """Відпустити recognizer (ONNX-сесії, ~650 МБ) детерміновано; ідемпотентно."""
        recognizer, self._recognizer = self._recognizer, None
        del recognizer
        gc.collect()


__all__ = ["SherpaEngine", "SAMPLE_RATE", "MAX_CHUNK_SECONDS", "MAX_MERGE_GAP_SECONDS"]
