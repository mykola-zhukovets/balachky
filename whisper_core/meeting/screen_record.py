"""Штатний запис екрана для режиму «Нарада».

Захоплення робить ``mss``, а контейнер WebM/VP9 кодує вже наявний PyAV.
Кодек VP9 (libvpx, ліцензія BSD) обрано замість H.264/libx264 (GPL), щоб
дистрибутив лишався вільним від GPL-компонентів. Свідомо обрано mss, а не dxcam:
для помірних 10–15 fps він достатньо швидкий, працює через чистий Python/ctypes
і не додає DLL поза wheel у frozen-збірку. Це також дає малий, легко підмінний у
unit-тестах контракт джерела кадрів.

Помилки тут ізольовані: callback лише повідомляє GUI, але не керує аудіо-нарадою.
``request_stop`` не чекає; фоновий координатор через ``wait_finished`` дочікується
flush і закриття WebM.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

try:  # Не ламаємо імпорт ядра, коли опційний wheel ще не встановили.
    import av
except Exception:  # pragma: no cover - залежить від інсталяції застосунку
    av = None

try:
    import mss
except Exception:  # pragma: no cover - залежить від інсталяції застосунку
    mss = None


@dataclass(frozen=True)
class ScreenMonitor:
    """Монітор, доступний mss; індексація починається з 1 (0 — усі разом)."""

    index: int
    left: int
    top: int
    width: int
    height: int

    @property
    def label(self) -> str:
        return f"{self.index}: {self.width}×{self.height} ({self.left}, {self.top})"


class _MSSSource:
    """Адаптер mss, щоб сервіс не залежав від конкретного джерела в тестах."""

    def __enter__(self):
        if mss is None:
            raise RuntimeError("mss не завантажився")
        self._capture = mss.mss()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._capture.close()

    def bounds(self, monitor_index: int):
        return self._capture.monitors[monitor_index]

    def grab(self, monitor_index: int):
        return self._capture.grab(self.bounds(monitor_index))


def list_monitors() -> list[ScreenMonitor]:
    """Повернути фізичні монітори mss; індекс 0 («усі») навмисно не показуємо."""
    if mss is None:
        return []
    try:
        with mss.mss() as capture:
            return [
                ScreenMonitor(i, mon["left"], mon["top"], mon["width"], mon["height"])
                for i, mon in enumerate(capture.monitors[1:], start=1)
            ]
    except Exception:
        logging.exception("Не вдалося перелічити монітори для запису наради")
        return []


class ScreenRecorder:
    """Один фоновий запис екрана. Публічний життєвий цикл: start/stop.

    ``source_factory`` та ``av_module`` — лише точки підміни для unit-тестів;
    продуктова інтеграція користується дефолтами. ``on_error`` викликається з
    фонового потоку й має бути thread-safe (контролер передає Qt Signal.emit).
    """

    def __init__(self, *, source_factory=None, av_module=None, on_error=None,
                 on_started=None):
        self._source_factory = source_factory or _MSSSource
        self._av = av if av_module is None else av_module
        self._on_error = on_error or (lambda exc: None)
        self._on_started = on_started or (lambda started_at, monitor: None)
        self._stop = threading.Event()
        self._started = threading.Event()
        self._finished = threading.Event()
        self._finished_ok = threading.Event()
        self._finished_error = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._error = None
        self.started_at: float | None = None
        self.monitor_index: int | None = None
        self.out_path: Path | None = None

    @property
    def error(self):
        return self._error

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def finished_ok(self) -> bool:
        """Потік завершився і WebM-контейнер закрився без помилки."""
        return self._finished_ok.is_set()

    @property
    def finished_error(self) -> bool:
        """Потік завершився, але запис або фіналізація WebM не вдалися."""
        return self._finished_error.is_set()

    def start(self, monitor_index: int, out_path, fps: int = 12) -> bool:
        """Запустити запис у фоні; True означає, що потік прийнято до роботи.

        Значення fps обмежуємо безпечним мінімумом, щоб битий конфіг не створив
        busy-loop. Реальна помилка mss/PyAV не вилітає в аудіо-контролер.
        """
        with self._lock:
            if self.is_running:
                return False
            self._stop.clear()
            self._started.clear()
            self._finished.clear()
            self._finished_ok.clear()
            self._finished_error.clear()
            self._error = None
            self.monitor_index = max(1, int(monitor_index))
            self.out_path = Path(out_path)
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
            self._fps = min(15, max(1, int(fps)))
            self.started_at = None
            self._thread = threading.Thread(
                target=self._run, name="meeting-screen-record", daemon=True)
            self._thread.start()
        return True

    def request_stop(self) -> None:
        """Миттєво заборонити нові кадри; не блокує навіть на завислому grab()."""
        self._stop.set()

    def wait_finished(self, timeout: float | None = None) -> bool:
        """Чи потік завершив фіналізацію; результат див. у finished_ok/error."""
        return self._finished.wait(timeout)

    def stop(self) -> None:
        """Сумісний псевдонім неблокуючого request_stop()."""
        self.request_stop()

    def wait_started(self, timeout: float = 1.0) -> bool:
        """Тестова/діагностична синхронізація: кадр або помилка вже оброблені."""
        return self._started.wait(timeout)

    def _run(self) -> None:
        container = None
        stream = None
        staged_path = None
        published = False
        container_closed = False
        try:
            if self._av is None:
                raise RuntimeError("PyAV не завантажився")
            with self._source_factory() as source:
                bounds = source.bounds(self.monitor_index)
                width, height = int(bounds["width"]), int(bounds["height"])
                # yuv420p потребує парних сторін; зайвий край не пишемо.
                width -= width % 2
                height -= height % 2
                if width <= 0 or height <= 0:
                    raise RuntimeError("монітор має неприпустимі межі")
                fd, staged_name = tempfile.mkstemp(
                    prefix=f".{self.out_path.stem}.",
                    suffix=self.out_path.suffix,
                    dir=str(self.out_path.parent),
                )
                staged_path = Path(staged_name)
                os.close(fd)
                # PyAV визначає контейнер за розширенням stage (screen.*.webm → WebM).
                container = self._av.open(str(staged_path), mode="w")
                # VP9 (libvpx, BSD) замість H.264/libx264 (GPL): дистрибутив без GPL.
                stream = container.add_stream("libvpx-vp9", rate=self._fps)
                stream.width, stream.height = width, height
                stream.pix_fmt = "yuv420p"
                # Реальний час: deadline=realtime, cpu-used=8 → мінімум CPU на нараду.
                stream.options = {"deadline": "realtime", "cpu-used": "8"}
                interval = 1.0 / self._fps
                next_frame = time.monotonic()
                while not self._stop.is_set():
                    captured = source.grab(self.monitor_index)
                    # mss.grab не переривається; кадр, захоплений до stop, викидаємо.
                    if self._stop.is_set():
                        break
                    frame = self._av.VideoFrame.from_ndarray(
                        self._frame_array(captured, width, height),
                        format="bgra").reformat(width=width, height=height, format="yuv420p")
                    if self._stop.is_set():
                        break
                    for packet in stream.encode(frame):
                        if self._stop.is_set():
                            break
                        container.mux(packet)
                    if self._stop.is_set():
                        break
                    if self.started_at is None:
                        self.started_at = time.time()
                        self._started.set()
                        self._on_started(self.started_at, self.monitor_index)
                    next_frame += interval
                    self._stop.wait(max(0.0, next_frame - time.monotonic()))
        except Exception as exc:
            self._report_error(exc)
            self._started.set()
        finally:
            if container is not None:
                try:
                    # Дописати залишок кадрів і закрити WebM — файл лишається програваним.
                    for packet in stream.encode():
                        container.mux(packet)
                except Exception as exc:
                    self._report_error(exc)
                try:
                    container.close()
                    container_closed = True
                except Exception as exc:
                    self._report_error(exc)
            if container_closed and staged_path is not None:
                try:
                    with staged_path.open("r+b") as staged:
                        os.fsync(staged.fileno())
                    os.replace(staged_path, self.out_path)
                    published = True
                except Exception as exc:
                    self._report_error(exc)
            if staged_path is not None and not published:
                try:
                    staged_path.unlink(missing_ok=True)
                except Exception as exc:
                    self._report_error(exc)
            self._started.set()
            if self._error is None:
                self._finished_ok.set()
            else:
                self._finished_error.set()
            self._finished.set()

    def _report_error(self, exc: Exception) -> None:
        """Зберегти першу помилку й не дублювати тост при каскаді flush/close."""
        if self._error is not None:
            return
        self._error = exc
        logging.exception("Запис екрана наради зупинено: %s", exc)
        try:
            self._on_error(exc)
        except Exception:
            logging.exception("Не вдалося повідомити про збій запису екрана")

    @staticmethod
    def _frame_array(frame, width: int, height: int):
        """mss дає BGRA; фейк може повертати ndarray із тим самим форматом."""
        data = np.asarray(frame)
        if data.ndim != 3 or data.shape[2] != 4:
            raise RuntimeError("джерело кадрів повернуло не-BGRA зображення")
        return data[:height, :width]
