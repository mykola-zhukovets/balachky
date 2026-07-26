"""Фолбек караоке-таймінгів для sherpa-onnx голосів (§4.5).

sherpa-onnx TTS не гарантує пофонемних тривалостей → для караоке синтезований WAV
проганяється через faster-whisper (уже в стеку, MIT), і його word-timestamps
зіставляються з нормалізованим текстом. Вмикається ЛИШЕ коли
`capabilities.native_word_timings == False`; для StyleTTS2/RAD-TTS фолбек не потрібен.

Порядок RAM суворий (§3.3): TTS завершити й вивантажити ПЕРЕД завантаженням faster-
whisper — це робить оркестратор/координатор. Тут — чисте зіставлення (ASR→слова),
тестоване без моделі (ASR-функція інжектується). aeneas/afaligner ВИКЛЮЧЕНІ (AGPL)."""
from __future__ import annotations

ROUTE_NATIVE = "native"
ROUTE_FALLBACK = "fallback"


def route_karaoke(capabilities) -> str:
    """Який шлях таймінгів для голосу: нативний (StyleTTS2/RAD-TTS) чи faster-whisper-
    фолбек (sherpa). За capabilities.native_word_timings."""
    return ROUTE_NATIVE if getattr(capabilities, "native_word_timings", False) \
        else ROUTE_FALLBACK


def align_asr_to_words(word_raw_spans, asr_words, *, source_start_cp: int = 0) -> list:
    """Зіставити ASR word-timestamps із словами нормалізованого тексту → word_timings
    у форматі §8.2. `word_raw_spans[i]` — (raw_start, raw_end) i-го слова; `asr_words`
    — [{"start_ms","end_ms",...}, ...] від faster-whisper по власному виходу.

    Послідовне зіставлення (той самий текст → той самий порядок слів); якщо ASR дав
    менше слів (злиття), решта слів беруть межі останнього ASR-слова; якщо більше —
    зайві ASR-слова ігноруються. Media-ms беруться з ASR."""
    out = []
    n = len(word_raw_spans)
    for i in range(n):
        rs, re_ = word_raw_spans[i]
        a = asr_words[i] if i < len(asr_words) else (asr_words[-1] if asr_words else None)
        if a is None:
            continue
        out.append({
            "word_index": i,
            "raw_start": source_start_cp + int(rs),
            "raw_end": source_start_cp + int(re_),
            "start_ms": int(a.get("start_ms", 0)),
            "end_ms": int(a.get("end_ms", 0)),
        })
    return out


def whisper_word_timestamps(wav_path: str, *, transcribe_fn) -> list:
    """Прогнати faster-whisper по WAV і повернути [{word,start_ms,end_ms}, ...].
    `transcribe_fn(wav_path)` інжектується (реальний faster-whisper на білді/у стеку;
    тести дають фейк). Порядок RAM (TTS unload ПЕРЕД STT) гарантує викликач."""
    words = []
    for seg in transcribe_fn(wav_path) or []:
        for w in seg.get("words", []) or []:
            words.append({
                "word": w.get("word", "").strip(),
                "start_ms": int(round(float(w.get("start", 0.0)) * 1000)),
                "end_ms": int(round(float(w.get("end", 0.0)) * 1000)),
            })
    return words
