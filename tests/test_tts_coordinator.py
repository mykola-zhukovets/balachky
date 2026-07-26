"""Хвиля 1: HeavyModelCoordinator + ModelLifecycle.force_unload (§3.3, §11.2).

RAM-переходи на профілі 8 ГБ (мок virtual_memory), lock-order без дедлоку,
force_unload через lifecycle-стан (не обхід), TTS-grace при idle=0."""
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.heavy_models import STT, TTS, HeavyModelCoordinator
from whisper_core.model_lifecycle import (LOADED, UNLOADED, ModelLifecycle)

_8GB = 8 * (1024 ** 3)
_16GB = 16 * (1024 ** 3)


class TestForceUnload(unittest.TestCase):
    def _lc(self, busy=False):
        self.unloaded = []
        return ModelLifecycle(
            timeout_seconds=0,               # «ніколи» — force_unload не залежить від таймауту
            unload=lambda: self.unloaded.append(True) or True,
            load=lambda: None, is_busy=lambda: busy)

    def test_force_unload_changes_state(self):
        lc = self._lc()
        self.assertTrue(lc.force_unload())
        self.assertEqual(lc.state, UNLOADED)
        self.assertEqual(len(self.unloaded), 1)

    def test_force_unload_refused_when_busy(self):
        lc = self._lc(busy=True)
        self.assertFalse(lc.force_unload())
        self.assertEqual(lc.state, LOADED)      # активну роботу не вбиваємо

    def test_reactivation_via_ensure_loaded(self):
        loaded = []
        lc = ModelLifecycle(timeout_seconds=0, unload=lambda: True,
                            load=lambda: loaded.append(True), is_busy=lambda: False)
        lc.force_unload()
        self.assertEqual(lc.state, UNLOADED)
        self.assertTrue(lc.ensure_loaded())      # реактивація через ensure_loaded
        self.assertEqual(lc.state, LOADED)
        self.assertEqual(len(loaded), 1)


class TestCoordinator8GB(unittest.TestCase):
    def _coord(self, *, stt_busy=False, total=_8GB):
        self.log = []
        return HeavyModelCoordinator(
            total_ram_provider=lambda: total,
            stt_force_unload=lambda: self.log.append("stt_unload") or True,
            stt_is_busy=lambda: stt_busy,
            tts_cancel=lambda: self.log.append("tts_cancel"),
            tts_shutdown=lambda: self.log.append("tts_shutdown"))

    def test_tts_rejected_during_recording(self):
        # STT зайнятий (запис) → TTS відхилено, STT НЕ вивантажено
        c = self._coord(stt_busy=True)
        self.assertIsNone(c.acquire_tts())
        self.assertNotIn("stt_unload", self.log)

    def test_tts_evicts_idle_stt(self):
        # STT вільний → force_unload STT, TTS-lease виданий
        c = self._coord(stt_busy=False)
        lease = c.acquire_tts()
        self.assertIsNotNone(lease)
        self.assertEqual(lease.kind, TTS)
        self.assertIn("stt_unload", self.log)

    def test_coresidence_allowed_on_big_machine(self):
        # ≥ 12 ГБ → STT не витісняємо, TTS поруч
        c = self._coord(stt_busy=False, total=_16GB)
        lease = c.acquire_tts()
        self.assertIsNotNone(lease)
        self.assertNotIn("stt_unload", self.log)

    def test_yield_to_microphone_order(self):
        # мікрофон перемагає: cancel + shutdown TTS ПЕРЕД поверненням (до STT load)
        c = self._coord()
        c.acquire(TTS, active=True)
        c.yield_to_microphone()
        self.assertIn("tts_cancel", self.log)
        self.assertIn("tts_shutdown", self.log)
        self.assertLess(self.log.index("tts_cancel"), self.log.index("tts_shutdown"))

    def test_sherpa_fallback_unloads_tts_before_stt(self):
        # TTS завершено й звільнено ПЕРЕД STT (строгий порядок §4.5)
        c = self._coord()
        c.acquire(TTS, active=False)
        c.yield_tts_for_stt()
        self.assertIn("tts_shutdown", self.log)
        # lease звільнено
        self.assertIsNone(c._leases.get(TTS))

    def test_grace_shutdown_when_idle_zero(self):
        # STT-idle-таймер=0 не вивантажив би TTS → окремий grace все одно завершує
        c = self._coord()
        c.acquire(TTS, active=False)
        c.tts_grace_shutdown()
        self.assertIn("tts_shutdown", self.log)
        self.assertIsNone(c._leases.get(TTS))

    def test_race_force_unload_false_no_lease(self):
        # CRITICAL §3.3: force_unload повернув False (STT не вивантажився) → lease НЕ
        # видається (інакше STT+TTS резидентні всупереч інваріанту). Мутація
        # (ігнорувати результат) червонить.
        self.log = []
        c = HeavyModelCoordinator(
            total_ram_provider=lambda: _8GB,
            stt_force_unload=lambda: False,      # не вдалося вивантажити STT
            stt_is_busy=lambda: False)
        self.assertIsNone(c.acquire_tts())       # відмова — STT лишився резидентним
        self.assertFalse(c.preempt_for(TTS))

    def test_ram_fail_closed_without_psutil(self):
        # без інжектованого провайдера, коли неможливо зміряти RAM (psutil
        # відсутній АБО є, але вимір падає) → fail-CLOSED (ексклюзивно), не
        # open. Перевіряємо саму ПОВЕДІНКУ коду при неможливості виміру, а не
        # залізо тестового раннера — підміняємо джерело числа (модуль psutil
        # у sys.modules), а не сподіваємось на конкретний обсяг RAM. Прогнано
        # й з підміненою "малою" машиною (без psutil), і з "великою"
        # (psutil є, справжні 64 ГБ, але сам вимір падає) — в обох випадках
        # результат однаковий, бо залежить лише від того, чи вдався вимір.
        import sys
        from unittest import mock

        import whisper_core.heavy_models as hm

        class _BrokenVirtualMemory:
            def __call__(self):
                raise RuntimeError("вимір недоступний")

        broken_psutil_big_machine = mock.MagicMock()
        broken_psutil_big_machine.virtual_memory = _BrokenVirtualMemory()

        cases = [
            ("psutil не встановлено (мала машина)", None),
            ("psutil є, вимір падає (велика машина)", broken_psutil_big_machine),
        ]
        for label, fake_psutil_module in cases:
            with self.subTest(label):
                with mock.patch.dict(sys.modules, {"psutil": fake_psutil_module}):
                    total = hm._default_total_ram()
                    self.assertLess(total, hm._DEFAULT_RESIDENT_THRESHOLD, label)
                    c = HeavyModelCoordinator()          # дефолтний провайдер
                    self.assertFalse(c.machine_allows_coresidence(), label)

    def test_two_threads_real_locks_no_deadlock(self):
        # двопотоковий тест з РЕАЛЬНИМИ locks: preempt_for → справжній
        # ModelLifecycle.force_unload (його _condition lock) навперейми з
        # yield_to_microphone → справжній tts_sidecar registry (idle_transition).
        from whisper_core.model_lifecycle import ModelLifecycle
        from whisper_core.tts import sidecar as tts_sidecar
        lc = ModelLifecycle(timeout_seconds=0, unload=lambda: True,
                            load=lambda: None, is_busy=lambda: False)

        def real_shutdown():
            with tts_sidecar.idle_transition():  # справжній registry lock
                pass

        c = HeavyModelCoordinator(
            total_ram_provider=lambda: _8GB,
            stt_force_unload=lc.force_unload, stt_is_busy=lambda: False,
            tts_shutdown=real_shutdown)
        c.acquire(TTS, active=False)
        errors = []

        def w(fn):
            try:
                for _ in range(300):
                    fn()
                    lc.ensure_loaded()           # повертаємо LOADED, щоб force_unload працював
            except Exception as e:               # noqa: BLE001
                errors.append(e)

        t1 = threading.Thread(target=w, args=(lambda: c.preempt_for(TTS),))
        t2 = threading.Thread(target=w, args=(c.yield_to_microphone,))
        t1.start(); t2.start()
        t1.join(timeout=20); t2.join(timeout=20)
        self.assertFalse(t1.is_alive(), "preempt завис (дедлок реальних locks?)")
        self.assertFalse(t2.is_alive(), "yield завис (дедлок?)")
        self.assertEqual(errors, [])

    def test_lock_order_no_deadlock(self):
        # два потоки навперейми: preempt_for і yield_to_microphone — без дедлоку
        c = self._coord()
        errors = []

        def worker(fn):
            try:
                for _ in range(200):
                    fn()
            except Exception as e:               # noqa: BLE001
                errors.append(e)

        c.acquire(TTS, active=False)
        t1 = threading.Thread(target=worker, args=(lambda: c.preempt_for(TTS),))
        t2 = threading.Thread(target=worker, args=(c.yield_to_microphone,))
        t1.start(); t2.start()
        t1.join(timeout=15); t2.join(timeout=15)
        self.assertFalse(t1.is_alive(), "preempt_for завис (дедлок?)")
        self.assertFalse(t2.is_alive(), "yield завис (дедлок?)")
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
