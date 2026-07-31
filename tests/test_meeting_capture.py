"""Юніти сторони захоплення (Б1) — capture.py. PyAudioWPatch і потік підмінені:
device-резолвери тестуємо з фейковим PyAudio, а watchdog/рівень/втрату пристрою —
на фейковому stream з інжектованим годинником (без реального аудіо і без потоків)."""
import unittest
from unittest.mock import patch

import numpy as np

from whisper_core.meeting import capture
from whisper_core.meeting.capture import CaptureStream, DeviceLost


class _FakePA:
    """Мінімальний PyAudio: віддає задані device-info, лічить terminate()."""

    paWASAPI = 13
    paFloat32 = 1
    terminations = 0

    def __init__(self, *, loopback=None, default_input=None, devices=None,
                 wasapi_host=None, raise_loopback=False):
        self._loopback = loopback
        self._default_input = default_input
        self._devices = devices or []
        self._wasapi_host = wasapi_host
        self._raise_loopback = raise_loopback

    # capture._pa() робить pyaudio.PyAudio() → повертаємо цей самий інстанс
    def PyAudio(self):
        return self

    def get_default_wasapi_loopback(self):
        if self._raise_loopback:
            raise OSError("нема loopback")
        return self._loopback

    def get_default_input_device_info(self):
        return self._default_input

    def get_host_api_info_by_type(self, _t):
        return {"index": self._wasapi_host}

    def get_device_count(self):
        return len(self._devices)

    def get_device_info_by_index(self, i):
        return self._devices[i]

    def terminate(self):
        type(self).terminations += 1


class LoopbackResolveTests(unittest.TestCase):
    def test_default_loopback_uses_generator_result_not_hardcoded_index(self):
        fake = _FakePA(loopback={"index": 34, "name": "Speakers [Loopback]"})
        _FakePA.terminations = 0
        with patch.object(capture, "pyaudio", fake):
            info = capture.default_loopback()
        self.assertEqual(info["index"], 34)          # взято з API, не хардкод
        self.assertEqual(_FakePA.terminations, 1)    # хендл прибрано

    def test_default_loopback_none_when_absent(self):
        fake = _FakePA(raise_loopback=True)
        with patch.object(capture, "pyaudio", fake):
            self.assertIsNone(capture.default_loopback())

    def test_default_loopback_none_when_patch_not_loaded(self):
        with patch.object(capture, "pyaudio", None):
            self.assertIsNone(capture.default_loopback())


class CommunicationsDeviceTests(unittest.TestCase):
    """default_communications_device_name(): роль WASAPI «для зв'язку», яку
    PyAudioWPatch не віддає взагалі (аудит 30.07, ризик 1) — дістаємо напряму
    через ctypes/COM. Чесна межа: поза Windows — завжди None, без здогадів."""

    def test_returns_none_off_windows(self):
        with patch("sys.platform", "linux"):
            self.assertIsNone(capture.default_communications_device_name())

    def test_never_raises_and_returns_str_or_none(self):
        # На цій машині Windows — результат залежить від живого COM/аудіо, тож
        # перевіряємо лише контракт (тип), а не конкретне ім'я пристрою.
        result = capture.default_communications_device_name()
        self.assertTrue(result is None or isinstance(result, str))


class InputResolveTests(unittest.TestCase):
    def _fake(self):
        # той самий мік присутній на MME (host 0) і WASAPI (host 5) + loopback
        devices = [
            {"index": 0, "name": "Microphone Array", "hostApi": 0,
             "maxInputChannels": 2, "isLoopbackDevice": False},
            {"index": 1, "name": "Microphone Array", "hostApi": 5,
             "maxInputChannels": 2, "isLoopbackDevice": False},
            {"index": 2, "name": "Speakers [Loopback]", "hostApi": 5,
             "maxInputChannels": 2, "isLoopbackDevice": True},
        ]
        return _FakePA(devices=devices, wasapi_host=5,
                       default_input={"index": 99, "name": "Системний"})

    def test_name_resolves_to_wasapi_device(self):
        with patch.object(capture, "pyaudio", self._fake()):
            info = capture.default_input("Microphone Array")
        self.assertEqual(info["index"], 1)           # саме WASAPI-двійник, не MME

    def test_unknown_name_falls_back_to_default(self):
        with patch.object(capture, "pyaudio", self._fake()):
            info = capture.default_input("Немає такого")
        self.assertEqual(info["index"], 99)

    def test_no_name_returns_system_default(self):
        with patch.object(capture, "pyaudio", self._fake()):
            self.assertEqual(capture.default_input()["index"], 99)


class _FakeStream:
    """read() віддає заздалегідь задані порції; після вичерпання — остання порція.
    get_read_available() за замовчуванням каже «повний блок готовий» — читач у
    тестах не дрімає (кейс «кадрів нема» задається через avail=0)."""

    def __init__(self, chunks, avail=None):
        self._chunks = list(chunks)
        self._i = 0
        self._avail = capture.READ_BLOCK if avail is None else avail

    def get_read_available(self):
        if isinstance(self._avail, Exception):
            raise self._avail
        return self._avail

    def read(self, _n, exception_on_overflow=True):
        chunk = self._chunks[min(self._i, len(self._chunks) - 1)]
        self._i += 1
        if isinstance(chunk, Exception):
            raise chunk
        return chunk


def _stream(*, kind="mic", channels=1, sink=None, on_stall=None,
            on_device_lost=None, on_sink_error=None, on_audio=None,
            on_silence=None, on_silence_resolved=None):
    cs = CaptureStream(
        kind=kind, device_index=0, channels=channels, rate=48000,
        sink=sink or (lambda pcm: None),
        on_stall=on_stall or (lambda: None),
        on_device_lost=on_device_lost or (lambda exc: None),
        on_sink_error=on_sink_error,
        on_audio=on_audio,
        on_silence=on_silence,
        on_silence_resolved=on_silence_resolved)
    return cs


class LevelTests(unittest.TestCase):
    def test_full_scale_reads_one_silence_reads_zero(self):
        cs = _stream(channels=1)
        cs._update_level(np.ones(128, dtype=np.float32).tobytes())
        rms, peak = cs.take_level()
        self.assertAlmostEqual(rms, 1.0, places=5)
        self.assertAlmostEqual(peak, 1.0, places=5)
        self.assertEqual(cs.take_level()[1], 0.0)    # peak скинуто після знімка

        cs._update_level(np.zeros(128, dtype=np.float32).tobytes())
        rms, peak = cs.take_level()
        self.assertEqual((rms, peak), (0.0, 0.0))


class WatchdogTests(unittest.TestCase):
    """Монітор незалежний від циклу читання: ловить і завислий read (потік стоїть
    у виклику), і порожній. Тестуємо його рішення напряму інжектованим `now`."""

    def test_stall_fires_once_after_threshold_then_recovers(self):
        stalls = []
        got = []
        cs = _stream(channels=1, sink=got.append, on_stall=lambda: stalls.append(1))
        cs._last_data = 0.0
        cs._stalled = False

        cs._check_stall(now=1.0)                     # < STALL_SECONDS (2.0)
        self.assertEqual(stalls, [])
        cs._check_stall(now=3.0)                     # перевищено → on_stall
        self.assertEqual(len(stalls), 1)
        cs._check_stall(now=3.5)                     # ще завмер → НЕ спамимо
        self.assertEqual(len(stalls), 1)

        # кадри повернулися через _pump → лічильник росте, прапорець stall знято
        # (_t0 = now, щоб gap-fill не домішувався — його тестує GapFillTests)
        cs._stream = _FakeStream([np.ones(4, dtype=np.float32).tobytes()])
        cs._running = True
        cs._t0 = 4.0
        cs._pump(now=4.0)
        self.assertEqual(cs.frames_written, 4)
        self.assertTrue(got)
        self.assertFalse(cs._stalled)
        cs._check_stall(now=5.0)                      # 5-4=1<2 → тиша, потік живий
        self.assertEqual(len(stalls), 1)
        cs._check_stall(now=7.0)                      # новий епізод завмирання
        self.assertEqual(len(stalls), 2)

    def test_silence_frames_are_not_a_stall(self):
        # loopback з активним звуком віддає silence-КАДРИ (нулі): _pump оновлює мітку,
        # монітор бачить свіжий _last_data → не спрацьовує
        stalls = []
        cs = _stream(channels=2, on_stall=lambda: stalls.append(1))
        cs._stream = _FakeStream([np.zeros(4 * 2, dtype=np.float32).tobytes()])
        cs._running = True
        cs._t0 = 100.0                               # нейтралізуємо gap-fill
        cs._pump(now=100.0)                          # кадри є → _last_data=100.0
        self.assertEqual(cs.frames_written, 4)
        cs._check_stall(now=100.1)                   # свіжа мітка → без stall
        self.assertEqual(stalls, [])


class SilenceWatchdogTests(unittest.TestCase):
    """Вартовий тиші системної доріжки (аудит 30.07, ризик 5): попереджає ОДИН
    раз, якщо за SILENCE_WARN_SECONDS доріжка не принесла жодного реального
    ненульового блока. Донабита gap-fill тиша НЕ рахується за «почутий звук»."""

    def test_all_zero_track_warns_once_after_threshold(self):
        warned = []
        cs = _stream(channels=1, on_silence=lambda: warned.append(1))
        cs._t0 = 0.0
        cs._check_silence(now=capture.SILENCE_WARN_SECONDS - 0.1)
        self.assertEqual(warned, [])                 # ще рано — не спамимо завчасно
        cs._check_silence(now=capture.SILENCE_WARN_SECONDS)
        self.assertEqual(len(warned), 1)
        cs._check_silence(now=capture.SILENCE_WARN_SECONDS + 30)
        self.assertEqual(len(warned), 1)              # один епізод — без повторів

    def test_real_audio_before_threshold_prevents_warning(self):
        # реальний (не gap-fill) блок зі звуком — heard_sound=True; поріг мовчить
        warned = []
        cs = _stream(channels=1, on_silence=lambda: warned.append(1))
        cs._t0 = 0.0
        cs._update_level(np.ones(128, dtype=np.float32).tobytes())
        cs._check_silence(now=capture.SILENCE_WARN_SECONDS + 5)
        self.assertEqual(warned, [])

    def test_gap_fill_zeros_do_not_count_as_heard_sound(self):
        # _fill_gap пише тишу напряму через sink, МИНАЮЧИ _update_level —
        # вартовий і далі мусить вважати доріжку «нечутою»
        cs = _stream(channels=1)
        cs._t0 = 0.0
        cs._fill_gap(now=5.0, exact=True)             # донабиває тишу за 5 с
        self.assertFalse(cs._heard_sound)

    def test_real_silent_block_read_does_not_count_as_heard_sound(self):
        # справжній кадр з мовчазного loopback (усі нулі, прийшов через read())
        # теж не має рахуватись «почутим звуком» — інакше вартовий ніколи не
        # спрацює на «слухаємо не той пристрій».
        warned = []
        cs = _stream(channels=1, on_silence=lambda: warned.append(1))
        cs._stream = _FakeStream([np.zeros(4, dtype=np.float32).tobytes()])
        cs._running = True
        cs._t0 = 0.0
        cs._pump(now=0.0)
        self.assertFalse(cs._heard_sound)
        cs._check_silence(now=capture.SILENCE_WARN_SECONDS)
        self.assertEqual(len(warned), 1)

    def test_real_sound_after_warning_fires_resolved_once(self):
        """Суддівське зауваження 30.07: попередження мусить мати «слід
        завершення» — коли після спрацювання приходить перший реальний звук,
        on_silence_resolved летить рівно один раз (банер ховається)."""
        warned = []
        resolved = []
        cs = _stream(channels=1, on_silence=lambda: warned.append(1),
                     on_silence_resolved=lambda: resolved.append(1))
        cs._t0 = 0.0
        cs._check_silence(now=capture.SILENCE_WARN_SECONDS)   # спрацювало
        self.assertEqual(len(warned), 1)
        self.assertEqual(resolved, [])                        # ще нема звуку

        cs._update_level(np.ones(128, dtype=np.float32).tobytes())
        self.assertEqual(len(resolved), 1)                    # звук прийшов → зникло

        # наступні блоки (тиша чи звук) НЕ повторюють resolved — немає флікеру
        cs._update_level(np.zeros(128, dtype=np.float32).tobytes())
        cs._update_level(np.ones(128, dtype=np.float32).tobytes())
        self.assertEqual(len(resolved), 1)

    def test_sound_before_warning_never_triggers_resolved(self):
        """Якщо звук прийшов ДО того, як попередження взагалі показали
        (_silence_notified лишається False) — resolved не потрібен, попередження
        просто ніколи не спрацює (test_real_audio_before_threshold_prevents_warning)."""
        resolved = []
        cs = _stream(channels=1, on_silence_resolved=lambda: resolved.append(1))
        cs._t0 = 0.0
        cs._update_level(np.ones(128, dtype=np.float32).tobytes())
        cs._check_silence(now=capture.SILENCE_WARN_SECONDS + 5)
        self.assertEqual(resolved, [])

    def test_pause_between_replies_does_not_flicker_after_resolved(self):
        """Реальний сценарій: репліка з'являється й зникає (пауза між
        репліками) ПІСЛЯ того, як банер уже сховано. Немає повторного
        показу/ховання — heard_sound залишається True назавжди."""
        warned = []
        resolved = []
        cs = _stream(channels=1, on_silence=lambda: warned.append(1),
                     on_silence_resolved=lambda: resolved.append(1))
        cs._t0 = 0.0
        cs._check_silence(now=capture.SILENCE_WARN_SECONDS)
        cs._update_level(np.ones(128, dtype=np.float32).tobytes())   # репліка
        self.assertEqual(len(resolved), 1)
        # пауза (нулі), потім ще одна репліка — сторожа мовчить: попередження
        # не з'являється знову, бо _heard_sound уже True
        for _ in range(5):
            cs._check_silence(now=capture.SILENCE_WARN_SECONDS + 60)
        cs._update_level(np.zeros(128, dtype=np.float32).tobytes())
        cs._update_level(np.ones(128, dtype=np.float32).tobytes())
        self.assertEqual(len(warned), 1)
        self.assertEqual(len(resolved), 1)


class DeviceBusyErrorTests(unittest.TestCase):
    """Розпізнавання виключного режиму WASAPI (аудит 30.07, ризик 6): чітке
    повідомлення замість загального «не вдалося ініціалізувати»."""

    def test_recognizes_known_exclusive_mode_markers(self):
        for text in (
            "AUDCLNT_E_DEVICE_IN_USE",
            "[Errno -9999] Unanticipated host error: 0x8889000A",
            "The device is already in use by another application.",
        ):
            self.assertTrue(capture.is_device_busy_error(DeviceLost(text)), text)

    def test_generic_error_is_not_flagged_busy(self):
        self.assertFalse(capture.is_device_busy_error(
            DeviceLost("Пристрій не знайдено")))


class NonBlockingReadTests(unittest.TestCase):
    def test_no_full_block_means_no_read_and_no_sink(self):
        # тиша loopback: avail=0 → читач НЕ входить у блокуючий read (segfault-безпека
        # stop) і нічого не пише; кадри «не приходять» → далі відпрацює монітор
        got = []
        cs = _stream(channels=1, sink=got.append)
        cs._running = True
        cs._stream = _FakeStream([b"never-read"], avail=0)
        cs._pump(now=1.0)
        self.assertEqual(got, [])
        self.assertEqual(cs.frames_written, 0)

    def test_avail_error_is_device_lost(self):
        lost = []
        cs = _stream(on_device_lost=lambda exc: lost.append(exc))
        cs._running = True
        cs._stream = _FakeStream([b""], avail=OSError("пристрій зник"))
        cs._pump(now=1.0)
        self.assertFalse(cs._running)
        self.assertEqual(len(lost), 1)
        self.assertIsInstance(lost[0], DeviceLost)


class GapFillTests(unittest.TestCase):
    """Живий факт: loopback на тиші БЛОКУЄ read (кадри не приходять) → пауза
    зникала б з доріжки і шкали mic/sys роз'їхались би. Донабивання тиші тримає
    обидві доріжки на спільній wall-clock шкалі від старту."""

    def test_missing_frames_below_threshold_is_zero(self):
        # 48000 кадрів за 1.1 с: дефіцит 0.1 с < порога 0.25 с → не смикаємось
        self.assertEqual(capture.missing_frames(1.1, 48000, 48000), 0)
        # доріжка попереду шкали (заокруглення) → теж 0
        self.assertEqual(capture.missing_frames(0.9, 48000, 48000), 0)

    def test_missing_frames_above_threshold_fills_down_to_margin(self):
        # 3 с тиші: 4 с минуло, доставлено 1 с → донабиваємо (3 − 0.25) с × rate:
        # запас лишаємо під буферизовані «в дорозі» кадри (гістерезис)
        self.assertEqual(capture.missing_frames(4.0, 48000, 48000),
                         int((3.0 - capture.GAP_FILL_MIN_SECONDS) * 48000))

    def test_pump_prepends_silence_after_pause(self):
        got = []
        cs = _stream(channels=1, sink=got.append)
        cs._running = True
        cs._t0 = 0.0
        cs._frames_written = 48000                    # 1 с доставлено
        data = np.ones(4, dtype=np.float32).tobytes()
        cs._stream = _FakeStream([data])
        cs._pump(now=4.0)                             # read повернувся після паузи 3 с

        # донабито тишу (пауза мінус гістерезис-запас) перед новими кадрами,
        # порціями ≤ READ_BLOCK
        expected = int((3.0 - capture.GAP_FILL_MIN_SECONDS) * 48000)
        silence = got[:-1]
        self.assertEqual(got[-1], data)               # реальні кадри — останні
        silence_frames = sum(len(b) // 4 for b in silence)
        self.assertEqual(silence_frames, expected)
        self.assertTrue(all(len(b) // 4 <= capture.READ_BLOCK for b in silence))
        self.assertTrue(all(b == b"\x00" * len(b) for b in silence))
        # шкала вирівняна: лічильник = тиша + доставлене (дефіцит не накопичиться)
        self.assertEqual(cs.frames_written, 48000 + expected + 4)

    def test_steady_stream_gets_no_fill(self):
        got = []
        cs = _stream(channels=1, sink=got.append)
        cs._running = True
        cs._t0 = 0.0
        cs._frames_written = 48000
        data = np.ones(4, dtype=np.float32).tobytes()
        cs._stream = _FakeStream([data])
        cs._pump(now=1.05)                            # дефіцит 0.05 с — джиттер
        self.assertEqual(got, [data])                 # без домішаної тиші

class LiveAudioCallbackTests(unittest.TestCase):
    def test_on_audio_receives_data_after_successful_sink(self):
        written, preview = [], []
        cs = _stream(channels=1, sink=written.append, on_audio=preview.append)
        data = np.ones(4, dtype=np.float32).tobytes()
        cs._running = True
        cs._t0 = 0.0
        cs._stream = _FakeStream([data])
        cs._pump(now=0.05)
        self.assertEqual(written, [data])
        self.assertEqual(preview, [data])

    def test_on_audio_error_does_not_stop_capture(self):
        written = []
        cs = _stream(channels=1, sink=written.append,
                     on_audio=lambda _data: (_ for _ in ()).throw(RuntimeError("preview")))
        data = np.ones(4, dtype=np.float32).tobytes()
        cs._running = True
        cs._t0 = 0.0
        cs._stream = _FakeStream([data])
        with self.assertLogs(level="ERROR"):
            cs._pump(now=0.05)
        self.assertTrue(cs._running)
        self.assertEqual(written, [data])
        self.assertEqual(cs.frames_written, 4)

class SinkFailureTests(unittest.TestCase):
    def test_sink_error_is_logged_and_thread_survives(self):
        # помилка sink (напр. гонка ротації до фіксу, диск) НЕ вбиває потік мовчки:
        # блок відкидається з логом, наступний блок пишеться далі
        calls = {"n": 0}
        got = []

        def flaky_sink(pcm):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("I/O operation on closed file")
            got.append(pcm)

        cs = _stream(channels=1, sink=flaky_sink)
        cs._running = True
        cs._t0 = 0.0
        data = np.ones(4, dtype=np.float32).tobytes()
        cs._stream = _FakeStream([data])
        with self.assertLogs(level="ERROR"):
            cs._pump(now=0.05)                        # sink упав → блок відкинуто
        self.assertTrue(cs._running)                  # потік живий
        self.assertEqual(cs.frames_written, 0)        # невдалий блок не порахований
        cs._pump(now=0.1)                             # наступний блок — ок
        self.assertEqual(got, [data])
        self.assertEqual(cs.frames_written, 4)

    def test_persistent_non_enospc_error_reaches_user_callback_once(self):
        warnings = []

        def denied(_pcm):
            raise PermissionError("access denied")

        cs = _stream(sink=denied, on_sink_error=lambda exc, elapsed: warnings.append(
            (exc, elapsed)))
        cs._stream = _FakeStream([np.ones(4, dtype=np.float32).tobytes()])
        cs._running = True
        cs._t0 = 10.0

        with self.assertLogs(level="ERROR"):
            cs._pump(now=20.0)                        # разовий збій — без тосту
        cs._pump(now=21.0)
        self.assertEqual(warnings, [])
        cs._pump(now=22.0)                            # стійкий збій → UI callback
        cs._pump(now=23.0)                            # той самий епізод не спамить

        self.assertTrue(cs._running)
        self.assertEqual(len(warnings), 1)
        self.assertIsInstance(warnings[0][0], PermissionError)
        self.assertEqual(warnings[0][1], 12.0)

    def test_alternating_sink_failures_warn_about_fifty_percent_loss_once(self):
        warnings = []
        calls = {"total": 0, "failed": 0}

        def alternating_sink(_pcm):
            calls["total"] += 1
            if calls["total"] % 2:
                calls["failed"] += 1
                raise PermissionError("access denied")

        cs = _stream(sink=alternating_sink,
                     on_sink_error=lambda exc, elapsed: warnings.append((exc, elapsed)))
        data = np.ones(capture.READ_BLOCK, dtype=np.float32).tobytes()
        cs._stream = _FakeStream([data])
        cs._running = True
        cs._t0 = 0.0

        with self.assertLogs(level="ERROR"):
            for block in range(400):
                cs._pump(now=block / 1000)

        self.assertEqual(calls, {"total": 400, "failed": 200})
        self.assertEqual(cs.frames_written, 819200)
        self.assertEqual(len(warnings), 1)
        self.assertIsInstance(warnings[0][0], PermissionError)

    def test_one_or_two_isolated_sink_failures_do_not_warn(self):
        warnings = []
        calls = {"n": 0}

        def twice_flaky_sink(_pcm):
            calls["n"] += 1
            if calls["n"] in (1, 301):
                raise PermissionError("access denied")

        cs = _stream(sink=twice_flaky_sink,
                     on_sink_error=lambda exc, elapsed: warnings.append((exc, elapsed)))
        data = np.ones(capture.READ_BLOCK, dtype=np.float32).tobytes()
        cs._stream = _FakeStream([data])
        cs._running = True
        cs._t0 = 0.0

        with self.assertLogs(level="ERROR"):
            for block in range(400):
                cs._pump(now=block / 1000)

        self.assertEqual(calls["n"], 400)
        self.assertEqual(warnings, [])


class SharedClockTests(unittest.TestCase):
    def test_three_tracks_fill_from_one_clock_origin(self):
        origin = 100.0
        streams = [_stream(kind=f"mic{i}", channels=1, sink=lambda _pcm: None)
                   for i in range(1, 4)]
        for stream in streams:
            stream.set_clock_origin(origin)
            stream._t0 = stream._clock_origin  # start() використовує ту саму опору
            stream._running = True
            stream._stream = _FakeStream([np.ones(4, dtype=np.float32).tobytes()])
            stream._pump(now=100.5)
        expected = int((0.5 - capture.GAP_FILL_MIN_SECONDS) * 48000) + 4
        self.assertTrue(all(stream.frames_written == expected for stream in streams))
class DeviceLostTests(unittest.TestCase):
    def test_read_error_triggers_device_lost_and_stops(self):
        lost = []
        cs = _stream(on_device_lost=lambda exc: lost.append(exc))
        cs._stream = _FakeStream([OSError("пристрій зник")])
        cs._running = True
        cs._pump(now=1.0)
        self.assertFalse(cs._running)                # чистий стоп, без зависання
        self.assertEqual(len(lost), 1)
        self.assertIsInstance(lost[0], DeviceLost)

    def test_disk_full_notifies_once_and_capture_stays_alive(self):
        import errno
        warnings = []

        def full(_pcm):
            raise OSError(errno.ENOSPC, "disk full")

        cs = _stream(sink=full, on_sink_error=lambda exc, elapsed: warnings.append(
            (exc.errno, elapsed)))
        cs._stream = _FakeStream([np.ones(4, dtype=np.float32).tobytes()])
        cs._running = True
        cs._t0 = 0.0
        cs._pump(now=65.0)
        self.assertEqual(len(warnings), 1)  # ENOSPC минає віконний поріг
        cs._pump(now=66.0)  # той самий епізод не спамить UI
        self.assertTrue(cs._running)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0][0], errno.ENOSPC)
        self.assertEqual(warnings[0][1], 65.0)

    def test_read_error_during_stop_is_silent(self):
        # stop() виставляє _running=False і закриває потік → read перерветься; це НЕ
        # втрата пристрою, on_device_lost не має спрацювати
        lost = []
        cs = _stream(on_device_lost=lambda exc: lost.append(exc))
        cs._stream = _FakeStream([OSError("stream closed")])
        cs._running = False                          # ми в процесі stop()
        cs._pump(now=1.0)
        self.assertEqual(lost, [])


class StopRecoverRaceTests(unittest.TestCase):
    """Б1: stop() і recover не сміють чіпати PortAudio одночасно на одному хендлі
    (use-after-free → segfault). Тут форсуємо саме те вікно, де join за 3 с
    відвалюється: reader застряг у блокуючому open (recovery), а stop closes."""

    def test_close_waits_for_inflight_open_instead_of_racing_portaudio(self):
        import threading
        from time import sleep

        in_open = threading.Event()       # reader увійшов у блокуючий open
        release_open = threading.Event()  # тест дозволяє open завершитись
        log = []

        class _BlockingStream:
            def close(self):
                log.append("stream_close")

        class _BlockingPA:
            paFloat32 = 1

            def PyAudio(self):
                return self

            def open(self, **_kw):
                log.append("open_enter")
                in_open.set()
                release_open.wait(2.0)
                log.append("open_exit")
                return _BlockingStream()

            def terminate(self):
                log.append("terminate")

        cs = _stream()
        errors = []

        def do_open():
            try:
                with patch.object(capture, "pyaudio", _BlockingPA()):
                    cs._open_audio()
            except Exception as exc:      # pragma: no cover — фіксуємо, якщо впаде
                errors.append(exc)

        opener = threading.Thread(target=do_open)
        opener.start()
        # reader тепер тримає _pa_lock усередині open (як recover до 30 с)
        self.assertTrue(in_open.wait(2.0))

        closed = []

        def do_close():
            cs._close_pa()                # stop()-гілка: має ЗАЧЕКАТИ на замок
            closed.append(True)

        closer = threading.Thread(target=do_close)
        closer.start()
        sleep(0.15)                       # даємо closer час дійти до замку й стати
        # Поки open у польоті — terminate НЕ сміє статися (інакше два виклики
        # PortAudio на одному хендлі = segfault), а _close_pa досі чекає:
        self.assertNotIn("terminate", log)
        self.assertEqual(closed, [])

        release_open.set()                # open завершується → замок звільняється
        opener.join(2.0)
        closer.join(2.0)
        self.assertEqual(errors, [])
        self.assertEqual(closed, [True])
        # terminate стався РІВНО після виходу з open — серіалізовано, не паралельно
        self.assertIn("terminate", log)
        self.assertLess(log.index("open_exit"), log.index("terminate"))


if __name__ == "__main__":
    unittest.main()
