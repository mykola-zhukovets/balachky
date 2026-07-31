"""Арбітр RAM для важких локальних моделей (§3.3, закриває блокер 2).

Проблема: STT вантажиться на старті; перший TTS-запит додав би ~2.5 ГБ ПОВЕРХ Whisper
(+Gemma/діаризація) — на 8 ГБ це paging/OOM. Дописати `or` у `_models_busy()` НЕ
вирішує — потрібен явний арбітр, що видає ексклюзивні leases й задає порядок витіснення.

Ключові правила:
  • Ексклюзивність за бюджетом RAM: STT і TTS НЕ резидентні разом на машині < 12 ГБ
    (детект total RAM; на потужній — дозволяємо co-residence).
  • Витіснення STT — ЧЕРЕЗ lifecycle-стан (force_unload), НЕ в обхід (інакше наступний
    ensure_loaded рано поверне на LOADED і транскрибуватиме по None). Реактивація —
    лише ensure_loaded.
  • Активну чужу операцію НЕ вбивати мовчки: STT зайнятий (йде запис) → TTS-запит
    ВІДХИЛЯЄТЬСЯ чесно (не чекає з утриманням locks).
  • Мікрофон завжди перемагає: yield_to_microphone() скасовує synth, глушить playback,
    завершує TTS-sidecar і звільняє TTS-lease ПЕРЕД завантаженням STT.
  • Lock-order coordinator → registry → engine. preempt/yield НЕ тримають
    coordinator_lock під час важкого unload (звільняють після позначення наміру, як
    ModelLifecycle.check_idle) — щоб не було дедлоку з наявним _unload_idle_models.

БЕЗ Qt. Grace-scheduler (окремий QTimer при idle=0) живе в app.py і кличе сюди
tts_shutdown; координатор дає механізм, GUI — таймер."""
from __future__ import annotations

import threading

STT = "STT"
TTS = "TTS"
GEMMA = "Gemma"
DIARIZATION = "diarization"

_DEFAULT_RESIDENT_THRESHOLD = 12 * (1024 ** 3)   # < 12 ГБ → не тримати STT+TTS разом


def _default_total_ram() -> int:
    """Загальна RAM машини. psutil опційний → якщо недоступний, FAIL-CLOSED: віддаємо
    значення НИЖЧЕ порога, тобто вмикаємо ексклюзивний режим (STT/TTS не резидентні
    разом) — на невідомому/8 ГБ залізі захист має спрацьовувати, а не мовчати
    (рецензія: fail-closed, не fail-open). Тести інжектять свій провайдер."""
    try:
        import psutil
        return int(psutil.virtual_memory().total)
    except Exception:                            # noqa: BLE001
        return _DEFAULT_RESIDENT_THRESHOLD - 1   # нижче порога → ексклюзивно (fail-closed)


class Lease:
    """Ексклюзивний дозвіл тримати важку модель резидентно. `active` — чи саме зараз
    іде робота (активний lease не витісняється preempt-ом)."""

    def __init__(self, kind: str):
        self.kind = kind
        self.active = False
        self.released = False


class HeavyModelCoordinator:
    def __init__(self, *, total_ram_provider=None,
                 resident_threshold_bytes: int = _DEFAULT_RESIDENT_THRESHOLD,
                 stt_force_unload=None, stt_is_busy=None,
                 tts_cancel=None, tts_shutdown=None):
        self._lock = threading.RLock()
        self._leases = {}                        # kind → Lease
        self._total_ram = total_ram_provider or _default_total_ram
        self._threshold = resident_threshold_bytes
        # інтеграція зі STT (ModelLifecycle) і TTS (sidecar) — через колбеки, щоб
        # координатор лишався БЕЗ Qt і без прямих залежностей.
        self._stt_force_unload = stt_force_unload or (lambda: True)
        self._stt_is_busy = stt_is_busy or (lambda: False)
        self._tts_cancel = tts_cancel or (lambda: None)
        self._tts_shutdown = tts_shutdown or (lambda: None)

    def machine_allows_coresidence(self) -> bool:
        """True — RAM дозволяє тримати STT і TTS разом (≥ поріг)."""
        return self._total_ram() >= self._threshold

    # --- leases ---
    def acquire(self, kind: str, *, active: bool = False) -> "Lease | None":
        with self._lock:
            existing = self._leases.get(kind)
            if existing is not None and not existing.released:
                existing.active = existing.active or active
                return existing
            lease = Lease(kind)
            lease.active = active
            self._leases[kind] = lease
            return lease

    def release(self, lease) -> None:
        if lease is None:
            return
        with self._lock:
            lease.released = True
            if self._leases.get(lease.kind) is lease:
                del self._leases[lease.kind]

    def set_active(self, kind: str, active: bool) -> None:
        with self._lock:
            lease = self._leases.get(kind)
            if lease is not None:
                lease.active = active

    # --- витіснення під TTS ---
    def preempt_for(self, kind: str) -> bool:
        """Звільнити місце під `kind` (Хвиля 1 — лише "TTS"). True — можна вантажити
        `kind`; False — відхилено (активна чужа операція).

        Co-residence дозволена (RAM ≥ поріг) → нічого не витісняємо. Інакше STT
        зайнятий → відхилити; STT вільний → force_unload ПОЗА coordinator_lock
        (lock-order coordinator→registry→engine)."""
        if self.machine_allows_coresidence():
            return True
        # fast-path перевірка зайнятості під coordinator_lock; важкий check-and-unload —
        # ПОЗА ним. Гонку закриває САМ force_unload: він атомарно (під lifecycle-lock)
        # ще раз звіряє busy й повертає False, якщо STT зайнятий/лишився LOADED. Тому
        # ПЕРЕВІРЯЄМО його результат — інакше lease видався б попри невивантажений STT
        # (рецензія: ігнорування False → race).
        with self._lock:
            if self._stt_is_busy():
                return False                     # активний STT не вбиваємо мовчки
        try:
            unloaded = self._stt_force_unload()  # True=не резидентний; False=зайнятий/LOADED
        except Exception:                        # noqa: BLE001
            return False
        return bool(unloaded)                    # lease лише коли STT ГАРАНТОВАНО не резидентний

    def acquire_tts(self, *, active: bool = True) -> "Lease | None":
        """Витіснити STT за потреби й узяти TTS-lease. None — відхилено (STT зайнятий)."""
        if not self.preempt_for(TTS):
            return None
        return self.acquire(TTS, active=active)

    # --- мікрофон завжди перемагає ---
    def yield_to_microphone(self) -> None:
        """Викликається СИНХРОННО у before_microphone_start ДО завантаження STT:
        скасувати synth, завершити TTS-sidecar, звільнити TTS-lease. Порядок —
        TTS геть, ПОТІМ мікрофон (гарантується синхронністю)."""
        self._tts_cancel()
        self._tts_shutdown()
        with self._lock:
            lease = self._leases.get(TTS)
        self.release(lease)

    # --- sherpa-фолбек караоке (§4.5): TTS завершити й вивантажити ПЕРЕД STT ---
    def yield_tts_for_stt(self) -> None:
        """Строгий порядок: спершу завершити TTS-WAV (виклик робиться ПІСЛЯ синтезу),
        звільнити TTS-lease (unload TTS), лише ПОТІМ caller бере STT. Не тримати обидва."""
        self._tts_shutdown()
        with self._lock:
            lease = self._leases.get(TTS)
        self.release(lease)

    def tts_grace_shutdown(self) -> None:
        """Викликається grace-таймером GUI (§3.3): завершити TTS-sidecar незалежно від
        STT-idle-таймера. Так «TTS не тримається постійно» не обходиться нулем STT."""
        self._tts_shutdown()
        with self._lock:
            lease = self._leases.get(TTS)
        self.release(lease)
