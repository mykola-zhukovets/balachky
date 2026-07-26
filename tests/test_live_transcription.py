import threading
import time

import numpy as np

from whisper_core.live import LiveTranscriber


class FakeVad:
    def __init__(self, values):
        self.values = iter(values)

    def is_speech(self, _audio):
        return next(self.values)


class FakeEngine:
    def __init__(self, gate=None, fail=False):
        self.calls = []
        self.gate = gate
        self.fail = fail
        self.started = threading.Event()

    def transcribe(self, audio):
        self.calls.append(len(audio))
        self.started.set()
        if self.gate is not None:
            self.gate.wait(2)
        if self.fail:
            raise RuntimeError("fake engine failure")
        return "raw", f"text-{len(audio)}", 0, [], []


def _chunk(value=1.0, n=10):
    return np.full(n, value, dtype=np.float32)


def test_live_segments_on_vad_pause():
    seen = []
    live = LiveTranscriber(FakeEngine(), vad=FakeVad([True, True, False, False]),
                           sample_rate=10, pause_ms=200, min_segment_s=0.1,
                           on_segment=seen.append)
    for _ in range(4):
        live.feed(_chunk())
    assert _wait_until(lambda: any(item.is_final for item in seen))
    live.stop(wait=True, timeout=2)
    finals = [item for item in seen if item.is_final]
    assert len(finals) == 1
    assert finals[0].start == 0
    # сегмент закривається чанком тиші, що добив pause_samples (3-тя секунда);
    # четвертий чанк тиші вже поза сегментом
    assert finals[0].end == 3


def test_backpressure_queue_stays_bounded_while_engine_is_busy():
    gate = threading.Event()
    engine = FakeEngine(gate)
    live = LiveTranscriber(engine, vad=FakeVad([True, False] * 4), sample_rate=10,
                           pause_ms=100, min_segment_s=0.1, max_pending_segments=1)
    for _ in range(8):
        live.feed(_chunk())
    assert _wait_until(lambda: engine.started.is_set())
    assert live.pending_segments <= 1
    gate.set()
    live.stop(wait=True, timeout=3)
    assert len(engine.calls) <= 1


def test_engine_failure_disables_live_without_breaking_feeder():
    errors = []
    live = LiveTranscriber(FakeEngine(fail=True), vad=FakeVad([True, False]),
                           sample_rate=10, pause_ms=100, min_segment_s=0.1,
                           on_error=errors.append)
    live.feed(_chunk())
    live.feed(_chunk(0))
    live._worker.join(2)
    live.feed(_chunk())  # capture caller remains safe after failure
    assert live.state == LiveTranscriber.ERROR
    assert len(errors) == 1


def test_stop_discards_unstarted_partial_preview():
    seen = []
    live = LiveTranscriber(FakeEngine(), vad=FakeVad([True]), sample_rate=10,
                           min_segment_s=0.1, on_segment=seen.append)
    live.feed(_chunk())
    assert _wait_until(lambda: live.buffered_blocks == 0)
    live.stop(wait=True, timeout=2)
    assert seen == []

def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_continuous_speech_forces_bounded_final_segments():
    engine = FakeEngine()
    live = LiveTranscriber(engine, vad=FakeVad([True] * 12), sample_rate=10,
                           partial_interval_s=100, min_segment_s=0.1,
                           max_segment_s=2.0, max_pending_blocks=32)
    for _ in range(12):
        live.feed(_chunk())
    assert _wait_until(lambda: len(engine.calls) >= 5)
    live.stop(wait=True, timeout=2)
    # За безперервного мовлення жоден FINAL не утримує понад 2 секунди аудіо.
    assert max(engine.calls) <= 20
    assert not live._frames


def test_feed_ring_buffer_is_bounded_when_worker_is_busy():
    gate = threading.Event()
    engine = FakeEngine(gate)
    live = LiveTranscriber(engine, vad=FakeVad([True, False]), sample_rate=10,
                           pause_ms=100, min_segment_s=0.1, max_pending_blocks=2)
    live.feed(_chunk())
    live.feed(_chunk(0))
    assert _wait_until(lambda: engine.started.is_set())
    started = time.monotonic()
    for _ in range(100):
        live.feed(_chunk())
    assert time.monotonic() - started < 0.1
    assert live.buffered_blocks <= 2
    assert live.dropped_blocks >= 98
    gate.set()
    live.stop(wait=True, timeout=2)


def test_ring_overflow_splits_segments_and_preserves_capture_timestamps():
    class BlockingVad:
        def __init__(self):
            self.values = iter([True, True, True, False])
            self.block = threading.Event()
            self.entered = threading.Event()
            self.release = threading.Event()

        def is_speech(self, _audio):
            value = next(self.values)
            if self.block.is_set() and not self.entered.is_set():
                self.entered.set()
                self.release.wait(2)
            return value

    seen, vad = [], BlockingVad()
    live = LiveTranscriber(FakeEngine(), vad=vad, sample_rate=10, pause_ms=100,
                           min_segment_s=0.1, max_pending_blocks=2,
                           on_segment=seen.append)
    live.feed(_chunk())
    assert _wait_until(lambda: bool(live._frames))
    vad.block.set()
    live.feed(_chunk())
    assert vad.entered.wait(1)
    # Третій блок губиться; наступний мусить почати окремий сегмент з t=3 s.
    live.feed(_chunk())
    live.feed(_chunk())
    live.feed(_chunk(0))
    assert live.dropped_blocks == 1
    vad.release.set()
    assert _wait_until(lambda: len([item for item in seen if item.is_final]) == 2)
    live.stop(wait=True, timeout=2)
    finals = [item for item in seen if item.is_final]
    assert [(item.start, item.end) for item in finals] == [(0, 2), (3, 5)]


def test_stop_drops_waiting_live_job_so_final_lock_is_not_delayed():
    engine_lock = threading.Lock()
    engine_lock.acquire()                 # імітуємо зайнятий engine до stop
    live_engine = FakeEngine()
    live = LiveTranscriber(live_engine, vad=FakeVad([True, False]), sample_rate=10,
                           pause_ms=100, min_segment_s=0.1, engine_lock=engine_lock)
    live.feed(_chunk())
    live.feed(_chunk(0))
    assert _wait_until(lambda: live._processing)
    live.stop()

    final_got_lock = threading.Event()
    final = threading.Thread(target=lambda: (engine_lock.acquire(), final_got_lock.set(), engine_lock.release()))
    final.start()
    engine_lock.release()
    assert final_got_lock.wait(0.3)
    final.join(1)
    live.stop(wait=True, timeout=1)
    assert live_engine.calls == []

def test_stale_live_error_does_not_disable_new_instance():
    from types import SimpleNamespace
    from fronts.desktop.app import DesktopApp

    stale, current = object(), object()
    disabled, synced, reported = [], [], []
    controller = SimpleNamespace(
        _live_dictation=current,
        _live_meeting=None,
        set_live_transcription=lambda on: disabled.append(on),
        window=SimpleNamespace(
            meeting=SimpleNamespace(
                sync_live_transcription=lambda on: synced.append(on)),
            settings=SimpleNamespace(
                sync_live_transcription=lambda on: synced.append(on))),
        live_disable_requested=SimpleNamespace(
            emit=lambda live: reported.append(live)),
        live_error=SimpleNamespace(emit=lambda _message: None),
    )
    DesktopApp._live_failed(controller, stale, RuntimeError("old worker"))
    DesktopApp._disable_live_transcription_from_gui(controller, stale)
    assert reported == [stale]
    assert disabled == synced == []

    DesktopApp._disable_live_transcription_from_gui(controller, current)
    assert disabled == synced == [False]


def test_live_transcription_config_round_trip(tmp_path):
    from whisper_core.config import Config

    path = tmp_path / "config.toml"
    cfg = Config(live_transcription=True)
    cfg.save(path)
    loaded = Config.load(path)
    assert loaded.live_transcription is True


# Адаптер: функції вище — pytest-стилю; проєкт ганяє unittest discover.
import inspect as _inspect
import tempfile as _tempfile
import unittest as _unittest
from pathlib import Path as _Path


class LiveTranscriberTests(_unittest.TestCase):
    pass


def _make_case(fn):
    def run(self):
        if "tmp_path" in _inspect.signature(fn).parameters:
            with _tempfile.TemporaryDirectory() as d:
                fn(_Path(d))
        else:
            fn()
    return run


for _name, _fn in list(globals().items()):
    if _name.startswith("test_") and callable(_fn):
        setattr(LiveTranscriberTests, _name, _make_case(_fn))
        del globals()[_name]
