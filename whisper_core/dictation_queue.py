"""Черга голосових диктувань (feature/dictation-queue, запит Миколи №10).

Раніше нове диктування блокувалось прапорцем ``_busy`` до кінця розшифровки
попередньої фрази. Тут — однопотокова FIFO-черга: щойно користувач відпустив
клавішу, він може диктувати наступну фразу, а попередні розшифровуються й
вставляються ПО ЧЕРЗІ, у порядку запису, у фоні.

Дизайн (навмисно простий — карпатіанські правила):
- один споживач (один потік) обробляє джоби строго по одному → без гонок і без
  накладання розшифровок; порядок запису = порядок вставки;
- аудіо тримається ЛИШЕ в памʼяті (``chunks``), на диск нічого не пишемо → немає
  нового сховища чутливого аудіо (зауваження Sol);
- ліміт черги — за кількістю І за сумарною тривалістю очікуючих фраз (щоб довгий
  «хвіст» не роздувся);
- скасування скидає всі фрази, що ЩЕ чекають (активна доробляється чесно).

Модуль чистий (без Qt/WinAPI): контролер передає callable ``process(job)`` для
фактичної розшифровки+вставки і callback ``on_state(pending, active)`` для
індикації (пілюля «у черзі: N»). Це робить логіку черги окремо тестованою.
"""
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class DictationJob:
    """Одна фраза в черзі. ``paste_target`` — (HWND, PID, заголовок) вікна на
    момент СВОГО старту (кожна фраза вставляється у свою ціль). ``nav_target`` —
    HWND для голосової навігації (у режимі черги зазвичай не задіяний)."""
    seq: int
    chunks: Any
    profile: Any
    terms: Any
    paste_target: Optional[tuple] = None
    nav_target: Optional[int] = None
    duration_s: float = 0.0
    # feature/processing-slider: знімок політики обробки на старті фрази (спека §5).
    processing_policy: Any = None
    state: str = "queued"      # queued | transcribing | done | error | cancelled


class DictationQueue:
    """Однопотокова FIFO-черга диктувань зі споживачем у фоні.

    ``process(job)`` — виконує розшифровку+вставку однієї фрази (контролер).
    ``on_state(pending, active)`` — pending = скільки фраз ЩЕ чекає (без активної),
    active = чи обробляється зараз якась фраза. Викликається з потоку черги, тож
    контролер має лише маршалити в GUI (Qt-сигнал), не чіпати віджети напряму.
    """

    def __init__(self, process: Callable[[DictationJob], None], *,
                 max_items: int = 4, max_seconds: float = 90.0,
                 on_state: Optional[Callable[[int, bool], None]] = None):
        self._process = process
        self._max_items = max(1, int(max_items))
        self._max_seconds = float(max_seconds)
        self._on_state = on_state
        self._q: "deque[DictationJob]" = deque()
        self._cv = threading.Condition()
        self._active: Optional[DictationJob] = None
        self._waiting_seconds = 0.0
        self._seq = 0
        self._stop = False
        self._worker: Optional[threading.Thread] = None

    # --- стан (під захистом cv) ---
    def _count_locked(self) -> int:
        return len(self._q) + (1 if self._active is not None else 0)

    def _is_full_locked(self) -> bool:
        return (self._count_locked() >= self._max_items
                or self._waiting_seconds >= self._max_seconds)

    def is_full(self) -> bool:
        """Чи повна черга (за кількістю або сумарною тривалістю очікуючих)."""
        with self._cv:
            return self._is_full_locked()

    def pending(self) -> int:
        """Скільки фраз ЩЕ чекає (не рахуючи ту, що обробляється зараз)."""
        with self._cv:
            return len(self._q)

    def busy(self) -> bool:
        """Чи є в системі хоч одна фраза (активна або в очікуванні)."""
        with self._cv:
            return self._active is not None or bool(self._q)

    # --- операції ---
    def enqueue(self, chunks, profile, terms, *, paste_target=None,
                nav_target=None, duration_s: float = 0.0,
                policy=None) -> Optional[DictationJob]:
        """Додати фразу в кінець черги. → джоб, або None якщо черга повна."""
        with self._cv:
            if self._is_full_locked():
                return None
            self._seq += 1
            job = DictationJob(
                seq=self._seq, chunks=chunks, profile=profile, terms=terms,
                paste_target=paste_target, nav_target=nav_target,
                duration_s=max(0.0, float(duration_s)), processing_policy=policy)
            self._q.append(job)
            self._waiting_seconds += job.duration_s
            self._cv.notify()
        self._ensure_worker()
        self._emit_state()
        return job

    def cancel_pending(self) -> int:
        """Скасувати всі фрази, що ЩЕ чекають (активна доробляється). → скільки."""
        with self._cv:
            n = len(self._q)
            for job in self._q:
                job.state = "cancelled"
            self._q.clear()
            self._waiting_seconds = 0.0
        if n:
            self._emit_state()
        return n

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Дочекатись, поки черга спорожніє (для тестів/охайного завершення)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while self._q or self._active is not None:
                if deadline is None:
                    self._cv.wait()
                else:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return False
                    self._cv.wait(left)
            return True

    def shutdown(self):
        """Спинити потік-споживача (лишає активну фразу доробитись)."""
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    # --- споживач ---
    def _ensure_worker(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._run, name="dictation-queue", daemon=True)
            self._worker.start()

    def _run(self):
        while True:
            with self._cv:
                while not self._q and not self._stop:
                    self._cv.wait()
                if self._stop and not self._q:
                    return
                job = self._q.popleft()
                self._waiting_seconds = max(0.0, self._waiting_seconds - job.duration_s)
                self._active = job
                job.state = "transcribing"
            self._emit_state()
            try:
                self._process(job)
                if job.state == "transcribing":
                    job.state = "done"
            except Exception:
                job.state = "error"      # не даємо впасти потоку-споживачу
            finally:
                with self._cv:
                    self._active = None
                    self._cv.notify_all()
                self._emit_state()

    def _emit_state(self):
        if self._on_state is None:
            return
        with self._cv:
            pending = len(self._q)
            active = self._active is not None
        try:
            self._on_state(pending, active)
        except Exception:
            pass
