"""feature/cascade-paste — тести каскадної вставки.

Покриваємо: побудову INPUT-масивів для type_unicode (структура, кирилиця,
сурогатні пари), предикати класів вікон, політику paste_text за класом,
блок-лист менеджерів паролів, гейт конфігу paste_typing_fallback (у самому
config і наскрізь через _work). SendInput не викликаємо по-справжньому —
підмінюємо _call_sendinput, щоб перевірити саме зібрані події.
Стиль — як test_voice_punctuation.py.
"""
import inspect
import itertools
import struct
import sys
import threading
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

    OWNER = 0x4_0000_4444

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
            kwargs = {"typing_fallback": typing_fallback}
            if "owner_hwnd" in inspect.signature(paste.paste_text).parameters:
                kwargs["owner_hwnd"] = self.OWNER
            result = paste.paste_text("текст", **kwargs)
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
        from fronts.desktop import paste

        clipboard = {"text": ""}
        delivered = []

        def write_excluded(text, owner_hwnd):
            self.assertEqual(owner_hwnd, self.OWNER)
            clipboard["text"] = text
            return True

        def deliver_clipboard():
            delivered.append(clipboard["text"])
            return True

        with patch.object(
                wininput, "get_foreground_info",
                return_value=("Notepad", "notepad.exe")), \
             patch.object(
                 wininput, "send_ctrl_v", side_effect=deliver_clipboard), \
             patch(
                 "whisper_core.win_hardening.set_clipboard_text_excluded",
                 side_effect=write_excluded), \
             patch.object(paste, "pyperclip") as clip, \
             patch("fronts.desktop.paste.time.sleep"):
            result = paste.paste_text(
                "текст", typing_fallback=False, owner_hwnd=self.OWNER)

        self.assertEqual(delivered, ["текст"])
        self.assertEqual(clipboard["text"], "текст")
        self.assertEqual(result, "ctrl_v")
        clip.copy.assert_not_called()

    def test_clipboard_filled_via_pyperclip_fallback(self):
        # Контракт, гілка 2: excluded-запис провалився → страхувальна сітка pyperclip.
        r = self._run("Notepad", "notepad.exe", typing_fallback=True, excluded_ok=False)
        r.excl.assert_called_once_with("текст", self.OWNER)
        r.clip.copy.assert_called_once_with("текст")

    def test_password_manager_blocked_text_in_buffer_excluded(self):
        r = self._run("Chrome_WidgetWin_1", "keepassxc.exe",
                      typing_fallback=True, excluded_ok=True)
        from fronts.desktop.paste import PASTE_BLOCKED
        self.assertEqual(r.result, PASTE_BLOCKED)
        r.cv.assert_not_called()
        r.csv.assert_not_called()
        r.tu.assert_not_called()
        r.excl.assert_called_once_with("текст", self.OWNER)
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
            _clipboard_owner_hwnd=0x4_0000_4444,
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
             patch.object(desktop_app, "begin_clipboard_restore", return_value="old"), \
             patch.object(desktop_app, "paste_text", return_value="ctrl_v") as pt, \
             patch.object(desktop_app, "restore_clipboard") as rc:
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        pt.assert_called_once_with(
            "текст", typing_fallback=False,
            owner_hwnd=controller._clipboard_owner_hwnd)
        rc.assert_called_once_with("old", expected="текст")
        self.assertEqual(messages, [])

    def test_blocklist_emits_and_skips_restore(self):
        from fronts.desktop import app as desktop_app
        from fronts.desktop.paste import PASTE_BLOCKED
        controller, messages = self._controller(paste_typing_fallback=True)
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "log_history", return_value=None), \
             patch.object(desktop_app, "begin_clipboard_restore", return_value="old"), \
             patch.object(desktop_app, "paste_text", return_value=PASTE_BLOCKED) as pt, \
             patch.object(desktop_app, "end_clipboard_restore") as end, \
             patch.object(desktop_app, "restore_clipboard") as rc:
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        pt.assert_called_once_with(
            "текст", typing_fallback=True,
            owner_hwnd=controller._clipboard_owner_hwnd)
        rc.assert_not_called()   # блокований → текст лишається в буфері
        end.assert_called_once_with()
        self.assertEqual(messages, [desktop_app.tr("app_paste_blocked")])

    def test_insert_worker_passes_cached_clipboard_owner(self):
        from fronts.desktop import app as desktop_app
        from fronts.desktop.paste import PASTE_BLOCKED
        controller, _messages = self._controller(paste_typing_fallback=True)
        with patch.object(desktop_app, "begin_clipboard_restore", return_value="old"), \
             patch.object(desktop_app, "end_clipboard_restore") as end, \
             patch.object(desktop_app, "paste_text", return_value=PASTE_BLOCKED) as pt:
            desktop_app.DesktopApp._insert_worker(controller, "повтор")
        pt.assert_called_once_with(
            "повтор", typing_fallback=True,
            owner_hwnd=controller._clipboard_owner_hwnd)
        end.assert_called_once_with()

    def test_paste_exception_ends_restore_operation(self):
        from fronts.desktop import app as desktop_app

        controller, _messages = self._controller(paste_typing_fallback=False)
        with patch.object(desktop_app, "begin_clipboard_restore",
                          return_value="old"), \
             patch.object(desktop_app, "end_clipboard_restore") as end, \
             patch.object(desktop_app, "paste_text",
                          side_effect=RuntimeError("paste failed")):
            with self.assertRaisesRegex(RuntimeError, "paste failed"):
                desktop_app._deliver_paste(
                    controller, "текст", auto_enter=False)
        end.assert_called_once_with()

    def test_insert_worker_exception_ends_restore_operation(self):
        from fronts.desktop import app as desktop_app

        controller, _messages = self._controller(paste_typing_fallback=False)
        with patch.object(desktop_app, "begin_clipboard_restore",
                          return_value="old"), \
             patch.object(desktop_app, "end_clipboard_restore") as end, \
             patch.object(desktop_app, "paste_text",
                          side_effect=RuntimeError("paste failed")):
            with self.assertRaisesRegex(RuntimeError, "paste failed"):
                desktop_app.DesktopApp._insert_worker(controller, "повтор")
        end.assert_called_once_with()

    def test_main_window_hwnd_is_cached_during_initialization(self):
        from fronts.desktop.app import DesktopApp
        source = inspect.getsource(DesktopApp.__init__)
        self.assertIn(
            "self._clipboard_owner_hwnd = int(self.window.winId())", source)


class ClipboardRestoreTimerRaceTests(unittest.TestCase):
    """Реальний _deliver_paste/paste_text з детермінованим часом і fake clipboard."""

    class Clock:
        def __init__(self):
            self.now = 0.0
            self.timers = []

        def timer(self, delay, fn, args=()):
            clock = self

            class Timer:
                def __init__(self):
                    self.deadline = clock.now + delay
                    self.cancelled = False

                def start(self):
                    clock.timers.append(self)

                def cancel(self):
                    self.cancelled = True

                def fire(self):
                    fn(*args)

            return Timer()

        def sleep(self, delay):
            target = self.now + delay
            while True:
                due = [timer for timer in self.timers
                       if not timer.cancelled and timer.deadline <= target]
                if not due:
                    break
                timer = min(due, key=lambda item: item.deadline)
                self.timers.remove(timer)
                self.now = timer.deadline
                timer.fire()
            self.now = target

    def test_timer_fires_between_a_restore_and_b_restore_preserves_original(self):
        from fronts.desktop import paste

        clipboard = {"text": "OLD"}
        clipboard_lock = threading.Lock()
        timers = []
        a_copied = threading.Event()
        b_copied = threading.Event()
        a_restored = threading.Event()
        allow_b_restore = threading.Event()
        worker_errors = []

        class ManualTimer:
            def __init__(self, _delay, fn):
                self.fn = fn
                self.cancelled = False
                self.fired = False

            def start(self):
                timers.append(self)

            def cancel(self):
                self.cancelled = True

            def fire(self):
                self.fired = True
                self.fn()

        def active_timers():
            return [timer for timer in timers
                    if not timer.cancelled and not timer.fired]

        def read_clipboard():
            with clipboard_lock:
                return clipboard["text"]

        def write_clipboard(text):
            with clipboard_lock:
                clipboard["text"] = text

        def paste_a():
            try:
                previous = paste.begin_clipboard_restore()
                write_clipboard("A")
                a_copied.set()
                if not b_copied.wait(1):
                    raise AssertionError("B не дійшла до copy")
                paste.restore_clipboard(previous, expected="A")
                a_restored.set()
            except BaseException as exc:
                worker_errors.append(exc)

        def paste_b():
            try:
                if not a_copied.wait(1):
                    raise AssertionError("A не дійшла до copy")
                previous = paste.begin_clipboard_restore()
                write_clipboard("B")
                b_copied.set()
                if not a_restored.wait(1):
                    raise AssertionError("A не дійшла до restore")
                if not allow_b_restore.wait(1):
                    raise AssertionError("B не дозволили restore")
                paste.restore_clipboard(previous, expected="B")
            except BaseException as exc:
                worker_errors.append(exc)

        paste.cancel_clipboard_restore()
        try:
            with patch.object(paste.threading, "Timer", ManualTimer), \
                 patch.object(paste.pyperclip, "paste",
                              side_effect=read_clipboard), \
                 patch.object(paste.pyperclip, "copy",
                              side_effect=write_clipboard):
                thread_a = threading.Thread(target=paste_a)
                thread_b = threading.Thread(target=paste_b)
                thread_a.start()
                thread_b.start()
                self.assertTrue(a_restored.wait(1))

                active = active_timers()
                self.assertEqual(len(active), 1)
                active[0].fire()
                self.assertEqual(
                    len(active_timers()), 1,
                    "mismatch під час активної B має перепланувати timer")

                allow_b_restore.set()
                thread_a.join(1)
                thread_b.join(1)
                self.assertFalse(thread_a.is_alive())
                self.assertFalse(thread_b.is_alive())
                self.assertEqual(worker_errors, [])

                active = active_timers()
                self.assertEqual(len(active), 1)
                active[0].fire()
        finally:
            allow_b_restore.set()
            paste.cancel_clipboard_restore()

        self.assertEqual(clipboard["text"], "OLD")

    def test_real_threads_restore_out_of_order_preserves_first_original(self):
        from fronts.desktop import paste

        clipboard = {"text": "OLD"}
        clipboard_lock = threading.Lock()
        timers = []
        a_copied = threading.Event()
        b_restored = threading.Event()
        worker_errors = []

        class ManualTimer:
            def __init__(self, _delay, fn):
                self.fn = fn
                self.cancelled = False

            def start(self):
                timers.append(self)

            def cancel(self):
                self.cancelled = True

            def fire(self):
                self.fn()

        def read_clipboard():
            with clipboard_lock:
                return clipboard["text"]

        def write_clipboard(text):
            with clipboard_lock:
                clipboard["text"] = text

        def paste_a():
            try:
                previous = paste.begin_clipboard_restore()
                write_clipboard("A")
                a_copied.set()
                if not b_restored.wait(1):
                    raise AssertionError("B не дійшла до restore")
                paste.restore_clipboard(previous, expected="A")
            except BaseException as exc:
                worker_errors.append(exc)

        def paste_b():
            try:
                if not a_copied.wait(1):
                    raise AssertionError("A не дійшла до copy")
                previous = paste.begin_clipboard_restore()
                write_clipboard("B")
                paste.restore_clipboard(previous, expected="B")
                b_restored.set()
            except BaseException as exc:
                worker_errors.append(exc)

        paste.cancel_clipboard_restore()
        try:
            with patch.object(paste.threading, "Timer", ManualTimer), \
                 patch.object(paste.pyperclip, "paste",
                              side_effect=read_clipboard), \
                 patch.object(paste.pyperclip, "copy",
                              side_effect=write_clipboard):
                thread_a = threading.Thread(target=paste_a)
                thread_b = threading.Thread(target=paste_b)
                thread_a.start()
                thread_b.start()
                thread_a.join(1)
                thread_b.join(1)
                self.assertFalse(thread_a.is_alive())
                self.assertFalse(thread_b.is_alive())
                self.assertEqual(worker_errors, [])
                active = [timer for timer in timers if not timer.cancelled]
                self.assertEqual(len(active), 1)
                active[0].fire()
        finally:
            paste.cancel_clipboard_restore()

        self.assertEqual(clipboard["text"], "OLD")

    def test_user_clipboard_between_pastes_becomes_new_restore_target(self):
        from fronts.desktop import paste

        clipboard = {"text": "OLD"}
        timers = []

        class ManualTimer:
            def __init__(self, _delay, fn):
                self.fn = fn
                self.cancelled = False

            def start(self):
                timers.append(self)

            def cancel(self):
                self.cancelled = True

            def fire(self):
                self.fn()

        with patch.object(paste.threading, "Timer", ManualTimer), \
             patch.object(paste.pyperclip, "paste",
                          side_effect=lambda: clipboard["text"]), \
             patch.object(paste.pyperclip, "copy",
                          side_effect=lambda text: clipboard.update(text=text)):
            previous_a = paste.begin_clipboard_restore()
            clipboard["text"] = "A"
            paste.restore_clipboard(previous_a, expected="A")

            clipboard["text"] = "X"
            previous_b = paste.begin_clipboard_restore()
            clipboard["text"] = "B"
            paste.restore_clipboard(previous_b, expected="B")

            active = [timer for timer in timers if not timer.cancelled]
            self.assertEqual(len(active), 1)
            active[0].fire()

        self.assertEqual(clipboard["text"], "X")

    def test_overlapping_begins_restore_in_start_order_preserves_original(self):
        from fronts.desktop import paste

        clock = self.Clock()
        clipboard = {"text": "OLD"}
        with patch.object(paste.threading, "Timer", clock.timer), \
             patch.object(paste.pyperclip, "paste",
                          side_effect=lambda: clipboard["text"]), \
             patch.object(paste.pyperclip, "copy",
                          side_effect=lambda text: clipboard.update(text=text)):
            previous_a = paste.begin_clipboard_restore()
            clipboard["text"] = "A"
            previous_b = paste.begin_clipboard_restore()
            clipboard["text"] = "B"
            paste.restore_clipboard(previous_a, expected="A")
            paste.restore_clipboard(previous_b, expected="B")
            clock.sleep(0.4)

        self.assertEqual(clipboard["text"], "OLD")

    def test_all_begin_copy_restore_interleavings_preserve_original(self):
        from fronts.desktop import paste

        actions = (
            ("A", "begin"), ("A", "copy"), ("A", "restore"),
            ("B", "begin"), ("B", "copy"), ("B", "restore"),
        )
        orders = [
            order for order in itertools.permutations(actions)
            if all(
                order.index((name, "begin"))
                < order.index((name, "copy"))
                < order.index((name, "restore"))
                for name in ("A", "B")
            )
        ]
        self.assertEqual(len(orders), 20)

        for order in orders:
            for fire_after in (None, *range(len(order) - 1)):
                with self.subTest(order=order, fire_after=fire_after):
                    clipboard = {"text": "OLD"}
                    previous = {}
                    timers = []

                    class ManualTimer:
                        def __init__(self, _delay, fn):
                            self.fn = fn
                            self.cancelled = False
                            self.fired = False

                        def start(self):
                            timers.append(self)

                        def cancel(self):
                            self.cancelled = True

                        def fire(self):
                            self.fired = True
                            self.fn()

                    def active_timers():
                        return [timer for timer in timers
                                if not timer.cancelled and not timer.fired]

                    paste.cancel_clipboard_restore()
                    with patch.object(paste.threading, "Timer", ManualTimer), \
                         patch.object(paste.pyperclip, "paste",
                                      side_effect=lambda: clipboard["text"]), \
                         patch.object(paste.pyperclip, "copy",
                                      side_effect=lambda text:
                                      clipboard.update(text=text)):
                        for index, (name, action) in enumerate(order):
                            if action == "begin":
                                previous[name] = paste.begin_clipboard_restore()
                            elif action == "copy":
                                clipboard["text"] = name
                            else:
                                paste.restore_clipboard(
                                    previous[name], expected=name)

                            if index == fire_after:
                                active = active_timers()
                                if active:
                                    active[0].fire()

                        active = active_timers()
                        self.assertEqual(len(active), 1)
                        active[0].fire()

                    self.assertEqual(clipboard["text"], "OLD")

    def test_three_overlapping_pastes_with_fires_between_restores_preserve_original(self):
        from fronts.desktop import paste

        clipboard = {"text": "OLD"}
        timers = []

        class ManualTimer:
            def __init__(self, _delay, fn):
                self.fn = fn
                self.cancelled = False
                self.fired = False

            def start(self):
                timers.append(self)

            def cancel(self):
                self.cancelled = True

            def fire(self):
                self.fired = True
                self.fn()

        def active_timers():
            return [timer for timer in timers
                    if not timer.cancelled and not timer.fired]

        paste.cancel_clipboard_restore()
        try:
            with patch.object(paste.threading, "Timer", ManualTimer), \
                 patch.object(paste.pyperclip, "paste",
                              side_effect=lambda: clipboard["text"]), \
                 patch.object(paste.pyperclip, "copy",
                              side_effect=lambda text:
                              clipboard.update(text=text)):
                previous_a = paste.begin_clipboard_restore()
                clipboard["text"] = "A"
                previous_b = paste.begin_clipboard_restore()
                clipboard["text"] = "B"

                paste.restore_clipboard(previous_a, expected="A")
                active_timers()[0].fire()
                self.assertEqual(len(active_timers()), 1)

                previous_c = paste.begin_clipboard_restore()
                clipboard["text"] = "C"
                paste.restore_clipboard(previous_b, expected="B")
                active_timers()[0].fire()
                self.assertEqual(len(active_timers()), 1)

                paste.restore_clipboard(previous_c, expected="C")
                active_timers()[0].fire()
        finally:
            paste.cancel_clipboard_restore()

        self.assertEqual(clipboard["text"], "OLD")

    def test_last_failed_operation_cancels_earlier_restore_and_keeps_its_text(self):
        from fronts.desktop import paste

        clipboard = {"text": "OLD"}
        timers = []

        class ManualTimer:
            def __init__(self, _delay, fn):
                self.fn = fn
                self.cancelled = False

            def start(self):
                timers.append(self)

            def cancel(self):
                self.cancelled = True

        paste.cancel_clipboard_restore()
        try:
            with patch.object(paste.threading, "Timer", ManualTimer), \
                 patch.object(paste.pyperclip, "paste",
                              side_effect=lambda: clipboard["text"]):
                previous_a = paste.begin_clipboard_restore()
                clipboard["text"] = "A"
                paste.begin_clipboard_restore()
                clipboard["text"] = "B"

                paste.restore_clipboard(previous_a, expected="A")
                paste.end_clipboard_restore()

                self.assertEqual(
                    [timer for timer in timers if not timer.cancelled], [])
        finally:
            paste.cancel_clipboard_restore()

        self.assertEqual(clipboard["text"], "B")

    def test_slow_finish_snapshot_does_not_hold_lock_or_commit_stale_copy(self):
        from fronts.desktop import paste

        clipboard = {"text": "A"}
        timers = []
        snapshot_started = threading.Event()
        release_snapshot = threading.Event()
        begin_finished = threading.Event()
        worker_errors = []
        finish_thread = None

        class ManualTimer:
            def __init__(self, _delay, fn):
                self.fn = fn
                self.cancelled = False

            def start(self):
                timers.append(self)

            def cancel(self):
                self.cancelled = True

            def fire(self):
                self.fn()

        def read_clipboard():
            current = clipboard["text"]
            if threading.current_thread() is finish_thread:
                snapshot_started.set()
                if not release_snapshot.wait(1):
                    raise AssertionError("slow snapshot не відпустили")
            return current

        def begin_b():
            try:
                previous = paste.begin_clipboard_restore()
                clipboard["text"] = "B"
                paste.restore_clipboard(previous, expected="B")
                begin_finished.set()
            except BaseException as exc:
                worker_errors.append(exc)

        paste.cancel_clipboard_restore()
        try:
            with patch.object(paste.threading, "Timer", ManualTimer), \
                 patch.object(paste.pyperclip, "paste",
                              side_effect=read_clipboard), \
                 patch.object(paste.pyperclip, "copy",
                              side_effect=lambda text: clipboard.update(text=text)):
                paste.restore_clipboard("OLD", expected="A")
                finish_thread = threading.Thread(target=timers[0].fire)
                finish_thread.start()
                self.assertTrue(snapshot_started.wait(1))

                begin_thread = threading.Thread(target=begin_b)
                begin_thread.start()
                self.assertTrue(
                    begin_finished.wait(0.5),
                    "begin B не має чекати на повільний clipboard I/O")

                release_snapshot.set()
                finish_thread.join(1)
                begin_thread.join(1)
                self.assertFalse(finish_thread.is_alive())
                self.assertFalse(begin_thread.is_alive())
                self.assertEqual(worker_errors, [])
                self.assertEqual(
                    clipboard["text"], "B",
                    "stale callback не має копіювати OLD після заміни timer")

                active = [timer for timer in timers if not timer.cancelled]
                self.assertEqual(len(active), 1)
                active[0].fire()
        finally:
            release_snapshot.set()
            paste.cancel_clipboard_restore()

        self.assertEqual(clipboard["text"], "OLD")

    def test_restore_replaced_during_copy_does_not_crash_stale_callback(self):
        from fronts.desktop import paste

        clipboard = {"text": "A"}
        timers = []
        copy_calls = 0

        class ManualTimer:
            def __init__(self, _delay, fn):
                self.fn = fn
                self.cancelled = False
                self.fired = False

            def start(self):
                timers.append(self)

            def cancel(self):
                self.cancelled = True

            def fire(self):
                self.fired = True
                self.fn()

        def active_timers():
            return [timer for timer in timers
                    if not timer.cancelled and not timer.fired]

        def replace_during_first_copy(text):
            nonlocal copy_calls
            copy_calls += 1
            if copy_calls == 1:
                previous = paste.begin_clipboard_restore()
                clipboard["text"] = "B"
                paste.restore_clipboard(previous, expected="B")
            else:
                clipboard["text"] = text
            return True

        paste.cancel_clipboard_restore()
        try:
            with patch.object(paste.threading, "Timer", ManualTimer), \
                 patch.object(paste.pyperclip, "paste",
                              side_effect=lambda: clipboard["text"]), \
                 patch.object(paste, "_safe_copy",
                              side_effect=replace_during_first_copy):
                paste.restore_clipboard("OLD", expected="A")
                active_timers()[0].fire()
                active_timers()[0].fire()
        finally:
            paste.cancel_clipboard_restore()

        self.assertEqual(clipboard["text"], "OLD")

    def test_overlapping_pastes_deliver_latest_and_restore_original(self):
        from fronts.desktop import app as desktop_app
        from fronts.desktop import paste

        clock = self.Clock()
        clipboard = {"text": "OLD"}
        delivered = []
        controller = SimpleNamespace(
            cfg=SimpleNamespace(
                restore_clipboard=True,
                paste_typing_fallback=False,
                paste_confirm_sound=False),
            transcription_error=MagicMock())

        def write_clipboard(text, _owner_hwnd=None):
            clipboard["text"] = text
            return True

        def send_ctrl_v():
            delivered.append(clipboard["text"])
            return True

        with patch.object(paste.threading, "Timer", clock.timer), \
             patch.object(paste.time, "sleep", clock.sleep), \
             patch.object(paste.pyperclip, "paste",
                          side_effect=lambda: clipboard["text"]), \
             patch.object(paste.pyperclip, "copy",
                          side_effect=lambda text: clipboard.update(text=text)), \
             patch("whisper_core.win_hardening.set_clipboard_text_excluded",
                   side_effect=write_clipboard), \
             patch.object(wininput, "get_foreground_info",
                          return_value=("Notepad", "notepad.exe")), \
             patch.object(wininput, "is_password_manager", return_value=False), \
             patch.object(wininput, "is_console_class", return_value=False), \
             patch.object(wininput, "is_rdp_class", return_value=False), \
             patch.object(wininput, "send_ctrl_v", side_effect=send_ctrl_v):
            desktop_app._deliver_paste(controller, "A", auto_enter=False)
            clock.sleep(0.2)  # менше за 0.4 с до відновлення A
            desktop_app._deliver_paste(controller, "B", auto_enter=False)
            clock.sleep(0.4)  # дочекатися єдиного актуального відновлення

        self.assertEqual(delivered, ["A", "B"])
        self.assertEqual(clipboard["text"], "OLD")
        self.assertEqual(sum(timer.cancelled for timer in clock.timers), 1,
                         "timer A має бути скасований, а не лишений осиротілим")

    def test_restore_does_not_overwrite_new_user_clipboard_content(self):
        from fronts.desktop import app as desktop_app
        from fronts.desktop import paste

        clock = self.Clock()
        clipboard = {"text": "OLD"}
        controller = SimpleNamespace(
            cfg=SimpleNamespace(
                restore_clipboard=True,
                paste_typing_fallback=False,
                paste_confirm_sound=False),
            transcription_error=MagicMock())

        def write_clipboard(text, _owner_hwnd=None):
            clipboard["text"] = text
            return True

        with patch.object(paste.threading, "Timer", clock.timer), \
             patch.object(paste.time, "sleep", clock.sleep), \
             patch.object(paste.pyperclip, "paste",
                          side_effect=lambda: clipboard["text"]), \
             patch.object(paste.pyperclip, "copy",
                          side_effect=lambda text: clipboard.update(text=text)), \
             patch("whisper_core.win_hardening.set_clipboard_text_excluded",
                   side_effect=write_clipboard), \
             patch.object(wininput, "get_foreground_info",
                          return_value=("Notepad", "notepad.exe")), \
             patch.object(wininput, "is_password_manager", return_value=False), \
             patch.object(wininput, "is_console_class", return_value=False), \
             patch.object(wininput, "is_rdp_class", return_value=False), \
             patch.object(wininput, "send_ctrl_v", return_value=True):
            desktop_app._deliver_paste(controller, "A", auto_enter=False)
            clock.sleep(0.2)
            clipboard["text"] = "USER"
            clock.sleep(0.2)

        self.assertEqual(clipboard["text"], "USER")

    def test_cancel_pending_restore_prevents_orphan_timer(self):
        from fronts.desktop import paste

        clock = self.Clock()
        clipboard = {"text": "A"}
        with patch.object(paste.threading, "Timer", clock.timer), \
             patch.object(paste.pyperclip, "paste",
                          side_effect=lambda: clipboard["text"]), \
             patch.object(paste.pyperclip, "copy",
                          side_effect=lambda text: clipboard.update(text=text)):
            paste.restore_clipboard("OLD", expected="A", delay=0.4)
            paste.cancel_clipboard_restore()
            clock.sleep(0.4)

        self.assertEqual(clipboard["text"], "A")

    def test_app_cleanup_cancels_pending_restore(self):
        from fronts.desktop import app as desktop_app

        class CleanupReached(Exception):
            pass

        controller = MagicMock()
        with patch.object(desktop_app, "cancel_clipboard_restore",
                          side_effect=CleanupReached) as cancel:
            with self.assertRaises(CleanupReached):
                desktop_app.DesktopApp._cleanup(controller)

        cancel.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
