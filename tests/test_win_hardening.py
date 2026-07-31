"""Юніт-тести для WinAPI обгортки та системного захисту (Т54 мілітарі-hardening).

Після рецензії (23.07) фейкові тести замінено на ЧЕСНІ — викликають реальний код і
перевіряють факти, а не відтворюють мок-послідовність у тілі тесту:
  • trigger_panic_lock — реальний метод DesktopApp знищує plaintext temp-копії
    нарад і кеш ключів (не лише мінімізує);
  • симетрія хоткей-матриці — реальні set_*_hotkey/_apply_key через фейк-self;
  • set_clipboard_text_excluded — чесний False при мовчазному провалі запису тексту;
  • WER-виняток — реальний DesktopApp.__init__ його викликає;
  • WDA на діалог наради — показ VideoPlayerDialog накладає афінність за тумблером.
"""
import ctypes
import gc
import inspect
import os
from pathlib import Path
import shutil
import threading
import time
import unittest
import uuid
import weakref
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from whisper_core import win_hardening
from whisper_core.config import Config
from whisper_core.meeting import storage_crypto
from fronts.desktop.hotkey import combos_equal
from fronts.desktop.app import DesktopApp


class _FakeTray:
    def __init__(self):
        self.notices = []

    def notify(self, text):
        self.notices.append(text)


class TestWinHardeningWrapper(unittest.TestCase):
    def test_is_display_affinity_supported_on_non_win(self):
        with patch("whisper_core.win_hardening.is_windows", return_value=False):
            self.assertFalse(win_hardening.is_display_affinity_supported())

    @patch("sys.getwindowsversion")
    @patch("whisper_core.win_hardening.is_windows", return_value=True)
    def test_is_display_affinity_supported_win10_2004(self, mock_is_win, mock_ver):
        mock_v = MagicMock()
        mock_v.major = 10
        mock_v.build = 19041
        mock_ver.return_value = mock_v
        self.assertTrue(win_hardening.is_display_affinity_supported())

        mock_v.build = 18362  # older Win10
        self.assertFalse(win_hardening.is_display_affinity_supported())

    @patch("whisper_core.win_hardening._user32")
    @patch("whisper_core.win_hardening.is_windows", return_value=True)
    def test_set_window_display_affinity(self, mock_is_win, mock_u32):
        mock_u32.SetWindowDisplayAffinity.return_value = 1
        res = win_hardening.set_window_display_affinity(12345, True)
        self.assertTrue(res)
        self.assertTrue(getattr(res, "succeeded", None))
        self.assertTrue(getattr(res, "supported", None))
        self.assertTrue(getattr(res, "enabled", None))
        self.assertEqual(getattr(res, "hwnd", None), 12345)
        self.assertIsNone(getattr(res, "error_code", "missing"))
        mock_u32.SetWindowDisplayAffinity.assert_called_once()
        args = mock_u32.SetWindowDisplayAffinity.call_args[0]
        self.assertEqual(args[1].value, win_hardening.WDA_EXCLUDEFROMCAPTURE)

        mock_u32.reset_mock()
        res_off = win_hardening.set_window_display_affinity(12345, False)
        self.assertTrue(res_off)
        self.assertTrue(getattr(res_off, "succeeded", None))
        self.assertFalse(getattr(res_off, "enabled", None))
        args_off = mock_u32.SetWindowDisplayAffinity.call_args[0]
        self.assertEqual(args_off[1].value, win_hardening.WDA_NONE)

    @patch("whisper_core.win_hardening.ctypes.get_last_error", return_value=5)
    @patch("whisper_core.win_hardening._user32")
    @patch("whisper_core.win_hardening.is_display_affinity_supported",
           return_value=True)
    def test_set_window_display_affinity_records_win32_failure(
            self, mock_supported, mock_u32, mock_get_last_error):
        mock_u32.SetWindowDisplayAffinity.return_value = 0

        result = win_hardening.set_window_display_affinity(45678, True)

        self.assertFalse(result)
        self.assertFalse(getattr(result, "succeeded", True))
        self.assertTrue(getattr(result, "supported", False))
        self.assertEqual(getattr(result, "error_code", None), 5)
        mock_get_last_error.assert_called_once_with()

    @patch("whisper_core.win_hardening._user32")
    @patch("whisper_core.win_hardening.is_display_affinity_supported",
           return_value=False)
    def test_set_window_display_affinity_reports_unsupported_without_call(
            self, mock_supported, mock_u32):
        result = win_hardening.set_window_display_affinity(45678, True)

        self.assertFalse(result)
        self.assertFalse(getattr(result, "supported", True))
        self.assertFalse(getattr(result, "succeeded", True))
        self.assertIsNone(getattr(result, "error_code", "missing"))
        mock_u32.SetWindowDisplayAffinity.assert_not_called()

    @patch("whisper_core.win_hardening._wer")
    @patch("whisper_core.win_hardening.is_windows", return_value=True)
    def test_exclude_process_from_wer(self, mock_is_win, mock_wer):
        mock_wer.WerAddExcludedApplication.return_value = 0
        self.assertTrue(win_hardening.exclude_process_from_wer("Balachky.exe"))
        mock_wer.WerAddExcludedApplication.assert_called_once()
        exe_name, all_users = (
            mock_wer.WerAddExcludedApplication.call_args.args)
        self.assertEqual(exe_name.value, "Balachky.exe")
        self.assertFalse(all_users.value)

    @patch("whisper_core.win_hardening._user32")
    @patch("whisper_core.win_hardening._kernel32")
    @patch("whisper_core.win_hardening.is_windows", return_value=True)
    def test_clear_clipboard(self, mock_is_win, mock_k32, mock_u32):
        mock_u32.OpenClipboard.return_value = 1
        mock_u32.EmptyClipboard.return_value = 1
        mock_u32.CloseClipboard.return_value = 1

        self.assertTrue(win_hardening.clear_clipboard())
        self.assertEqual(
            mock_u32.mock_calls,
            [call.OpenClipboard(None), call.EmptyClipboard(),
             call.CloseClipboard()],
        )


class TestCaptureProtectionActualState(unittest.TestCase):
    def tearDown(self):
        win_hardening._capture_windows.clear()
        results = getattr(win_hardening, "_capture_results", None)
        if results is not None:
            results.clear()

    def test_registry_returns_and_records_each_live_window_result(self):
        class _Widget:
            def __init__(self, hwnd):
                self._hwnd = hwnd

            def winId(self):
                return self._hwnd

        first = _Widget(101)
        second = _Widget(202)
        win_hardening._capture_windows.add(first)
        win_hardening._capture_windows.add(second)
        outcomes = {
            101: SimpleNamespace(hwnd=101, enabled=True, supported=True,
                                 succeeded=True, error_code=None),
            202: SimpleNamespace(hwnd=202, enabled=True, supported=True,
                                 succeeded=False, error_code=5),
        }

        with patch("whisper_core.win_hardening.set_window_display_affinity",
                   side_effect=lambda hwnd, enable: outcomes[hwnd]):
            returned = win_hardening.set_capture_protection_enabled(True)

        self.assertIsNotNone(
            returned, "реєстр має повернути фактичний результат кожного вікна")
        self.assertEqual(
            {item.hwnd: item for item in returned}, outcomes)
        recorded = win_hardening.capture_protection_results()
        self.assertEqual(
            {item.hwnd: item for item in recorded}, outcomes)

    def test_app_aggregates_active_failed_and_unsupported(self):
        from fronts.desktop import app as desktop_app

        self.assertTrue(
            hasattr(desktop_app, "_aggregate_screen_protection_state"),
            "app.py має агрегувати фактичні результати WDA")
        aggregate = desktop_app._aggregate_screen_protection_state
        ok = SimpleNamespace(supported=True, succeeded=True)
        refused = SimpleNamespace(supported=True, succeeded=False)
        unavailable = SimpleNamespace(supported=False, succeeded=False)

        self.assertEqual(aggregate((ok, ok), supported=True), "active")
        self.assertEqual(aggregate((ok, refused), supported=True), "failed")
        self.assertEqual(aggregate((unavailable,), supported=False), "unsupported")

    def test_dead_failed_window_recalculates_and_emits_active(self):
        from PySide6.QtCore import QObject, Signal

        class _Widget:
            def __init__(self, hwnd):
                self._hwnd = hwnd

            def winId(self):
                return self._hwnd

        class _Controller(QObject):
            screen_protection_state_changed = Signal(str)

            def __init__(self):
                super().__init__()
                self.cfg = SimpleNamespace(screen_protection=True)

            def screen_protection_state(self):
                return DesktopApp.screen_protection_state(self)

            def apply_screen_protection_to_window(self, widget):
                return DesktopApp.apply_screen_protection_to_window(self, widget)

        protected = win_hardening.DisplayAffinityResult(
            hwnd=101, enabled=True, supported=True,
            succeeded=True, error_code=None)
        refused = win_hardening.DisplayAffinityResult(
            hwnd=202, enabled=True, supported=True,
            succeeded=False, error_code=5)
        outcomes = {101: protected, 202: refused}
        main_window = _Widget(101)
        failed_dialog = _Widget(202)
        failed_dialog_ref = weakref.ref(failed_dialog)
        controller = _Controller()
        emitted = []
        controller.screen_protection_state_changed.connect(emitted.append)
        win_hardening.set_capture_protection_enabled(True)

        with patch(
                "whisper_core.win_hardening.is_display_affinity_supported",
                return_value=True), patch(
                "whisper_core.win_hardening.set_window_display_affinity",
                side_effect=lambda hwnd, enable: outcomes[hwnd]):
            controller.apply_screen_protection_to_window(main_window)
            controller.apply_screen_protection_to_window(failed_dialog)
            before = (
                controller.screen_protection_state(),
                list(emitted),
                len(win_hardening.capture_protection_results()),
            )

            del failed_dialog
            gc.collect()

            after_actual = controller.screen_protection_state()
            live_result_count = len(
                win_hardening.capture_protection_results())

        self.assertIsNone(failed_dialog_ref())
        self.assertEqual(before, ("failed", ["active", "failed"], 2))
        self.assertEqual(after_actual, "active")
        self.assertEqual(live_result_count, 1)
        self.assertEqual(
            emitted[-1], "active",
            "прибирання failed-вікна має емітити новий фактичний агрегат")

    def test_removing_active_window_without_state_change_does_not_emit(self):
        from PySide6.QtCore import QObject, Signal

        class _Widget:
            def __init__(self, hwnd):
                self._hwnd = hwnd

            def winId(self):
                return self._hwnd

        class _Controller(QObject):
            screen_protection_state_changed = Signal(str)

            def __init__(self):
                super().__init__()
                self.cfg = SimpleNamespace(screen_protection=True)

            def screen_protection_state(self):
                return DesktopApp.screen_protection_state(self)

            def apply_screen_protection_to_window(self, widget):
                return DesktopApp.apply_screen_protection_to_window(
                    self, widget)

            def remove_screen_protection_from_window(self, widget):
                return DesktopApp.remove_screen_protection_from_window(
                    self, widget)

        def protected(hwnd, enable):
            return win_hardening.DisplayAffinityResult(
                hwnd=hwnd, enabled=enable, supported=True,
                succeeded=True, error_code=None)

        controller = _Controller()
        emitted = []
        controller.screen_protection_state_changed.connect(emitted.append)
        first = _Widget(101)
        second = _Widget(202)
        win_hardening.set_capture_protection_enabled(True)

        with patch(
                "whisper_core.win_hardening.is_display_affinity_supported",
                return_value=True), patch(
                "whisper_core.win_hardening.set_window_display_affinity",
                side_effect=protected):
            try:
                controller.apply_screen_protection_to_window(first)
                controller.apply_screen_protection_to_window(second)
                emitted.clear()

                self.assertTrue(
                    controller.remove_screen_protection_from_window(first))

                self.assertEqual(
                    controller.screen_protection_state(), "active")
                self.assertEqual(
                    emitted, [],
                    "незмінний active-агрегат не має емітитися повторно")
            finally:
                controller.remove_screen_protection_from_window(first)
                controller.remove_screen_protection_from_window(second)
                win_hardening.set_capture_protection_enabled(False)


class TestScreenProtectionSettingsActualState(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qapp = QApplication.instance() or QApplication([])

    def test_indicator_uses_failed_result_instead_of_saved_intent(self):
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import QVBoxLayout, QWidget
        from fronts.desktop.i18n import current_language, set_language
        from fronts.desktop.pages.settings import SettingsPage

        language = current_language()
        self.addCleanup(set_language, language)
        set_language("uk")

        class _Controller(QObject):
            screen_protection_state_changed = Signal(str)
            panic_lock_key_captured = Signal(str)

            def __init__(self):
                super().__init__()
                self.cfg = SimpleNamespace(
                    screen_protection=False, panic_lock_hotkey="",
                    save=lambda: None)

            def set_screen_protection(self, enabled):
                return DesktopApp.set_screen_protection(self, enabled)

            def screen_protection_state(self):
                return DesktopApp.screen_protection_state(self)

            def start_panic_key_capture(self):
                pass

            def clear_panic_lock_hotkey(self):
                pass

        class _ProtectionPage(SettingsPage):
            def __init__(self, controller):
                QWidget.__init__(self)
                self.controller = controller
                layout = QVBoxLayout(self)
                layout.addWidget(self._protection_group(controller.cfg))

        refused = SimpleNamespace(
            hwnd=101, enabled=True, supported=True,
            succeeded=False, error_code=5)
        with patch(
                "whisper_core.win_hardening.is_display_affinity_supported",
                return_value=True), patch(
                "whisper_core.win_hardening.set_capture_protection_enabled",
                return_value=(refused,)):
            page = _ProtectionPage(_Controller())
            page.resize(640, 320)
            page.show()
            self.qapp.processEvents()
            page._screen_protection.click()
            self.qapp.processEvents()

        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)
        status = getattr(page, "_screen_protection_status", None)
        self.assertIsNotNone(status, "немає індикатора фактичного стану")
        self.assertFalse(status.isHidden(), "failed-стан приховано")
        self.assertGreater(status.width(), 0, "статус не відрендерився")
        self.assertEqual(
            status.text(), "Не вдалося ввімкнути захист екрана")
        self.assertNotEqual(
            status.text(), "Захист екрана діє")
        self.assertEqual(status.accessibleName(), status.text())
        for state, expected in (
                ("active", "Захист екрана діє"),
                ("unsupported", "Система не підтримує захист екрана")):
            with self.subTest(state=state):
                page._set_screen_protection_state(state)
                self.assertFalse(status.isHidden())
                self.assertEqual(status.text(), expected)
                self.assertEqual(status.accessibleName(), status.text())

    def test_failed_disable_stays_visible_and_explains_capture_risk(self):
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import QVBoxLayout, QWidget
        from fronts.desktop.pages.settings import SettingsPage

        class _Controller(QObject):
            screen_protection_state_changed = Signal(str)
            panic_lock_key_captured = Signal(str)

            def __init__(self):
                super().__init__()
                self.emitted = []
                self.returned_state = None
                self.cfg = SimpleNamespace(
                    screen_protection=True, panic_lock_hotkey="",
                    save=lambda: None)
                self.screen_protection_state_changed.connect(
                    self.emitted.append)

            def set_screen_protection(self, enabled):
                self.returned_state = DesktopApp.set_screen_protection(
                    self, enabled)
                return self.returned_state

            def screen_protection_state(self):
                return DesktopApp.screen_protection_state(self)

            def start_panic_key_capture(self):
                pass

            def clear_panic_lock_hotkey(self):
                pass

        class _ProtectionPage(SettingsPage):
            def __init__(self, controller):
                QWidget.__init__(self)
                self.controller = controller
                layout = QVBoxLayout(self)
                layout.addWidget(self._protection_group(controller.cfg))

        refused = win_hardening.DisplayAffinityResult(
            hwnd=123, enabled=False, supported=True,
            succeeded=False, error_code=5)
        controller = _Controller()
        with patch(
                "whisper_core.win_hardening.is_display_affinity_supported",
                return_value=True), patch(
                "whisper_core.win_hardening.set_capture_protection_enabled",
                return_value=(refused,)):
            page = _ProtectionPage(controller)
            page.resize(640, 320)
            page.show()
            self.qapp.processEvents()
            page._screen_protection.click()
            self.qapp.processEvents()
            returned_state = controller.returned_state

        self.addCleanup(page.close)
        self.addCleanup(page.deleteLater)
        status = page._screen_protection_status
        self.assertEqual(returned_state, "failed")
        self.assertEqual(controller.emitted, ["failed"])
        self.assertFalse(controller.cfg.screen_protection)
        self.assertEqual(refused.enabled, False)
        self.assertFalse(refused.succeeded)
        self.assertFalse(status.isHidden(), "відмову при вимкненні приховано")
        # Жорсткий укр. літерал — assertEqual з tr(того самого ключа) не
        # ловить видалений/зламаний ключ set_screen_protection_state_disable_failed
        # (обидва боки порівняння тоді стали б однаково "зламаним" сирим ключем).
        self.assertEqual(
            status.text(),
            "Не вдалося зняти захист екрана. Перезапустіть Балачки перед "
            "записом екрана, бо їхні вікна можуть лишатися прихованими.")
        self.assertEqual(status.accessibleName(), status.text())


class TestClipboardExcludedHonesty(unittest.TestCase):
    """x64 ABI, ownership transfer і чесні error paths без live clipboard."""

    OWNER = 0x4_0000_4444
    HANDLES = [0x1_0000_1111 + i for i in range(4)]
    POINTERS = [0x2_0000_2222 + i for i in range(4)]
    TRANSFERS = [0x3_0000_3333 + i for i in range(4)]

    @staticmethod
    def _call_public(text, owner_hwnd):
        """RED compatibility: стара signature теж доходить до assertion checks."""
        if "owner_hwnd" in inspect.signature(
                win_hardening.set_clipboard_text_excluded).parameters:
            return win_hardening.set_clipboard_text_excluded(text, owner_hwnd)
        return win_hardening.set_clipboard_text_excluded(text)

    def _run(self, *, text="текст", owner_hwnd=OWNER, configure=None):
        user32 = MagicMock()
        kernel32 = MagicMock()
        memmove = MagicMock()
        tracker = MagicMock()

        user32.RegisterClipboardFormatW.side_effect = [101, 102, 103]
        user32.OpenClipboard.return_value = 1
        user32.EmptyClipboard.return_value = 1
        user32.CloseClipboard.return_value = 1
        user32.SetClipboardData.side_effect = list(self.TRANSFERS)
        kernel32.GlobalAlloc.side_effect = list(self.HANDLES)
        kernel32.GlobalLock.side_effect = list(self.POINTERS)
        kernel32.GlobalUnlock.return_value = 0
        kernel32.GetLastError.return_value = 0
        kernel32.GlobalFree.return_value = 0
        if configure:
            configure(user32, kernel32, memmove)

        for name, mock in (
                ("RegisterClipboardFormatW", user32.RegisterClipboardFormatW),
                ("OpenClipboard", user32.OpenClipboard),
                ("EmptyClipboard", user32.EmptyClipboard),
                ("GlobalAlloc", kernel32.GlobalAlloc),
                ("GlobalLock", kernel32.GlobalLock),
                ("memmove", memmove),
                ("GlobalUnlock", kernel32.GlobalUnlock),
                ("GetLastError", kernel32.GetLastError),
                ("SetClipboardData", user32.SetClipboardData),
                ("GlobalFree", kernel32.GlobalFree),
                ("CloseClipboard", user32.CloseClipboard)):
            tracker.attach_mock(mock, name)

        with patch.object(win_hardening, "is_windows", return_value=True), \
             patch.object(win_hardening, "_user32", user32), \
             patch.object(win_hardening, "_kernel32", kernel32), \
             patch.object(win_hardening, "log"), \
             patch("ctypes.memmove", memmove):
            result = self._call_public(text, owner_hwnd)
        return SimpleNamespace(result=result, user32=user32, kernel32=kernel32,
                               memmove=memmove, tracker=tracker)

    @staticmethod
    def _call_names(result):
        return [item[0] for item in result.tracker.mock_calls]

    def test_x64_clipboard_prototypes_are_pointer_safe(self):
        self.assertEqual(ctypes.sizeof(ctypes.c_void_p), 8)
        self.assertEqual(ctypes.sizeof(ctypes.c_long), 4)
        expected = (
            (win_hardening._user32.OpenClipboard,
             (ctypes.wintypes.HWND,), ctypes.wintypes.BOOL),
            (win_hardening._user32.EmptyClipboard,
             (), ctypes.wintypes.BOOL),
            (win_hardening._user32.CloseClipboard,
             (), ctypes.wintypes.BOOL),
            (win_hardening._user32.RegisterClipboardFormatW,
             (ctypes.wintypes.LPCWSTR,), ctypes.wintypes.UINT),
            (win_hardening._user32.SetClipboardData,
             (ctypes.wintypes.UINT, ctypes.wintypes.HANDLE), ctypes.wintypes.HANDLE),
            (win_hardening._kernel32.GlobalAlloc,
             (ctypes.wintypes.UINT, ctypes.c_size_t), ctypes.wintypes.HGLOBAL),
            (win_hardening._kernel32.GlobalLock,
             (ctypes.wintypes.HGLOBAL,), ctypes.wintypes.LPVOID),
            (win_hardening._kernel32.GlobalUnlock,
             (ctypes.wintypes.HGLOBAL,), ctypes.wintypes.BOOL),
            (win_hardening._kernel32.GlobalFree,
             (ctypes.wintypes.HGLOBAL,), ctypes.wintypes.HGLOBAL),
            (win_hardening._kernel32.GetLastError,
             (), ctypes.wintypes.DWORD),
        )
        for function, argtypes, restype in expected:
            with self.subTest(function=function.__name__):
                self.assertEqual(function.argtypes, argtypes)
                self.assertIs(function.restype, restype)

    def test_high_bit_handles_pointers_and_owner_survive(self):
        r = self._run()
        self.assertTrue(r.result)
        owner_arg = r.user32.OpenClipboard.call_args.args[0]
        self.assertIsNotNone(owner_arg)
        self.assertEqual(owner_arg.value, self.OWNER)
        self.assertEqual([c.args[0] for c in r.kernel32.GlobalLock.call_args_list],
                         self.HANDLES)
        self.assertEqual([c.args[0] for c in r.memmove.call_args_list], self.POINTERS)
        self.assertEqual([c.args[0] for c in r.kernel32.GlobalUnlock.call_args_list],
                         self.HANDLES)
        self.assertEqual([c.args[1] for c in r.user32.SetClipboardData.call_args_list],
                         self.HANDLES)

    def test_owner_zero_rejected_before_winapi(self):
        r = self._run(owner_hwnd=0)
        self.assertFalse(r.result)
        self.assertEqual(r.tracker.mock_calls, [])

    def test_primary_marker_registration_failure_does_not_open(self):
        def configure(user32, _kernel32, _memmove):
            user32.RegisterClipboardFormatW.side_effect = [0, 102, 103]

        r = self._run(configure=configure)
        self.assertFalse(r.result)
        r.user32.OpenClipboard.assert_not_called()

    def test_contention_does_not_mutate_or_retry(self):
        def configure(user32, _kernel32, _memmove):
            user32.OpenClipboard.return_value = 0

        r = self._run(configure=configure)
        self.assertFalse(r.result)
        r.user32.OpenClipboard.assert_called_once()
        owner_arg = r.user32.OpenClipboard.call_args.args[0]
        self.assertIsNotNone(owner_arg)
        self.assertEqual(owner_arg.value, self.OWNER)
        r.user32.EmptyClipboard.assert_not_called()
        r.kernel32.GlobalAlloc.assert_not_called()
        r.user32.SetClipboardData.assert_not_called()
        r.user32.CloseClipboard.assert_not_called()

    def test_emptyclipboard_failure_closes_once_without_allocating(self):
        def configure(user32, _kernel32, _memmove):
            user32.EmptyClipboard.return_value = 0

        r = self._run(configure=configure)
        self.assertFalse(r.result)
        r.kernel32.GlobalAlloc.assert_not_called()
        r.user32.CloseClipboard.assert_called_once_with()

    def test_globalalloc_failure_does_not_free_null(self):
        def configure(_user32, kernel32, _memmove):
            kernel32.GlobalAlloc.side_effect = [0]

        r = self._run(configure=configure)
        self.assertFalse(r.result)
        r.kernel32.GlobalLock.assert_not_called()
        r.kernel32.GlobalFree.assert_not_called()
        r.user32.CloseClipboard.assert_called_once_with()

    def test_globallock_failure_frees_exact_handle_once(self):
        def configure(_user32, kernel32, _memmove):
            kernel32.GlobalLock.side_effect = [0]

        r = self._run(configure=configure)
        self.assertFalse(r.result)
        r.kernel32.GlobalFree.assert_called_once_with(self.HANDLES[0])
        r.kernel32.GlobalUnlock.assert_not_called()
        r.user32.SetClipboardData.assert_not_called()
        r.user32.CloseClipboard.assert_called_once_with()

    def test_unlock_zero_no_error_is_success_for_all_handles(self):
        r = self._run()
        self.assertTrue(r.result)
        self.assertEqual(r.kernel32.GlobalUnlock.call_count, 4)
        self.assertEqual(r.kernel32.GetLastError.call_count, 4)
        self.assertEqual(r.user32.SetClipboardData.call_count, 4)
        r.kernel32.GlobalFree.assert_not_called()
        r.user32.CloseClipboard.assert_called_once_with()
        calls = r.tracker.mock_calls
        unlock_indexes = [i for i, item in enumerate(calls)
                          if str(item).startswith("call.GlobalUnlock(")]
        for index in unlock_indexes:
            self.assertEqual(calls[index + 1], call.GetLastError())
        per_handle = ["GlobalAlloc", "GlobalLock", "memmove", "GlobalUnlock",
                      "GetLastError", "SetClipboardData"]
        self.assertEqual(
            self._call_names(r),
            ["RegisterClipboardFormatW"] * 3 + ["OpenClipboard", "EmptyClipboard"] +
            per_handle * 4 + ["CloseClipboard"])

    def test_unlock_zero_nonzero_error_blocks_transfer_and_frees(self):
        def configure(_user32, kernel32, _memmove):
            kernel32.GetLastError.return_value = 158

        r = self._run(configure=configure)
        self.assertFalse(r.result)
        r.kernel32.GlobalUnlock.assert_called_once_with(self.HANDLES[0])
        r.kernel32.GetLastError.assert_called_once_with()
        r.user32.SetClipboardData.assert_not_called()
        r.kernel32.GlobalFree.assert_called_once_with(self.HANDLES[0])
        calls = r.tracker.mock_calls
        unlock_index = calls.index(call.GlobalUnlock(self.HANDLES[0]))
        self.assertEqual(calls[unlock_index + 1], call.GetLastError())
        self.assertEqual(
            self._call_names(r),
            ["RegisterClipboardFormatW"] * 3 + ["OpenClipboard", "EmptyClipboard",
             "GlobalAlloc", "GlobalLock", "memmove", "GlobalUnlock",
             "GetLastError", "GlobalFree", "CloseClipboard"])

    def test_unlock_nonzero_blocks_transfer_without_reading_last_error(self):
        def configure(_user32, kernel32, _memmove):
            kernel32.GlobalUnlock.return_value = 1

        r = self._run(configure=configure)
        self.assertFalse(r.result)
        r.kernel32.GetLastError.assert_not_called()
        r.user32.SetClipboardData.assert_not_called()
        r.kernel32.GlobalFree.assert_called_once_with(self.HANDLES[0])

    def test_memmove_exception_unlocks_then_frees_without_transfer(self):
        def configure(_user32, _kernel32, memmove):
            memmove.side_effect = RuntimeError("write failed")

        r = self._run(configure=configure)
        self.assertFalse(r.result)
        r.kernel32.GlobalUnlock.assert_called_once_with(self.HANDLES[0])
        r.kernel32.GetLastError.assert_called_once_with()
        r.kernel32.GlobalFree.assert_called_once_with(self.HANDLES[0])
        r.user32.SetClipboardData.assert_not_called()

    def test_failed_transfer_frees_exact_untransferred_handle(self):
        def configure(user32, _kernel32, _memmove):
            user32.SetClipboardData.side_effect = [0]

        r = self._run(configure=configure)
        self.assertFalse(r.result)
        r.user32.SetClipboardData.assert_called_once_with(
            win_hardening.CF_UNICODETEXT, self.HANDLES[0])
        r.kernel32.GlobalFree.assert_called_once_with(self.HANDLES[0])
        r.user32.CloseClipboard.assert_called_once_with()

    def test_failed_primary_marker_keeps_text_transferred_but_returns_false(self):
        def configure(user32, _kernel32, _memmove):
            user32.SetClipboardData.side_effect = [self.TRANSFERS[0], 0,
                                                    self.TRANSFERS[2],
                                                    self.TRANSFERS[3]]

        r = self._run(configure=configure)
        self.assertFalse(r.result)
        r.kernel32.GlobalFree.assert_called_once_with(self.HANDLES[1])
        self.assertNotIn(call.GlobalFree(self.HANDLES[0]), r.tracker.mock_calls)

    def test_later_allocation_failures_never_free_transferred_handles(self):
        for failed_index in (1, 2, 3):
            with self.subTest(failed_index=failed_index):
                handles = list(self.HANDLES)
                handles[failed_index] = 0

                def configure(_user32, kernel32, _memmove):
                    kernel32.GlobalAlloc.side_effect = handles

                r = self._run(configure=configure)
                expected = failed_index != 1
                self.assertEqual(r.result, expected)
                r.kernel32.GlobalFree.assert_not_called()
                transferred = self.HANDLES[:failed_index]
                actual = [item.args[1]
                          for item in r.user32.SetClipboardData.call_args_list]
                for handle in transferred:
                    self.assertIn(handle, actual)

    def test_globalfree_failure_reads_last_error_immediately_without_retry(self):
        def configure(_user32, kernel32, _memmove):
            kernel32.GlobalLock.side_effect = [0]
            kernel32.GlobalFree.return_value = self.HANDLES[0]
            kernel32.GetLastError.return_value = 6

        r = self._run(configure=configure)
        self.assertFalse(r.result)
        r.kernel32.GlobalFree.assert_called_once_with(self.HANDLES[0])
        r.kernel32.GetLastError.assert_called_once_with()
        calls = r.tracker.mock_calls
        free_index = calls.index(call.GlobalFree(self.HANDLES[0]))
        self.assertEqual(calls[free_index + 1], call.GetLastError())

    def test_close_failure_returns_false_without_freeing_transferred_handles(self):
        def configure(user32, kernel32, _memmove):
            user32.CloseClipboard.return_value = 0
            kernel32.GetLastError.side_effect = [0, 0, 0, 0, 5]

        r = self._run(configure=configure)
        self.assertFalse(r.result)
        self.assertEqual(r.user32.SetClipboardData.call_count, 4)
        r.kernel32.GlobalFree.assert_not_called()
        r.user32.CloseClipboard.assert_called_once_with()
        self.assertEqual(r.kernel32.GetLastError.call_count, 5)
        calls = r.tracker.mock_calls
        close_index = calls.index(call.CloseClipboard())
        self.assertEqual(calls[close_index + 1], call.GetLastError())

    def test_success_never_frees_transferred_handles(self):
        r = self._run()
        self.assertTrue(r.result)
        self.assertEqual(r.user32.SetClipboardData.call_count, 4)
        r.kernel32.GlobalFree.assert_not_called()
        r.user32.CloseClipboard.assert_called_once_with()

    def test_unicode_empty_and_large_payloads_are_exact_utf16le(self):
        texts = ("Україна — їжак 👋 𐐷", "", "А" * 40_000)
        for text in texts:
            with self.subTest(length=len(text)):
                r = self._run(text=text)
                expected = text.encode("utf-16-le") + b"\x00\x00"
                first_alloc = r.kernel32.GlobalAlloc.call_args_list[0]
                first_move = r.memmove.call_args_list[0]
                self.assertGreater(len(expected), 65_536 if len(text) > 1000 else 0)
                self.assertEqual(first_alloc.args, (win_hardening.GMEM_MOVEABLE,
                                                    len(expected)))
                self.assertEqual(first_move.args,
                                 (self.POINTERS[0], expected, len(expected)))
                self.assertEqual(r.memmove.call_args_list[2].args[1], b"\x00" * 4)
                self.assertEqual(r.memmove.call_args_list[2].args[2], 4)
                self.assertEqual(r.memmove.call_args_list[3].args[1], b"\x00" * 4)
                self.assertEqual(r.memmove.call_args_list[3].args[2], 4)
                owner_arg = r.user32.OpenClipboard.call_args.args[0]
                self.assertIsNotNone(owner_arg)
                self.assertEqual(owner_arg.value, self.OWNER)


class TestPanicLockReal(unittest.TestCase):
    """Блокер рецензії 1: реальний DesktopApp.trigger_panic_lock знищує РОЗШИФРОВАНІ
    temp-копії нарад (не лише мінімізує вікно). Перевіряємо ФАКТИ."""

    def setUp(self):
        # tempfile.mkdtemp/TemporaryDirectory отримують deny-ACL у деяких пісочницях.
        # Звичайна унікальна папка під writable worktree лишається доступною і на
        # хості, тож тестуємо файлові факти без sandbox-specific пропусків.
        self.temp_root = Path.cwd() / f".post49-panic-{uuid.uuid4().hex}"
        self.temp_root.mkdir()
        self.temp_patcher = patch(
            "fronts.desktop.app.tempfile.gettempdir",
            return_value=str(self.temp_root),
        )
        self.temp_patcher.start()

    def tearDown(self):
        self.temp_patcher.stop()
        storage_crypto._PASSWORD_CACHE.clear()
        shutil.rmtree(self.temp_root, ignore_errors=True)

    @staticmethod
    def _panic_app():
        return SimpleNamespace(
            _clear_meeting_plain_cache=MagicMock(return_value=True),
            window=MagicMock(),
            tray=SimpleNamespace(notify=MagicMock()),
        )

    def _wait_for_restore_generation_change(self, paste, generation):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with paste._restore_lock:
                if paste._restore_generation != generation:
                    return
            time.sleep(0.001)
        self.fail("panic не інвалідував покоління clipboard restore")

    def test_panic_prevents_pending_timer_from_restoring_secret(self):
        from fronts.desktop import paste

        clipboard = {"text": "SECRET-BEFORE-DICTATION"}
        timers = []
        snapshot_read = threading.Event()
        release_snapshot = threading.Event()
        callback_errors = []
        callback = None

        class ManualTimer:
            def __init__(self, _delay, fn):
                self.fn = fn
                self.cancelled = False

            def start(self):
                timers.append(self)

            def cancel(self):
                self.cancelled = True

            def fire(self):
                try:
                    self.fn()
                except BaseException as exc:
                    callback_errors.append(exc)

        def read_clipboard():
            current = clipboard["text"]
            if threading.current_thread() is callback:
                snapshot_read.set()
                if not release_snapshot.wait(1):
                    raise AssertionError("clipboard snapshot не відпустили")
            return current

        def clear_clipboard():
            clipboard["text"] = ""
            return True

        paste.cancel_clipboard_restore()
        try:
            with patch.object(paste.threading, "Timer", ManualTimer), \
                    patch.object(paste.pyperclip, "paste",
                                 side_effect=read_clipboard), \
                    patch.object(paste.pyperclip, "copy",
                                 side_effect=lambda text:
                                 clipboard.update(text=text)), \
                    patch("fronts.desktop.app._cleanup_panic_plaintext_temps",
                          return_value=True), \
                    patch("whisper_core.win_hardening.clear_clipboard",
                          side_effect=clear_clipboard):
                previous = paste.begin_clipboard_restore()
                clipboard["text"] = "DICTATION"
                paste.restore_clipboard(previous, expected="DICTATION")

                callback = threading.Thread(target=timers[0].fire)
                callback.start()
                self.assertTrue(snapshot_read.wait(1))
                failures = DesktopApp.trigger_panic_lock(self._panic_app())
                release_snapshot.set()
                callback.join(1)

            self.assertEqual(failures, ())
            self.assertFalse(callback.is_alive())
            self.assertEqual(callback_errors, [])
            self.assertEqual(clipboard["text"], "")
            self.assertIsNone(paste._session_original)
            self.assertEqual(paste._active_restore_operations, 0)
        finally:
            release_snapshot.set()
            paste.cancel_clipboard_restore()

    def test_panic_invalidates_callback_already_holding_original(self):
        from fronts.desktop import paste

        clipboard = {"text": "DICTATION"}
        timers = []
        check_started = threading.Event()
        release_check = threading.Event()
        clear_called = threading.Event()
        copy_calls = []
        callback_errors = []
        panic_result = []

        class ManualTimer:
            def __init__(self, _delay, fn):
                self.fn = fn

            def start(self):
                timers.append(self)

            def cancel(self):
                pass

            def fire(self):
                try:
                    self.fn()
                except BaseException as exc:
                    callback_errors.append(exc)

        real_check = getattr(
            paste, "_restore_copy_is_current",
            lambda _timer, _generation: True,
        )

        def gated_check(timer, generation):
            check_started.set()
            if not release_check.wait(1):
                raise AssertionError("generation check не відпустили")
            return real_check(timer, generation)

        def copy_clipboard(text):
            copy_calls.append(text)
            clipboard["text"] = text
            return True

        def clear_clipboard():
            clear_called.set()
            clipboard["text"] = ""
            return True

        app = self._panic_app()
        paste.cancel_clipboard_restore()
        try:
            with patch.object(paste.threading, "Timer", ManualTimer), \
                    patch.object(paste.pyperclip, "paste",
                                 side_effect=lambda: clipboard["text"]), \
                    patch.object(paste, "_safe_copy",
                                 side_effect=copy_clipboard), \
                    patch.object(paste, "_restore_copy_is_current",
                                 side_effect=gated_check, create=True), \
                    patch("fronts.desktop.app._cleanup_panic_plaintext_temps",
                          return_value=True), \
                    patch("whisper_core.win_hardening.clear_clipboard",
                          side_effect=clear_clipboard):
                paste.restore_clipboard(
                    "SECRET-BEFORE-DICTATION", expected="DICTATION")
                generation = getattr(paste, "_restore_generation", 0)
                callback = threading.Thread(target=timers[0].fire)
                callback.start()
                self.assertTrue(
                    check_started.wait(1),
                    "callback не дійшов до generation check перед _safe_copy",
                )

                panic = threading.Thread(
                    target=lambda: panic_result.append(
                        DesktopApp.trigger_panic_lock(app)))
                panic.start()
                self._wait_for_restore_generation_change(paste, generation)
                self.assertFalse(
                    clear_called.is_set(),
                    "panic не має чистити буфер до завершення callback",
                )

                release_check.set()
                callback.join(1)
                panic.join(1)

            self.assertFalse(callback.is_alive())
            self.assertFalse(panic.is_alive())
            self.assertEqual(callback_errors, [])
            self.assertEqual(panic_result, [()])
            self.assertEqual(copy_calls, [])
            self.assertEqual(clipboard["text"], "")
        finally:
            release_check.set()
            paste.cancel_clipboard_restore()

    def test_panic_waits_for_copy_in_flight_before_clearing(self):
        from fronts.desktop import paste

        clipboard = {"text": "DICTATION"}
        timers = []
        copy_started = threading.Event()
        release_copy = threading.Event()
        clear_called = threading.Event()
        panic_result = []
        begin_started = threading.Event()
        begin_finished = threading.Event()
        begin_result = []

        class ManualTimer:
            def __init__(self, _delay, fn):
                self.fn = fn

            def start(self):
                timers.append(self)

            def cancel(self):
                pass

            def fire(self):
                self.fn()

        def blocked_copy(text):
            copy_started.set()
            if not release_copy.wait(1):
                raise AssertionError("clipboard copy не відпустили")
            clipboard["text"] = text
            return True

        def clear_clipboard():
            clear_called.set()
            clipboard["text"] = ""
            return True

        paste.cancel_clipboard_restore()
        try:
            with patch.object(paste.threading, "Timer", ManualTimer), \
                    patch.object(paste.pyperclip, "paste",
                                 side_effect=lambda: clipboard["text"]), \
                    patch.object(paste, "_safe_copy",
                                 side_effect=blocked_copy), \
                    patch("fronts.desktop.app._cleanup_panic_plaintext_temps",
                          return_value=True), \
                    patch("whisper_core.win_hardening.clear_clipboard",
                          side_effect=clear_clipboard):
                paste.restore_clipboard(
                    "SECRET-BEFORE-DICTATION", expected="DICTATION")
                generation = getattr(paste, "_restore_generation", 0)
                callback = threading.Thread(target=timers[0].fire)
                callback.start()
                self.assertTrue(copy_started.wait(1))

                panic = threading.Thread(
                    target=lambda: panic_result.append(
                        DesktopApp.trigger_panic_lock(self._panic_app())))
                panic.start()
                self._wait_for_restore_generation_change(paste, generation)
                self.assertFalse(
                    clear_called.is_set(),
                    "panic не має чистити буфер, поки _safe_copy у польоті",
                )

                def begin_during_panic():
                    begin_started.set()
                    begin_result.append(paste.begin_clipboard_restore())
                    begin_finished.set()

                begin = threading.Thread(target=begin_during_panic)
                begin.start()
                self.assertTrue(begin_started.wait(1))
                self.assertFalse(
                    begin_finished.wait(0.05),
                    "нова restore-сесія не має стартувати всередині panic barrier",
                )

                release_copy.set()
                callback.join(1)
                panic.join(1)
                begin.join(1)

            self.assertFalse(callback.is_alive())
            self.assertFalse(panic.is_alive())
            self.assertFalse(begin.is_alive())
            self.assertEqual(panic_result, [()])
            self.assertEqual(begin_result, [""])
            self.assertEqual(clipboard["text"], "")
        finally:
            release_copy.set()
            paste.cancel_clipboard_restore()

    def test_panic_reports_partial_failure_when_restore_barrier_times_out(self):
        from fronts.desktop import paste
        from fronts.desktop.i18n import tr

        clipboard = {"text": "DICTATION"}
        timers = []
        copy_started = threading.Event()
        release_copy = threading.Event()
        panic_finished = threading.Event()
        panic_result = []
        clear_calls = []

        class ManualTimer:
            def __init__(self, _delay, fn):
                self.fn = fn

            def start(self):
                timers.append(self)

            def cancel(self):
                pass

            def fire(self):
                self.fn()

        def blocked_copy(text):
            copy_started.set()
            release_copy.wait(2)
            clipboard["text"] = text
            return True

        def clear_clipboard():
            clear_calls.append(True)
            clipboard["text"] = ""
            return True

        app = self._panic_app()
        paste.cancel_clipboard_restore()
        try:
            with patch.object(paste.threading, "Timer", ManualTimer), \
                    patch.object(paste.pyperclip, "paste",
                                 side_effect=lambda: clipboard["text"]), \
                    patch.object(paste, "_safe_copy",
                                 side_effect=blocked_copy), \
                    patch("fronts.desktop.app._cleanup_panic_plaintext_temps",
                          return_value=True), \
                    patch("whisper_core.win_hardening.clear_clipboard",
                          side_effect=clear_clipboard):
                paste.restore_clipboard(
                    "SECRET-BEFORE-DICTATION", expected="DICTATION")
                callback = threading.Thread(target=timers[0].fire)
                callback.start()
                self.assertTrue(copy_started.wait(1))

                def run_panic():
                    panic_result.append(DesktopApp.trigger_panic_lock(app))
                    panic_finished.set()

                panic = threading.Thread(target=run_panic)
                panic.start()
                self.assertTrue(
                    panic_finished.wait(1),
                    "panic має завершитися після короткого barrier timeout",
                )

                failures = panic_result[0]
                notice = app.tray.notify.call_args.args[0]
                self.assertIn("panic_step_clipboard", failures)
                # Жорсткий укр. літерал — assertIn з tr(того самого ключа) не
                # ловить видалений/зламаний ключ panic_step_clipboard.
                self.assertIn("очистити буфер обміну", notice)
                self.assertNotEqual(notice, tr("panic_toast_locked"))
                self.assertEqual(
                    clear_calls, [],
                    "після barrier timeout не можна чистити перед in-flight copy",
                )

                release_copy.set()
                callback.join(1)
                panic.join(1)
        finally:
            release_copy.set()
            paste.cancel_clipboard_restore()

    def test_overlapping_panics_keep_new_restore_blocked_until_both_finish(self):
        from fronts.desktop import paste

        clipboard = {"text": ""}
        first_clear_started = threading.Event()
        second_clear_started = threading.Event()
        release_first = threading.Event()
        release_second = threading.Event()
        begin_started = threading.Event()
        begin_finished = threading.Event()
        panic_results = []
        begin_result = []

        def first_clear():
            first_clear_started.set()
            if not release_first.wait(1):
                raise AssertionError("перший panic clear не відпустили")
            clipboard["text"] = ""
            return True

        def second_clear():
            second_clear_started.set()
            if not release_second.wait(1):
                raise AssertionError("другий panic clear не відпустили")
            clipboard["text"] = ""
            return True

        def begin_restore():
            begin_started.set()
            begin_result.append(paste.begin_clipboard_restore())
            begin_finished.set()

        paste.cancel_clipboard_restore()
        try:
            with patch.object(paste.pyperclip, "paste",
                              side_effect=lambda: clipboard["text"]):
                first = threading.Thread(
                    target=lambda: panic_results.append(
                        paste.panic_clear_clipboard(first_clear)))
                second = threading.Thread(
                    target=lambda: panic_results.append(
                        paste.panic_clear_clipboard(second_clear)))
                first.start()
                self.assertTrue(first_clear_started.wait(1))
                second.start()
                self.assertTrue(second_clear_started.wait(1))

                release_first.set()
                first.join(1)
                self.assertFalse(first.is_alive())

                begin = threading.Thread(target=begin_restore)
                begin.start()
                self.assertTrue(begin_started.wait(1))
                self.assertFalse(
                    begin_finished.wait(0.05),
                    "перший panic не має відкривати restore під другим panic",
                )

                release_second.set()
                second.join(1)
                begin.join(1)

            self.assertFalse(second.is_alive())
            self.assertFalse(begin.is_alive())
            self.assertEqual(panic_results, [True, True])
            self.assertEqual(begin_result, [""])
        finally:
            release_first.set()
            release_second.set()
            paste.cancel_clipboard_restore()

    def test_panic_destroys_plaintext_keys_and_minimizes(self):
        # 1. Реальна temp-тека з «розшифрованою» нарадою у плейн-кеші.
        plain_dir = self.temp_root / "active-meeting"
        plain_dir.mkdir()
        owner = SimpleNamespace(cleanup=lambda: shutil.rmtree(plain_dir))
        self.assertTrue(os.path.isdir(plain_dir))
        # 2. Кеш ключів населений.
        storage_crypto._PASSWORD_CACHE["fake_root"] = b"0" * 32

        app = SimpleNamespace()
        app._meeting_plain_cache = {"sess1": (owner, str(plain_dir))}
        # Реальний _clear_meeting_plain_cache (unbound) — саме його має покликати panic.
        app._clear_meeting_plain_cache = (
            lambda sid=None: DesktopApp._clear_meeting_plain_cache(app, sid))
        app.window = MagicMock()
        notices = []
        app.tray = SimpleNamespace(notify=notices.append)

        with patch("whisper_core.win_hardening.clear_clipboard") as clip:
            clip.return_value = True
            failures = DesktopApp.trigger_panic_lock(app)

        # ФАКТИ, не мок-послідовність:
        self.assertFalse(os.path.exists(plain_dir),
                         "розшифрована temp-копія наради має бути знищена panic-lock")
        self.assertEqual(app._meeting_plain_cache, {})
        self.assertEqual(len(storage_crypto._PASSWORD_CACHE), 0,
                         "кеш ключів має бути порожній")
        app.window.showMinimized.assert_called_once()
        clip.assert_called_once()
        self.assertEqual(len(notices), 1)
        self.assertEqual(failures, ())

    def test_panic_step_failure_is_reported_instead_of_full_success(self):
        from fronts.desktop.i18n import tr

        app = SimpleNamespace(
            _clear_meeting_plain_cache=MagicMock(side_effect=OSError("locked")),
            window=MagicMock(),
            tray=SimpleNamespace(notify=MagicMock()),
        )

        with self.assertLogs(level="ERROR") as logs, \
                patch("whisper_core.win_hardening.clear_clipboard", return_value=True):
            failures = DesktopApp.trigger_panic_lock(app)

        notice = app.tray.notify.call_args.args[0]
        self.assertIn("panic_step_meeting_cache", "\n".join(logs.output))
        self.assertIn("panic_step_meeting_cache", failures)
        self.assertIn(tr("panic_step_meeting_cache"), notice)
        self.assertNotEqual(notice, tr("panic_toast_locked"))

    def test_panic_reports_clipboard_false_without_exception(self):
        from fronts.desktop.i18n import tr

        app = SimpleNamespace(
            _clear_meeting_plain_cache=MagicMock(return_value=True),
            window=MagicMock(),
            tray=SimpleNamespace(notify=MagicMock()),
        )

        with patch("whisper_core.win_hardening.clear_clipboard", return_value=False):
            failures = DesktopApp.trigger_panic_lock(app)

        notice = app.tray.notify.call_args.args[0]
        self.assertIn("panic_step_clipboard", failures)
        self.assertIn(tr("panic_step_clipboard"), notice)
        self.assertNotEqual(notice, tr("panic_toast_locked"))

    def test_panic_removes_all_plaintext_temp_patterns(self):
        targets = (
            self.temp_root / "balachky-meeting-test.tmp",
            self.temp_root / "balachky-meeting-media-test",
            self.temp_root / "balachky-tts-plain-test",
        )
        targets[0].write_text("plain", encoding="utf-8")
        targets[1].mkdir()
        (targets[1] / "audio.wav").write_bytes(b"plain")
        targets[2].mkdir()
        (targets[2] / "speech.wav").write_bytes(b"plain")
        app = SimpleNamespace(
            _clear_meeting_plain_cache=MagicMock(return_value=True),
            window=MagicMock(),
            tray=SimpleNamespace(notify=MagicMock()),
        )

        with patch("whisper_core.win_hardening.clear_clipboard", return_value=True):
            failures = DesktopApp.trigger_panic_lock(app)

        self.assertEqual(failures, ())
        self.assertTrue(all(not target.exists() for target in targets))

    def test_panic_reports_temp_target_that_stays_locked(self):
        from fronts.desktop.i18n import tr

        locked = self.temp_root / "balachky-tts-plain-locked.wav"
        locked.write_bytes(b"plain")
        real_unlink = Path.unlink
        real_exists = Path.exists

        def deny_locked(path, *args, **kwargs):
            if path == locked:
                raise PermissionError("file is locked")
            return real_unlink(path, *args, **kwargs)

        def hide_locked(path):
            if path == locked:
                return False
            return real_exists(path)

        app = SimpleNamespace(
            _clear_meeting_plain_cache=MagicMock(return_value=True),
            window=MagicMock(),
            tray=SimpleNamespace(notify=MagicMock()),
        )
        with self.assertLogs(level="WARNING") as logs, \
                patch.object(Path, "unlink", deny_locked), \
                patch.object(Path, "exists", hide_locked), \
                patch("whisper_core.win_hardening.clear_clipboard", return_value=True):
            failures = DesktopApp.trigger_panic_lock(app)

        notice = app.tray.notify.call_args.args[0]
        self.assertIn("balachky-tts-plain-locked.wav", "\n".join(logs.output))
        self.assertTrue(locked.exists())
        self.assertIn("panic_step_temp_files", failures)
        self.assertIn(tr("panic_step_temp_files"), notice)
        self.assertNotEqual(notice, tr("panic_toast_locked"))


class TestHotkeyMatrixSymmetry(unittest.TestCase):
    """Блокер рецензії 3: конфлікт-матриця хоткеїв симетрична НА РЕАЛЬНОМУ коді —
    для кожної впорядкованої пари (сетер, інший ключ) зайнята «інша» комбінація
    відхиляє сетер. Прибирання будь-якого ключа з будь-якого taken-списку (напр.
    panic з set_note) валить відповідну ітерацію."""

    _KEYS = ("ptt_key", "undo_paste_key", "insert_last_key", "note_hotkey",
             "command_edit_hotkey", "meeting_bookmark_hotkey", "panic_lock_hotkey")
    _COMBO = "ctrl+alt+shift+k"

    def _app(self, **fields):
        defaults = {k: "" for k in self._KEYS}
        defaults["ptt_key"] = "ctrl+shift+space"    # дефолт PTT
        defaults.update(fields)
        app = SimpleNamespace()
        app.cfg = SimpleNamespace(**defaults, save=lambda: None)
        app.tray = _FakeTray()
        return app

    def _call_setter(self, app, key, combo):
        """Викликати РЕАЛЬНИЙ сетер для config-ключа (конфлікт-шлях повертає
        рано, тож фейку досить cfg+tray)."""
        if key == "ptt_key":
            return DesktopApp._apply_key(app, combo)
        if key == "undo_paste_key":
            return DesktopApp.set_action_hotkey(app, "undo", combo)
        if key == "insert_last_key":
            return DesktopApp.set_action_hotkey(app, "insert", combo)
        if key == "note_hotkey":
            return DesktopApp.set_note_hotkey(app, combo)
        if key == "command_edit_hotkey":
            return DesktopApp.set_command_edit_hotkey(app, combo)
        if key == "meeting_bookmark_hotkey":
            return DesktopApp.set_meeting_bookmark_hotkey(app, combo)
        if key == "panic_lock_hotkey":
            return DesktopApp.set_panic_lock_hotkey(app, combo)
        raise AssertionError(key)

    def test_full_conflict_matrix_both_directions(self):
        for setter_key in self._KEYS:
            for other_key in self._KEYS:
                if setter_key == other_key:
                    continue
                # Зайняти «інший» ключ комбінацією; сетер має її відхилити.
                app = self._app(**{other_key: self._COMBO})
                res = self._call_setter(app, setter_key, self._COMBO)
                self.assertFalse(
                    res, f"set({setter_key}) мав відхилити збіг із {other_key}")

    def test_note_rejects_panic_combo(self):
        # Пряма мутація-детектор: прибирання panic з taken set_note валить це.
        app = self._app(panic_lock_hotkey=self._COMBO)
        self.assertFalse(DesktopApp.set_note_hotkey(app, self._COMBO))
        self.assertEqual(app.cfg.note_hotkey, "")

    def test_panic_rejects_note_combo(self):
        app = self._app(note_hotkey=self._COMBO)
        self.assertFalse(DesktopApp.set_panic_lock_hotkey(app, self._COMBO))
        self.assertEqual(app.cfg.panic_lock_hotkey, "")

    def test_combos_equal_is_order_insensitive(self):
        self.assertTrue(combos_equal("ctrl+alt+n", "alt+ctrl+n"))
        self.assertFalse(combos_equal("ctrl+alt+n", "ctrl+alt+m"))


class TestWerExclusionOnInit(unittest.TestCase):
    """Блокер рецензії 6: DesktopApp.__init__ реєструє WER-виняток. Мокуємо обгортку
    так, щоб вона кинула сентинел — доводить, що __init__ ДІЙСНО її кличе (а не
    що метод сам по собі працює). Видалення виклику з __init__ → сентинел не
    злетить → тест впаде."""

    def test_init_invokes_exclude_process_from_wer(self):
        from PySide6.QtWidgets import QApplication
        qapp = QApplication.instance() or QApplication([])
        self.addCleanup(qapp.processEvents)
        from fronts.desktop import app as desktop_app

        sentinel = RuntimeError("wer-sentinel")
        calls = {"n": 0}

        def fake_wer(*a, **k):
            calls["n"] += 1
            raise sentinel

        with patch("whisper_core.win_hardening.exclude_process_from_wer", fake_wer), \
             patch.object(desktop_app, "_migrate_update_cache", lambda: None):
            with self.assertRaises(RuntimeError) as ctx:
                desktop_app.DesktopApp(qapp, MagicMock(), cfg=Config())

        self.assertIs(ctx.exception, sentinel, "має злетіти саме наш сентинел")
        self.assertEqual(calls["n"], 1, "__init__ має покликати WER-виняток рівно раз")


class TestVideoDialogCaptureProtection(unittest.TestCase):
    """Блокер рецензії 4: WDA_EXCLUDEFROMCAPTURE на вікно відтворення наради. Показ
    діалога при увімкненому тумблері накладає афінність; вимкнення тумблера знімає
    її з ЖИВОГО вікна. Форсуємо фолбек без QtMultimedia — нативний QMediaPlayer
    тесту не потрібен (і ризикує 0xC000041D при offscreen-деструкції)."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qapp = QApplication.instance() or QApplication([])

    def tearDown(self):
        win_hardening._capture_windows.clear()
        win_hardening._capture_results.clear()
        win_hardening.set_capture_protection_enabled(False)
        self.qapp.processEvents()

    def _dialog(self, parent=None):
        from fronts.desktop import video_player
        with patch.object(video_player, "_HAVE_QTMM", False):
            dlg = video_player.VideoPlayerDialog(path=None, parent=parent)
        return dlg

    def test_wda_refusal_on_video_dialog_emits_failed_state(self):
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import QMainWindow

        class _Controller(QObject):
            screen_protection_state_changed = Signal(str)

            def __init__(self):
                super().__init__()
                self.cfg = SimpleNamespace(screen_protection=True)

            def screen_protection_state(self):
                return DesktopApp.screen_protection_state(self)

            def apply_screen_protection_to_window(self, widget):
                return DesktopApp.apply_screen_protection_to_window(self, widget)

        win_hardening._capture_windows.clear()
        win_hardening._capture_results.clear()
        win_hardening.set_capture_protection_enabled(True)
        controller = _Controller()
        states = []
        controller.screen_protection_state_changed.connect(states.append)
        host = QMainWindow()
        host.controller = controller
        refused = win_hardening.DisplayAffinityResult(
            hwnd=101, enabled=True, supported=True,
            succeeded=False, error_code=5)

        try:
            with patch(
                    "whisper_core.win_hardening.is_display_affinity_supported",
                    return_value=True), patch(
                    "whisper_core.win_hardening.set_window_display_affinity",
                    return_value=refused):
                dlg = self._dialog(parent=host)
                dlg.show()
                self.qapp.processEvents()

                self.assertEqual(controller.screen_protection_state(), "failed")
                self.assertIn(
                    "failed", states,
                    "відмова WDA на відео-вікні має дійти до controller-сигналу")
        finally:
            dlg.close()
            dlg.deleteLater()
            host.close()
            host.deleteLater()
            self.qapp.processEvents()

    def test_close_failed_video_dialog_emits_restored_active_state(self):
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import QMainWindow

        class _Controller(QObject):
            screen_protection_state_changed = Signal(str)

            def __init__(self):
                super().__init__()
                self.cfg = SimpleNamespace(screen_protection=True)

            def screen_protection_state(self):
                return DesktopApp.screen_protection_state(self)

            def apply_screen_protection_to_window(self, widget):
                return DesktopApp.apply_screen_protection_to_window(self, widget)

            def remove_screen_protection_from_window(self, widget):
                return DesktopApp.remove_screen_protection_from_window(
                    self, widget)

        protected = win_hardening.DisplayAffinityResult(
            hwnd=101, enabled=True, supported=True,
            succeeded=True, error_code=None)
        refused = win_hardening.DisplayAffinityResult(
            hwnd=202, enabled=True, supported=True,
            succeeded=False, error_code=5)
        controller = _Controller()
        emitted = []
        controller.screen_protection_state_changed.connect(emitted.append)
        host = QMainWindow()
        host.controller = controller
        dlg = None

        try:
            with patch(
                    "whisper_core.win_hardening.is_display_affinity_supported",
                    return_value=True), patch(
                    "whisper_core.win_hardening.set_window_display_affinity",
                    side_effect=(protected, refused)):
                controller.apply_screen_protection_to_window(host)
                dlg = self._dialog(parent=host)
                dlg.show()
                self.qapp.processEvents()
                self.assertEqual(emitted[-1], "failed")

                dlg.close()
                self.qapp.processEvents()

                self.assertEqual(
                    emitted[-1], "active",
                    "closeEvent має прибрати failed-результат і емітити агрегат")
                self.assertEqual(
                    len(win_hardening.capture_protection_results()), 1)
        finally:
            if dlg is not None:
                dlg.deleteLater()
            host.close()
            host.deleteLater()
            self.qapp.processEvents()

    def test_show_applies_wda_when_toggle_on(self):
        win_hardening.set_capture_protection_enabled(True)
        with patch("whisper_core.win_hardening.set_window_display_affinity") as m:
            dlg = self._dialog()
            dlg.show()
            self.qapp.processEvents()
        try:
            true_calls = [c for c in m.call_args_list
                          if len(c.args) >= 2 and c.args[1] is True]
            self.assertTrue(
                true_calls,
                "показ вікна наради при увімкненому тумблері має накласти WDA=True")
        finally:
            dlg.close()
            dlg.deleteLater()
            self.qapp.processEvents()

    def test_toggle_off_clears_live_dialog(self):
        win_hardening.set_capture_protection_enabled(True)
        dlg = self._dialog()
        dlg.show()
        self.qapp.processEvents()
        with patch("whisper_core.win_hardening.set_window_display_affinity") as m:
            win_hardening.set_capture_protection_enabled(False)
        try:
            false_calls = [c for c in m.call_args_list
                           if len(c.args) >= 2 and c.args[1] is False]
            self.assertTrue(
                false_calls,
                "вимкнення тумблера має зняти WDA з живого вікна наради")
        finally:
            dlg.close()
            dlg.deleteLater()
            self.qapp.processEvents()


class TestConfigDefaults(unittest.TestCase):
    def test_config_defaults(self):
        cfg = Config()
        self.assertFalse(cfg.screen_protection)
        self.assertEqual(cfg.panic_lock_hotkey, "")


class StubSignalsExistOnRealController(unittest.TestCase):
    """Вартовий проти пастки канону §4.1: тест-заглушка _NavController оголошує
    сигнал, а справжній DesktopApp — ні. Тоді тести зелені, а живий застосунок
    падає AttributeError (так загубився panic_lock_key_captured при злитті хвиль).
    Кожен сигнал заглушки має існувати й на реальному контролері."""

    def test_every_stub_signal_exists_on_desktop_app(self):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import Signal
        from fronts.desktop.app import DesktopApp
        from tests.render_nav_smoke import _NavController

        stub_signals = {
            name for name, value in vars(_NavController).items()
            if isinstance(value, Signal)
        }
        self.assertTrue(stub_signals, "заглушка втратила сигнали — вартовий сліпий")
        missing = sorted(n for n in stub_signals if not hasattr(DesktopApp, n))
        self.assertEqual(
            missing, [],
            f"заглушка оголошує сигнали, яких немає у DesktopApp: {missing} — "
            "живий застосунок упаде AttributeError")


if __name__ == "__main__":
    unittest.main()
