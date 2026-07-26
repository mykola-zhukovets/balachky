"""Неблокувальна жива «розшифровка» для потоків 16 кГц float32.

Це тільки прев'ю: після stop повна «розшифровка» лишається джерелом істини.
Callback кладе готові блоки до обмеженого кільцевого буфера; VAD, склеювання
та виклики моделі виконуються єдиним worker-потоком.
"""
from __future__ import annotations

from collections import deque
import logging
import threading
from dataclasses import dataclass
from typing import Callable

import numpy as np


@dataclass(frozen=True)
class LiveSegment:
    start: float
    end: float
    text: str
    is_final: bool


class RMSVAD:
    def __init__(self, threshold: float = 0.012):
        self.threshold = float(threshold)

    def is_speech(self, audio: np.ndarray) -> bool:
        return bool(len(audio) and np.sqrt(np.mean(audio * audio)) >= self.threshold)


@dataclass
class _Job:
    audio: np.ndarray
    start: float
    end: float
    is_final: bool
    segment_id: int


@dataclass
class _Input:
    """Один поданий callback-блок та його позиція у capture-потоці."""
    audio: np.ndarray
    start: int
    gap_before: bool = False


class LiveTranscriber:
    """VAD-сегменти в одному worker; callback ніколи не чекає worker або модель.

    Job, який уже виконується всередині ``engine.transcribe()``, скасувати не
    можна. Це прийнятне обмеження preview: примусові сегменти обмежено
    ``max_segment_s`` (типово 60 с), тому фінальний прохід чекає не довше за
    одну таку розшифровку.
    """
    RUNNING, STOPPED, ERROR = "running", "stopped", "error"

    def __init__(self, engine, *, terms=None, engine_lock=None, vad=None,
                 sample_rate=16000, pause_ms=700, partial_interval_s=4.0,
                 min_segment_s=0.25, max_pending_segments=2,
                 max_pending_blocks=128, max_segment_s=60.0,
                 on_segment: Callable[[LiveSegment], None] | None = None,
                 on_error: Callable[[Exception], None] | None = None):
        self.engine, self.terms = engine, terms
        self.engine_lock = engine_lock or threading.Lock()
        self.vad = vad or RMSVAD()
        self.sample_rate = int(sample_rate)
        self.pause_samples = max(1, int(pause_ms * self.sample_rate / 1000))
        self.partial_samples = max(1, int(partial_interval_s * self.sample_rate))
        self.min_samples = max(1, int(min_segment_s * self.sample_rate))
        self.max_segment_samples = max(self.min_samples, int(max_segment_s * self.sample_rate))
        self.max_pending_segments = max(1, int(max_pending_segments))
        self.max_pending_blocks = max(1, int(max_pending_blocks))
        self.on_segment, self.on_error = on_segment, on_error
        self.state = self.RUNNING
        self._stop_requested = threading.Event()
        self._lock = threading.Condition()
        self._inputs, self._jobs = deque(), deque()
        self._dropped_blocks = 0
        # Callback рахує кожен поданий блок, навіть коли той доводиться
        # відкинути. Так timestamps не стискаються, поки worker зайнятий.
        self._input_cursor = 0
        self._pending_gap = False
        self._processing = False
        # Ці поля змінює лише worker. Вони не доступні callback-потоку.
        self._frames = []
        self._segment_start = self._cursor = self._silence = 0
        self._next_partial_at, self._id_seq, self._active_id = self.partial_samples, 0, None
        self._worker = threading.Thread(target=self._run, name="live-transcriber", daemon=True)
        self._worker.start()

    @property
    def pending_segments(self):
        with self._lock:
            return len(self._jobs)

    @property
    def dropped_blocks(self):
        with self._lock:
            return self._dropped_blocks

    @property
    def buffered_blocks(self):
        with self._lock:
            return len(self._inputs)

    def feed(self, chunk):
        """Callback: копіює один блок і неблокувально кладе його у ring buffer."""
        audio = np.asarray(chunk, dtype=np.float32).reshape(-1).copy()
        if not len(audio):
            return
        start = self._input_cursor
        self._input_cursor += len(audio)
        if not self._lock.acquire(blocking=False):
            # GIL робить інкремент безпечним; worker цього лічильника не змінює.
            self._dropped_blocks += 1
            self._pending_gap = True
            return
        try:
            if self.state != self.RUNNING:
                return
            gap_before, self._pending_gap = self._pending_gap, False
            if len(self._inputs) >= self.max_pending_blocks:
                self._inputs.popleft()
                self._dropped_blocks += 1
                # Розрив починається перед найстарішим уцілілим блоком. За
                # місткості 1 ним буде саме блок, який додаємо нижче.
                if self._inputs:
                    self._inputs[0].gap_before = True
                else:
                    gap_before = True
            self._inputs.append(_Input(audio, start, gap_before))
            self._lock.notify()
        finally:
            self._lock.release()

    def stop(self, *, wait=False, timeout=None):
        """Скасувати невиконане прев'ю, щоб фінальний прохід отримав модель першим."""
        # Event ставимо до condition-lock: worker, що чекає engine, бачить stop одразу.
        self._stop_requested.set()
        with self._lock:
            if self.state != self.RUNNING:
                return
            self.state = self.STOPPED
            # Preview після stop не потрібне: фінальна транскрипція є джерелом істини.
            self._inputs.clear()
            self._jobs.clear()
            self._lock.notify_all()
        if wait:
            self._worker.join(timeout)

    def _consume(self, item: _Input):
        """Worker-only: VAD, сегментація і формування bounded live jobs."""
        audio = item.audio
        if item.gap_before:
            # Втрачене аудіо не можна склеювати з наступною фразою. Закриваємо
            # попередній сегмент і продовжуємо з capture-позиції, а не з числа
            # блоків, які встиг обробити worker.
            self._close_current()
            self._cursor = item.start
        try:
            speech = bool(self.vad.is_speech(audio))
        except Exception:
            logging.exception("Live VAD упав; використовую RMS")
            speech = RMSVAD().is_speech(audio)

        start = self._cursor
        self._cursor += len(audio)
        if not self._frames and speech:
            self._id_seq += 1
            self._active_id, self._segment_start = self._id_seq, start
            self._next_partial_at = self.partial_samples
        if not self._frames and not speech:
            return
        self._frames.append(audio)
        self._silence = 0 if speech else self._silence + len(audio)
        length = sum(len(frame) for frame in self._frames)
        if self._silence >= self.pause_samples or length >= self.max_segment_samples:
            self._close_current()
        elif speech and length >= self._next_partial_at:
            self._enqueue(_Job(np.concatenate(self._frames),
                               self._segment_start / self.sample_rate,
                               self._cursor / self.sample_rate, False,
                               self._active_id or 0))
            self._next_partial_at = length + self.partial_samples

    def _close_current(self):
        if not self._frames:
            return
        audio, segment_id = np.concatenate(self._frames), self._active_id or 0
        start, end = self._segment_start / self.sample_rate, self._cursor / self.sample_rate
        self._frames, self._silence, self._active_id = [], 0, None
        if len(audio) >= self.min_samples:
            self._enqueue(_Job(audio, start, end, True, segment_id))

    def _enqueue(self, job):
        """Worker-only producer; condition lock лише для видимості job-черги."""
        with self._lock:
            if self.state != self.RUNNING:
                return
            if not job.is_final and (self._jobs or self._processing):
                return
            if job.is_final:
                self._jobs = deque(item for item in self._jobs if item.is_final)
            self._jobs.append(job)
            while len(self._jobs) > self.max_pending_segments:
                a, b = self._jobs.popleft(), self._jobs.popleft()
                self._jobs.appendleft(_Job(np.concatenate((a.audio, b.audio)),
                                           a.start, b.end, True, b.segment_id))
            self._lock.notify()

    def _take_job_or_input(self):
        with self._lock:
            while self.state == self.RUNNING and not self._jobs and not self._inputs:
                self._lock.wait()
            if self.state != self.RUNNING:
                return None, None
            if self._jobs:
                self._processing = True
                return self._jobs.popleft(), None
            return None, self._inputs.popleft()

    def _run(self):
        while True:
            job, item = self._take_job_or_input()
            if job is None and item is None:
                return
            if item is not None:
                self._consume(item)
                continue
            try:
                # Не стоїмо безумовно на lock: stop між спробами означає, що
                # фінальна транскрипція забере engine без зайвого live job.
                while not self._stop_requested.is_set():
                    # Не блокуємося в acquire(): stop може скасувати live до того,
                    # як він отримає engine, залишивши lock фінальному проходу.
                    if self.engine_lock.acquire(blocking=False):
                        break
                    self._stop_requested.wait(0.05)
                else:
                    return
                try:
                    if self._stop_requested.is_set():
                        continue
                    # feature/model-bottlenecks (під-хвиля 2): живе прев'ю СВІДОМО
                    # без word_timestamps. Текст тут транзитний — кожен частковий
                    # шматок перерозпізнається на льоту й замінюється фінальним;
                    # DTW-прохід на кожному проміжному фрагменті був би витратою без
                    # користі (прев'ю показує сирий рядок, не картку з підсвіткою).
                    result = (self.engine.transcribe(job.audio, self.terms)
                              if self.terms is not None else self.engine.transcribe(job.audio))
                finally:
                    self.engine_lock.release()
                text = result[1] if isinstance(result, tuple) and len(result) > 1 else result
                with self._lock:
                    stale = not job.is_final and (self.state != self.RUNNING
                                                   or job.segment_id != self._active_id)
                if str(text or "").strip() and not stale and self.on_segment:
                    self.on_segment(LiveSegment(job.start, job.end, str(text).strip(), job.is_final))
            except Exception as exc:
                logging.exception("Жива розшифровка вимкнена після помилки")
                with self._lock:
                    self.state = self.ERROR
                    self._stop_requested.set()
                    self._inputs.clear()
                    self._jobs.clear()
                    self._lock.notify_all()
                if self.on_error:
                    self.on_error(exc)
                return
            finally:
                with self._lock:
                    self._processing = False
                    self._lock.notify_all()
