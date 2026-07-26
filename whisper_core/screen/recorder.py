"""Незалежний OBS-lite запис монітора, вікна чи області у фоновому потоці."""
from __future__ import annotations
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
import numpy as np
try:
    import av
except Exception:  # pragma: no cover
    av = None
try:
    import mss
except Exception:  # pragma: no cover
    mss = None
from .win32 import print_window, window_rect

@dataclass(frozen=True)
class ScreenRecordOptions:
    fps: int = 30
    resolution: str = "native"
    format: str = "webm"
    quality: str = "medium"
    system_audio: bool = False

def available_formats(av_module=None) -> list[str]:
    """Контейнери, доступні для запису екрана. Єдиний кодек — VP9 (libvpx,
    ліцензія BSD); GPL-кодек H.264/libx264 і контейнер MP4 прибрано з
    ліцензійних міркувань, лишається WebM (єдиний надійний VP9-контейнер у PyAV)."""
    return ["webm"]

class ScreenRecorder:
    """Власний lifecycle: start(source, opts, out_path), stop, finished_ok/error."""
    def __init__(self, *, av_module=None, mss_factory=None, window_grabber=None,
                 on_error=None, on_started=None, on_finished=None):
        self._av = av if av_module is None else av_module
        self._mss_factory = mss_factory or (mss.mss if mss else None)
        self._window_grabber = window_grabber or print_window
        self._on_error = on_error or (lambda _e: None)
        self._on_started = on_started or (lambda _t: None)
        self._on_finished = on_finished or (lambda _p, _ok: None)
        self._stop = threading.Event(); self._finished = threading.Event()
        self._finished_ok = threading.Event(); self._finished_error = threading.Event()
        self._started = threading.Event(); self._thread = None; self._error = None
        self._lock = threading.Lock(); self.out_path = self.started_at = None
    @property
    def is_running(self): return self._thread is not None and self._thread.is_alive()
    @property
    def error(self): return self._error
    @property
    def finished_ok(self): return self._finished_ok.is_set()
    @property
    def finished_error(self): return self._finished_error.is_set()
    def start(self, source: dict, opts: ScreenRecordOptions | dict, out_path) -> bool:
        with self._lock:
            if self.is_running: return False
            self.source = dict(source)
            raw = opts if isinstance(opts, ScreenRecordOptions) else ScreenRecordOptions(**opts)
            self.opts = ScreenRecordOptions(min(60, max(5, int(raw.fps))), raw.resolution,
                raw.format if raw.format in available_formats(self._av) else "webm", raw.quality, raw.system_audio)
            self.out_path = Path(out_path); self.out_path.parent.mkdir(parents=True, exist_ok=True)
            self._stop.clear(); self._finished.clear(); self._started.clear(); self._finished_ok.clear(); self._finished_error.clear(); self._error = None
            self._thread = threading.Thread(target=self._run, name="screen-studio-record", daemon=True)
            self._thread.start()
        return True
    def request_stop(self): self._stop.set()
    stop = request_stop
    def wait_finished(self, timeout=None): return self._finished.wait(timeout)
    def wait_started(self, timeout=1.0): return self._started.wait(timeout)
    def _bounds(self, capture):
        kind = self.source.get("kind", "monitor")
        if kind == "monitor": return dict(capture.monitors[max(1, int(self.source.get("index", 1)))])
        if kind == "rect":
            left, top, width, height = self.source["rect"]
            return {"left": left, "top": top, "width": width, "height": height}
        if kind == "window":
            left, top, width, height = window_rect(int(self.source["hwnd"]))
            return {"left": left, "top": top, "width": width, "height": height}
        raise RuntimeError("Невідоме джерело запису екрана")
    def _grab(self, capture, bounds):
        if self.source.get("kind") == "window":
            frame = self._window_grabber(int(self.source["hwnd"]))
            if frame is not None:
                if not np.any(frame[:, :, :3]):
                    raise RuntimeError("Вікно повернуло чорний кадр: захищений DRM-вміст не можна записати")
                return frame
        return np.asarray(capture.grab(bounds))
    @staticmethod
    def _size(width, height, resolution):
        limit = {"1080p": 1080, "720p": 720}.get(resolution)
        if limit and height > limit: width, height = round(width * limit / height), limit
        return max(2, width - width % 2), max(2, height - height % 2)
    def _run(self):
        container = stream = capture = None
        try:
            if self._av is None or self._mss_factory is None: raise RuntimeError("mss або PyAV не завантажився")
            capture = self._mss_factory(); bounds = self._bounds(capture)
            width, height = self._size(int(bounds["width"]), int(bounds["height"]), self.opts.resolution)
            container = self._av.open(str(self.out_path), mode="w", format=self.opts.format)
            stream = container.add_stream("libvpx-vp9", rate=self.opts.fps); stream.width, stream.height = width, height; stream.pix_fmt = "yuv420p"
            # VP9 у реальному часі: deadline=realtime + cpu-used=8 (найшвидше
            # кодування), crf — якість (менше = краще), b=0 → режим постійної
            # якості. libx264-preset прибрано: VP9 його не знає.
            stream.options = {"deadline": "realtime", "cpu-used": "8", "b": "0",
                "crf": {"low": "40", "medium": "32", "high": "24"}.get(self.opts.quality, "32")}
            interval, next_frame = 1 / self.opts.fps, time.monotonic()
            while not self._stop.is_set():
                image = self._grab(capture, bounds)
                if self._stop.is_set(): break
                frame = self._av.VideoFrame.from_ndarray(image[:height, :width], format="bgra").reformat(width=width, height=height, format="yuv420p")
                for packet in stream.encode(frame): container.mux(packet)
                if not self.started_at:
                    self.started_at = time.time(); self._started.set(); self._on_started(self.started_at)
                next_frame += interval; self._stop.wait(max(0, next_frame - time.monotonic()))
        except Exception as exc:
            self._report_error(exc)
        finally:
            if container is not None:
                try:
                    for packet in stream.encode(): container.mux(packet)
                except Exception as exc: self._report_error(exc)
                try: container.close()
                except Exception as exc: self._report_error(exc)
            if capture is not None:
                try: capture.close()
                except Exception: pass
            self._started.set(); (self._finished_ok if self._error is None else self._finished_error).set(); self._finished.set()
            try: self._on_finished(self.out_path, self._error is None)
            except Exception: logging.exception("Не вдалося повідомити про завершення запису екрана")
    def _report_error(self, exc):
        if self._error is not None: return
        self._error = exc; logging.exception("Незалежний запис екрана зупинено: %s", exc)
        try: self._on_error(exc)
        except Exception: logging.exception("Не вдалося повідомити про збій запису екрана")
