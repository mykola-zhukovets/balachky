"""Контракти незалежного запису екрана без реального дисплея/PyAV."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch
import numpy as np

from whisper_core.config import Config
from whisper_core.screen.recorder import ScreenRecorder, ScreenRecordOptions, available_formats

class _Frame:
    def reformat(self, **_kwargs): return self
class _VideoFrame:
    @staticmethod
    def from_ndarray(_data, format):
        assert format == "bgra"; return _Frame()
class _Stream:
    def __init__(self): self.frames=0; self.rate=None
    def encode(self, frame=None):
        if frame is not None: self.frames += 1
        return []
class _Container:
    def __init__(self): self.closed=False; self.stream=_Stream()
    def add_stream(self, codec, rate): self.codec=codec; self.rate=rate; self.stream.rate=rate; return self.stream
    def mux(self, _packet): pass
    def close(self): self.closed=True
class _AV:
    VideoFrame=_VideoFrame
    codecs_available={"libx264", "libvpx-vp9"}
    def __init__(self): self.containers=[]
    def open(self, _path, mode, format=None):
        self.mode=mode; self.format=format; c=_Container(); self.containers.append(c); return c
class _MSS:
    def __init__(self, fail=False): self.monitors=[{}, {"left":0,"top":0,"width":8,"height":6}]; self.fail=fail; self.closed=False; self.entered=threading.Event()
    def grab(self, _bounds):
        self.entered.set()
        if self.fail: raise RuntimeError("grab failed")
        return np.ones((6,8,4), dtype=np.uint8)
    def close(self): self.closed=True

class ScreenStudioTests(unittest.TestCase):
    def test_start_stop_monitor_flushes_video_and_container(self):
        av, source = _AV(), _MSS()
        with tempfile.TemporaryDirectory() as tmp:
            rec=ScreenRecorder(av_module=av,mss_factory=lambda:source)
            self.assertTrue(rec.start({"kind":"monitor","index":1}, ScreenRecordOptions(fps=60, format="mkv"), Path(tmp)/"a.mkv"))
            self.assertTrue(rec.wait_started()); rec.stop(); self.assertTrue(rec.wait_finished(1))
        self.assertTrue(rec.finished_ok); self.assertTrue(source.closed); self.assertTrue(av.containers[0].closed); self.assertEqual(av.containers[0].stream.rate,60)
    def test_window_uses_printwindow_before_mss_fallback(self):
        av, source = _AV(), _MSS()
        with patch("whisper_core.screen.recorder.window_rect", return_value=(0,0,8,6)):
            rec=ScreenRecorder(av_module=av,mss_factory=lambda:source,window_grabber=lambda _hwnd:np.ones((6,8,4),dtype=np.uint8))
            with tempfile.TemporaryDirectory() as tmp:
                rec.start({"kind":"window","hwnd":123}, {"fps":30}, Path(tmp)/"a.mp4"); self.assertTrue(rec.wait_started()); rec.stop(); rec.wait_finished(1)
        self.assertTrue(rec.finished_ok)
    def test_capture_error_is_isolated_and_finalizes(self):
        source=_MSS(fail=True); rec=ScreenRecorder(av_module=_AV(),mss_factory=lambda:source)
        with tempfile.TemporaryDirectory() as tmp:
            rec.start({"kind":"monitor","index":1},{},Path(tmp)/"a.mp4"); self.assertTrue(rec.wait_finished(1))
        self.assertTrue(rec.finished_error); self.assertIsNotNone(rec.error); self.assertTrue(source.closed)
    def test_config_roundtrip_and_webm_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"config.toml"; cfg=Config(screen_record_fps=99,screen_record_format="mkv",screen_recordings_dir="C:/video"); cfg.save(path); loaded=Config.load(path)
        self.assertEqual(loaded.screen_record_fps,60); self.assertEqual(loaded.screen_record_format,"mkv"); self.assertEqual(loaded.screen_recordings_dir,"C:/video")
        # H.264/libx264 (GPL) і контейнер MP4 прибрано; лишається WebM/VP9 (BSD) —
        # єдиний надійний VP9-контейнер (у PyAV немає муксера з іменем 'mkv').
        self.assertEqual(available_formats(_AV()), ["webm"])

class DesktopAppScreenRecordStartTests(unittest.TestCase):
    """Аудит 'тихі відмови' №2 (fronts/desktop/pages/screen.py:265-267): коли
    DesktopApp.screen_record_start повертає False, кнопка на сторінці більше
    не має мовчати — контролер зобов'язаний надіслати screen_record_error
    ПЕРЕД поверненням False, інакше UI нема на що реагувати."""

    def _fake_self(self, tmp):
        emitted_errors = []
        emitted_states = []
        fake = SimpleNamespace(
            _screen_recorder=None,
            _screen_recordings_root=lambda: Path(tmp),
            screen_record_error=SimpleNamespace(emit=emitted_errors.append),
            screen_record_state=SimpleNamespace(emit=emitted_states.append),
        )
        return fake, emitted_errors, emitted_states

    def test_already_recording_emits_error_before_returning_false(self):
        from fronts.desktop.app import DesktopApp
        with tempfile.TemporaryDirectory() as tmp:
            fake, errors, states = self._fake_self(tmp)
            fake._screen_recorder = SimpleNamespace(is_running=True)
            result = DesktopApp.screen_record_start(fake, {"kind": "monitor", "index": 1}, {})
        self.assertFalse(result)
        self.assertEqual(len(errors), 1, "має бути видима причина відмови, а не тиша")
        self.assertEqual(states, [])

    def test_engine_rejected_start_emits_error_before_returning_false(self):
        from fronts.desktop.app import DesktopApp
        rejecting_recorder = SimpleNamespace(start=lambda *a, **k: False)
        with tempfile.TemporaryDirectory() as tmp:
            fake, errors, states = self._fake_self(tmp)
            with mock.patch("whisper_core.screen.recorder.ScreenRecorder",
                            return_value=rejecting_recorder):
                result = DesktopApp.screen_record_start(
                    fake, {"kind": "monitor", "index": 1}, {"format": "webm"})
        self.assertFalse(result)
        self.assertEqual(len(errors), 1, "рушій відхилив старт — людина мусить дізнатись чому")
        self.assertEqual(states, [])

    def test_successful_start_emits_recording_state_and_no_error(self):
        from fronts.desktop.app import DesktopApp
        accepting_recorder = SimpleNamespace(start=lambda *a, **k: True)
        with tempfile.TemporaryDirectory() as tmp:
            fake, errors, states = self._fake_self(tmp)
            with mock.patch("whisper_core.screen.recorder.ScreenRecorder",
                            return_value=accepting_recorder):
                result = DesktopApp.screen_record_start(
                    fake, {"kind": "monitor", "index": 1}, {"format": "webm"})
        self.assertTrue(result)
        self.assertEqual(errors, [])
        self.assertEqual(states, ["recording"])


if __name__ == "__main__": unittest.main()

class WindowEnumerationTests(unittest.TestCase):
    def test_non_windows_or_missing_ctypes_returns_a_list(self):
        from whisper_core.screen import win32
        with patch.object(win32, "_user32", return_value=None):
            self.assertEqual(win32.list_windows(), [])
