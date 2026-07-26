"""feature/double-tap: таймінги подвійного натиску як третій режим PTT.

Логіку тестуємо на реальних методах DesktopApp.on_press / on_release /
_on_double_tap, прив'язаних до легкого стабу (без QApplication) і з фейковим
годинником замість time.time — детермінований контроль вікна подвійного тапу.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fronts.desktop.app import DesktopApp


class _Clock:
    """Фейковий годинник: t контролюємо вручну, advance зсуває час (секунди)."""
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _controller(mode="double_tap", double_tap_ms=400, recording=False):
    """Стаб контролера з полями, яких торкаються on_press/on_release/_on_double_tap.
    started/stopped рахують виклики _start_recording / _stop_and_transcribe."""
    calls = SimpleNamespace(started=0, stopped=0)
    recorder = SimpleNamespace(recording=recording)

    def start():
        calls.started += 1
        recorder.recording = True

    def stop():
        calls.stopped += 1
        recorder.recording = False

    ctl = SimpleNamespace(
        cfg=SimpleNamespace(ptt_mode=mode, double_tap_ms=double_tap_ms),
        recorder=recorder,
        _busy=False,
        _last_tap=0.0,
        _key_down=False,
        _mic_testing=False,
        _cancel_guard=False,
        _capturing=False,
        _meeting_active=False,
        _dictaphone_active=False,   # feature/player-recordings: гейт диктофона (on_press)
        _mic_warned=False,
        _rec_started=0.0,
        _start_recording=start,
        _stop_and_transcribe=stop,
        _calls=calls,
    )
    # прив'язуємо реальний метод до стабу (self=ctl) — тестуємо код, не копію
    ctl._on_double_tap = DesktopApp._on_double_tap.__get__(ctl)
    return ctl


def _press(ctl):
    DesktopApp.on_press(ctl)


def _release(ctl):
    DesktopApp.on_release(ctl)


class DoubleTapStartTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.p = patch("fronts.desktop.app.time.time", self.clock.time)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def _tap(self, ctl):
        """Одне фізичне натискання-відпускання PTT-комбо."""
        _press(ctl)
        _release(ctl)

    def test_double_tap_within_window_starts(self):
        ctl = _controller(double_tap_ms=400)
        self._tap(ctl)
        self.clock.advance(0.3)              # 300 мс < 400 мс
        self._tap(ctl)
        self.assertEqual(ctl._calls.started, 1)
        self.assertTrue(ctl.recorder.recording)

    def test_double_tap_at_window_edge_starts(self):
        ctl = _controller(double_tap_ms=400)
        self._tap(ctl)
        self.clock.advance(0.4)              # рівно 400 мс — включно
        self._tap(ctl)
        self.assertEqual(ctl._calls.started, 1)

    def test_single_tap_does_not_start(self):
        ctl = _controller()
        self._tap(ctl)
        self.assertEqual(ctl._calls.started, 0)
        self.assertFalse(ctl.recorder.recording)

    def test_slow_second_tap_does_not_start(self):
        ctl = _controller(double_tap_ms=400)
        self._tap(ctl)
        self.clock.advance(0.7)              # 700 мс > 400 мс — надто пізно
        self._tap(ctl)
        self.assertEqual(ctl._calls.started, 0)

    def test_slow_pair_becomes_new_first_tap(self):
        # два надто повільні тапи, потім швидкий третій → пара 2+3 стартує
        ctl = _controller(double_tap_ms=400)
        self._tap(ctl)
        self.clock.advance(0.7)
        self._tap(ctl)                       # став новим «першим»
        self.clock.advance(0.2)
        self._tap(ctl)                       # пара в межах вікна
        self.assertEqual(ctl._calls.started, 1)

    def test_window_clamped_low(self):
        # double_tap_ms=50 клампиться до 200 мс → 150 мс усе ще стартує
        ctl = _controller(double_tap_ms=50)
        self._tap(ctl)
        self.clock.advance(0.15)
        self._tap(ctl)
        self.assertEqual(ctl._calls.started, 1)

    def test_window_clamped_high(self):
        # double_tap_ms=5000 клампиться до 600 мс → 800 мс НЕ стартує
        ctl = _controller(double_tap_ms=5000)
        self._tap(ctl)
        self.clock.advance(0.8)
        self._tap(ctl)
        self.assertEqual(ctl._calls.started, 0)

    def test_autorepeat_press_is_not_a_tap(self):
        # OS-автоповтор: press без проміжного release не рахується новим тапом
        ctl = _controller(double_tap_ms=400)
        _press(ctl)                          # тап 1 — клавіша лишається затиснута
        self.clock.advance(0.05)
        _press(ctl)                          # авто-повтор (was_down=True) — ігнор
        _press(ctl)
        self.assertEqual(ctl._calls.started, 0)
        _release(ctl)


class DoubleTapStopTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.p = patch("fronts.desktop.app.time.time", self.clock.time)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def _tap(self, ctl):
        _press(ctl)
        _release(ctl)

    def test_single_tap_while_recording_stops(self):
        ctl = _controller(recording=True)
        self._tap(ctl)
        self.assertEqual(ctl._calls.stopped, 1)
        self.assertFalse(ctl.recorder.recording)

    def test_start_then_stop_cycle(self):
        ctl = _controller(double_tap_ms=400)
        self._tap(ctl)
        self.clock.advance(0.2)
        self._tap(ctl)                       # подвійний → старт
        self.assertTrue(ctl.recorder.recording)
        self.clock.advance(0.2)
        self._tap(ctl)                       # одинарний → стоп
        self.assertEqual(ctl._calls.started, 1)
        self.assertEqual(ctl._calls.stopped, 1)
        self.assertFalse(ctl.recorder.recording)

    def test_release_does_not_stop(self):
        # у double_tap відпускання клавіші саме по собі не зупиняє запис
        ctl = _controller(recording=True)
        _release(ctl)
        self.assertEqual(ctl._calls.stopped, 0)
        self.assertTrue(ctl.recorder.recording)

    def test_busy_ignores_taps(self):
        ctl = _controller(double_tap_ms=400)
        ctl._busy = True
        self._tap(ctl)
        self.clock.advance(0.2)
        self._tap(ctl)
        self.assertEqual(ctl._calls.started, 0)


class DoubleTapGateTests(unittest.TestCase):
    """Гейти, спільні з hold/toggle, не мають ламатися у double_tap."""
    def setUp(self):
        self.clock = _Clock()
        self.p = patch("fronts.desktop.app.time.time", self.clock.time)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def _tap(self, ctl):
        _press(ctl)
        _release(ctl)

    def test_capturing_gate_blocks(self):
        ctl = _controller(double_tap_ms=400)
        ctl._capturing = True
        self._tap(ctl)
        self.clock.advance(0.2)
        self._tap(ctl)
        self.assertEqual(ctl._calls.started, 0)

    def test_meeting_gate_blocks(self):
        ctl = _controller(double_tap_ms=400)
        ctl._meeting_active = True
        self._tap(ctl)
        self.clock.advance(0.2)
        self._tap(ctl)
        self.assertEqual(ctl._calls.started, 0)


if __name__ == "__main__":
    unittest.main()
