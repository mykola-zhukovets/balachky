"""Підпроцес-воркер TTS: `python -m whisper_core.tts.worker` (dev) / окремий EXE (frozen).

Уся важка робота (torch/StyleTTS2/RAD-TTS) ізольована тут — краш нативного шару вбиває
лише цей процес, а не GUI. IPC — ПОТОКОВИЙ по реченнях (§3.2): перше речення →
`chunk_ready` одразу (TTFS < 0.5 c), решта синтезується наперед; `cancel` читається
ОКРЕМИМ control-потоком ПІД ЧАС synthesize (у послідовному loop cancel не прочитався б).

Контракт (див. whisper_core.tts.__init__):
  ← ping                → pong
  ← load_voice{id,voice_id,engine,manifest_path,voice_rev}
                        → voice_loaded{id,voice_id,sample_rate} | error{id,message}
  ← synthesize{id,text,voice_id,source_start_cp,lexicon_snapshot,lexicon_rev,
               voice_rev,want_timings,wav_dir}
                        → accepted{id} | busy{id,active_id}
                          progress{id,sentence,total}
                          chunk_ready{id,sentence,wav_path,start_ms,timings,normalized_text}
                          result{id,sentences,sample_rate} | cancelled{id} | error{id,message}
  ← cancel{id}          → cancelled{id}   (кооперативно між реченнями; далі hard-kill батьком)
  ← shutdown            → процес завершується

ВАЖЛИВО: у stdout — ЛИШЕ наш JSON; логи torch ідуть у stderr. PYTHONUTF8/reconfigure —
кирилиця/апостроф інакше валять процес на cp1251-консолі."""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading

from . import (ENV_FAKE_BACKEND,
               FIRST_CHUNK_MAX_WORDS, MSG_ACCEPTED, MSG_BUSY, MSG_CANCEL,
               MSG_CANCELLED, MSG_CHUNK_READY, MSG_ERROR, MSG_LOAD_VOICE,
               MSG_PING, MSG_PONG, MSG_PROGRESS, MSG_RESULT, MSG_SHUTDOWN,
               MSG_SYNTHESIZE, MSG_VOICE_LOADED)
from .normalize import normalize


# --- порізання на речення + cap першого чанку (§3.2) --------------------------

_SENT_END = re.compile(r'([.!?:])(\s+|$)')


def split_sentences(text: str) -> list:
    """Порізати текст на речення по [.!?:]. Порожні відкидаємо, короткі хвости
    (≤20 символів) приклеюємо до попереднього (як bench.split_to_parts)."""
    text = re.sub(r'(\w[^.,!:?\-])\n', r'\1. ', text or "")
    text = text.replace('\n', ' ')
    parts = []
    buf = ""
    i = 0
    for m in _SENT_END.finditer(text):
        end = m.end(1)
        buf += text[i:end]
        i = m.end()
        if len(buf.strip()) <= 20 and i < len(text):
            buf += " "
            continue
        parts.append(buf.strip())
        buf = ""
    tail = (buf + text[i:]).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def split_sentences_spans(text: str) -> list:
    """Offset-ТОЧНИЙ спліт: [(sentence_slice, start_offset_in_text), ...] БЕЗ зміни
    позицій символів (на відміну від split_sentences, що робить \\n→'. ' і grouping).
    Потрібен караоке (Хвиля 2): raw-координати слів мусять точно лягати в редактор.
    Короткі хвости (≤20) приклеюються (не скидаємо start)."""
    text = text or ""
    out = []
    start = 0
    for m in _SENT_END.finditer(text):
        end = m.end(1)
        seg = text[start:end]
        if len(seg.strip()) <= 20 and end < len(text):
            continue                            # grouping: не скидаємо start
        out.append((seg, start))
        start = m.end()
    if start < len(text) and text[start:].strip():
        out.append((text[start:], start))
    return [(s, o) for (s, o) in out if s.strip()]


def _char_after_nth_word(seg: str, n: int) -> int:
    """Char-позиція у seg ПІСЛЯ n-го слова (для offset-точного різання)."""
    words = list(re.finditer(r"\S+", seg))
    if len(words) <= n:
        return len(seg)
    return words[n - 1].end()


def apply_first_chunk_cap_spans(chunks: list,
                                max_words: int = FIRST_CHUNK_MAX_WORDS) -> list:
    """Offset-ЗБЕРІГАЮЧИЙ cap першого чанку для караоке-гілки (§3.2, блокер TTFS).
    chunks — [(seg, offset), ...] від split_sentences_spans. Якщо перший seg довгий
    (немає крапки — перелік/адреса), ріжемо по м'якій межі (кома/двокрапка/N слів),
    ЗБЕРІГАЮЧИ offset (cut — char-позиція в ОРИГІНАЛЬНОМУ seg, тож span-map лишається
    точним). Перший звук < 0.5 c навіть на 60-словному пункті без крапок."""
    if not chunks:
        return chunks
    seg, offset = chunks[0]
    words = seg.split()
    if len(words) <= max_words:
        return chunks
    head = " ".join(words[:max_words])
    soft = re.search(r'^(.{1,%d}?[,:;])\s' % (len(head) + 20), seg)
    if soft and len(soft.group(1).split()) <= max_words:
        cut = len(soft.group(1))                 # char-позиція в seg (offset-точно)
    else:
        cut = _char_after_nth_word(seg, max_words)
    head_seg = seg[:cut]
    rest_seg = seg[cut:]
    out = [(head_seg, offset)]
    if rest_seg.strip():
        out.append((rest_seg, offset + cut))     # offset зсунуто на cut — span-map точний
    return out + chunks[1:]


def apply_first_chunk_cap(sentences: list, max_words: int = FIRST_CHUNK_MAX_WORDS) -> list:
    """Якщо перше «речення» довге (суцільний абзац без крапки), ріжемо ПЕРШИЙ чанк
    по м'якій межі (кома/двокрапка/N слів), щоб перший звук лишався < 0.5 c навіть
    на довгому вступі (§3.2, умова cap 8-12 слів)."""
    if not sentences:
        return sentences
    first = sentences[0]
    words = first.split()
    if len(words) <= max_words:
        return sentences
    # спробувати м'яку межу (кома/двокрапка) у межах cap
    head = " ".join(words[:max_words])
    soft = re.search(r'^(.{1,%d}?[,:;])\s' % (len(head) + 20), first)
    if soft and len(soft.group(1).split()) <= max_words:
        cut = len(soft.group(1))
    else:
        cut = len(head)
    head_chunk = first[:cut].strip()
    rest = first[cut:].strip()
    out = [head_chunk]
    if rest:
        out.append(rest)
    return out + sentences[1:]


# --- запис WAV (stdlib wave; без soundfile/scipy) ----------------------------

def write_wav(path: str, wav, sample_rate: int) -> None:
    import wave as _wave
    import numpy as np
    arr = np.asarray(wav, dtype=np.float32).reshape(-1)
    peak = float(np.max(np.abs(arr))) if arr.size else 1.0
    if peak > 1.0:
        arr = arr / peak
    pcm = (arr * 32767.0).astype(np.int16)
    with _wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm.tobytes())


def _cleanup_request_files(wav_dir: str) -> None:
    """Прибрати всі .part і s*.wav цього запиту (на cancel/error/shutdown, §3.2)."""
    if not wav_dir or not os.path.isdir(wav_dir):
        return
    try:
        for name in os.listdir(wav_dir):
            if name.endswith(".part") or re.match(r"s\d+\.wav$", name):
                try:
                    os.remove(os.path.join(wav_dir, name))
                except OSError:
                    pass
    except OSError:
        pass


# --- ядро потокового синтезу (тестується без підпроцесу) ---------------------

def synthesize_stream(engine, msg: dict, emit, is_cancelled) -> None:
    """Синтезувати `msg["text"]` по реченнях через `engine`, викликаючи `emit(payload)`
    на кожну подію. `is_cancelled()` перевіряється МІЖ реченнями (кооперативний cancel).

    engine — уже завантажений TtsEngine; msg — тіло synthesize; emit пише JSON;
    is_cancelled — читає прапорець, що його виставляє control-потік."""
    req_id = msg.get("id")
    wav_dir = msg.get("wav_dir") or ""
    want_timings = bool(msg.get("want_timings"))
    # source_start_cp (editor-anchor) НЕ застосовуємо у воркері — вертаємо fragment-
    # relative; anchor додає БАТЬКО (§3.2, ревізія). Поле лишається в IPC для parent.
    speed = 1.0                                   # синтез ЗАВЖДИ 1.0; темп — у плеєрі (§8.4)
    text = msg.get("text", "")

    # want_timings → offset-ТОЧНИЙ спліт (караоке raw-координати) + offset-зберігаючий
    # cap першого чанку (TTFS < 0.5 c гарантований і для playback з караоке — блокер
    # суду: «Прослухати» завжди йде через want_timings=True). Без караоке — звичайний
    # спліт із cap.
    if want_timings:
        chunks = apply_first_chunk_cap_spans(split_sentences_spans(text))
    else:
        chunks = [(s, None) for s in
                  apply_first_chunk_cap(split_sentences(text))]
    total = len(chunks)
    # словник вимови (Хвиля 4, §6.2): PronPipeline зі знімка профілю. text_replace
    # застосовуємо ТУТ (span-map під нашим контролем); stress/phonetic — в адаптері.
    from .lexicon import PronPipeline
    pipeline = PronPipeline.from_ipc(msg.get("lexicon_snapshot") or [])
    caps = engine.capabilities()
    sample_rate = caps.sample_rate

    if is_cancelled():
        emit({"type": MSG_CANCELLED, "id": req_id})
        _cleanup_request_files(wav_dir)
        return

    done = 0
    for i, (sentence, offset) in enumerate(chunks):
        if is_cancelled():
            emit({"type": MSG_CANCELLED, "id": req_id})
            _cleanup_request_files(wav_dir)
            return
        emit({"type": MSG_PROGRESS, "id": req_id, "sentence": i, "total": total})
        norm_res = normalize(sentence)
        norm_res = pipeline.apply_text_replace(norm_res)   # §6.2 рівень 1 (span-map цілий)
        res = engine.synthesize(norm_res.text, speed=speed, want_timings=want_timings,
                                lexicon=pipeline)          # stress/phonetic — в адаптері
        part = os.path.join(wav_dir, f"s{i}.wav.part")
        final = os.path.join(wav_dir, f"s{i}.wav")
        write_wav(part, res.wav, res.sample_rate or sample_rate)
        os.replace(part, final)                   # атомарно: плеєр читає лише готові
        timings = None
        if want_timings and res.token_durations and res.phoneme_to_word:
            from . import timings as _t
            word_raw_spans = _t.normalized_word_raw_spans(norm_res)
            # FRAGMENT-relative (§3.2, ревізія): додаємо лише offset речення в
            # ФРАГМЕНТІ, НЕ editor-anchor. Karaoke Хвилі 2 працює в КОПІЇ ListenPanel
            # (anchor=0). Editor-anchor (source_start_cp) додає БАТЬКО, якщо колись
            # підсвічуватиме повний документ — так synthesis і editor coordinate
            # systems не змішуються у воркері.
            timings = _t.build_word_timings(
                res.token_durations, res.phoneme_to_word, res.frame_hop_ms,
                word_raw_spans, source_start_cp=(offset or 0))
        emit({"type": MSG_CHUNK_READY, "id": req_id, "sentence": i,
              "wav_path": final, "start_ms": 0,
              "timings": timings, "normalized_text": res.normalized_text})
        done += 1
    emit({"type": MSG_RESULT, "id": req_id, "sentences": done,
          "sample_rate": sample_rate})


# --- рушій за запитом (fake за ENV або коли torch недоступний) ----------------

def make_engine(engine_kind: str, manifest_path: str):
    """Створити й завантажити рушій. FakeTtsEngine — ЛИШЕ за тестовою env-змінною
    ENV_FAKE_BACKEND; у ПРОДІ (без неї) збій завантаження рушія (немає torch/моделі)
    підіймає EngineLoadError, яку воркер повертає як IPC `error` — НІКОЛИ тиха
    заглушка за успіх (критична ревізія: відсутня модель ≠ тиша)."""
    from .engines import create_engine
    if os.environ.get(ENV_FAKE_BACKEND):
        eng = create_engine("fake")
        eng.load(manifest_path)
        return eng
    eng = create_engine(engine_kind)              # EngineLoadError НЕ ловимо — нехай
    eng.load(manifest_path)                        # спливе у воркер → IPC error
    return eng


# --- прогрів важких імпортів рушіїв (фікс import-lock deadlock, frozen Windows) --

# Модулі, які ПРОГРІВАЄМО в run() ОДНОПОТОКОВО, ДО старту reader-потоку. Порядок:
# найважче спільне першим — `torch`, далі `styletts2_inference.models` (тягне torch/
# torchaudio/transformers/librosa/huggingface_hub одним імпортом), потім укр-G2P і
# стек RAD-TTS. `styletts2_inference.models` та підмодулі `tts_uk.*` — лише class/def
# на рівні модуля (звірено), моделі НЕ вантажать.
# ⚠ `tts_uk.inference` СВІДОМО відсутній: він на рівні МОДУЛЯ робить hf_hub_download +
# torch.load з відносного `models/…` (CWD) і без теки голосу падає — його прогрів
# зламав би старт. Його важкі залежності прогріваємо натомість через `vocos.pretrained`
# і `tts_uk.radtts`/`tts_uk.data`, тож коли radtts.load() зробить `import tts_uk.inference`
# при живому reader — усі C-розширення вже в sys.modules (лишається тільки file-I/O
# завантаження ваг, а не import-lock).
_WARMUP_MODULES = (
    "numpy",
    "torch",
    "styletts2_inference.models",
    "ipa_uk",
    "ukrainian_word_stress",
    "vocos.pretrained",
    "tts_uk.radtts",
    "tts_uk.data",
)


def _warm_engine_imports(modules=_WARMUP_MODULES, import_module=None, log=None):
    """Прогріти важкі імпорти рушіїв ОДНОПОТОКОВО (перед стартом daemon-reader).

    Корінь бага: раніше run() грів лише numpy, а torch/styletts2_inference/ipa_uk/
    ukrainian_word_stress (styletts2.load) і `import tts_uk.inference` (radtts.load)
    імпортувались на головному потоці ПРИ ЖИВОМУ reader, заблокованому на stdin-пайпі
    → детермінований import-lock/DLL-loader-lock deadlock у frozen Windows (voice_loaded
    не приходив, CPU процесу заморожений). Прогрів робить ці імпорти single-threaded,
    поки reader ще не піднято, тож у load() лишаються лише кеш-хіти + file-I/O ваг.

    Tolerant: відсутній модуль (білд без torch) чи будь-яка помилка імпорту = пропуск,
    деградація як раніше (load() потім чесно поверне EngineLoadError через IPC). Повертає
    список успішно прогрітих модулів (для юніт-тесту й чесного таймінгу в stderr)."""
    import importlib
    import time
    imp = import_module or importlib.import_module
    warmed = []
    for name in modules:
        t0 = time.monotonic()
        try:
            imp(name)
        except Exception as exc:                  # noqa: BLE001 — ImportError/інше: тихо деградуємо
            if log is not None:
                log(f"[tts-warmup] skip {name}: {type(exc).__name__}: {exc}")
            continue
        warmed.append(name)
        if log is not None:
            log(f"[tts-warmup] {name} {time.monotonic() - t0:.2f}s")
    return warmed


# --- воркер із control-потоком -----------------------------------------------

class TtsWorker:
    """Стан воркера: активний рушій + один активний synthesize.

    control-потік читає stdin і кладе керівні прапорці (cancel/shutdown) + робочі
    повідомлення (load_voice/synthesize) у черги; головний потік синтезує."""

    def __init__(self, stdin, stdout):
        self._stdin = stdin
        self._stdout = stdout
        self._write_lock = threading.Lock()
        self._work_q = queue.Queue()
        self._engine = None
        self._engine_kind = ""
        self._active_id = None
        self._active_lock = threading.Lock()     # захищає claim/clear active_id
        self._cancel_ids = set()
        self._cancel_lock = threading.Lock()
        self._shutdown = threading.Event()

    def _emit(self, payload: dict) -> None:
        with self._write_lock:
            self._stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._stdout.flush()

    def _is_cancelled(self, req_id) -> bool:
        with self._cancel_lock:
            return req_id in self._cancel_ids or self._shutdown.is_set()

    def _reader_loop(self) -> None:
        """control-потік: весь час читає stdin; ping/cancel обробляє одразу,
        load_voice/synthesize кладе в чергу головному потоку."""
        for line in self._stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._emit({"type": MSG_ERROR, "message": "Пошкоджений JSON у запиті"})
                continue
            kind = msg.get("type")
            if kind == MSG_PING:
                self._emit({"type": MSG_PONG})
            elif kind == MSG_CANCEL:
                with self._cancel_lock:
                    self._cancel_ids.add(msg.get("id"))
            elif kind == MSG_SHUTDOWN:
                self._shutdown.set()
                self._work_q.put(None)
                break
            elif kind == MSG_SYNTHESIZE:
                # BUSY-claim на control-потоці (§3.2 reject-busy страхувальник від
                # гонки): другий synthesize, ПОКИ активний перший, → busy одразу, у
                # чергу не ставимо. Latest-wins не ламається, бо parent шле cancel і
                # ЧЕКАЄ cancelled (main очистить active_id) ПЕРЕД новим synthesize.
                with self._active_lock:
                    if self._active_id is not None:
                        self._emit({"type": MSG_BUSY, "id": msg.get("id"),
                                    "active_id": self._active_id})
                        continue
                    self._active_id = msg.get("id")
                self._work_q.put(msg)
            elif kind == MSG_LOAD_VOICE:
                self._work_q.put(msg)
            else:
                self._emit({"type": MSG_ERROR, "id": msg.get("id"),
                            "message": f"Невідомий тип повідомлення: {kind!r}"})
        self._work_q.put(None)

    def _handle_load_voice(self, msg: dict) -> None:
        req_id = msg.get("id")
        try:
            self._engine_kind = msg.get("engine", "")
            if self._engine is not None:
                try:
                    self._engine.unload()             # повний unload попереднього (§7.7)
                except Exception:                     # noqa: BLE001
                    pass
                self._engine = None
            self._engine = make_engine(self._engine_kind, msg.get("manifest_path", ""))
            self._emit({"type": MSG_VOICE_LOADED, "id": req_id,
                        "voice_id": msg.get("voice_id"),
                        "sample_rate": self._engine.capabilities().sample_rate})
        except Exception as exc:                      # noqa: BLE001
            self._engine = None
            self._emit({"type": MSG_ERROR, "id": req_id, "message": str(exc)})

    def _handle_synthesize(self, msg: dict) -> None:
        # active_id уже claimed control-потоком (reject-busy). Тут лише синтез.
        req_id = msg.get("id")
        if self._engine is None:
            # ледачий load за manifest_path запиту (§3.2)
            self._engine = make_engine(msg.get("engine", self._engine_kind),
                                       msg.get("manifest_path", ""))
        self._emit({"type": MSG_ACCEPTED, "id": req_id})
        try:
            synthesize_stream(self._engine, msg, self._emit,
                              lambda: self._is_cancelled(req_id))
        except Exception as exc:                      # noqa: BLE001
            _cleanup_request_files(msg.get("wav_dir") or "")
            self._emit({"type": MSG_ERROR, "id": req_id, "message": str(exc)})
        finally:
            with self._active_lock:
                self._active_id = None
            with self._cancel_lock:
                self._cancel_ids.discard(req_id)

    def run(self) -> int:
        # Прогріти важкі імпорти рушіїв (torch/styletts2_inference/ipa_uk/…, стек
        # RAD-TTS) ОДНОПОТОКОВО, ДО старту reader-потоку — інакше вони виконуються на
        # головному потоці при живому reader (блокованому на stdin-пайпі) і на frozen
        # Windows детермінований import-lock/DLL-loader-lock deadlock (див.
        # _warm_engine_imports). Прогрів tolerant: відсутній torch = пропуск.
        # Друк рушіїв на імпорті глушимо в stderr (stdout — JSON-канал IPC).
        import contextlib
        with contextlib.redirect_stdout(sys.stderr):
            _warm_engine_imports(
                log=lambda m: print(m, file=sys.stderr, flush=True))
        reader = threading.Thread(target=self._reader_loop, daemon=True)
        reader.start()
        # Дренуємо чергу до None-sentinel (його control-потік кладе ПІСЛЯ всієї
        # черги). НЕ виходимо по _shutdown як guard — інакше queued synthesize між
        # load_voice і shutdown загубився б (race). Довгий synth усе одно уриває
        # _is_cancelled, що містить _shutdown.
        while True:
            item = self._work_q.get()
            if item is None:
                break
            kind = item.get("type")
            if kind == MSG_LOAD_VOICE:
                self._handle_load_voice(item)
            elif kind == MSG_SYNTHESIZE:
                self._handle_synthesize(item)
        return 0


def main() -> int:
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    return TtsWorker(sys.stdin, sys.stdout).run()


def selftest() -> int:
    """Self-check для frozen-gate (§12.1): РЕАЛЬНИЙ synth через FakeBackend (не
    import-probe) + import-probe нативних TTS-DLL. Друкує рядок SELFTEST і повертає
    0 (ок) / 1 (збій). Умова: у повному frozen-gate додатково прогнати обома
    рушіями (torch), тут — базовий рівень, що не потребує моделі."""
    import os
    import tempfile
    os.environ.setdefault(ENV_FAKE_BACKEND, "1")
    ok = True
    detail = []
    # 1) РЕАЛЬНИЙ потоковий synth через FakeBackend у temp-теку
    try:
        d = tempfile.mkdtemp(prefix="tts-selftest-")
        eng = make_engine("styletts2", d)
        events = []
        synthesize_stream(eng, {"id": "st", "text": "Перевірка озвучення тут.",
                                "wav_dir": d, "want_timings": False},
                          events.append, lambda: False)
        made = any(e.get("type") == MSG_CHUNK_READY for e in events)
        result = any(e.get("type") == MSG_RESULT for e in events)
        ok = ok and made and result
        detail.append(f"synth={'ok' if made and result else 'FAIL'}")
    except Exception as exc:                      # noqa: BLE001
        ok = False
        detail.append(f"synth=EXC:{exc}")
    # 2) import-probe нативних TTS-DLL (torch/sherpa) — не валить selftest, лише звіт
    for mod in ("torch", "sherpa_onnx"):
        try:
            import importlib
            importlib.import_module(mod)
            detail.append(f"{mod}=present")
        except ImportError:
            detail.append(f"{mod}=absent")
    print("SELFTEST " + ("PASS" if ok else "FAIL") + " " + " ".join(detail),
          flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
