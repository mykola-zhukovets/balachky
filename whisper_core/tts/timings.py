"""Караоке-таймінги (§8). ХВИЛЯ 2: наскрізний span-map raw→слова + складання
меж слів у media-ms, злиття речень, cache-key, бінарний пошук активного слова.

Clock domain — media-ms (§8.4): синтез ЗАВЖДИ 1.0x, темп задає лише плеєр, тож
таймінги НЕ перераховуються при зміні швидкості (караоке порівнює start_ms/end_ms з
QMediaPlayer.position(), уже в media-ms). БЕЗ Qt — конверсію UTF-16 викликає Qt-межа.

Span-map (§8.2): normalize дає normalized→raw (NormResult.spans). Тут добудовуємо
нормалізоване-слово→raw-діапазон, а межі токенів (token_durations × frame_hop_ms)
згортаються у слова через phoneme_to_word (токен→індекс нормалізованого слова).
Розгорнуте число/абревіатура дають ОДИН raw-діапазон на весь свій час звучання."""
from __future__ import annotations

import hashlib
import json
import re


# --- координатні хелпери (Хвиля 1) -------------------------------------------

def absolute_codepoint(source_start_cp: int, raw_offset: int) -> int:
    """Абсолютна позиція слова в редакторі (code points) = document anchor +
    зсув у переданому воркеру фрагменті (§8.2)."""
    return int(source_start_cp) + int(raw_offset)


def codepoint_to_utf16(text: str, cp_offset: int) -> int:
    """code-point-offset → UTF-16-offset за ПОВНИМ snapshot тексту редактора
    (умова). Один astral-символ перед словом інакше зсунув би підсвічування."""
    text = text or ""
    cp_offset = max(0, min(int(cp_offset), len(text)))
    return len(text[:cp_offset].encode("utf-16-le")) // 2


def utf16_length(text: str) -> int:
    return len((text or "").encode("utf-16-le")) // 2


# --- span-map: нормалізоване слово → raw-діапазон (§8.2) ----------------------

_WORD_RE = re.compile(r"\S+")


def split_words(text: str) -> list:
    """Розбити текст на слова (непробільні пробіги) з їх char-діапазонами:
    список (word, start, end) у code-point-координатах."""
    return [(m.group(), m.start(), m.end()) for m in _WORD_RE.finditer(text or "")]


def normalized_word_raw_spans(norm_result) -> list:
    """Для кожного слова НОРМАЛІЗОВАНОГО тексту → його діапазон у СИРОМУ тексті
    (через NormResult.spans). У незмінних (identity) пробігах відображення
    позиційне (raw=out+зсув); у РОЗГОРНУТИХ токенах («23»→«двадцять три») усі
    нормалізовані слова колапсують в ОДИН сирий діапазон (§8.2)."""
    spans = []
    for _word, ns, ne in split_words(norm_result.text):
        spans.append((_raw_start(norm_result, ns), _raw_end(norm_result, ne)))
    return spans


def _raw_start(nr, out_pos: int) -> int:
    for os_, oe, rs, re_ in nr.spans:
        if os_ <= out_pos < oe:
            # identity (довжини рівні) → позиційно; інакше (розгортання/стиск) → rs
            return rs + (out_pos - os_) if (oe - os_) == (re_ - rs) else rs
    return nr.spans[-1][3] if nr.spans else out_pos


def _raw_end(nr, out_pos_excl: int) -> int:
    p = out_pos_excl - 1
    for os_, oe, rs, re_ in nr.spans:
        if os_ <= p < oe:
            return rs + (p - os_) + 1 if (oe - os_) == (re_ - rs) else re_
    return nr.spans[-1][3] if nr.spans else out_pos_excl


# --- складання меж слів у media-ms -------------------------------------------

def build_word_timings(token_durations, phoneme_to_word, frame_hop_ms,
                       word_raw_spans, *, source_start_cp: int = 0) -> list:
    """Токенні тривалості (кадри) × hop → межі слів у мс, згорнуті через
    phoneme_to_word, у координатах СИРОГО тексту з document anchor.

    Повертає [{word_index, raw_start, raw_end, start_ms, end_ms}, ...] відсортовано
    за появою. token_durations[i] — кадри i-го токена; phoneme_to_word[i] — індекс
    нормалізованого слова цього токена; word_raw_spans[w] — (raw_start, raw_end)."""
    token_durations = list(token_durations or [])
    phoneme_to_word = list(phoneme_to_word or [])
    if not token_durations or not phoneme_to_word:
        return []
    n = min(len(token_durations), len(phoneme_to_word))
    # кумулятивний старт кожного токена (media-ms)
    starts = []
    t = 0.0
    for d in token_durations[:n]:
        starts.append(t)
        t += float(d) * float(frame_hop_ms)
    ends = [starts[i] + float(token_durations[i]) * float(frame_hop_ms)
            for i in range(n)]
    out = []
    seen = {}
    order = []
    for i in range(n):
        w = phoneme_to_word[i]
        if w not in seen:
            seen[w] = [starts[i], ends[i]]
            order.append(w)
        else:
            seen[w][1] = ends[i]           # розширюємо кінець слова
    for w in order:
        rs, re_ = word_raw_spans[w] if 0 <= w < len(word_raw_spans) else (0, 0)
        s_ms, e_ms = seen[w]
        out.append({
            "word_index": w,
            "raw_start": source_start_cp + int(rs),
            "raw_end": source_start_cp + int(re_),
            "start_ms": int(round(s_ms)),
            "end_ms": int(round(e_ms)),
        })
    return out


def active_word_index(word_timings, position_ms: int) -> int:
    """Індекс у word_timings активного на position_ms слова (бінарний пошук за
    start_ms; слово вважається активним від свого start_ms до start_ms наступного).
    -1 — позиція раніше першого слова."""
    if not word_timings:
        return -1
    lo, hi = 0, len(word_timings) - 1
    if position_ms < word_timings[0]["start_ms"]:
        return -1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if word_timings[mid]["start_ms"] <= position_ms:
            lo = mid
        else:
            hi = mid - 1
    return lo


# --- злиття речень у єдиний потік media-ms (§8.6 combined) --------------------

def merge_sentences(sentence_timings, durations_ms) -> tuple:
    """Змістити per-речення word_timings на кумулятивний offset у combined-потоці.
    sentence_timings[i] — word_timings i-го речення (у локальних media-ms);
    durations_ms[i] — тривалість WAV i-го речення. Повертає (global_word_timings,
    sentence_starts_ms) — для навігації по реченнях (§8.5)."""
    global_words = []
    sentence_starts = []
    offset = 0
    for i, words in enumerate(sentence_timings):
        sentence_starts.append(offset)
        for wt in words:
            shifted = dict(wt)
            shifted["start_ms"] = wt["start_ms"] + offset
            shifted["end_ms"] = wt["end_ms"] + offset
            shifted["sentence"] = i            # ТЕКСТОВА належність (не ms-вікно) — §8.5
            global_words.append(shifted)
        offset += int(durations_ms[i]) if i < len(durations_ms) else 0
    return global_words, sentence_starts


def sentence_at(sentence_starts, position_ms: int) -> int:
    """Індекс речення, що звучить на position_ms (для підсвічування речення й нав)."""
    if not sentence_starts:
        return -1
    idx = -1
    for i, s in enumerate(sentence_starts):
        if s <= position_ms:
            idx = i
        else:
            break
    return idx


def next_sentence_start(sentence_starts, position_ms: int) -> "int | None":
    for s in sentence_starts:
        if s > position_ms:
            return s
    return None


def prev_sentence_start(sentence_starts, position_ms: int) -> "int | None":
    """Початок попереднього речення (з допуском ~250 мс, щоб «назад» на початку
    поточного речення стрибав саме на попереднє, а не лишався)."""
    prev = None
    for s in sentence_starts:
        if s < position_ms - 250:
            prev = s
        else:
            break
    return prev


def wav_duration_ms(path: str) -> int:
    """Тривалість WAV у мс (для offset речень у combined-потоці)."""
    import wave
    try:
        with wave.open(path, "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate() or 1
            return int(round(frames * 1000.0 / rate))
    except Exception:                          # noqa: BLE001
        return 0


# --- cache-key (§8.3) --------------------------------------------------------

def cache_key(normalized_text: str, voice_id: str, voice_rev, engine_version,
              lexicon_rev, sentence_index: int) -> str:
    """Ключ кешу тривалостей = хеш (normalized_text, voice_id, voice_sha/revision,
    engine_version, lexicon_revision, sentence_index). Швидкість у ключ НЕ входить
    (синтез завжди 1.0x). Зміна голосу/словника → інший ключ → старі WAV/timings
    не переграються під новий голос (§8.3)."""
    payload = json.dumps([
        normalized_text or "", str(voice_id), str(voice_rev),
        str(engine_version), str(lexicon_rev), int(sentence_index)],
        ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
