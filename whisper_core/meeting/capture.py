"""Захоплення звуку наради: незалежні блокуючі WASAPI-потоки через
PyAudioWPatch — довільні мікрофони та системний loopback, кожен окремо.

Чому окремо, а не мікс на льоту: два аудіо-годинники дрейфують; синхронізації не
робимо взагалі — сирі доріжки пишемо роздільно, зшиваємо за таймкодами
транскрипції постфактум (див. postprocess). Тут лише: читати сирі байти блокуючим
read(), віддавати їх у sink (segment-writer із session.py), рахувати дешевий рівень
для смужки, стерегти зависання (watchdog) і зміну пристрою.

ЖИВІ ФАКТИ (проби 15.07, важливо):
- WASAPI-loopback без активного системного звуку БЛОКУЄ read() — він НЕ віддає
  silence-кадри на тиші (silence-кадри йдуть лише коли активний рендер-сеанс
  грає тишу); на тиші get_read_available() чесно тримає 0.
- Закривати потік з іншого треда, поки читач стоїть у блокуючому read(), НЕ МОЖНА:
  use-after-free у PortAudio → segfault усього застосунку (перевірено живо).

Наслідки закладені в конструкцію:
- читач НІКОЛИ не блокується: читаємо лише коли get_read_available() накопичив
  повний блок, інакше коротка дрімота з перевіркою прапорця — тож stop() це
  прапорець → миттєвий join → закриття вже БЕЗ конкуренції з read (безпечно і
  швидко, < 0.5 с навіть на тиші);
- watchdog — окремий потік-монітор: стежить за міткою останнього кадру незалежно
  від стану читача;
- пауза системного звуку ЗНИКАЛА б із доріжки і шкали mic/sys роз'їхались би —
  тому коли кадри повертаються, донабиваємо в sink тишу на пропущений час
  (_fill_gap): обидві доріжки тримають спільну wall-clock шкалу від старту,
  і зшивка за таймкодами (Б3) лишається чесною на довгих нарадах.

Рівень рахуємо ТУТ (RMS/peak, як recorder._cb) — важке (dBFS, балістику) робить
GUI. Передача в GUI — через атомарні float-поля (GIL робить одиночний STORE/LOAD
неподільним), peak накопичується між знімками (peak-hold), скидається в take_level.
"""
from collections import deque
import errno
import logging
import threading
from time import monotonic, sleep

import numpy as np

try:                                 # C-розширення форку; мокається в тестах
    import pyaudiowpatch as pyaudio
except Exception:                    # pragma: no cover - лише коли пакет не встановлено
    pyaudio = None

# NATIVE_RATE / NATIVE_CHANNELS — з пакета (спільний формат), доступні як
# capture.NATIVE_RATE згідно з контрактом спеки, без дублювання значень.
# Ре-експорт через присвоєння (а не голий import) — інакше pyflakes вважає
# імпорт зайвим, а app.py звертається саме до capture.NATIVE_CHANNELS.
from . import NATIVE_CHANNELS as _NATIVE_CHANNELS, NATIVE_RATE as _NATIVE_RATE

NATIVE_CHANNELS = _NATIVE_CHANNELS
NATIVE_RATE = _NATIVE_RATE

STALL_SECONDS = 2.0                  # watchdog: без кадрів довше → закрити сегмент [A]
READ_BLOCK = 4096                    # кадрів на один read()
_WATCH_INTERVAL = 0.25               # як часто монітор перевіряє мітку останнього кадру
_POLL_INTERVAL = 0.05                # дрімота читача, коли повний блок ще не накопичився
RECOVERY_INITIAL_DELAY = 0.5         # 0.5, 1, 2, 4, 5 … с
RECOVERY_MAX_DELAY = 5.0
RECOVERY_TIMEOUT = 30.0
# Нативні resolver/PyAudio.open не мають cancel API, тому дедлайн не може
# перервати виклик, що вже завис. Не починаємо нову спробу менш ніж за секунду
# до дедлайну; окремий timeout-потік тут невиправдано ускладнить cleanup.
RECOVERY_OPEN_MIN_REMAINING = 1.0
GAP_FILL_MIN_SECONDS = 0.25          # менший дефіцит не донабиваємо (джиттер буферизації)
SINK_ERROR_WINDOW_BLOCKS = 12        # приблизно секунда нативного аудіо
SINK_ERROR_MIN_FAILURES = 3          # один-два випадкові збої не тривожать
SINK_ERROR_NOTIFY_RATIO = 0.25       # втрачено щонайменше чверть останніх блоків
# Вартовий тиші (аудит 30.07, ризик 5; піднято після зауваження рецензента
# 30.07): 90 с — компроміс, не точна межа. На живих нарадах 60-90 с тиші на
# старті (учасники збираються, вітаються без звуку) — не рідкість, тож 60 с
# передчасно лякали людей помилковим попередженням; 90 с досить довго, щоб
# пережити звичайний організаційний початок, і досить рано, щоб попередження
# ще було корисним, поки нараду можна врятувати перемиканням пристрою в
# Windows. МЕЖА: чисто програмно «ще ніхто не говорив» і «слухаємо не той
# пристрій» невідрізнимі — обидва дають суцільні нулі на loopback.
SILENCE_WARN_SECONDS = 90.0

_BYTES_PER_SAMPLE = 4               # float32


def missing_frames(elapsed: float, frames_delivered: int, rate: int) -> int:
    """Скільки кадрів тиші бракує доріжці до спільної wall-clock шкали.

    elapsed — секунд від старту потоку; frames_delivered — скільки кадрів уже
    лягло в sink. Гістерезис на GAP_FILL_MIN_SECONDS: дефіцит у межах порога → 0
    (нормальний джиттер буферизації, не смикаємось); більше → донабиваємо НЕ до
    нуля, а до залишкового відставання в один поріг — бо після паузи прилітають
    ще кадри, буферизовані «в дорозі», і заповнення до нуля виганяло б доріжку
    ПОПЕРЕДУ wall-clock (живий замір: +0.28 с на 3-секундній паузі)."""
    gap = elapsed - frames_delivered / rate
    if gap <= GAP_FILL_MIN_SECONDS:
        return 0
    return int((gap - GAP_FILL_MIN_SECONDS) * rate)


class LoopbackUnavailable(Exception):
    """WASAPI loopback не резолвиться (нема пристрою або PyAudioWPatch не завантажився)."""


class DeviceLost(Exception):
    """Дефолтний пристрій змінився/зник посеред запису [A]."""


def _pa():
    """Свіжий PyAudio-хендл або None, якщо форк не завантажився."""
    if pyaudio is None:
        return None
    try:
        return pyaudio.PyAudio()
    except Exception:
        logging.exception("Не вдалося ініціалізувати PyAudioWPatch")
        return None


def _wasapi_host_index(pa) -> "int | None":
    """Індекс host-api WASAPI (мік наради має бути тим самим фізичним пристроєм,
    що й мік диктування — а імена збігаються лише в межах WASAPI)."""
    try:
        info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        return info["index"]
    except Exception:
        return None


def list_loopback_devices() -> list:
    """Усі WASAPI-loopback пристрої (для діагностики/майбутнього вибору)."""
    pa = _pa()
    if pa is None:
        return []
    try:
        return list(pa.get_loopback_device_info_generator())
    except Exception:
        logging.exception("Не вдалося перелічити loopback-пристрої")
        return []
    finally:
        pa.terminate()


def list_input_devices() -> list:
    """Унікальні WASAPI-мікрофони, придатні для незалежних доріжок наради."""
    pa = _pa()
    if pa is None:
        return []
    try:
        host = _wasapi_host_index(pa)
        result = []
        seen = set()
        for index in range(pa.get_device_count()):
            try:
                device = pa.get_device_info_by_index(index)
            except Exception:
                continue
            name = str(device.get("name") or "").strip()
            if (not name or device.get("maxInputChannels", 0) <= 0
                    or device.get("isLoopbackDevice")
                    or (host is not None and device.get("hostApi") != host)
                    or name in seen):
                continue
            result.append(device)
            seen.add(name)
        return result
    except Exception:
        logging.exception("Не вдалося перелічити WASAPI-мікрофони")
        return []
    finally:
        pa.terminate()


def default_loopback() -> "dict | None":
    """Дефолтний loopback через get_default_wasapi_loopback(). НЕ хардкодимо
    індекс — буває кілька двійників від Steam/віртуальних пристроїв [A].
    None → системного loopback нема (або форк не завантажився).

    ЧЕСНО ПРО МЕЖУ: PyAudioWPatch (і сам WASAPI host у PortAudio) не розрізняє
    ролі Windows-пристроїв — це завжди мультимедійний дефолт (eConsole/
    eMultimedia). Роль «для зв'язку» (eCommunications), куди Teams/Meet
    найчастіше кладуть дзвінок, ця бібліотека взагалі не віддає — див.
    default_communications_device_name() нижче, окремий шлях через ctypes/COM."""
    pa = _pa()
    if pa is None:
        return None
    try:
        return pa.get_default_wasapi_loopback()
    except Exception:
        logging.exception("Дефолтний loopback недоступний")
        return None
    finally:
        pa.terminate()


def default_communications_device_name() -> "str | None":
    """Ім'я пристрою виводу з роллю WASAPI «для зв'язку» (eCommunications).

    Навіщо окремо від default_loopback(): те, що реально піде у запис —
    мультимедійний дефолт (eConsole). Але Teams/Meet у Windows часто грають
    саме в пристрій «для зв'язку», якщо він відрізняється (наприклад, USB-
    гарнітура призначена лише роллю зв'язку) — тоді запис буде порожнім, хоча
    виглядатиме успішним. PyAudioWPatch ролей не знає (див. default_loopback),
    тож дістаємо ім'я напряму через ctypes/COM (IMMDeviceEnumerator) — без
    нової залежності пакета.

    Лише Windows. Будь-яка помилка (COM недоступний, пристрою немає, роль не
    налаштована) → None: чесне «не вдалося дізнатись», а не вигаданий здогад."""
    import sys
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

        ole32 = ctypes.windll.ole32

        def _guid(s):
            g = _GUID()
            if ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g)) != 0:
                raise OSError("CLSIDFromString")
            return g

        class _PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]

        class _PROPVARIANT(ctypes.Structure):
            # VT_LPWSTR (=31): рядковий покажчик у першому 8-байтному слові
            # union'у PROPVARIANT — цього досить, повний union не потрібен.
            _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                        ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                        ("data", ctypes.c_uint64), ("data2", ctypes.c_uint64)]

        # Явний ABI-контракт для прямих викликів ole32 (без нього
        # ctypes мовчки припускає c_int-аргументи й result — на 64-бітних
        # покажчиках/HRESULT це або тихо ріже дані, або просто випадково
        # працює; canonical-перевірка — tests/test_ctypes_audit.py).
        ole32.CLSIDFromString.argtypes = [
            ctypes.c_wchar_p, ctypes.POINTER(_GUID)]
        ole32.CLSIDFromString.restype = ctypes.HRESULT
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        ole32.CoInitializeEx.restype = ctypes.HRESULT
        ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
        ole32.CoCreateInstance.restype = ctypes.HRESULT
        ole32.PropVariantClear.argtypes = [ctypes.POINTER(_PROPVARIANT)]
        ole32.PropVariantClear.restype = ctypes.HRESULT
        ole32.CoUninitialize.argtypes = []
        ole32.CoUninitialize.restype = None

        def _call(obj, index, restype, argtypes, *args):
            """Виклик методу COM-інтерфейсу за індексом у vtable (без comtypes)."""
            vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))
            table = ctypes.cast(vtbl.contents, ctypes.POINTER(ctypes.c_void_p * 30))
            func = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(table.contents[index])
            return func(obj, *args)

        COINIT_APARTMENTTHREADED = 0x2
        init_hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        should_uninit = init_hr in (0, 1)   # S_OK / S_FALSE (вже ініціалізовано тут же)
        try:
            enumerator = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(
                ctypes.byref(_guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")), None, 23,
                ctypes.byref(_guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")),
                ctypes.byref(enumerator))
            if hr != 0 or not enumerator:
                return None
            try:
                device = ctypes.c_void_p()
                # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender=0, eCommunications=2)
                hr = _call(enumerator, 4, ctypes.HRESULT,
                           [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
                           0, 2, ctypes.byref(device))
                if hr != 0 or not device:
                    return None
                try:
                    store = ctypes.c_void_p()
                    hr = _call(device, 4, ctypes.HRESULT,
                               [ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)],
                               0, ctypes.byref(store))
                    if hr != 0 or not store:
                        return None
                    try:
                        key = _PROPERTYKEY()
                        key.fmtid = _guid("{a45c254e-df1c-4efd-8020-67d146a850e0}")
                        key.pid = 14   # PKEY_Device_FriendlyName
                        pv = _PROPVARIANT()
                        hr = _call(store, 5, ctypes.HRESULT,
                                   [ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT)],
                                   ctypes.byref(key), ctypes.byref(pv))
                        if hr != 0 or pv.vt != 31:
                            return None
                        name = ctypes.cast(pv.data, ctypes.c_wchar_p).value
                        ole32.PropVariantClear(ctypes.byref(pv))
                        return name
                    finally:
                        _call(store, 2, ctypes.c_ulong, [])
                finally:
                    _call(device, 2, ctypes.c_ulong, [])
            finally:
                _call(enumerator, 2, ctypes.c_ulong, [])
        finally:
            if should_uninit:
                ole32.CoUninitialize()
    except Exception:
        logging.exception("Не вдалося дізнатися пристрій «для зв'язку» (WASAPI eCommunications)")
        return None


_DEVICE_BUSY_MARKERS = ("device_in_use", "0x8889000a", "in use", "exclusive")


def is_device_busy_error(exc: Exception) -> bool:
    """Пристрій зайнятий іншим застосунком у виключному режимі WASAPI
    (AUDCLNT_E_DEVICE_IN_USE). PyAudioWPatch не дає окремого коду помилки —
    лише текст винятку від PortAudio/WASAPI, тож розпізнаємо за відомими
    маркерами HRESULT/тексту (аудит 30.07, ризик 6)."""
    text = str(exc).lower()
    return any(marker in text for marker in _DEVICE_BUSY_MARKERS)


def default_input(name: "str | None" = None) -> "dict | None":
    """Дефолтний мікрофон (WASAPI). Якщо задано `name` (з cfg.input_device) —
    резолвимо його серед WASAPI-входів за ІМЕНЕМ, щоб мік наради й мік диктування
    були одним пристроєм (індекси PyAudioWPatch ≠ індекси sounddevice, тож
    зіставляємо саме за іменем). Не знайдено/None → системний дефолтний вхід."""
    pa = _pa()
    if pa is None:
        return None
    try:
        if name:
            host = _wasapi_host_index(pa)
            for i in range(pa.get_device_count()):
                try:
                    d = pa.get_device_info_by_index(i)
                except Exception:
                    continue
                if d.get("maxInputChannels", 0) <= 0 or d.get("isLoopbackDevice"):
                    continue
                if host is not None and d.get("hostApi") != host:
                    continue
                if d.get("name") == name:
                    return d
            logging.warning("Мікрофон «%s» не знайдено серед WASAPI-входів — системний", name)
        return pa.get_default_input_device_info()
    except Exception:
        logging.exception("Дефолтний мікрофон недоступний")
        return None
    finally:
        pa.terminate()


def selected_input(name: "str | None" = None) -> "dict | None":
    """Резолв вибраного мікрофона без тихої підміни іншим default-пристроєм."""
    if not name:
        return default_input()
    for device in list_input_devices():
        if device.get("name") == name:
            return device
    logging.warning("Вибраний мікрофон «%s» недоступний", name)
    return None


class CaptureStream:
    """Один блокуючий WASAPI-потік (мік АБО loopback) у власному threading.Thread.

    Кожен прочитаний блок сирих байтів → sink(pcm: bytes) (segment-writer із
    session.py). Рівень (rms, peak) рахуємо тут — GUI читає take_level(). Watchdog:
    якщо read() не віддає кадрів понад STALL_SECONDS — викликаємо on_stall() (session
    закриє сегмент) і НЕ вішаємо UI. Помилка read/зміна дефолтного пристрою →
    on_device_lost(exc) і чистий стоп.
    """

    def __init__(self, *, kind: str, device_index: int, channels: int,
                 rate: int, sink, on_stall, on_device_lost, on_sink_error=None,
                 device_resolver=None, on_audio_state=None, on_gap=None,
                 on_recovery_failed=None, on_audio=None, on_silence=None,
                 on_silence_resolved=None):
        self.kind = kind
        self._device_index = device_index
        self._channels = channels
        self._rate = rate
        self._sink = sink
        self._on_stall = on_stall
        self._on_device_lost = on_device_lost
        self._on_sink_error = on_sink_error or (lambda _exc, _elapsed: None)
        # resolver викликається лише після помилки. Для mic він повертає обраний
        # пристрій, а коли його вже нема — системний default; для sys щоразу бере
        # get_default_wasapi_loopback(), отже підхоплює новий output default.
        self._device_resolver = device_resolver
        self._on_audio_state = on_audio_state or (lambda _state: None)
        self._on_gap = on_gap or (lambda _seconds: None)
        self._on_recovery_failed = on_recovery_failed or (lambda _exc: None)
        # feature/live-transcription: необов'язковий live-consumer сирих блоків
        # (те саме джерело, що й sink доріжки).
        self._on_audio = on_audio
        # Вартовий тиші (ризик 5): _heard_sound лишається True назавжди щойно
        # прийшов перший реальний ненульовий блок — донабитий gap-fill (нулі)
        # у цей прапорець не рахується.
        self._on_silence = on_silence or (lambda: None)
        # Суддівське зауваження 30.07: попередження про тишу не мало «сліду»,
        # коли звук зрештою з'являвся, — банер лишався червоним усю нараду.
        # on_silence_resolved викликається РІВНО раз: коли перший реальний
        # звук приходить ПІСЛЯ того, як попередження вже показали. Флікеру
        # нема — _heard_sound одного разу True назавжди (див. _update_level),
        # тож коротка репліка й наступна пауза між репліками НЕ повертають
        # попередження назад: воно згасло один раз і більше не спрацює для
        # цієї доріжки.
        self._on_silence_resolved = on_silence_resolved or (lambda: None)
        self._heard_sound = False
        self._silence_notified = False
        self._bytes_per_frame = channels * _BYTES_PER_SAMPLE
        self._pa = None
        self._stream = None
        self._thread = None          # читальний потік (блокуючий read)
        self._watch_thread = None    # монітор зависання
        self._running = False
        self._frames_written = 0
        self._meter_rms = 0.0
        self._meter_peak = 0.0
        self._last_data = 0.0        # monotonic мітка останнього кадру (watchdog)
        self._stalled = False        # щоб on_stall не спамити щоцикл монітора
        self._t0 = 0.0               # monotonic старту потоку (шкала для _fill_gap)
        self._clock_origin = None      # спільна шкала для N CaptureStream
        self._sink_failed = False    # щоб лог помилки sink не спамив щоблок
        self._sink_error_window = deque(maxlen=SINK_ERROR_WINDOW_BLOCKS)
        self._sink_error_count = 0
        self._sink_error_notified = False
        self._recovering = False
        self._recovery_requested = threading.Event()
        # Watchdog працює в іншому потоці. Запит прив'язаний саме до stream,
        # який watchdog бачив: запізнілий is_active=False старого дескриптора
        # не має права перевідкривати вже новий здоровий потік.
        self._stream_generation = 0
        self._recovery_generation = None
        self._state_lock = threading.Lock()
        # Reentrant: reader-потік відкриває/закриває PortAudio у recovery, а stop()
        # закриває його з GUI-потоку. Один замок серіалізує open/close/terminate —
        # два потоки НІКОЛИ не чіпають один хендл разом (Б1: інакше use-after-free
        # → segfault, коли stop join'иться лише 3 с, а recover ще у sleep/open).
        self._pa_lock = threading.RLock()

    def set_clock_origin(self, origin: float) -> None:
        """Задати спільний monotonic-старт до відкриття N аудіопотоків."""
        if self._running:
            raise RuntimeError("clock origin можна змінити лише до start()")
        self._clock_origin = float(origin)

    def start(self) -> None:
        """Відкрити потік і запустити читальний потік + монітор. Форк не
        завантажився → LoopbackUnavailable; помилка відкриття пристрою → DeviceLost."""
        self._sink_failed = False
        self._sink_error_window.clear()
        self._sink_error_count = 0
        self._sink_error_notified = False
        self._heard_sound = False
        self._silence_notified = False
        if pyaudio is None:
            raise LoopbackUnavailable("PyAudioWPatch не завантажився")
        self._open_audio()
        self._running = True
        self._t0 = self._clock_origin if self._clock_origin is not None else monotonic()
        self._last_data = monotonic()
        self._thread = threading.Thread(
            target=self._loop, name=f"capture-{self.kind}", daemon=True)
        self._watch_thread = threading.Thread(
            target=self._watch, name=f"watch-{self.kind}", daemon=True)
        self._thread.start()
        self._watch_thread.start()

    def _open_audio(self) -> None:
        """Відкрити один PyAudio-потік. Викликається лише з його reader thread
        після старту, тому close/read ніколи не конкурують."""
        if pyaudio is None:
            raise LoopbackUnavailable("PyAudioWPatch не завантажився")
        # Під замком: паралельний stop()._close_pa() не має права terminate'нути
        # хендл, поки цей open ще в польоті (Б1).
        with self._pa_lock:
            self._pa = pyaudio.PyAudio()
            try:
                stream = self._pa.open(
                    format=pyaudio.paFloat32, channels=self._channels, rate=self._rate,
                    input=True, frames_per_buffer=READ_BLOCK,
                    input_device_index=self._device_index)
                with self._state_lock:
                    self._stream = stream
                    self._stream_generation += 1
            except Exception as exc:
                self._close_pa()
                raise DeviceLost(str(exc)) from exc

    def configure_recovery(self, *, device_resolver, on_audio_state=None,
                           on_gap=None, on_recovery_failed=None) -> None:
        """Увімкнути recovery після конструювання.

        Це зберігає початкову сигнатуру CaptureStream для старих викликів і
        фейків UI, але дає runtime-контролеру живі callbacks.
        """
        self._device_resolver = device_resolver
        if on_audio_state is not None:
            self._on_audio_state = on_audio_state
        if on_gap is not None:
            self._on_gap = on_gap
        if on_recovery_failed is not None:
            self._on_recovery_failed = on_recovery_failed

    def _loop(self) -> None:
        while self._running:
            if self._recovery_requested.is_set():
                with self._state_lock:
                    requested_generation = self._recovery_generation
                    self._recovery_requested.clear()
                    self._recovery_generation = None
                    current_generation = self._stream_generation
                # Запит від старого watchdog більше не актуальний.
                if requested_generation != current_generation:
                    continue
                lost = DeviceLost("PyAudio-потік став неактивним")
                if self._device_resolver is None:
                    self._running = False
                    self._on_device_lost(lost)
                else:
                    self._recover(lost, requested_generation)
                continue
            self._pump(monotonic())

    def _record_sink_outcome(self, failed: bool) -> bool:
        """Додати результат блока у рухоме вікно; повернути ознаку стійкої втрати."""
        if len(self._sink_error_window) == SINK_ERROR_WINDOW_BLOCKS:
            self._sink_error_count -= self._sink_error_window[0]
        self._sink_error_window.append(failed)
        self._sink_error_count += failed
        if not self._sink_error_count:
            # Повністю чисте вікно завершує епізод; наступний може попередити знову.
            self._sink_error_notified = False
        return (
            self._sink_error_count >= SINK_ERROR_MIN_FAILURES
            and self._sink_error_count / len(self._sink_error_window)
            >= SINK_ERROR_NOTIFY_RATIO
        )

    def _pump(self, now: float) -> None:
        """Одна ітерація читання БЕЗ блокування: read лише коли накопичився повний
        блок (інакше дрімота — на тиші loopback read блокував би читача, а закриття
        потоку під заблокованим read = segfault). Винесено з циклу окремим методом,
        щоб механіку тестувати детерміновано (фейковий stream). Кадри → донабити
        пропуск тиші (_fill_gap) → sink/рівень/лічильник. Помилка sink НЕ вбиває
        потік мовчки: блок відкидається з логом (подвійний захист разом із замком
        доріжки в session)."""
        with self._state_lock:
            stream = self._stream
        if stream is None:
            return
        try:
            if stream.get_read_available() < READ_BLOCK:
                sleep(_POLL_INTERVAL)      # кадрів ще нема (тиша/пауза) — не блокуємось
                return
            data = stream.read(READ_BLOCK, exception_on_overflow=False)
        except (OSError, IOError) as exc:
            # реальна втрата пристрою АБО потік уже закривається у stop().
            # Під час stop() _running уже False — тоді тихо виходимо, без on_device_lost.
            if self._running:
                lost = DeviceLost(str(exc))
                if self._device_resolver is None:  # старий контракт для зовнішніх викликів
                    self._running = False
                    self._on_device_lost(lost)
                else:
                    self._recover(lost)
            return
        nframes = len(data) // self._bytes_per_frame if self._bytes_per_frame else 0
        if not nframes:
            return
        try:
            self._fill_gap(now)
            self._sink(data)
        except Exception as exc:
            persistent = self._record_sink_outcome(failed=True)
            if not self._sink_failed:    # логнути раз на епізод, не 12 разів/с
                self._sink_failed = True
                logging.exception(
                    "sink впав (%s) — блок відкинуто, потік читає далі", self.kind)
            # ENOSPC показуємо одразу. Інші збої — коли в останніх 12 блоках
            # втрачено ≥25% і щонайменше три: це ловить і суцільні, і почергові
            # відмови, не тривожачи через один-два випадкові збої.
            disk_full = isinstance(exc, OSError) and exc.errno == errno.ENOSPC
            if (disk_full or persistent) and not self._sink_error_notified:
                self._sink_error_notified = True
                try:
                    self._on_sink_error(exc, max(0.0, now - self._t0))
                except Exception:
                    logging.exception("on_sink_error впав (%s)", self.kind)
            return
        self._record_sink_outcome(failed=False)
        self._sink_failed = False
        if self._on_audio is not None:
            try:
                # Live preview є необов'язковим consumer: його помилка не впливає
                # на запис доріжки або читальний потік.
                self._on_audio(data)
            except Exception:
                logging.exception("on_audio впав (%s); capture триває", self.kind)
        self._frames_written += nframes
        self._update_level(data)
        self._last_data = now
        self._stalled = False

    def _fill_gap(self, now: float, *, exact: bool = False) -> int:
        """Донабити тишу за пропущений час: loopback на паузі системного звуку
        БЛОКУЄ read (кадри просто не приходять), і без цього шкали mic/sys
        роз'їхались би — зшивка Б3 брехала б мітками на довгих нарадах. Пишемо
        нулі float32 порціями по READ_BLOCK (щоб ротація сегментів жила своїм
        звичайним кроком); frames_written враховує донабите — дефіцит не
        накопичується повторно."""
        elapsed = now - self._t0
        missing = (max(0, int(elapsed * self._rate) - self._frames_written)
                   if exact else missing_frames(elapsed, self._frames_written, self._rate))
        if not missing:
            return 0
        filled = missing
        logging.info("Донабито %.2f с тиші після паузи потоку (%s)",
                     missing / self._rate, self.kind)
        block = b"\x00" * (READ_BLOCK * self._bytes_per_frame)
        while missing > 0:
            n = min(missing, READ_BLOCK)
            self._sink(block[:n * self._bytes_per_frame])
            self._frames_written += n
            missing -= n
        return filled

    def _recover(self, lost: DeviceLost, generation=None) -> None:
        """Закрити померлий stream у reader-потоці й повторно відкрити його до
        30 с. Нова спроба ніколи не торкається UI; source резолвиться заново,
        тому WASAPI loopback переходить на новий default output."""
        # Event міг бути встановлений watchdog одночасно з read-error. Його
        # очищення на вході не дає успішному reconnect одразу запустити ще один.
        with self._state_lock:
            self._recovery_requested.clear()
            self._recovery_generation = None
            current_generation = self._stream_generation
        if generation is not None and generation != current_generation:
            return
        if self._recovering or not self._running:
            return
        self._recovering = True
        started = monotonic()
        self._on_device_lost(lost)     # збережений callback: повідомляє про розрив
        self._on_audio_state("reconnecting")
        self._close_stream()
        self._close_pa()
        deadline = started + RECOVERY_TIMEOUT
        delay = RECOVERY_INITIAL_DELAY
        try:
            while self._running and monotonic() < deadline:
                try:
                    # Resolver також може блокувати, тож не починаємо нову
                    # спробу, коли до дедлайну вже замало часу.
                    if deadline - monotonic() < RECOVERY_OPEN_MIN_REMAINING:
                        break
                    device = self._device_resolver()
                    if device is None:
                        raise DeviceLost("Пристрій не знайдено")
                    # Перевірка безпосередньо перед PyAudio.open: resolver міг
                    # витратити весь запас часу.
                    if deadline - monotonic() < RECOVERY_OPEN_MIN_REMAINING:
                        break
                    self._device_index = device["index"]
                    self._open_audio()
                    now = monotonic()
                    if now >= deadline:
                        self._close_stream()
                        self._close_pa()
                        break
                    # Тут без гістерезису: це саме підтверджений outage, тож усі
                    # доріжки отримують однакову wall-clock шкалу.
                    before = self._frames_written
                    self._fill_gap(now, exact=True)
                    filled = self._frames_written - before
                    if filled:
                        self._on_gap(filled / self._rate)
                    self._last_data = now
                    self._stalled = False
                    self._on_audio_state("reconnected")
                    # Запит старого watchdog міг прилетіти під час reopen.
                    # Успіх означає, що він уже не стосується нового покоління.
                    with self._state_lock:
                        self._recovery_requested.clear()
                        self._recovery_generation = None
                    return
                except Exception as exc:
                    logging.info("Не вдалося відновити %s: %s", self.kind, exc)
                    self._close_stream()
                    self._close_pa()
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        break
                    sleep(min(delay, remaining))
                    delay = min(delay * 2, RECOVERY_MAX_DELAY)
            if self._running:
                now = monotonic()
                before = self._frames_written
                self._fill_gap(now, exact=True)
                filled = self._frames_written - before
                if filled:
                    self._on_gap(filled / self._rate)
                self._running = False
                self._on_audio_state("failed")
                self._on_recovery_failed(lost)
        finally:
            self._recovering = False

    def _watch(self) -> None:
        while self._running:
            sleep(_WATCH_INTERVAL)
            now = monotonic()
            self._check_stall(now)
            self._check_silence(now)

    def _check_silence(self, now: float) -> None:
        """Один раз попередити, якщо за SILENCE_WARN_SECONDS від старту доріжка
        не принесла жодного реального ненульового блока (див. SILENCE_WARN_SECONDS
        і docstring класу щодо межі — «ще ніхто не говорив» і «не той пристрій»
        з коду невідрізнимі, тому це саме попередження, а не діагноз)."""
        if self._heard_sound or self._silence_notified:
            return
        if now - self._t0 < SILENCE_WARN_SECONDS:
            return
        self._silence_notified = True
        try:
            self._on_silence()
        except Exception:
            logging.exception("on_silence впав (%s)", self.kind)

    def _check_stall(self, now: float) -> None:
        """Монітор: якщо кадрів нема довше STALL_SECONDS (read завис чи віддає
        порожньо — напр. Bluetooth-перемикання, тиша loopback) — закрити сегмент,
        підняти прапорець, НЕ вішати UI. Одне спрацювання на епізод (скидається,
        коли кадри повертаються у _pump)."""
        # is_active=False — API-сигнал смерті, не природна тиша loopback. Не
        # закриваємо тут: watch-thread не має права торкатися stream, поки reader
        # потенційно у read(); лише просимо reader безпечно зробити retry.
        with self._state_lock:
            stream = self._stream
            generation = self._stream_generation
        try:
            inactive = stream is not None and hasattr(stream, "is_active") and not stream.is_active()
        except Exception:
            inactive = True
        if inactive:
            with self._state_lock:
                if (self._stream is stream
                        and self._stream_generation == generation
                        and self._running):
                    self._recovery_generation = generation
                    self._recovery_requested.set()
        if now - self._last_data > STALL_SECONDS and not self._stalled:
            self._stalled = True
            self._last_data = now
            try:
                self._on_stall()
            except Exception:
                logging.exception("on_stall впав (%s)", self.kind)

    def _update_level(self, data: bytes) -> None:
        arr = np.frombuffer(data, dtype=np.float32)
        if arr.size:
            self._meter_rms = float(np.sqrt(np.mean(arr ** 2)))
            self._meter_peak = max(self._meter_peak, float(np.max(np.abs(arr))))
            if not self._heard_sound and np.any(arr != 0.0):
                self._heard_sound = True
                # Попередження вже показане (_silence_notified) і лише тепер
                # прийшов перший справжній звук → воно неправдиве, ховаємо.
                # Один виклик на доріжку: _heard_sound більше ніколи не впаде
                # назад у False, тож повторного _on_silence_resolved не буде.
                if self._silence_notified:
                    try:
                        self._on_silence_resolved()
                    except Exception:
                        logging.exception("on_silence_resolved впав (%s)", self.kind)

    def stop(self) -> None:
        """Прапорець → join → закрити. Читач не блокується (read лише при повному
        блоці в буфері), тож join повертається за ≤ _WATCH_INTERVAL і на тиші, і
        під звуком — stop() швидкий (< 0.5 с). Закриваємо ПІСЛЯ join: закриття
        потоку, поки читач усередині read(), — це use-after-free у PortAudio
        (segfault, перевірено живо). Останній завершений sink уже на диску.

        Виняток — recovery: reader міг застрягти у sleep/блокуючому open понад
        join-таймаут. Тоді _close_stream/_close_pa не покладаються на 3 с, а через
        _pa_lock дочекаються виходу reader-потоку з PortAudio перед close (Б1)."""
        self._running = False
        for t in (self._thread, self._watch_thread):
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=STALL_SECONDS + 1.0)
        self._close_stream()
        self._close_pa()
        self._thread = self._watch_thread = None

    def _close_stream(self) -> None:
        # У штатному stop викликається після join читача — конкуренції з read
        # уже нема; close на активному потоці сам зупиняє його (Pa_CloseStream ≈
        # abort). Замок дає безпеку й тоді, коли reader ще в recovery (Б1): якщо
        # там триває open, ця гілка дочекається його виходу, а не закриє наосліп.
        with self._pa_lock:
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    logging.exception("Помилка закриття потоку (%s)", self.kind)
                self._stream = None

    def _close_pa(self) -> None:
        with self._pa_lock:
            if self._pa is not None:
                try:
                    self._pa.terminate()
                except Exception:
                    pass
                self._pa = None

    def take_level(self) -> tuple:
        """Знімок рівня для GUI-таймера: (rms, peak) лінійної амплітуди 0..1.
        peak — накопичений максимум від попереднього виклику (peak-hold), скидається."""
        rms = self._meter_rms
        peak = self._meter_peak
        self._meter_peak = 0.0
        return rms, peak

    @property
    def frames_written(self) -> int:
        return self._frames_written
