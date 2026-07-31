"""feature/dictation-queue (запит Миколи №10): логіка черги диктувань.

Тестуємо чистий модуль whisper_core.dictation_queue напряму (без Qt/WinAPI):
порядок обробки (FIFO), відсутність накладання (один споживач), порожня черга,
ліміти (кількість і сумарна тривалість) та скасування очікуючих фраз.

Активний джоб «тримаємо» через Event — так стан черги детермінований, поки ми
перевіряємо ліміти/скасування (без sleep-гадання). Тести — unittest.TestCase,
щоб їх підхопив `unittest discover` QA-гейта.
"""
import threading
import time
import unittest
from types import SimpleNamespace

from whisper_core.dictation_queue import DictationQueue
from fronts.desktop.app import (
    _pipeline_busy, _dictation_start_blocked, _should_queue)


class DictationQueueTest(unittest.TestCase):
    def test_empty_queue(self):
        """Свіжа черга: нічого не чекає, не зайнята, не повна, wait_idle миттєвий."""
        q = DictationQueue(lambda job: None, max_items=4, max_seconds=90.0)
        try:
            self.assertEqual(q.pending(), 0)
            self.assertFalse(q.busy())
            self.assertFalse(q.is_full())
            self.assertTrue(q.wait_idle(1.0))
        finally:
            q.shutdown()

    def test_fifo_order_and_no_overlap(self):
        """Фрази обробляються строго по порядку запису і НЕ накладаються."""
        order = []
        state = {"cur": 0, "max": 0}
        lock = threading.Lock()

        def proc(job):
            with lock:
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
            time.sleep(0.01)             # вікно, у яке впіймали б накладання
            with lock:
                order.append(job.seq)
                state["cur"] -= 1

        q = DictationQueue(proc, max_items=10, max_seconds=1000.0)
        try:
            for _ in range(5):
                self.assertIsNotNone(q.enqueue("chunks", None, None, duration_s=1.0))
            self.assertTrue(q.wait_idle(5.0))
            self.assertEqual(order, [1, 2, 3, 4, 5])   # вхід == обробка
            self.assertEqual(state["max"], 1)          # жодного накладання
        finally:
            q.shutdown()

    def test_limit_by_count(self):
        """Ліміт за кількістю: active+waiting == max_items → нові відхиляються."""
        started, release = threading.Event(), threading.Event()

        def proc(job):
            started.set()
            release.wait(5.0)

        q = DictationQueue(proc, max_items=3, max_seconds=1000.0)
        try:
            self.assertIsNotNone(q.enqueue("a", None, None, duration_s=1.0))
            self.assertTrue(started.wait(2.0))          # job1 active, тримається
            self.assertIsNotNone(q.enqueue("b", None, None, duration_s=1.0))   # waiting
            self.assertIsNotNone(q.enqueue("c", None, None, duration_s=1.0))   # → всього 3
            self.assertTrue(q.is_full())
            self.assertIsNone(q.enqueue("d", None, None, duration_s=1.0))      # повна → відмова
            self.assertEqual(q.pending(), 2)
            release.set()
            self.assertTrue(q.wait_idle(5.0))
        finally:
            release.set()
            q.shutdown()

    def test_limit_by_total_seconds(self):
        """Ліміт за сумарною тривалістю очікуючих: понад max_seconds → відмова."""
        started, release = threading.Event(), threading.Event()

        def proc(job):
            started.set()
            release.wait(5.0)

        q = DictationQueue(proc, max_items=100, max_seconds=50.0)
        try:
            self.assertIsNotNone(q.enqueue("a", None, None, duration_s=25.0))
            self.assertTrue(started.wait(2.0))          # job1 active → не в очікуванні
            self.assertIsNotNone(q.enqueue("b", None, None, duration_s=20.0))
            self.assertIsNotNone(q.enqueue("c", None, None, duration_s=35.0))
            with q._cv:
                waiting = [
                    (job.chunks, job.duration_s) for job in q._q]
                waiting_seconds = q._waiting_seconds
            self.assertEqual(waiting, [("b", 20.0), ("c", 35.0)])
            self.assertEqual(waiting_seconds, 55.0)
            self.assertEqual(
                waiting_seconds, sum(duration for _, duration in waiting))
            self.assertTrue(q.is_full())
            self.assertIsNone(q.enqueue("d", None, None, duration_s=30.0))     # понад ліміт
            release.set()
            self.assertTrue(q.wait_idle(5.0))
        finally:
            release.set()
            q.shutdown()

    def test_cancel_pending_keeps_active(self):
        """Скасування прибирає лише те, що ЩЕ чекає; активна доробляється чесно."""
        started, release = threading.Event(), threading.Event()

        def proc(job):
            started.set()
            release.wait(5.0)

        q = DictationQueue(proc, max_items=10, max_seconds=1000.0)
        try:
            j1 = q.enqueue("a", None, None, duration_s=1.0)
            self.assertTrue(started.wait(2.0))          # j1 active/тримається
            j2 = q.enqueue("b", None, None, duration_s=1.0)
            j3 = q.enqueue("c", None, None, duration_s=1.0)
            self.assertEqual(q.pending(), 2)

            self.assertEqual(q.cancel_pending(), 2)     # скасували обидві очікуючі
            self.assertEqual(q.pending(), 0)
            self.assertEqual(j2.state, "cancelled")
            self.assertEqual(j3.state, "cancelled")

            release.set()
            self.assertTrue(q.wait_idle(5.0))
            self.assertEqual(j1.state, "done")          # активна доробилась
        finally:
            release.set()
            q.shutdown()

    def test_on_state_reports_backlog(self):
        """on_state повідомляє (скільки ще чекає, чи йде обробка) — для пілюлі «+N»."""
        started, release = threading.Event(), threading.Event()
        states = []
        slock = threading.Lock()

        def proc(job):
            started.set()
            release.wait(5.0)

        def on_state(pending, active):
            with slock:
                states.append((pending, active))

        q = DictationQueue(proc, max_items=10, max_seconds=1000.0, on_state=on_state)
        try:
            q.enqueue("a", None, None, duration_s=1.0)
            self.assertTrue(started.wait(2.0))
            q.enqueue("b", None, None, duration_s=1.0)   # один чекає за активним
            deadline = time.monotonic() + 2.0
            seen = False
            while time.monotonic() < deadline:
                with slock:
                    seen = any(p >= 1 and a for (p, a) in states)
                if seen:
                    break
                time.sleep(0.01)
            self.assertTrue(seen)
            release.set()
            self.assertTrue(q.wait_idle(5.0))
        finally:
            release.set()
            q.shutdown()


class _FakeQueue:
    """Легкий сурогат черги для гейтів: лише busy()/is_full()."""
    def __init__(self, busy=False, full=False):
        self._busy = busy
        self._full = full

    def busy(self):
        return self._busy

    def is_full(self):
        return self._full


class QueueGatesTest(unittest.TestCase):
    """Інтеграційні гейти черги (модульні функції з fronts.desktop.app) на легких
    getattr-захищених стабах контролера — без Qt/WinAPI. Суд відзначив, що прямих
    тестів на гейти бракувало; тут фіксуємо їхній контракт."""

    @staticmethod
    def _app(*, busy=False, recording=False, queue=None, queue_enabled=True,
             paste_preview=False, formfill=False, voice_nav=False):
        return SimpleNamespace(
            _busy=busy,
            recorder=SimpleNamespace(recording=recording),
            _queue=queue,
            formfill_capturing=formfill,
            cfg=SimpleNamespace(
                dictation_queue_enabled=queue_enabled,
                paste_preview=paste_preview,
                voice_nav_enabled=voice_nav),
        )

    # --- _pipeline_busy: нарада/нотатка/диктофон/тест-мік ждуть, поки триває будь-яка
    #     розшифровка (спільні мікрофон і модель) ------------------------------
    def test_pipeline_idle_when_nothing_runs(self):
        self.assertFalse(_pipeline_busy(self._app(queue=_FakeQueue(busy=False))))

    def test_pipeline_busy_when_legacy_phrase_in_flight(self):
        # стара блокувальна фраза (_busy) блокує навіть без черги
        self.assertTrue(_pipeline_busy(self._app(busy=True, queue=None)))

    def test_pipeline_busy_when_queue_busy(self):
        self.assertTrue(_pipeline_busy(self._app(queue=_FakeQueue(busy=True))))

    # --- _dictation_start_blocked: старт НОВОГО запису -------------------------
    def test_start_blocked_while_recording(self):
        self.assertTrue(_dictation_start_blocked(self._app(recording=True)))

    def test_start_blocked_while_legacy_busy(self):
        self.assertTrue(_dictation_start_blocked(self._app(busy=True)))

    def test_start_blocked_when_queue_full(self):
        self.assertTrue(
            _dictation_start_blocked(self._app(queue=_FakeQueue(full=True))))

    def test_start_not_blocked_by_background_transcription(self):
        # КЛЮЧОВЕ: фонова розшифровка (черга зайнята, але НЕ повна й запис не йде)
        # старт нового запису НЕ блокує — заради цього черга й існує
        self.assertFalse(_dictation_start_blocked(
            self._app(recording=False, busy=False,
                      queue=_FakeQueue(busy=True, full=False))))

    # --- _should_queue: чи ставити нову фразу в чергу -------------------------
    def test_should_queue_for_plain_dictation(self):
        self.assertTrue(_should_queue(self._app(queue=_FakeQueue())))

    def test_should_not_queue_without_queue(self):
        self.assertFalse(_should_queue(self._app(queue=None)))

    def test_should_not_queue_when_disabled(self):
        self.assertFalse(_should_queue(
            self._app(queue=_FakeQueue(), queue_enabled=False)))

    def test_should_not_queue_in_preview(self):
        self.assertFalse(_should_queue(
            self._app(queue=_FakeQueue(), paste_preview=True)))

    def test_should_not_queue_in_formfill(self):
        self.assertFalse(_should_queue(
            self._app(queue=_FakeQueue(), formfill=True)))

    def test_should_not_queue_in_voice_nav(self):
        self.assertFalse(_should_queue(
            self._app(queue=_FakeQueue(), voice_nav=True)))


if __name__ == "__main__":
    unittest.main()
