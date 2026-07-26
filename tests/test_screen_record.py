"""Unit-тести штатного запису екрана без дисплея, mss або реального PyAV."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from whisper_core.config import Config
from whisper_core.meeting.screen_record import ScreenRecorder


class _Frame:
    def reformat(self, **_kw):
        return self


class _VideoFrame:
    @staticmethod
    def from_ndarray(_data, format):
        assert format == "bgra"
        return _Frame()


class _Stream:
    def __init__(self, *, encode_error=False, flush_error=False):
        self.frames = 0
        self.encode_error = encode_error
        self.flush_error = flush_error

    def encode(self, frame=None):
        if frame is not None:
            self.frames += 1
            if self.encode_error:
                raise RuntimeError("encode failed")
        elif self.flush_error:
            raise RuntimeError("flush failed")
        return []


class _Container:
    def __init__(self, **kw):
        self.stream = _Stream(**{k: v for k, v in kw.items() if k != "close_error"})
        self.closed = False
        self.close_error = kw.get("close_error", False)

    def add_stream(self, codec, rate):
        self.codec, self.rate = codec, rate
        return self.stream

    def mux(self, _packet):
        pass

    def close(self):
        self.closed = True
        if self.close_error:
            raise RuntimeError("close failed")


class _AV:
    VideoFrame = _VideoFrame

    def __init__(self, **kw):
        self.containers = []
        self.kw = kw

    def open(self, _path, mode):
        self.assert_mode = mode
        container = _Container(**self.kw)
        self.containers.append(container)
        return container


class _Source:
    def __init__(self, fail=False, block=False):
        self.fail = fail
        self.block = block
        self.closed = False
        self.grabs = 0
        self.grab_entered = threading.Event()
        self.release = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def bounds(self, _index):
        return {"width": 8, "height": 6}

    def grab(self, _index):
        self.grabs += 1
        self.grab_entered.set()
        if self.block:
            self.release.wait(2.0)
        if self.fail:
            raise RuntimeError("capture failed")
        return np.zeros((6, 8, 4), dtype=np.uint8)


class ScreenRecorderTests(unittest.TestCase):
    def _start(self, source, av=None, fps=12):
        recorder = ScreenRecorder(source_factory=lambda: source, av_module=av or _AV())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertTrue(recorder.start(1, Path(tmp.name) / "screen.mp4", fps))
        return recorder

    def test_start_stop_flushes_and_closes_container(self):
        av = _AV()
        source = _Source()
        recorder = self._start(source, av)
        self.assertTrue(recorder.wait_started())
        recorder.request_stop()
        self.assertTrue(recorder.wait_finished(1.0))
        self.assertFalse(recorder.is_running)
        self.assertTrue(source.closed)
        self.assertTrue(av.containers[0].closed)
        self.assertGreater(av.containers[0].stream.frames, 0)

    def test_request_stop_discards_frame_returned_after_stop(self):
        av = _AV()
        source = _Source(block=True)
        recorder = self._start(source, av)
        self.assertTrue(source.grab_entered.wait(1.0))
        recorder.request_stop()
        source.release.set()
        self.assertTrue(recorder.wait_finished(1.0))
        self.assertEqual(source.grabs, 1)
        self.assertEqual(av.containers[0].stream.frames, 0)

    def test_hung_grab_does_not_block_stop_and_finished_stays_honest(self):
        source = _Source(block=True)
        recorder = self._start(source)
        self.assertTrue(source.grab_entered.wait(1.0))
        started = time.monotonic()
        recorder.request_stop()
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertFalse(recorder.wait_finished(0.02))
        source.release.set()
        self.assertTrue(recorder.wait_finished(1.0))

    def test_encode_flush_or_close_error_is_available_and_container_closes(self):
        for options in ({"encode_error": True}, {"flush_error": True},
                        {"close_error": True}):
            with self.subTest(options=options):
                av = _AV(**options)
                recorder = self._start(_Source(), av)
                self.assertTrue(recorder.wait_started())
                recorder.request_stop()
                self.assertTrue(recorder.wait_finished(1.0))
                self.assertIsNotNone(recorder.error)
                self.assertTrue(av.containers[0].closed)

    def test_close_error_finishes_as_error_and_marks_screen_failed(self):
        from fronts.desktop.app import DesktopApp

        recorder = self._start(_Source(), _AV(close_error=True))
        self.assertTrue(recorder.wait_started())
        recorder.request_stop()
        self.assertTrue(recorder.wait_finished(1.0))
        self.assertFalse(recorder.finished_ok)
        self.assertTrue(recorder.finished_error)

        failed, errors = [], []
        session = SimpleNamespace(set_screen_failed=lambda: failed.append(True))
        controller = SimpleNamespace(
            meeting_screen_error=SimpleNamespace(emit=lambda message: errors.append(message)))
        controller._mark_screen_failed = (
            lambda sess, err: DesktopApp._mark_screen_failed(controller, sess, err))
        self.assertFalse(DesktopApp._await_screen_close(controller, session, recorder))
        self.assertEqual(failed, [True])
        self.assertEqual(len(errors), 1)

    def test_capture_failure_is_reported_without_raising_to_session(self):
        errors = []
        source = _Source(fail=True)
        recorder = ScreenRecorder(
            source_factory=lambda: source, av_module=_AV(), on_error=errors.append)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(recorder.start(1, Path(tmp) / "screen.mp4", 12))
            self.assertTrue(recorder.wait_started())
            recorder.request_stop()
            self.assertTrue(recorder.wait_finished(1.0))
        self.assertEqual(len(errors), 1)
        self.assertIn("capture failed", str(errors[0]))
        self.assertTrue(source.closed)

    def test_missing_monitor_falls_back_and_metadata_uses_actual_monitor(self):
        from fronts.desktop.app import DesktopApp
        from whisper_core.meeting.screen_record import ScreenMonitor

        written = []
        cfg = SimpleNamespace(meeting_screen_monitor=2)
        controller = SimpleNamespace(
            cfg=cfg,
            list_meeting_screen_monitors=lambda: [ScreenMonitor(1, 0, 0, 8, 6)],
            set_meeting_screen_monitor=lambda value: setattr(cfg, "meeting_screen_monitor", value),
        )
        monitor = DesktopApp._screen_monitor_for_start(controller)
        session = SimpleNamespace(set_screen_recording=lambda started, mon: written.append(mon))
        DesktopApp._mark_screen_started(controller, session, 1.0, monitor)
        self.assertEqual(monitor, 1)
        self.assertEqual(cfg.meeting_screen_monitor, 1)
        self.assertEqual(written, [1])
    def test_config_screen_keys_roundtrip_and_fps_is_clamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("meeting_screen_fps = 1000\n", encoding="utf-8")
            loaded = Config.load(path)
        self.assertEqual(loaded.meeting_screen_fps, 15)


if __name__ == "__main__":
    unittest.main()
