"""feature/cascade-paste — тести каскадної вставки.

Покриваємо: побудову INPUT-масивів для type_unicode (структура, кирилиця,
сурогатні пари), предикати класів вікон, політику paste_text за класом,
блок-лист менеджерів паролів, гейт конфігу paste_typing_fallback (у самому
config і наскрізь через _work). SendInput не викликаємо по-справжньому —
підмінюємо _call_sendinput, щоб перевірити саме зібрані події.
Стиль — як test_voice_punctuation.py.
"""
import struct
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fronts.desktop import wininput


def _capture(store):
    """side_effect для _call_sendinput: зберегти список подій, повернути 'усі прийнято'."""
    def fake(inputs):
        store.append(list(inputs))
        return len(inputs)
    return fake


class TypeUnicodeInputTests(unittest.TestCase):
    def test_cyrillic_atomic_single_call(self):
        captured = []
        with patch.object(wininput, "_call_sendinput", side_effect=_capture(captured)):
            ok = wininput.type_unicode("Привіт")
        self.assertTrue(ok)
        self.assertEqual(len(captured), 1, "увесь текст має йти одним SendInput")
        inputs = captured[0]
        self.assertEqual(len(inputs), 2 * len("Привіт"), "press+release на символ")
        for ev in inputs:
            self.assertEqual(ev.type, wininput.INPUT_KEYBOARD)
            self.assertEqual(ev.u.ki.wVk, 0, "Unicode-ввід: віртуальна клавіша = 0")
            self.assertTrue(ev.u.ki.dwFlags & wininput.KEYEVENTF_UNICODE)
        presses = [e for e in inputs if not (e.u.ki.dwFlags & wininput.KEYEVENTF_KEYUP)]
        self.assertEqual([e.u.ki.wScan for e in presses], [ord(c) for c in "Привіт"])

    def test_press_then_release_flags(self):
        captured = []
        with patch.object(wininput, "_call_sendinput", side_effect=_capture(captured)):
            wininput.type_unicode("a")
        press, release = captured[0]
        self.assertEqual(press.u.ki.dwFlags, wininput.KEYEVENTF_UNICODE)
        self.assertEqual(release.u.ki.dwFlags,
                         wininput.KEYEVENTF_UNICODE | wininput.KEYEVENTF_KEYUP)

    def test_surrogate_pair_for_non_bmp(self):
        captured = []
        with patch.object(wininput, "_call_sendinput", side_effect=_capture(captured)):
            wininput.type_unicode("😀")   # U+1F600 — поза BMP
        inputs = captured[0]
        self.assertEqual(len(inputs), 4, "2 сурогати × press+release")
        units = list(struct.unpack("<2H", "😀".encode("utf-16-le")))
        presses = [e for e in inputs if not (e.u.ki.dwFlags & wininput.KEYEVENTF_KEYUP)]
        self.assertEqual([e.u.ki.wScan for e in presses], units)
        self.assertTrue(0xD800 <= units[0] <= 0xDBFF, "перший — high surrogate")
        self.assertTrue(0xDC00 <= units[1] <= 0xDFFF, "другий — low surrogate")

    def test_char_to_units_matches_utf16(self):
        for ch in ["П", "a", "😀", "𝔸", "ї"]:
            expected = tuple(struct.unpack(f"<{len(ch.encode('utf-16-le')) // 2}H",
                                           ch.encode("utf-16-le")))
            self.assertEqual(wininput._char_to_units(ch), expected)

    def test_delay_mode_calls_per_char(self):
        calls = []
        with patch.object(wininput, "_call_sendinput",
                          side_effect=lambda inp: calls.append(len(inp)) or len(inp)), \
             patch("fronts.desktop.wininput.time.sleep") as slp:
            ok = wininput.type_unicode("абв", char_delay_ms=5)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 3, "у режимі паузи — окремий виклик на символ")
        self.assertEqual(slp.call_count, 3)

    def test_empty_text_no_call(self):
        with patch.object(wininput, "_call_sendinput") as m:
            self.assertTrue(wininput.type_unicode(""))
        m.assert_not_called()

    def test_send_ctrl_v_sequence(self):
        captured = []
        with patch.object(wininput, "_call_sendinput", side_effect=_capture(captured)):
            self.assertTrue(wininput.send_ctrl_v())
        seq = [(e.u.ki.wVk, bool(e.u.ki.dwFlags & wininput.KEYEVENTF_KEYUP))
               for e in captured[0]]
        self.assertEqual(seq, [(wininput.VK_CONTROL, False), (wininput.VK_V, False),
                               (wininput.VK_V, True), (wininput.VK_CONTROL, True)])

    def test_send_ctrl_shift_v_sequence(self):
        captured = []
        with patch.object(wininput, "_call_sendinput", side_effect=_capture(captured)):
            self.assertTrue(wininput.send_ctrl_shift_v())
        seq = [(e.u.ki.wVk, bool(e.u.ki.dwFlags & wininput.KEYEVENTF_KEYUP))
               for e in captured[0]]
        self.assertEqual(seq, [(wininput.VK_CONTROL, False), (wininput.VK_SHIFT, False),
                               (wininput.VK_V, False), (wininput.VK_V, True),
                               (wininput.VK_SHIFT, True), (wininput.VK_CONTROL, True)])


class ClassPredicateTests(unittest.TestCase):
    def test_console_classes(self):
        self.assertTrue(wininput.is_console_class("ConsoleWindowClass"))
        self.assertTrue(wininput.is_console_class("CASCADIA_HOSTING_WINDOW_CLASS"))
        self.assertFalse(wininput.is_console_class("Notepad"))
        self.assertFalse(wininput.is_console_class(""))

    def test_rdp_class(self):
        self.assertTrue(wininput.is_rdp_class("TscShellContainerClass"))
        self.assertFalse(wininput.is_rdp_class("Notepad"))

    def test_password_manager_case_insensitive(self):
        for exe in ("KeePass.exe", "keepassxc.exe", "Bitwarden.exe",
                    "1Password.exe", "CredentialUIBroker.exe"):
            self.assertTrue(wininput.is_password_manager(exe), exe)
        self.assertFalse(wininput.is_password_manager("notepad.exe"))
        self.assertFalse(wininput.is_password_manager(""))


class PastePolicyTests(unittest.TestCase):
    """paste_text доставляє за класом активного вікна; буфер заповнюється завжди."""

    def _run(self, cls, exe, typing_fallback, excluded_ok=True):
        """excluded_ok моделює обидві гілки страхувальної сітки:
          True  — set_clipboard_text_excluded вдалося (текст у буфері через нього,
                  ексклюзивні формати history/cloud збережено, pyperclip НЕ чіпаємо);
          False — excluded-запис провалився → pyperclip-фолбек кладе текст."""
        from fronts.desktop import paste
        with patch.object(wininput, "get_foreground_info", return_value=(cls, exe)), \
             patch.object(wininput, "send_ctrl_v", return_value=True) as cv, \
             patch.object(wininput, "send_ctrl_shift_v", return_value=True) as csv, \
             patch.object(wininput, "type_unicode", return_value=True) as tu, \
             patch("whisper_core.win_hardening.set_clipboard_text_excluded",
                   return_value=excluded_ok) as excl, \
             patch.object(paste, "pyperclip") as clip, \
             patch("fronts.desktop.paste.time.sleep"):
            result = paste.paste_text("текст", typing_fallback=typing_fallback)
        return SimpleNamespace(result=result, cv=cv, csv=csv, tu=tu, clip=clip, excl=excl)

    def test_console_uses_ctrl_shift_v(self):
        r = self._run("ConsoleWindowClass", "cmd.exe", typing_fallback=True)
        self.assertEqual(r.result, "ctrl_shift_v")
        r.csv.assert_called_once()
        r.cv.assert_not_called()
        r.tu.assert_not_called()

    def test_windows_terminal_uses_ctrl_shift_v(self):
        r = self._run("CASCADIA_HOSTING_WINDOW_CLASS", "WindowsTerminal.exe",
                      typing_fallback=True)
        self.assertEqual(r.result, "ctrl_shift_v")

    def test_rdp_uses_ctrl_v_even_with_typing(self):
        r = self._run("TscShellContainerClass", "mstsc.exe", typing_fallback=True)
        self.assertEqual(r.result, "ctrl_v")
        r.cv.assert_called_once()
        r.tu.assert_not_called()

    def test_regular_window_types_when_enabled(self):
        r = self._run("Notepad", "notepad.exe", typing_fallback=True)
        self.assertEqual(r.result, "type_unicode")
        r.tu.assert_called_once()
        r.cv.assert_not_called()

    def test_regular_window_pastes_when_typing_off(self):
        r = self._run("Notepad", "notepad.exe", typing_fallback=False)
        self.assertEqual(r.result, "ctrl_v")
        r.cv.assert_called_once()
        r.tu.assert_not_called()

    def test_clipboard_filled_via_excluded_write(self):
        # Контракт «текст ЗАВЖДИ в буфері», гілка 1: excluded-запис вдався → текст
        # у буфері через нього; pyperclip НЕ кличемо (інакше затер би ексклюзивні
        # формати history/cloud). Перевіряємо ФАКТ запису тексту, не «copy раз».
        r = self._run("Notepad", "notepad.exe", typing_fallback=True, excluded_ok=True)
        r.excl.assert_called_once_with("текст")
        r.clip.copy.assert_not_called()

    def test_clipboard_filled_via_pyperclip_fallback(self):
        # Контракт, гілка 2: excluded-запис провалився → страхувальна сітка pyperclip.
        r = self._run("Notepad", "notepad.exe", typing_fallback=True, excluded_ok=False)
        r.clip.copy.assert_called_once_with("текст")

    def test_password_manager_blocked_text_in_buffer_excluded(self):
        r = self._run("Chrome_WidgetWin_1", "keepassxc.exe",
                      typing_fallback=True, excluded_ok=True)
        from fronts.desktop.paste import PASTE_BLOCKED
        self.assertEqual(r.result, PASTE_BLOCKED)
        r.cv.assert_not_called()
        r.csv.assert_not_called()
        r.tu.assert_not_called()
        r.excl.assert_called_once_with("текст")   # текст лишається в буфері (excluded)
        r.clip.copy.assert_not_called()

    def test_password_manager_blocked_text_in_buffer_fallback(self):
        r = self._run("Chrome_WidgetWin_1", "keepassxc.exe",
                      typing_fallback=True, excluded_ok=False)
        from fronts.desktop.paste import PASTE_BLOCKED
        self.assertEqual(r.result, PASTE_BLOCKED)
        r.cv.assert_not_called()
        r.csv.assert_not_called()
        r.tu.assert_not_called()
        r.clip.copy.assert_called_once_with("текст")  # лишається в буфері (фолбек)

    def test_keyboard_last_resort_when_sendinput_fails(self):
        # keyboard тепер імпортується ЛІНИВО у фолбек-гілці (native-режим без
        # цього фолбека бібліотеку взагалі не вантажить) — патчимо sys.modules
        from fronts.desktop import paste
        kb = MagicMock()
        with patch.object(wininput, "get_foreground_info",
                          return_value=("Notepad", "notepad.exe")), \
             patch.object(wininput, "send_ctrl_v", return_value=False), \
             patch("whisper_core.win_hardening.set_clipboard_text_excluded",
                   return_value=True), \
             patch.object(paste, "pyperclip"), \
             patch.dict(sys.modules, {"keyboard": kb}), \
             patch("fronts.desktop.paste.time.sleep"):
            result = paste.paste_text("текст", typing_fallback=False)
        kb.send.assert_called_once_with("ctrl+v")
        self.assertEqual(result, "keyboard")

    def test_returns_none_when_everything_fails(self):
        from fronts.desktop import paste
        kb = MagicMock()
        kb.send.side_effect = RuntimeError("no input")
        with patch.object(wininput, "get_foreground_info",
                          return_value=("Notepad", "notepad.exe")), \
             patch.object(wininput, "send_ctrl_v", return_value=False), \
             patch("whisper_core.win_hardening.set_clipboard_text_excluded",
                   return_value=True), \
             patch.object(paste, "pyperclip"), \
             patch.dict(sys.modules, {"keyboard": kb}), \
             patch("fronts.desktop.paste.time.sleep"):
            result = paste.paste_text("текст", typing_fallback=False)
        self.assertIsNone(result)


class ConfigGateTests(unittest.TestCase):
    def test_default_is_false(self):
        # консервативний дефолт: «з коробки» — Ctrl+V як досі, набір — opt-in
        from whisper_core.config import Config
        self.assertFalse(Config().paste_typing_fallback)

    def test_save_load_roundtrip(self):
        import os
        import tempfile
        from whisper_core.config import Config
        c = Config()
        c.paste_typing_fallback = True   # не-дефолт: перевіряємо, що персиститься
        fd, path = tempfile.mkstemp(suffix=".toml")
        os.close(fd)
        try:
            c.save(path)
            self.assertTrue(Config.load(path).paste_typing_fallback)
        finally:
            os.remove(path)


class WorkPipelineTests(unittest.TestCase):
    """Наскрізь через _work: конфіг-гейт передається в paste_text, а блок-лист
    емітить трей-повідомлення й НЕ відновлює буфер."""

    @staticmethod
    def _controller(paste_typing_fallback):
        messages = []

        class Err:
            def emit(self, msg):
                messages.append(msg)

        class Counter:
            def __init__(self):
                self.count = 0

            def emit(self, *a):
                self.count += 1

        controller = SimpleNamespace(
            recorder=SimpleNamespace(to_audio=lambda chunks: "audio"),
            _transcribe_with_fallback=lambda audio, terms, **_kw: ("raw", "текст", 1.0, [], []),
            output_mode="paste",
            transcription_error=Err(),
            transcribed=Counter(),
            finished=Counter(),
            cfg=SimpleNamespace(sounds=False, voice_punctuation=False, language="uk",
                                restore_clipboard=True,
                                paste_typing_fallback=paste_typing_fallback),
            _busy=True,
        )
        return controller, messages

    def test_typing_flag_passed_and_restore_on_success(self):
        from fronts.desktop import app as desktop_app
        controller, messages = self._controller(paste_typing_fallback=False)
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "log_history", return_value=None), \
             patch.object(desktop_app, "snapshot_clipboard", return_value="old"), \
             patch.object(desktop_app, "paste_text", return_value="ctrl_v") as pt, \
             patch.object(desktop_app, "restore_clipboard") as rc:
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        pt.assert_called_once_with("текст", typing_fallback=False)
        rc.assert_called_once_with("old")
        self.assertEqual(messages, [])

    def test_blocklist_emits_and_skips_restore(self):
        from fronts.desktop import app as desktop_app
        from fronts.desktop.paste import PASTE_BLOCKED
        controller, messages = self._controller(paste_typing_fallback=True)
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "log_history", return_value=None), \
             patch.object(desktop_app, "snapshot_clipboard", return_value="old"), \
             patch.object(desktop_app, "paste_text", return_value=PASTE_BLOCKED) as pt, \
             patch.object(desktop_app, "restore_clipboard") as rc:
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        pt.assert_called_once_with("текст", typing_fallback=True)
        rc.assert_not_called()   # блокований → текст лишається в буфері
        self.assertEqual(messages, [desktop_app.tr("app_paste_blocked")])


if __name__ == "__main__":
    unittest.main()
