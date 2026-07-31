"""Повний ABI-контракт для всіх ctypes-викликів WinAPI."""
import ast
import ctypes
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fronts.desktop import context
from whisper_core.screen import win32 as screen_win32


ROOT = Path(__file__).resolve().parents[1]


EXPECTED_PROTOTYPES = {
    "fronts/desktop/app.py": {
        "SetCurrentProcessExplicitAppUserModelID", "SetProcessDpiAwareness",
    },
    "fronts/desktop/backdrop.py": {
        "DwmExtendFrameIntoClientArea", "DwmSetWindowAttribute",
    },
    "fronts/desktop/context.py": {
        "CloseHandle", "EnumChildWindows", "GetClassNameW",
        "GetForegroundWindow", "GetWindowTextLengthW", "GetWindowTextW",
        "GetWindowThreadProcessId", "OpenProcess",
        "QueryFullProcessImageNameW", "SendInput",
    },
    "fronts/desktop/hotkeys_native.py": {
        "GetAsyncKeyState", "GetCurrentThreadId", "GetMessageW",
        "PeekMessageW", "PostThreadMessageW", "RegisterHotKey",
        "UnregisterHotKey",
    },
    "fronts/desktop/main_window.py": {"DwmSetWindowAttribute"},
    "fronts/desktop/motion.py": {"SystemParametersInfoW"},
    "fronts/desktop/mousehook.py": {
        "CallNextHookEx", "DispatchMessageW", "GetCurrentThreadId",
        "GetMessageW", "GetModuleHandleW", "PeekMessageW",
        "PostThreadMessageW", "SetWindowsHookExW", "TranslateMessage",
        "UnhookWindowsHookEx",
    },
    "fronts/desktop/report.py": {"GlobalMemoryStatusEx"},
    "fronts/desktop/wininput.py": {
        "CloseHandle", "GetClassNameW", "GetForegroundWindow",
        "GetWindowTextW", "GetWindowThreadProcessId", "OpenProcess",
        "QueryFullProcessImageNameW", "SendInput", "SetForegroundWindow",
    },
    "scripts/screenshots.py": {
        "DwmGetWindowAttribute", "SetProcessDpiAwareness",
    },
    "scripts/shot_chip_popovers.py": {"SetProcessDpiAwareness"},
    "scripts/shot_focus_ring.py": {"SetProcessDpiAwareness"},
    "scripts/visual_gate.py": {"SetProcessDpiAwareness"},
    "tests/test_native_hotkeys.py": {
        "PostThreadMessageW", "RegisterHotKey", "UnregisterHotKey",
    },
    "whisper_core/meeting/capture.py": {
        "CLSIDFromString", "CoCreateInstance", "CoInitializeEx",
        "CoUninitialize", "PropVariantClear",
    },
    "whisper_core/meeting/storage_crypto.py": {
        "CryptProtectData", "CryptUnprotectData", "LocalFree",
    },
    "whisper_core/offline_package.py": {"GetVolumeInformationW"},
    "whisper_core/screen/win32.py": {
        "CreateCompatibleBitmap", "CreateCompatibleDC", "DeleteDC",
        "DeleteObject", "EnumWindows", "GetBitmapBits", "GetWindow",
        "GetWindowDC", "GetWindowLongW", "GetWindowRect",
        "GetWindowTextLengthW", "GetWindowTextW", "IsWindowVisible",
        "PrintWindow", "ReleaseDC", "SelectObject",
    },
    "whisper_core/win_hardening.py": {
        "CloseClipboard", "EmptyClipboard", "GetLastError", "GlobalAlloc",
        "GlobalFree", "GlobalLock", "GlobalUnlock", "OpenClipboard",
        "RegisterClipboardFormatW", "SetClipboardData",
        "SetWindowDisplayAffinity", "WerAddExcludedApplication",
    },
}


def _declared_prototype_attributes(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    declared = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if not isinstance(target, ast.Attribute):
                continue
            if target.attr not in {"argtypes", "restype"}:
                continue
            function = target.value
            if isinstance(function, ast.Attribute):
                declared.setdefault(function.attr, set()).add(target.attr)
    return declared


class PrototypeCoverageTests(unittest.TestCase):
    def test_every_called_winapi_export_declares_argtypes_and_restype(self):
        for relative, functions in EXPECTED_PROTOTYPES.items():
            declared = _declared_prototype_attributes(ROOT / relative)
            for function in sorted(functions):
                with self.subTest(file=relative, function=function):
                    self.assertEqual(
                        declared.get(function), {"argtypes", "restype"})


class _FakeFunction:
    """Мінімальна модель ctypes-функції з його небезпечними дефолтами."""

    def __init__(self, result=0):
        self.argtypes = None
        self.restype = ctypes.c_int
        self.result = result
        self.calls = []

    @staticmethod
    def _value(value):
        return getattr(value, "value", value)

    @staticmethod
    def _convert_argument(value, declared_type):
        raw = _FakeFunction._value(value)
        if not isinstance(raw, int):
            return value
        try:
            return declared_type(raw).value
        except (AttributeError, TypeError, ValueError):
            return value

    def __call__(self, *args):
        if self.argtypes is None:
            converted = tuple(
                ctypes.c_int(arg).value if isinstance(arg, int) else arg
                for arg in args)
        else:
            converted = tuple(
                self._convert_argument(arg, declared)
                for arg, declared in zip(args, self.argtypes))
        self.calls.append(converted)
        result = self.result(*converted) if callable(self.result) else self.result
        if self.restype is None:
            return None
        return self.restype(result).value


class HighBitHandleTests(unittest.TestCase):
    HWND = 0x1_2345_6789
    PROCESS = 0x2_3456_789A
    WINDOW_DC = 0x3_4567_89AB
    MEMORY_DC = 0x4_5678_9ABC
    BITMAP = 0x5_6789_ABCD
    OLD_OBJECT = 0x6_789A_BCDE

    def test_context_resolver_preserves_high_bit_hwnd_and_process_handle(self):
        user32 = SimpleNamespace(
            GetForegroundWindow=_FakeFunction(self.HWND),
        )
        kernel32 = SimpleNamespace(
            OpenProcess=_FakeFunction(self.PROCESS),
            QueryFullProcessImageNameW=_FakeFunction(0),
            CloseHandle=_FakeFunction(1),
        )

        def load(name, **_kwargs):
            return user32 if name == "user32" else kernel32

        resolver = context.ContextResolver()
        with patch.object(context.ctypes, "WinDLL", side_effect=load):
            self.assertEqual(resolver._foreground_hwnd(), self.HWND)
            self.assertEqual(resolver._exe_for_pid(42), "")

        self.assertEqual(
            kernel32.QueryFullProcessImageNameW.calls[0][0], self.PROCESS)
        self.assertEqual(kernel32.CloseHandle.calls[0][0], self.PROCESS)

    def test_screen_capture_preserves_all_high_bit_gdi_handles(self):
        user32 = SimpleNamespace(
            IsWindowVisible=_FakeFunction(1),
            GetWindow=_FakeFunction(0),
            GetWindowLongW=_FakeFunction(0),
            GetWindowTextLengthW=_FakeFunction(0),
            GetWindowTextW=_FakeFunction(0),
            GetWindowRect=_FakeFunction(1),
            EnumWindows=_FakeFunction(1),
            GetWindowDC=_FakeFunction(self.WINDOW_DC),
            PrintWindow=_FakeFunction(1),
            ReleaseDC=_FakeFunction(1),
        )
        gdi32 = SimpleNamespace(
            CreateCompatibleDC=_FakeFunction(self.MEMORY_DC),
            CreateCompatibleBitmap=_FakeFunction(self.BITMAP),
            SelectObject=_FakeFunction(self.OLD_OBJECT),
            GetBitmapBits=_FakeFunction(lambda _bitmap, size, _raw: size),
            DeleteObject=_FakeFunction(1),
            DeleteDC=_FakeFunction(1),
        )
        libraries = SimpleNamespace(user32=user32, gdi32=gdi32)

        with patch.object(screen_win32.ctypes, "windll", libraries), \
             patch.object(screen_win32.os, "name", "nt"), \
             patch.object(screen_win32, "window_rect",
                          return_value=(0, 0, 2, 2)):
            frame = screen_win32.print_window(self.HWND)

        self.assertEqual(frame.shape, (2, 2, 4))
        self.assertEqual(gdi32.CreateCompatibleDC.calls[0], (self.WINDOW_DC,))
        self.assertEqual(
            gdi32.CreateCompatibleBitmap.calls[0],
            (self.WINDOW_DC, 2, 2))
        self.assertEqual(
            gdi32.SelectObject.calls[0], (self.MEMORY_DC, self.BITMAP))
        self.assertEqual(
            user32.PrintWindow.calls[0],
            (self.HWND, self.MEMORY_DC, screen_win32.PW_RENDERFULLCONTENT))
        self.assertEqual(
            user32.ReleaseDC.calls[0], (self.HWND, self.WINDOW_DC))


if __name__ == "__main__":
    unittest.main()
