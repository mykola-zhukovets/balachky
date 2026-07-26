"""Потокобезпечний lifecycle важких локальних моделей.

Qt тут навмисно немає: GUI лише періодично викликає ``check_idle()``, а
транскрипційні потоки заходять через ``activity()``/``ensure_loaded()``.
"""
from __future__ import annotations

from contextlib import contextmanager
import threading
import time


LOADED = "loaded"
UNLOADED = "unloaded"
LOADING = "loading"


class ModelLifecycle:
    """Серіалізує unload/reload і не допускає unload під час активної роботи.

    ``timeout_seconds == 0`` означає “Ніколи”. ``unload`` повертає False, якщо
    остання атомарна перевірка виявила роботу, що щойно почалася.
    """

    def __init__(self, *, timeout_seconds, unload, load, is_busy=None,
                 clock=time.monotonic):
        self._timeout = max(0, int(timeout_seconds or 0))
        self._unload = unload
        self._load = load
        self._is_busy = is_busy or (lambda: False)
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._state = LOADED
        self._last_activity = self._clock()
        self._active = 0

    @property
    def state(self) -> str:
        with self._condition:
            return self._state

    @property
    def timeout_seconds(self) -> int:
        with self._condition:
            return self._timeout

    def set_timeout(self, seconds) -> None:
        with self._condition:
            self._timeout = max(0, int(seconds or 0))
            self._last_activity = self._clock()

    def touch(self) -> None:
        """Позначити користувацьку/модельну активність і почати відлік заново."""
        with self._condition:
            self._last_activity = self._clock()

    def check_idle(self) -> bool:
        """Вивантажити після таймауту. Повертає True лише за фактом unload."""
        with self._condition:
            now = self._clock()
            if self._timeout <= 0 or self._state != LOADED:
                return False
            if self._active or self._is_busy():
                self._last_activity = now
                return False
            if now - self._last_activity < self._timeout:
                return False

            # Callback виконується під lifecycle-lock. GUI запускає check_idle у
            # daemon-потоці, тож це не морозить UI; натомість перший хоткей не
            # може почати reload, доки попередня модель фізично не відпущена.
            try:
                unloaded = self._unload()
            except Exception:
                # Unload міг уже частково відпустити ресурси (engine=None), а
                # потім впасти на завершенні sidecar. Лишити стан LOADED було б
                # фатально: наступний ensure_loaded миттєво повернув би без
                # reload → transcribe по None. Симетрично до ensure_loaded
                # позначаємо UNLOADED, щоб наступна робота гарантовано reload-нула.
                self._state = UNLOADED
                self._condition.notify_all()
                raise
            if unloaded is False:
                self._last_activity = self._clock()
                return False
            self._state = UNLOADED
            self._condition.notify_all()
            return True

    def force_unload(self) -> bool:
        """Негайно вивантажити для витіснення координатором RAM (Sol-умова TTS):
        як check_idle, але БЕЗ залежності від таймауту — важлива саме миттєвість.

        НЕ вивантажує під час активної роботи (запис/файл) — повертає False, і
        координатор відхиляє TTS-запит чесним тостом (мікрофон/активна робота не
        вбивається мовчки). Реактивація STT — лише через ensure_loaded (він
        поставить LOADING→LOADED). Симетрично до check_idle позначаємо UNLOADED
        навіть за винятку unload, інакше наступний ensure_loaded рано повернув би
        на LOADED і працював би по None."""
        with self._condition:
            if self._state != LOADED:
                return True                  # уже не резидентна → безпечно витісняти
            if self._active or self._is_busy():
                return False                 # активна робота → НЕ вивантажуємо (race-safe)
            try:
                unloaded = self._unload()
            except Exception:
                self._state = UNLOADED
                self._condition.notify_all()
                raise
            if unloaded is False:
                self._last_activity = self._clock()
                return False
            self._state = UNLOADED
            self._condition.notify_all()
            return True

    def ensure_loaded(self) -> bool:
        """Дочекатися/виконати lazy-load. True — модель завантажив цей виклик."""
        with self._condition:
            while self._state == LOADING:
                self._condition.wait()
            if self._state == LOADED:
                self._last_activity = self._clock()
                return False
            self._state = LOADING

        try:
            self._load()
        except Exception:
            with self._condition:
                self._state = UNLOADED
                self._condition.notify_all()
            raise

        with self._condition:
            self._state = LOADED
            self._last_activity = self._clock()
            self._condition.notify_all()
        return True

    @contextmanager
    def activity(self, *, load=True):
        """Lease для роботи: заборонити unload і, типово, зробити lazy-load."""
        with self._condition:
            self._active += 1
            self._last_activity = self._clock()
        try:
            if load:
                self.ensure_loaded()
            yield
        finally:
            with self._condition:
                self._active = max(0, self._active - 1)
                self._last_activity = self._clock()
                self._condition.notify_all()
