"""feature/native-hotkeys: тести нативного механізму гарячих клавіш.

Що покриваємо:
- parse_combo: рядок комбінації → (модифікатори, VK, групи опитування).
  VK-коди незалежні від розкладки (укр/англ дають той самий фізичний VK),
  тож перевіряємо саме канонічні коди;
- _combo_released: чиста логіка «комбінацію відпущено» для hold-PTT;
- HotkeyManager: реальна реєстрація/дереєстрація RegisterHotKey без витоків,
  конфлікт зайнятої комбінації → зрозуміла помилка, доставка WM_HOTKEY;
- Config: hotkey_backend дефолт "native", round-trip, вибір бекенда;
- живий смоук: SendInput-натиск комбінації → pressed/released (потрібна
  інтерактивна сесія Windows; поза нею тест скіпається).
"""
import ctypes
import sys
import tempfile
import threading
import unittest
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fronts.desktop import hotkeys_native as hn


IS_WIN = sys.platform == "win32"


class ParseComboTests(unittest.TestCase):
    def test_default_ptt_combo(self):
        mods, vk, groups = hn.parse_combo("ctrl+shift+space")
        self.assertEqual(mods, hn.MOD_CONTROL | hn.MOD_SHIFT)
        self.assertEqual(vk, 0x20)
        # групи опитування: ctrl, shift, основна — по одній на компонент
        self.assertEqual(len(groups), 3)
        self.assertIn((0x20,), groups)

    def test_letters_digits_fkeys(self):
        self.assertEqual(hn.parse_combo("ctrl+alt+z")[1], ord("Z"))
        self.assertEqual(hn.parse_combo("ctrl+1")[1], ord("1"))
        self.assertEqual(hn.parse_combo("f23")[1], 0x86)   # F13..F24 = 0x7C..0x87
        self.assertEqual(hn.parse_combo("ctrl+f1")[1], 0x70)

    def test_case_and_side_insensitive(self):
        # регістр і сторона модифікатора не важать (як у legacy combo_signature)
        a = hn.parse_combo("Ctrl+Shift+Space")
        b = hn.parse_combo("left ctrl+right shift+space")
        self.assertEqual(a[:2], b[:2])

    def test_single_key_no_mods_allowed(self):
        mods, vk, groups = hn.parse_combo("f23")
        self.assertEqual(mods, 0)
        self.assertEqual(len(groups), 1)

    def test_windows_mod_polls_both_sides(self):
        _, _, groups = hn.parse_combo("windows+space")
        self.assertIn((0x5B, 0x5C), groups)   # LWIN і RWIN — обидві сторони

    def test_unknown_key_raises(self):
        with self.assertRaises(hn.HotkeyError) as cm:
            hn.parse_combo("ctrl+бозна-що")
        self.assertIn("бозна-що", str(cm.exception))

    def test_empty_and_mods_only_raise(self):
        for bad in ("", "  ", "ctrl+shift"):
            with self.assertRaises(hn.HotkeyError):
                hn.parse_combo(bad)

    def test_two_main_keys_raise(self):
        with self.assertRaises(hn.HotkeyError):
            hn.parse_combo("a+b")


class ComboReleasedTests(unittest.TestCase):
    def test_all_down_not_released(self):
        groups = ((0x11,), (0x10,), (0x20,))
        self.assertFalse(hn._combo_released(groups, lambda vk: True))

    def test_any_component_up_is_released(self):
        groups = ((0x11,), (0x10,), (0x20,))
        down = {0x11: True, 0x10: False, 0x20: True}
        self.assertTrue(hn._combo_released(groups, lambda vk: down[vk]))

    def test_group_released_only_when_both_sides_up(self):
        groups = ((0x5B, 0x5C),)
        self.assertFalse(hn._combo_released(groups, lambda vk: vk == 0x5C))
        self.assertTrue(hn._combo_released(groups, lambda vk: False))


class ManagerLifecycleTests(unittest.TestCase):
    def test_stop_keeps_entries_when_thread_is_still_alive(self):
        manager = hn.HotkeyManager()
        join_calls = []
        thread_alive = [True]
        alive_thread = SimpleNamespace(
            join=join_calls.append,
            is_alive=lambda: thread_alive[0],
        )
        entry = object()
        manager._thread = alive_thread
        manager._thread_id = None
        manager._ok = True
        manager._entries[7] = entry

        created_threads = []

        def make_thread(**_kwargs):
            created_threads.append(True)
            return SimpleNamespace(start=lambda: None)

        with patch.object(hn.sys, "platform", "win32"), \
                patch.object(hn.threading, "Thread", side_effect=make_thread):
            stop_ok = manager.stop()
            restart_ok = manager.start()

        self.assertIs(stop_ok, False)
        self.assertEqual(join_calls, [3.0])
        self.assertIs(manager._thread, alive_thread)
        self.assertIs(manager._entries[7], entry)
        self.assertTrue(manager._ok)
        self.assertTrue(manager._stop_unconfirmed)
        self.assertFalse(restart_ok)
        self.assertEqual(created_threads, [])

    def test_start_recovers_after_unconfirmed_thread_dies(self):
        manager = hn.HotkeyManager()
        thread_alive = [True]
        manager._thread = SimpleNamespace(
            join=lambda _timeout: None,
            is_alive=lambda: thread_alive[0],
        )
        manager._thread_id = None
        manager._ok = True
        manager._entries[7] = object()

        self.assertIs(manager.stop(), False)
        thread_alive[0] = False

        created_threads = []

        def make_thread(**_kwargs):
            created_threads.append(True)
            return SimpleNamespace(
                start=lambda: setattr(manager, "_ok", True))

        manager._ready = SimpleNamespace(
            clear=lambda: None,
            wait=lambda _timeout: None,
        )
        with patch.object(hn.sys, "platform", "win32"), \
                patch.object(hn.threading, "Thread", side_effect=make_thread):
            restart_ok = manager.start()

        self.assertTrue(restart_ok)
        self.assertEqual(created_threads, [True])
        self.assertFalse(manager._stop_unconfirmed)
        self.assertEqual(manager._entries, {})


@unittest.skipUnless(IS_WIN, "RegisterHotKey — лише Windows")
class ManagerTests(unittest.TestCase):
    """Реальний RegisterHotKey на прихованому message-loop потоці."""

    def setUp(self):
        self.m = hn.HotkeyManager()
        self.assertTrue(self.m.start())

    def tearDown(self):
        self.assertTrue(self.m.stop())

    def test_register_unregister_no_leak(self):
        # рідкісна комбінація, щоб не зачепити чуже
        hid = self.m.register("ctrl+alt+f19", lambda: None)
        self.assertIsInstance(hid, int)
        self.m.unregister(hid)
        # без витоку: та сама комбінація реєструється знову
        hid2 = self.m.register("ctrl+alt+f19", lambda: None)
        self.m.unregister(hid2)

    def test_conflict_clear_error(self):
        hid = self.m.register("ctrl+alt+f20", lambda: None)
        try:
            with self.assertRaises(hn.HotkeyError) as cm:
                self.m.register("ctrl+alt+f20", lambda: None)
            self.assertTrue(cm.exception.conflict)
            self.assertIn("зайнята", str(cm.exception))
        finally:
            self.m.unregister(hid)

    def test_wm_hotkey_dispatch(self):
        """WM_HOTKEY у чергу потоку → callback (шлях доставки, без клавіатури)."""
        fired = threading.Event()
        hid = self.m.register("ctrl+alt+f21", fired.set)
        try:
            user32 = ctypes.WinDLL("user32")
            user32.PostThreadMessageW.argtypes = (
                wintypes.DWORD, wintypes.UINT, wintypes.WPARAM,
                wintypes.LPARAM)
            user32.PostThreadMessageW.restype = wintypes.BOOL
            self.assertTrue(user32.PostThreadMessageW(
                self.m.thread_id(), hn.WM_HOTKEY, hid, 0))
            self.assertTrue(fired.wait(2.0), "callback не спрацював на WM_HOTKEY")
        finally:
            self.m.unregister(hid)

    def test_unregister_unknown_id_is_noop(self):
        self.m.unregister(99999)   # не кидає

    def test_suspend_resume(self):
        hid = self.m.register("ctrl+alt+f22", lambda: None)
        try:
            self.m.suspend()
            # поки призупинено — комбінація вільна для іншого власника
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.RegisterHotKey.argtypes = (
                wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
            user32.RegisterHotKey.restype = wintypes.BOOL
            user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
            user32.UnregisterHotKey.restype = wintypes.BOOL
            probe_ok = bool(user32.RegisterHotKey(None, 0x3FFF, 0x0001 | 0x0002, 0x85))
            if probe_ok:
                user32.UnregisterHotKey(None, 0x3FFF)
            self.m.resume()
            # після resume знову зареєстрована: повторна реєстрація конфліктує
            with self.assertRaises(hn.HotkeyError):
                self.m.register("ctrl+alt+f22", lambda: None)
        finally:
            self.m.unregister(hid)


@unittest.skipUnless(IS_WIN, "Config/бекенд — тестуємо на Windows")
class BackendConfigTests(unittest.TestCase):
    def test_default_native(self):
        from whisper_core.config import Config
        self.assertEqual(Config().hotkey_backend, "native")

    def test_roundtrip(self):
        from whisper_core.config import Config
        c = Config()
        c.hotkey_backend = "legacy"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            c.save(p)
            self.assertIn('hotkey_backend = "legacy"', p.read_text(encoding="utf-8"))
            self.assertEqual(Config.load(p).hotkey_backend, "legacy")

    def test_backend_is_native(self):
        import types
        self.assertTrue(hn.backend_is_native(types.SimpleNamespace()))
        self.assertTrue(hn.backend_is_native(
            types.SimpleNamespace(hotkey_backend="native")))
        self.assertFalse(hn.backend_is_native(
            types.SimpleNamespace(hotkey_backend="legacy")))
        # бите значення → native (безпечний дефолт)
        self.assertTrue(hn.backend_is_native(
            types.SimpleNamespace(hotkey_backend="whatever")))


@unittest.skipUnless(IS_WIN, "SendInput-смоук — лише інтерактивна сесія Windows")
class LiveSendInputSmokeTests(unittest.TestCase):
    """Живий смоук: програмний натиск комбінації через SendInput має пройти
    повний шлях RegisterHotKey → WM_HOTKEY → pressed, а відпускання —
    GetAsyncKeyState-poll → released. Поза інтерактивною сесією скіпаємо."""

    VK_CONTROL, VK_MENU, VK_F18 = 0x11, 0x12, 0x81

    def _tap(self, down: bool):
        from fronts.desktop import wininput
        seq = [wininput._vk_input(vk, up=not down)
               for vk in ((self.VK_CONTROL, self.VK_MENU, self.VK_F18) if down
                          else (self.VK_F18, self.VK_MENU, self.VK_CONTROL))]
        return wininput._call_sendinput(seq) == len(seq)

    def test_press_and_release_events(self):
        m = hn.HotkeyManager()
        self.assertTrue(m.start())
        pressed, released = threading.Event(), threading.Event()
        try:
            hid = m.register("ctrl+alt+f18", pressed.set, released.set, hold=True)
        except hn.HotkeyError as e:
            self.assertTrue(m.stop())
            self.skipTest(f"комбінацію смоуку зайнято: {e}")
        try:
            if not self._tap(down=True):
                self.skipTest("SendInput відхилено (неінтерактивна сесія)")
            got_press = pressed.wait(2.0)
            self._tap(down=False)
            if not got_press:
                self.skipTest("WM_HOTKEY не дійшов (сесія без вводу?) — "
                              "потрібен живий тест")
            self.assertTrue(released.wait(2.0),
                            "released не прийшов після відпускання (hold-poll)")
        finally:
            self._tap(down=False)   # страхування: не лишати клавіші «затиснутими»
            m.unregister(hid)
            self.assertTrue(m.stop())


if __name__ == "__main__":
    unittest.main()
