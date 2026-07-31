"""Контракт runtime-відновлення аудіо: тільки фейкові PyAudio-потоки.

Цей файл навмисно не потребує реального WASAPI-пристрою. Зовнішній runner може
запустити його разом з наявними тестами; у межах задачі тестовий набір не
запускаємо.
"""
import tempfile
import unittest
import inspect
from pathlib import Path
from time import monotonic
from unittest.mock import patch

from whisper_core.meeting import capture
from whisper_core.meeting.capture import CaptureStream
from whisper_core.meeting.session import STATUS_INTERRUPTED, create_session, load_meta
from fronts.desktop import recorder as recorder_module
from fronts.desktop.recorder import Recorder


class _ReadFailure:
    def __init__(self, chunks_before_failure=0):
        self.chunks_before_failure = chunks_before_failure

    def get_read_available(self):
        return capture.READ_BLOCK

    def read(self, *_args, **_kwargs):
        if self.chunks_before_failure:
            self.chunks_before_failure -= 1
            return b"\x01\x00\x00\x00"
        raise OSError("USB microphone unplugged")

    def close(self):
        pass


class _OpenFailure:
    def close(self):
        pass


class _OpenGood:
    def get_read_available(self):
        return 0

    def close(self):
        pass


class _FakePA:
    def __init__(self, outcomes):
        self.outcomes = outcomes

    def open(self, **_kwargs):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def terminate(self):
        pass


class _FakePyAudio:
    paFloat32 = 1

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.opens = 0

    def PyAudio(self):
        self.opens += 1
        return _FakePA(self.outcomes)


class AudioResilienceTests(unittest.TestCase):
    def _stream(self, *, sink, lost, gap, failed):
        return CaptureStream(
            kind="mic", device_index=1, channels=1, rate=100,
            sink=sink, on_stall=lambda: None, on_device_lost=lost,
            device_resolver=lambda: {"index": 2, "name": "Default microphone"},
            on_gap=gap, on_recovery_failed=failed)

    def test_read_error_is_detected_and_retried_without_waiting_two_seconds(self):
        """Фейковий read кидає помилку одразу; дві невдалі спроби → успіх."""
        audio = _FakePyAudio([OSError("not ready"), OSError("not ready"), _OpenGood()])
        states, losses, gaps = [], [], []
        written = []
        cs = self._stream(sink=written.append, lost=losses.append,
                          gap=gaps.append, failed=lambda _exc: None)
        cs._stream = _ReadFailure(chunks_before_failure=2)
        cs._running = True
        cs._t0 = monotonic()
        cs._on_audio_state = states.append
        with patch.object(capture, "pyaudio", audio), \
             patch.object(capture, "sleep", lambda _seconds: None), \
             patch.object(capture, "RECOVERY_TIMEOUT", 1.0):
            cs._pump(monotonic())
            cs._pump(monotonic())
            cs._t0 = monotonic() - 1.0
            before = monotonic()
            cs._pump(monotonic())
        self.assertLess(monotonic() - before, 2.0)
        self.assertEqual(len(losses), 1)
        self.assertEqual(states, ["reconnecting", "reconnected"])
        self.assertEqual(audio.opens, 3)
        self.assertTrue(gaps and gaps[0] >= 0.9)
        # До sink пішла float32-тиша, прив'язана до wall-clock розриву.
        self.assertGreaterEqual(sum(len(x) for x in written), 90 * 4)

    def test_reconnect_gap_is_silence_with_matching_wall_clock_duration(self):
        audio = _FakePyAudio([_OpenGood()])
        written, gaps = [], []
        cs = self._stream(sink=written.append, lost=lambda _exc: None,
                          gap=gaps.append, failed=lambda _exc: None)
        cs._running = True
        cs._t0 = monotonic() - 2.0
        with patch.object(capture, "pyaudio", audio):
            cs._recover(capture.DeviceLost("Bluetooth changed profile"))
        silence_frames = sum(len(block) // 4 for block in written)
        self.assertEqual(gaps[0], silence_frames / 100)
        self.assertGreaterEqual(gaps[0], 1.9)

    def test_permanent_failure_finalizes_preserved_audio_as_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = create_session(Path(tmp), ["mic"], rate=100, channels=1)
            session.mic_sink(b"\x01\x00\x00\x00" * 40)  # аудіо до розриву
            audio = _FakePyAudio([OSError("gone")] * 8)
            cs = self._stream(
                sink=session.mic_sink, lost=lambda _exc: None,
                gap=lambda seconds: session.record_audio_gap("mic", seconds),
                failed=lambda _exc: session.finalize(STATUS_INTERRUPTED))
            cs._running = True
            cs._t0 = monotonic() - 1.0
            with patch.object(capture, "pyaudio", audio), \
                 patch.object(capture, "sleep", lambda _seconds: None), \
                 patch.object(capture, "RECOVERY_TIMEOUT", 0.01):
                cs._recover(capture.DeviceLost("device gone"))
            meta = load_meta(session.dir)
            self.assertEqual(meta.status, STATUS_INTERRUPTED)
            self.assertTrue(list((session.dir / "mic").glob("*.f32")))
        self.assertIn("mic", meta.audio_interruptions)

    def test_successful_capture_recovery_consumes_stale_watchdog_request(self):
        """Watchdog старого stream не може одразу запустити другий reconnect."""
        audio = _FakePyAudio([_OpenGood()])
        cs = self._stream(sink=lambda _data: None, lost=lambda _exc: None,
                          gap=lambda _seconds: None, failed=lambda _exc: None)
        cs._running = True
        cs._stream_generation = 3
        states = []

        def on_state(state):
            states.append(state)
            if state == "reconnected":
                # Імітуємо watchdog, який побачив inactive старий дескриптор
                # поки reader завершував reopen.
                cs._recovery_generation = 3
                cs._recovery_requested.set()

        cs._on_audio_state = on_state
        with patch.object(capture, "pyaudio", audio):
            cs._recover(capture.DeviceLost("old stream"), generation=3)

        self.assertEqual(states, ["reconnecting", "reconnected"])
        self.assertFalse(cs._recovery_requested.is_set())
        self.assertIsNone(cs._recovery_generation)

    def test_device_switch_cancels_recovery_and_closes_displaced_stream(self):
        """Перемикання під час retry лишає рівно один активний callback."""
        class FakeStream:
            active_streams = []

            def __init__(self, callback):
                self.callback = callback
                self.started = False
                self.closed = False

            @property
            def active(self):
                return self.started and not self.closed

            def start(self):
                self.started = True
                FakeStream.active_streams.append(self)
                if len(FakeStream.active_streams) > 1:
                    raise AssertionError("two active callbacks")

            def stop(self):
                if self in FakeStream.active_streams:
                    FakeStream.active_streams.remove(self)

            def close(self):
                self.stop()
                self.closed = True

        class FakeSD:
            def __init__(self):
                self.outcomes = ["stream", OSError("gone"), OSError("gone"), "stream"]
                self.created = []

            def query_devices(self):
                return [
                    {"name": "A", "max_input_channels": 1},
                    {"name": "B", "max_input_channels": 1},
                ]

            def InputStream(self, **kwargs):
                outcome = self.outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                stream = FakeStream(kwargs["callback"])
                self.created.append(stream)
                return stream

        fake_sd = FakeSD()
        switched = False

        def during_retry(_seconds):
            nonlocal switched
            if not switched:
                switched = True
                recorder.set_input_device("B")

        with patch.object(recorder_module, "sd", fake_sd):
            recorder = Recorder(100, input_device="A")
            self.addCleanup(recorder.close)
            recorder.start()
            old = recorder._stream
            with patch.object(recorder_module, "sleep", during_retry):
                recorder._recover(recorder._stream_generation)
            current = recorder._stream
            recorder.close()

        self.assertTrue(switched)
        self.assertTrue(old.closed)
        self.assertIsNot(current, old)
        self.assertTrue(current.closed)  # close() завжди закриває витіснений stream
        self.assertEqual(FakeStream.active_streams, [])

    def test_stale_callback_cannot_request_recovery_after_device_switch(self):
        """Callback, що почався до switch, не може отруїти новий stream."""
        class FakeStream:
            def __init__(self, callback):
                self.callback = callback
                self.started = False

            @property
            def active(self):
                return self.started

            def start(self):
                self.started = True

            def stop(self):
                self.started = False

            def close(self):
                self.stop()

        class FakeSD:
            def query_devices(self):
                return [
                    {"name": "A", "max_input_channels": 1},
                    {"name": "B", "max_input_channels": 1},
                ]

            def InputStream(self, **kwargs):
                return FakeStream(kwargs["callback"])

        with patch.object(recorder_module, "sd", FakeSD()):
            recorder = Recorder(100, input_device="A")
            self.addCleanup(recorder.close)
            recorder.start()
            old_callback = recorder._stream.callback

            class SwitchThenFail:
                def copy(self):
                    # Імітуємо switch рівно після перевірки generation у _cb,
                    # але до того, як callback попросить recovery.
                    recorder.set_input_device("B")
                    raise RuntimeError("old callback failed")

            old_callback(SwitchThenFail(), 0, None, None)

            self.assertFalse(recorder._recovery_requested.is_set())
            self.assertIsNone(recorder._recovery_generation)
            recorder.close()

    def test_missing_selected_microphone_reports_default_fallback_on_recording_start(self):
        class FakeStream:
            active = False

            def start(self):
                self.active = True

            def stop(self):
                self.active = False

            def close(self):
                self.active = False

        class FakeSD:
            @staticmethod
            def query_devices():
                return [{"name": "Laptop microphone", "max_input_channels": 1}]

            @staticmethod
            def InputStream(**kwargs):
                self.assertIsNone(kwargs["device"])
                return FakeStream()

        states = []
        with patch.object(recorder_module, "sd", FakeSD()):
            recorder = Recorder(
                100, input_device="Disconnected headset",
                on_audio_state=states.append)
            self.addCleanup(recorder.close)
            self.assertEqual(states, [])              # до запису не тривожимо
            recorder.start()
            recorder.start()                         # один fallback, не спам

        self.assertEqual(states, ["fallback"])

    def test_existing_constructor_calls_remain_valid(self):
        """Нові hooks optional: старі recorder/capture виклики не змінюються."""
        recorder = inspect.signature(Recorder)
        capture_stream = inspect.signature(CaptureStream)
        self.assertEqual(recorder.parameters["input_device"].default, None)
        self.assertEqual(capture_stream.parameters["on_device_lost"].default,
                         inspect.Parameter.empty)
        for name in ("device_resolver", "on_audio_state", "on_gap",
                     "on_recovery_failed"):
            self.assertEqual(capture_stream.parameters[name].default, None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
