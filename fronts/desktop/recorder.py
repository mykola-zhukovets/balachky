"""Аудіо-запис. Callback у PortAudio-потоці — append у буфер + дешева метрика рівня.

Патерн snapshot-на-release збережено з оригіналу: на стоп повертаємо копію
буфера, щоб наступний запис не гонявся з поточною транскрипцією.

Рівень для смужки: у callback рахуємо ЛИШЕ RMS/peak (лінійна амплітуда) —
важке (dBFS, балістика) робить GUI. Передача в GUI — через атомарні float-поля
(GIL робить одиночний STORE/LOAD attr неподільним), peak накопичується між
знімками GUI (peak-hold), скидається у take_meter. Черга не потрібна: GUI-таймер
30 fps сам задає темп, зайвий Qt-сигнал на кожен блок не спамимо.
"""
import logging
import math
import threading
from time import monotonic, sleep

import numpy as np
import sounddevice as sd

# --- тест мікрофона (feature/audio-qol; патерн Discord/Teams/Zoom) ---
MIC_TEST_SECONDS = 3.0        # скільки записуємо перед відтворенням користувачу

# Пороги вердикту за ПІКОВИМ рівнем 3-с запису (dBFS повної шкали):
#   < -50 dBFS  → «тиша»  (мікрофон заглушено/від'єднано — цифрова тиша)
#   -50..-24    → «тихо»  (сигнал є, але слабкий — підняти гучність мікрофона)
#   >= -24 dBFS → «добре» (нормальний рівень мовлення; піки мови зазвичай -18..-6)
_MIC_SILENCE_DBFS = -50.0
_MIC_QUIET_DBFS = -24.0

# Відновлення не має чекати на OS-подію (Bluetooth/WASAPI дають її не всюди):
# перевіряємо активність PortAudio-потоку чотири рази на секунду, а відкривати
# його пробуємо з обмеженим exponential backoff.
_RECOVERY_WATCH_INTERVAL = 0.25
_RECOVERY_INITIAL_DELAY = 0.5
_RECOVERY_MAX_DELAY = 5.0
_RECOVERY_TIMEOUT = 30.0
# Цей дедлайн не може перервати завислий нативний query/open виклик. Не
# починаємо нову спробу менш ніж за секунду до нього, але timeout-обгортка
# окремим потоком тут дала б більше складності, ніж користі.
_RECOVERY_OPEN_MIN_REMAINING = 1.0


def peak_dbfs(audio) -> float:
    """Піковий рівень аудіо (float32 -1..1) у dBFS. Порожнє/тиша → -inf."""
    if audio is None or len(audio) == 0:
        return float("-inf")
    peak = float(np.max(np.abs(audio)))
    if peak <= 1e-10:
        return float("-inf")
    return 20.0 * math.log10(peak)


def classify_mic_level(audio) -> str:
    """Записане аудіо → людський вердикт тесту мікрофона: 'silence' | 'quiet' |
    'good'. Чиста функція (пороги вище); UI мапить код у текст через i18n."""
    db = peak_dbfs(audio)
    if db < _MIC_SILENCE_DBFS:
        return "silence"
    if db < _MIC_QUIET_DBFS:
        return "quiet"
    return "good"


def _resolve_output_device(name: "str | None"):
    """ІМʼЯ пристрою виводу → індекс. Не знайдено/None → None (системний дефолт)."""
    if not name:
        return None
    try:
        for idx, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0 and d["name"] == name:
                return idx
    except Exception:
        logging.exception("Не вдалося знайти пристрій виводу «%s»", name)
        return None
    logging.warning("Пристрій виводу «%s» не знайдено — системний за замовчуванням", name)
    return None


def play_audio(audio, sample_rate: int, device: "str | None" = None) -> None:
    """Відтворити запис на обраний пристрій ВИВОДУ (ІМʼЯ; None → системний).
    Блокує до кінця відтворення. Якщо обраний пристрій недоступний (від'єднано) —
    нефатальний відкат на системний за замовчуванням, а не виняток."""
    dev = _resolve_output_device(device)
    try:
        sd.play(audio, sample_rate, device=dev)
        sd.wait()
    except Exception:
        if dev is None:
            raise                          # системний і так не працює — хай ловить викликач
        logging.exception("Вивід «%s» недоступний — відтворюю на системному", device)
        sd.play(audio, sample_rate)        # відкат на пристрій за замовчуванням
        sd.wait()


def list_output_devices() -> list[str]:
    """Читабельні імена пристроїв ВИВОДУ (max_output_channels>0). Та сама
    WASAPI-дедуплікація, що й list_input_devices — для відтворення тесту мікрофона."""
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        logging.exception("Не вдалося перелічити пристрої виводу")
        return []
    preferred = None
    for i, h in enumerate(hostapis):
        if h["name"] == "Windows WASAPI":
            preferred = i
            break
    names: list[str] = []
    seen: set[str] = set()
    for d in devices:
        if d["max_output_channels"] <= 0:
            continue
        if preferred is not None and d["hostapi"] != preferred:
            continue
        name = d["name"]
        if name not in seen:
            seen.add(name)
            names.append(name)
    if not names:                # WASAPI недоступний — усі виходи, дедуп за іменем
        for d in devices:
            if d["max_output_channels"] > 0 and d["name"] not in seen:
                seen.add(d["name"])
                names.append(d["name"])
    return names


def list_input_devices() -> list[str]:
    """Читабельні імена мікрофонів (max_input_channels>0), дедуплікація хостапі.

    На Windows беремо лише WASAPI-входи — інакше кожен фізичний мікрофон
    з'явиться 3-4 рази (MME+DirectSound+WASAPI+WDM-KS). Поза Windows (або якщо
    WASAPI відсутній) — усі входи, дедуп за іменем. Повертаємо ІМЕНА (їх і
    зберігаємо в config — індекс плаває між перезапусками)."""
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        logging.exception("Не вдалося перелічити аудіопристрої")
        return []
    preferred = None
    for i, h in enumerate(hostapis):
        if h["name"] == "Windows WASAPI":
            preferred = i
            break
    names: list[str] = []
    seen: set[str] = set()
    for d in devices:
        if d["max_input_channels"] <= 0:
            continue
        if preferred is not None and d["hostapi"] != preferred:
            continue
        name = d["name"]
        if name not in seen:
            seen.add(name)
            names.append(name)
    if not names:                # WASAPI недоступний — усі входи, дедуп за іменем
        for d in devices:
            if d["max_input_channels"] > 0 and d["name"] not in seen:
                seen.add(d["name"])
                names.append(d["name"])
    return names


class Recorder:
    def __init__(self, sample_rate: int, input_device: "str | None" = None,
                 on_audio_state=None):
        self.sr = sample_rate
        self._recording = False
        self._paused = False
        self._frames: list = []
        self._live_sink = None    # callback читає це поле навіть коли live вимкнено
        # метрика рівня (лінійна амплітуда 0..1); пише callback, читає GUI-таймер
        self._meter_rms = 0.0
        self._meter_peak = 0.0
        self._device_name = input_device   # реально відкритий пристрій
        self._preferred_device_name = input_device  # вибір користувача для retry
        self._sink = None            # feature/player-recordings: стрім-сток диктофона
        self._sink_failed = False    # лог помилки sink — раз на епізод, не щоблок
        self._stream = None
        self._on_audio_state = on_audio_state or (lambda _state: None)
        self._recovery_requested = threading.Event()
        self._recovering = False
        self._recovery_lock = threading.Lock()
        # Серіалізує generation + Event без блокування callback на close().
        self._recovery_state_lock = threading.Lock()
        # Усі зміни власника stream проходять через цей lock. Номер покоління
        # скасовує retry/callback, що належить уже витісненому потоку.
        self._stream_lock = threading.RLock()
        self._stream_generation = 0
        self._recovery_generation = None
        self._closed = False
        self._recording_started = 0.0
        self._gap_started = None
        self._gap_markers: list[dict] = []
        self._watch_thread = threading.Thread(
            target=self._watch_stream, name="dictation-audio-watch", daemon=True)
        self._watch_thread.start()
        with self._stream_lock:
            self._open_stream(input_device)

    # --- керування потоком: резолв імені → індекс, м'який відкат на дефолт ---
    def _resolve_device(self, name: "str | None"):
        """ІМʼЯ мікрофона → індекс вводу. Не знайдено/None → None (системний дефолт)."""
        if not name:
            return None
        try:
            for idx, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0 and d["name"] == name:
                    return idx
        except Exception:
            logging.exception("Не вдалося знайти мікрофон «%s»", name)
            return None
        logging.warning("Мікрофон «%s» не знайдено — системний за замовчуванням", name)
        return None

    def _callback_for(self, generation: int):
        return lambda indata, frames, time_info, status: self._cb(
            indata, frames, time_info, status, generation)

    @staticmethod
    def _close_stream_object(stream) -> None:
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            logging.debug("Мікрофон уже зупинений", exc_info=True)
        try:
            stream.close()
        except Exception:
            logging.debug("Мікрофон уже закритий", exc_info=True)

    def _open_stream(self, name: "str | None", *, start: bool = True,
                     deadline: "float | None" = None) -> bool:
        """Відкрити потік під stream lock; за потреби стартувати його окремо.

        Recovery передає start=False: спершу дописуємо тишу, і тільки тоді
        PortAudio дістає право викликати callback нового покоління.
        """
        device = self._resolve_device(name)
        attempts = [(device, name)]
        if device is not None:
            attempts.append((None, None))
        for target_device, actual_name in attempts:
            stream = None
            try:
                if (deadline is not None
                        and deadline - monotonic() < _RECOVERY_OPEN_MIN_REMAINING):
                    return False
                generation = self._stream_generation + 1
                stream = sd.InputStream(
                    samplerate=self.sr, channels=1, dtype="float32",
                    device=target_device, callback=self._callback_for(generation))
                self._stream = stream
                with self._recovery_state_lock:
                    self._stream_generation = generation
                self._device_name = actual_name if target_device is not None else None
                if start:
                    stream.start()
                return True
            except Exception:
                logging.exception("Мікрофон недоступний (%s)", actual_name)
                if self._stream is stream:
                    self._stream = None
                self._close_stream_object(stream)
        self._device_name = None
        return False

    def set_input_device(self, name: "str | None"):
        """Атомарно витіснити старий потік потоком вибраного мікрофона."""
        with self._stream_lock:
            self._preferred_device_name = name
            # Навіть невдалий вибір скасовує recovery старого пристрою.
            with self._recovery_state_lock:
                self._stream_generation += 1
                self._recovery_generation = None
                self._recovery_requested.clear()
            self._close_current_stream()
            self._open_stream(name)

    def _request_recovery(self, generation: int, reason: str) -> None:
        """Позначити померлий callback/потік; retry виконує watcher, не callback."""
        # Callback старого stream може дійти сюди вже після set_input_device().
        # Цей lock не бере власність на stream: stop()/close() не чекають на
        # callback, який чекає lock, а перевірка й Event лишаються атомарними.
        with self._recovery_state_lock:
            if (self._recording and not self._closed
                    and generation == self._stream_generation):
                logging.warning("Мікрофонний потік потребує відновлення: %s", reason)
                self._recovery_generation = generation
                self._recovery_requested.set()

    def _watch_stream(self) -> None:
        """Стежить за inactive stream і запускає рівно один retry-цикл.

        Звернення до ``stream.active`` мусить лишатися ПІД ``_stream_lock``, під
        яким потік і закривають. Інакше між виходом із замка й перевіркою потік
        встигає закритись, і ми питаємо властивість у вже знищеного нативного
        обʼєкта PortAudio. Це не Python-виняток, який ловить except: це порушення
        доступу до памʼяті в нативному коді, від якого процес падає цілком.
        Знайдено 25.07 повним прогоном: аварія у цьому потоці на половині набору.
        """
        while not self._closed:
            sleep(_RECOVERY_WATCH_INTERVAL)
            with self._stream_lock:
                if not self._recording or self._paused:
                    continue
                stream = self._stream
                generation = self._stream_generation
                # перевірка стану — тут же, поки закриття не може вклинитись
                try:
                    inactive = stream is None or not stream.active
                except Exception:
                    inactive = True
            if self._closed:            # закрились, поки ми виходили із замка
                return
            if inactive:
                self._request_recovery(generation, "inactive stream")
            if self._recovery_requested.is_set() and not self._recovering:
                self._recover(generation)

    def _close_current_stream(self) -> None:
        """Витіснити поточний stream; викликати тільки під _stream_lock."""
        old = self._stream
        self._stream = None
        self._close_stream_object(old)

    def _append_recovery_silence(self, ended: float) -> float:
        """Зберегти gap у доріжці та окремо позначити його для діагностики."""
        started = self._gap_started
        self._gap_started = None
        if started is None or not self._recording:
            return 0.0
        seconds = max(0.0, ended - started)
        frames = int(round(seconds * self.sr))
        if frames:
            self._frames.append(np.zeros((frames, 1), dtype=np.float32))
        self._gap_markers.append({
            "start": max(0.0, started - self._recording_started),
            "duration": seconds,
        })
        return seconds

    def _recover(self, generation=None) -> None:
        if not self._recovery_lock.acquire(blocking=False):
            return
        try:
            with self._stream_lock:
                with self._recovery_state_lock:
                    expected = (self._recovery_generation if generation is None
                                else generation)
                    self._recovery_requested.clear()
                    self._recovery_generation = None
                if (expected != self._stream_generation or self._closed
                        or not self._recording):
                    return
                self._recovering = True
                self._gap_started = monotonic()
                self._close_current_stream()
            self._on_audio_state("reconnecting")
            deadline = monotonic() + _RECOVERY_TIMEOUT
            delay = _RECOVERY_INITIAL_DELAY
            while monotonic() < deadline:
                with self._stream_lock:
                    if (expected != self._stream_generation or self._closed
                            or not self._recording):
                        return
                    # _resolve_device()/InputStream() не мають cancel API: не
                    # починаємо спробу, коли вона вже не має часу завершитися.
                    if deadline - monotonic() < _RECOVERY_OPEN_MIN_REMAINING:
                        break
                    opened = self._open_stream(
                        self._preferred_device_name, start=False, deadline=deadline)
                    expected = self._stream_generation
                    if opened:
                        now = monotonic()
                        if now >= deadline:
                            self._close_current_stream()
                            break
                        # Callback ще не запущений: silence завжди перед живим звуком.
                        duration = self._append_recovery_silence(now)
                        stream = self._stream
                        try:
                            stream.start()
                        except Exception:
                            logging.exception("Не вдалося стартувати відновлений мікрофон")
                            self._close_current_stream()
                        else:
                            logging.info("Мікрофон відновлено після %.2f с", duration)
                            with self._recovery_state_lock:
                                self._recovery_requested.clear()
                                self._recovery_generation = None
                            self._on_audio_state("reconnected")
                            return
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                sleep(min(delay, remaining))
                delay = min(delay * 2, _RECOVERY_MAX_DELAY)
            with self._stream_lock:
                if expected == self._stream_generation:
                    self._append_recovery_silence(monotonic())
                    if self._recording and not self._closed:
                        self._on_audio_state("failed")
        finally:
            self._recovering = False
            self._recovery_lock.release()
    def _cb(self, indata, frames, time_info, status, generation=None):
        try:
            if generation is not None and generation != self._stream_generation:
                return
            if self._recording and not self._paused:
                # feature/player-recordings: заданий sink (диктофон) отримує блок
                # ЗАМІСТЬ накопичення у RAM — запис стрімиться на диск. Помилка sink
                # не сміє вбити audio-callback: блок відкидається з одним логом
                # (і НЕ провокує recovery мікрофона — це збій диска, не пристрою).
                if self._sink is not None:
                    try:
                        self._sink(indata.copy())
                    except Exception:
                        if not self._sink_failed:
                            self._sink_failed = True
                            logging.exception("Recorder sink впав — блок відкинуто")
                else:
                    self._frames.append(indata.copy())
                # лише дешеві RMS/peak; dBFS і балістику рахує GUI (реальний час!)
                self._meter_rms = float(np.sqrt(np.mean(indata ** 2)))
                self._meter_peak = max(self._meter_peak, float(np.max(np.abs(indata))))
                # feature/live-transcription: живе прев'ю отримує СИРИЙ блок — той
                # самий сигнал, що йде у _frames/_sink, тобто у фінальну розшифровку.
                # DSP (gate/AGC) для диктування накладається суцільним буфером в
                # app._work ПЕРЕД Whisper як джерело істини (AGC нормалізує цілий
                # запис — поблочно у callback він дав би непослідовне підсилення),
                # тож прев'ю навмисно лишається сирим. Live НІКОЛИ не блокує та не
                # валить audio-callback: власна помилка лише відключає consumer.
                sink = self._live_sink
                if sink is not None:
                    try:
                        sink(indata)
                    except Exception:
                        # Live preview ніколи не зупиняє диктофон.
                        logging.exception("Live sink диктування впав; відключаю")
                        self._live_sink = None
        except Exception as exc:
            # Виняток callback не можна пускати в PortAudio: він часто завершує
            # callback без можливості безпечно відкрити новий поток тут же.
            self._request_recovery(generation, str(exc))

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def has_stream(self) -> bool:
        """Чи є відкритий аудіо-потік. False = мікрофон недоступний → запис
        не дасть жодного кадру (сенсу входити в стан «запис» нема)."""
        return self._stream is not None

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, on: bool):
        """Чесна пауза: callback просто не пише кадри (потік не чіпаємо)."""
        self._paused = bool(on)

    def set_live_sink(self, sink):
        """Живий стік розшифровки: callback читає _live_sink щоблоку. Присвоєння
        посилання атомарне (GIL), тож окремий лок не потрібен."""
        self._live_sink = sink

    def current_rms(self) -> float:
        """feature/qol-pack: поточний RMS-рівень (лінійна амплітуда 0..1) БЕЗ
        скидання піку. Окремо від take_meter (той обнуляє peak для LevelMeter) —
        щоб автостоп по тиші не гонявся з живою смужкою рівня за один і той самий
        peak-hold."""
        return self._meter_rms

    def take_meter(self) -> tuple:
        """Знімок рівня для GUI-таймера: (rms, peak) лінійної амплітуди 0..1.
        peak — накопичений максимум від попереднього виклику (peak-hold), скидається."""
        rms = self._meter_rms
        peak = self._meter_peak
        self._meter_peak = 0.0
        return rms, peak

    def start(self, sink=None):
        """sink (feature/player-recordings): callback віддає блоки у sink
        ЗАМІСТЬ накопичення в RAM — для стрімінгу диктофона на диск.
        None (диктування) — стара поведінка без змін."""
        self._frames = []        # спершу очистити, потім прапорець — інакше
        self._paused = False     # callback встигне дописати у список, який відкинемо
        self._meter_rms = 0.0    # не тягнути «хвіст» рівня з попереднього запису
        self._meter_peak = 0.0
        self._gap_markers = []
        self._recording_started = monotonic()
        self._sink = sink
        self._sink_failed = False
        self._recording = True

    def stop(self) -> list:
        self._recording = False
        self._paused = False
        self._sink = None
        return list(self._frames)  # snapshot

    @property
    def gap_markers(self) -> list[dict]:
        """Копія позначок штучної тиші, доданої під час reconnect."""
        return list(self._gap_markers)

    def to_audio(self, chunks: list):
        """chunks → float32 ndarray (mono, 16 kHz) або None, якщо порожньо/надто коротко."""
        if not chunks:
            return None
        audio = np.concatenate(chunks, axis=0).flatten()
        if len(audio) < self.sr * 0.3:
            return None
        return audio

    def close(self):
        with self._stream_lock:
            self._closed = True
            self._recording = False
            with self._recovery_state_lock:
                self._stream_generation += 1
                self._recovery_generation = None
                self._recovery_requested.clear()
            self._close_current_stream()
