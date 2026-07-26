"""Малі Win32-обгортки системного захисту (Т54 «мілітарі-hardening»).

Використовує чистий ctypes (без нових залежностей). Тонкий шар-обгортка, який
дозволяє легко мокувати WinAPI у юніт-тестах.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from ctypes import wintypes

log = logging.getLogger(__name__)

# --- WinAPI константи ---
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_user32 = getattr(ctypes, "windll", None) and getattr(ctypes.windll, "user32", None)
_kernel32 = getattr(ctypes, "windll", None) and getattr(ctypes.windll, "kernel32", None)
_wer = getattr(ctypes, "windll", None) and getattr(ctypes.windll, "wer", None)


def is_windows() -> bool:
    return sys.platform == "win32" and _user32 is not None and _kernel32 is not None


def is_display_affinity_supported() -> bool:
    """Перевіряє, чи підтримується WDA_EXCLUDEFROMCAPTURE (Windows 10 2004+ / build >= 19041)."""
    if not is_windows():
        return False
    try:
        ver = sys.getwindowsversion()
        # Windows 10 build 19041 = version 2004
        return ver.major > 10 or (ver.major == 10 and ver.build >= 19041)
    except Exception:
        return False


def set_window_display_affinity(hwnd: int, enable: bool = True) -> bool:
    """Виставити WDA_EXCLUDEFROMCAPTURE для вікна. → True якщо WinAPI підтвердив."""
    if not is_windows() or not hwnd:
        return False
    try:
        affinity = WDA_EXCLUDEFROMCAPTURE if enable else WDA_NONE
        res = _user32.SetWindowDisplayAffinity(ctypes.wintypes.HWND(int(hwnd)), wintypes.DWORD(affinity))
        return bool(res)
    except Exception as e:
        log.warning("SetWindowDisplayAffinity не вдався (hwnd=%s): %s", hwnd, e)
        return False


# --- Захист від захоплення екрана: живий реєстр вікон нарад ---
# Головне вікно накладає WDA саме на себе (main_window), але вікна відтворення
# наради (відеоплеєр запису) створюються/знищуються динамічно. Реєструємо їх тут,
# щоб тумблер міг накласти/зняти WDA на ВСІ живі вікна, а не лише головне.
import weakref as _weakref

_capture_protection_on = False
_capture_windows: "_weakref.WeakSet" = _weakref.WeakSet()


def _window_hwnd(widget) -> int:
    try:
        return int(widget.winId())
    except Exception:
        return 0


def capture_protection_enabled() -> bool:
    """Поточний стан тумблера захисту від захоплення екрана."""
    return _capture_protection_on


def protect_window(widget) -> bool:
    """Зареєструвати вікно наради й застосувати ПОТОЧНИЙ стан захисту.
    Нове вікно бере актуальний стан тумблера; тумблер потім керує ним через
    set_capture_protection_enabled. → результат WinAPI-виклику."""
    try:
        _capture_windows.add(widget)
    except Exception:
        pass
    return set_window_display_affinity(_window_hwnd(widget), _capture_protection_on)


def set_capture_protection_enabled(enable: bool) -> None:
    """Тумблер: запамʼятати стан і накласти/ЗНЯТИ WDA на всіх ЖИВИХ
    зареєстрованих вікнах нарад. Нові вікна візьмуть стан у protect_window."""
    global _capture_protection_on
    _capture_protection_on = bool(enable)
    for w in list(_capture_windows):
        set_window_display_affinity(_window_hwnd(w), _capture_protection_on)


def clear_clipboard() -> bool:
    """Очистити системний буфер обміну. → True при успіху."""
    if not is_windows():
        return False
    try:
        if not _user32.OpenClipboard(None):
            return False
        try:
            return bool(_user32.EmptyClipboard())
        finally:
            _user32.CloseClipboard()
    except Exception as e:
        log.warning("clear_clipboard не вдався: %s", e)
        return False


def set_clipboard_text_excluded(text: str) -> bool:
    """Записати текст у буфер обміну з маркерами виключення з історії та хмари.

    Встановлює:
      • CF_UNICODETEXT — сам текст
      • ExcludeClipboardContentFromMonitorProcessing — глобальний прапор виключення
      • CanIncludeInClipboardHistory = 0 — заборона локальної історії
      • CanUploadToCloudClipboard = 0 — заборона хмарної синхронізації
    """
    if not is_windows():
        return False
    if text is None:
        text = ""

    try:
        fmt_exclude = _user32.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
        fmt_history = _user32.RegisterClipboardFormatW("CanIncludeInClipboardHistory")
        fmt_cloud = _user32.RegisterClipboardFormatW("CanUploadToCloudClipboard")

        if not _user32.OpenClipboard(None):
            return False

        try:
            _user32.EmptyClipboard()

            # 1. Основний текст CF_UNICODETEXT — ОБОВʼЯЗКОВИЙ. Якщо будь-який крок
            # (GlobalAlloc/GlobalLock/SetClipboardData) мовчки провалився, текст у
            # буфер НЕ потрапив → це НЕ успіх: повертаємо False, щоб paste.py увімкнув
            # pyperclip-фолбек (страхувальна сітка «текст завжди в буфері»).
            text_ok = False
            encoded = text.encode("utf-16-le") + b"\x00\x00"
            h_text = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
            if h_text:
                ptr = _kernel32.GlobalLock(h_text)
                if ptr:
                    ctypes.memmove(ptr, encoded, len(encoded))
                    _kernel32.GlobalUnlock(h_text)
                    if _user32.SetClipboardData(CF_UNICODETEXT, h_text):
                        text_ok = True
            if not text_ok:
                return False

            # 2. ExcludeClipboardContentFromMonitorProcessing
            if fmt_exclude:
                marker = b"1\x00"
                h_ex = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(marker))
                if h_ex:
                    ptr_ex = _kernel32.GlobalLock(h_ex)
                    if ptr_ex:
                        ctypes.memmove(ptr_ex, marker, len(marker))
                        _kernel32.GlobalUnlock(h_ex)
                        _user32.SetClipboardData(fmt_exclude, h_ex)

            # 3. CanIncludeInClipboardHistory = 0 (DWORD)
            if fmt_history:
                val_zero = wintypes.DWORD(0)
                h_hist = _kernel32.GlobalAlloc(GMEM_MOVEABLE, ctypes.sizeof(val_zero))
                if h_hist:
                    ptr_h = _kernel32.GlobalLock(h_hist)
                    if ptr_h:
                        ctypes.memmove(ptr_h, ctypes.byref(val_zero), ctypes.sizeof(val_zero))
                        _kernel32.GlobalUnlock(h_hist)
                        _user32.SetClipboardData(fmt_history, h_hist)

            # 4. CanUploadToCloudClipboard = 0 (DWORD)
            if fmt_cloud:
                val_zero = wintypes.DWORD(0)
                h_cloud = _kernel32.GlobalAlloc(GMEM_MOVEABLE, ctypes.sizeof(val_zero))
                if h_cloud:
                    ptr_c = _kernel32.GlobalLock(h_cloud)
                    if ptr_c:
                        ctypes.memmove(ptr_c, ctypes.byref(val_zero), ctypes.sizeof(val_zero))
                        _kernel32.GlobalUnlock(h_cloud)
                        _user32.SetClipboardData(fmt_cloud, h_cloud)

            return True
        finally:
            _user32.CloseClipboard()
    except Exception as e:
        log.warning("set_clipboard_text_excluded не вдався: %s", e)
        return False


def exclude_process_from_wer(exe_name: str | None = None) -> bool:
    """Зареєструвати виняток у Windows Error Reporting (WerAddExcludedApplication).

    Гарантує, що краш-дампи свого процесу не відправлятимуться в хмару Microsoft.
    """
    if not is_windows() or _wer is None:
        return False
    try:
        if not exe_name:
            exe_name = os.path.basename(sys.executable or "Balachky.exe")
        # WerAddExcludedApplication(pwzExeName, bAllUsers=False)
        res = _wer.WerAddExcludedApplication(wintypes.LPCWSTR(exe_name), wintypes.BOOL(False))
        return res == 0
    except Exception as e:
        log.warning("exclude_process_from_wer не вдався: %s", e)
        return False
