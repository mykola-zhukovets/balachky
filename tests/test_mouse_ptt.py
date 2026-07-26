"""feature/mouse-ptt — тести бічної кнопки миші як кнопки запису.

Три шари:
- чиста частина: розбір mouseData → кнопка; маршрутизація down/up → press/release
  (жодного WinAPI, лише логіка);
- проводка в застосунку: _apply_mouse_ptt піднімає/зупиняє хук за конфігом і
  скидає налаштування в none, коли хук не встав (MouseHook замокано);
- живий тест: SetWindowsHookExW реально повертає хендл, а stop() коректно знімає
  хук — БЕЗ реальних кліків (кнопки в агента нема).
"""
import sys
import re
import tempfile
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from whisper_core.config import Config
from fronts.desktop import mousehook as mh
from fronts.desktop.mousehook import (
    button_from_mouse_data, route_event, MouseHook,
    WM_XBUTTONDOWN, WM_XBUTTONUP, XBUTTON1, XBUTTON2,
)


class ParseMouseDataTests(unittest.TestCase):
    """High-word поля mouseData → канонічне ім'я бічної кнопки."""

    def test_xbutton1(self):
        self.assertEqual(button_from_mouse_data(XBUTTON1 << 16), "x1")

    def test_xbutton2(self):
        self.assertEqual(button_from_mouse_data(XBUTTON2 << 16), "x2")

    def test_low_word_ignored(self):
        # low-word (координати/прапорці) не має впливати на розбір кнопки
        self.assertEqual(button_from_mouse_data((XBUTTON2 << 16) | 0xFFFF), "x2")

    def test_ordinary_button_is_none(self):
        # звичайні кнопки (ліва/права/середня) не несуть X-біта у high-word
        self.assertIsNone(button_from_mouse_data(0))


class RouteEventTests(unittest.TestCase):
    """Чиста маршрутизація: збіг кнопки → press/release; інакше — нічого."""

    def setUp(self):
        self.calls = []
        self.press = lambda: self.calls.append("press")
        self.release = lambda: self.calls.append("release")

    def test_down_matching_calls_press(self):
        handled = route_event(WM_XBUTTONDOWN, "x1", "x1", self.press, self.release)
        self.assertTrue(handled)
        self.assertEqual(self.calls, ["press"])

    def test_up_matching_calls_release(self):
        handled = route_event(WM_XBUTTONUP, "x1", "x1", self.press, self.release)
        self.assertTrue(handled)
        self.assertEqual(self.calls, ["release"])

    def test_other_button_ignored(self):
        # затиснута x2, а слухаємо x1 → нічого
        handled = route_event(WM_XBUTTONDOWN, "x2", "x1", self.press, self.release)
        self.assertFalse(handled)
        self.assertEqual(self.calls, [])

    def test_none_button_ignored(self):
        handled = route_event(WM_XBUTTONDOWN, None, "x1", self.press, self.release)
        self.assertFalse(handled)
        self.assertEqual(self.calls, [])

    def test_full_hold_cycle(self):
        route_event(WM_XBUTTONDOWN, "x2", "x2", self.press, self.release)
        route_event(WM_XBUTTONUP, "x2", "x2", self.press, self.release)
        self.assertEqual(self.calls, ["press", "release"])


class ConfigTests(unittest.TestCase):
    def test_defaults_none(self):
        self.assertEqual(Config().ptt_mouse_button, "none")

    def test_roundtrip(self):
        c = Config()
        c.ptt_mouse_button = "x2"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.toml")
            c.save(p)
            loaded = Config.load(p)
        self.assertEqual(loaded.ptt_mouse_button, "x2")


class _FakeHook:
    """Замінник MouseHook: фіксує аргументи конструктора й виклики start/stop."""
    start_result = True
    constructed = 0

    def __init__(self, button, on_press, on_release):
        self.button = button
        self.on_press = on_press
        self.on_release = on_release
        self.started = False
        self.stopped = False
        _FakeHook.constructed += 1

    def start(self):
        self.started = True
        return self.start_result

    def stop(self):
        self.stopped = True


def _controller(button, existing_hook=None):
    """Мінімальний контролер для DesktopApp._apply_mouse_ptt (без Qt/рушія)."""
    hotkey = SimpleNamespace(
        pressed=SimpleNamespace(emit=lambda: None),
        released=SimpleNamespace(emit=lambda: None))
    saves = []
    notified = []
    cfg = SimpleNamespace(ptt_mouse_button=button)
    cfg.save = lambda: saves.append(cfg.ptt_mouse_button)
    tray = SimpleNamespace(notify=lambda msg: notified.append(msg))
    return SimpleNamespace(mouse_hook=existing_hook, cfg=cfg, hotkey=hotkey,
                           tray=tray, _saves=saves, _notified=notified)


class ApplyMousePttTests(unittest.TestCase):
    """Проводка: _apply_mouse_ptt піднімає хук на ті самі сигнали hotkey,
    перезапускає при зміні кнопки й акуратно скидає в none при збої."""

    def setUp(self):
        _FakeHook.start_result = True
        _FakeHook.constructed = 0

    def test_enabled_starts_hook_on_hotkey_signals(self):
        from fronts.desktop.app import DesktopApp
        ctl = _controller("x1")
        with patch.object(mh, "MouseHook", _FakeHook):
            DesktopApp._apply_mouse_ptt(ctl)
        hook = ctl.mouse_hook
        self.assertIsInstance(hook, _FakeHook)
        self.assertTrue(hook.started)
        self.assertEqual(hook.button, "x1")
        # кнопка викликає ТІ САМІ сигнали, що й клавіша (успадкування hold/toggle)
        self.assertIs(hook.on_press, ctl.hotkey.pressed.emit)
        self.assertIs(hook.on_release, ctl.hotkey.released.emit)

    def test_none_stops_existing_and_starts_nothing(self):
        from fronts.desktop.app import DesktopApp
        old = _FakeHook("x1", None, None)
        _FakeHook.constructed = 0                  # не рахувати «старий» хук вище
        ctl = _controller("none", existing_hook=old)
        with patch.object(mh, "MouseHook", _FakeHook):
            DesktopApp._apply_mouse_ptt(ctl)
        self.assertTrue(old.stopped)
        self.assertIsNone(ctl.mouse_hook)
        self.assertEqual(_FakeHook.constructed, 0)  # новий хук не створювався

    def test_change_button_restarts_hook(self):
        from fronts.desktop.app import DesktopApp
        old = _FakeHook("x1", None, None)
        ctl = _controller("x2", existing_hook=old)
        with patch.object(mh, "MouseHook", _FakeHook):
            DesktopApp._apply_mouse_ptt(ctl)
        self.assertTrue(old.stopped)              # старий знято
        self.assertIsInstance(ctl.mouse_hook, _FakeHook)
        self.assertEqual(ctl.mouse_hook.button, "x2")

    def test_failed_hook_resets_to_none(self):
        from fronts.desktop.app import DesktopApp
        _FakeHook.start_result = False
        ctl = _controller("x2")
        with patch.object(mh, "MouseHook", _FakeHook):
            DesktopApp._apply_mouse_ptt(ctl)
        self.assertIsNone(ctl.mouse_hook)          # ввімкнене, що не діє, не лишаємо
        self.assertEqual(ctl.cfg.ptt_mouse_button, "none")
        self.assertIn("none", ctl._saves)          # скид збережено
        self.assertEqual(len(ctl._notified), 1)    # користувача попереджено тостом


class LiveHookTests(unittest.TestCase):
    """Реальне встановлення/зняття WH_MOUSE_LL — без жодних кліків."""

    def test_install_and_unhook(self):
        if sys.platform != "win32":
            self.skipTest("WH_MOUSE_LL лише на Windows")
        hook = MouseHook("x1", on_press=lambda: None, on_release=lambda: None)
        if not hook.start():
            self.skipTest("SetWindowsHookExW недоступний у цьому сеансі "
                          "(немає інтерактивного десктопу)")
        try:
            self.assertIsNotNone(hook._hook)   # SetWindowsHookExW повернув хендл
            self.assertTrue(hook._ok)
        finally:
            hook.stop()
        self.assertIsNone(hook._thread)        # потік хука коректно завершився
        self.assertFalse(hook._ok)


class LastErrorDiagnosticTests(unittest.TestCase):
    """Регресія: при збої WinAPI діагностика логує РЕАЛЬНИЙ код Win32-помилки,
    а не завжди 0. Вимагає, щоб user32 бралося як WinDLL(use_last_error=True);
    з пласким ctypes.windll.user32 ctypes.get_last_error() лишається 0."""

    def test_stop_logs_real_error_on_failed_post(self):
        if sys.platform != "win32":
            self.skipTest("WinAPI лише на Windows")
        hook = MouseHook("x1", on_press=lambda: None, on_release=lambda: None)
        # хук не піднімали: підставляємо фейковий потік і завідомо НЕІСНУЮЧИЙ tid
        # (непарний, не кратний 4 → не буває реальним thread id), тож
        # PostThreadMessageW гарантовано провалиться і виставить LastError.
        hook._thread = SimpleNamespace(join=lambda _t: None)
        hook._thread_id = 0x7FFFFFFF
        with self.assertLogs(level="WARNING") as cm:
            hook.stop()
        msg = "\n".join(cm.output)
        self.assertIn("PostThreadMessage", msg)
        m = re.search(r"err=(\d+)", msg)
        self.assertIsNotNone(m, f"немає коду помилки у лозі: {msg!r}")
        # ключова перевірка: код НЕнульовий (реальний Win32 error, не заглушка 0)
        self.assertNotEqual(int(m.group(1)), 0,
                            f"очікували реальний Win32-код, отримали err=0: {msg!r}")


if __name__ == "__main__":
    unittest.main()
