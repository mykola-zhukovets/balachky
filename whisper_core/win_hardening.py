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
from dataclasses import dataclass

log = logging.getLogger(__name__)

# --- WinAPI константи ---
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_user32 = (
    ctypes.WinDLL("user32", use_last_error=True)
    if sys.platform == "win32" else None
)
_kernel32 = getattr(ctypes, "windll", None) and getattr(ctypes.windll, "kernel32", None)
_wer = getattr(ctypes, "windll", None) and getattr(ctypes.windll, "wer", None)

if _user32 is not None and _kernel32 is not None:
    # На старій Windows символ може бути відсутній: це supported=False, а не
    # падіння імпорту всього модуля.
    if hasattr(_user32, "SetWindowDisplayAffinity"):
        _user32.SetWindowDisplayAffinity.argtypes = (
            wintypes.HWND, wintypes.DWORD)
        _user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
    _user32.OpenClipboard.argtypes = (wintypes.HWND,)
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.argtypes = ()
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.argtypes = ()
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.RegisterClipboardFormatW.argtypes = (wintypes.LPCWSTR,)
    _user32.RegisterClipboardFormatW.restype = wintypes.UINT
    _user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    _user32.SetClipboardData.restype = wintypes.HANDLE

    _kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    _kernel32.GlobalLock.restype = wintypes.LPVOID
    _kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    _kernel32.GlobalUnlock.restype = wintypes.BOOL
    _kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
    _kernel32.GlobalFree.restype = wintypes.HGLOBAL
    _kernel32.GetLastError.argtypes = ()
    _kernel32.GetLastError.restype = wintypes.DWORD

if _wer is not None:
    _wer.WerAddExcludedApplication.argtypes = (wintypes.LPCWSTR, wintypes.BOOL)
    _wer.WerAddExcludedApplication.restype = ctypes.c_long


def is_windows() -> bool:
    return sys.platform == "win32" and _user32 is not None and _kernel32 is not None


def is_display_affinity_supported() -> bool:
    """Перевіряє, чи підтримується WDA_EXCLUDEFROMCAPTURE (Windows 10 2004+ / build >= 19041)."""
    if (not is_windows()
            or getattr(_user32, "SetWindowDisplayAffinity", None) is None):
        return False
    try:
        ver = sys.getwindowsversion()
        # Windows 10 build 19041 = version 2004
        return ver.major > 10 or (ver.major == 10 and ver.build >= 19041)
    except Exception:
        return False


@dataclass(frozen=True)
class DisplayAffinityResult:
    """Фактичний результат одного застосування display affinity до вікна."""

    hwnd: int
    enabled: bool
    supported: bool
    succeeded: bool
    error_code: int | None = None

    def __bool__(self) -> bool:
        return self.succeeded


def set_window_display_affinity(
        hwnd: int, enable: bool = True) -> DisplayAffinityResult:
    """Застосувати display affinity і повернути підтверджений результат Win32."""
    hwnd = int(hwnd or 0)
    enabled = bool(enable)
    supported = is_display_affinity_supported()
    if not supported:
        return DisplayAffinityResult(
            hwnd, enabled, supported=False, succeeded=False)
    if not hwnd:
        return DisplayAffinityResult(
            hwnd, enabled, supported=True, succeeded=False)
    try:
        affinity = WDA_EXCLUDEFROMCAPTURE if enabled else WDA_NONE
        succeeded = bool(_user32.SetWindowDisplayAffinity(
            ctypes.wintypes.HWND(hwnd), wintypes.DWORD(affinity)))
        error_code = None if succeeded else int(ctypes.get_last_error())
        if not succeeded:
            log.warning(
                "SetWindowDisplayAffinity не вдався (hwnd=%s, error=%s)",
                hwnd, error_code)
        return DisplayAffinityResult(
            hwnd, enabled, supported=True, succeeded=succeeded,
            error_code=error_code)
    except Exception as e:
        log.warning("SetWindowDisplayAffinity не вдався (hwnd=%s): %s", hwnd, e)
        return DisplayAffinityResult(
            hwnd, enabled, supported=True, succeeded=False)


# --- Захист від захоплення екрана: живий реєстр вікон нарад ---
# Головне вікно накладає WDA саме на себе (main_window), але вікна відтворення
# наради (відеоплеєр запису) створюються/знищуються динамічно. Реєструємо їх тут,
# щоб тумблер міг накласти/зняти WDA на ВСІ живі вікна, а не лише головне.
import weakref as _weakref

_capture_protection_on = False
_capture_windows: "_weakref.WeakSet" = _weakref.WeakSet()
_capture_results: "_weakref.WeakKeyDictionary" = _weakref.WeakKeyDictionary()
_capture_finalizers: "_weakref.WeakKeyDictionary" = _weakref.WeakKeyDictionary()
_capture_removal_callbacks: "_weakref.WeakKeyDictionary" = (
    _weakref.WeakKeyDictionary())


def _window_hwnd(widget) -> int:
    try:
        return int(widget.winId())
    except Exception:
        return 0


def capture_protection_enabled() -> bool:
    """Поточний стан тумблера захисту від захоплення екрана."""
    return _capture_protection_on


def _notify_capture_result_removed(callback) -> None:
    try:
        callback()
    except Exception:
        log.exception("Не вдалося оновити агрегат захисту після закриття вікна")


def protect_window(widget, on_result_removed=None) -> DisplayAffinityResult:
    """Зареєструвати вікно наради й застосувати ПОТОЧНИЙ стан захисту.
    Нове вікно бере актуальний стан тумблера; тумблер потім керує ним через
    set_capture_protection_enabled. → результат WinAPI-виклику."""
    try:
        _capture_windows.add(widget)
    except Exception:
        pass
    result = set_window_display_affinity(
        _window_hwnd(widget), _capture_protection_on)
    try:
        _capture_results[widget] = result
    except Exception:
        pass
    if on_result_removed is not None:
        try:
            previous = _capture_finalizers.pop(widget, None)
            if previous is not None:
                previous.detach()
            _capture_removal_callbacks[widget] = on_result_removed
            _capture_finalizers[widget] = _weakref.finalize(
                widget, _notify_capture_result_removed, on_result_removed)
        except Exception:
            pass
    return result


def unprotect_window(widget) -> bool:
    """Прибрати живе вікно з агрегату й сповістити його контролер один раз."""
    callback = None
    removed = False
    try:
        finalizer = _capture_finalizers.pop(widget, None)
        if finalizer is not None:
            finalizer.detach()
        callback = _capture_removal_callbacks.pop(widget, None)
        removed = widget in _capture_results
        _capture_results.pop(widget, None)
        _capture_windows.discard(widget)
    except Exception:
        return False
    if removed and callback is not None:
        _notify_capture_result_removed(callback)
    return removed


def set_capture_protection_enabled(
        enable: bool) -> tuple[DisplayAffinityResult, ...]:
    """Тумблер: запамʼятати стан і накласти/ЗНЯТИ WDA на всіх ЖИВИХ
    зареєстрованих вікнах нарад. Нові вікна візьмуть стан у protect_window."""
    global _capture_protection_on
    _capture_protection_on = bool(enable)
    results = []
    for w in list(_capture_windows):
        result = set_window_display_affinity(
            _window_hwnd(w), _capture_protection_on)
        results.append(result)
        try:
            _capture_results[w] = result
        except Exception:
            pass
    return tuple(results)


def capture_protection_results() -> tuple[DisplayAffinityResult, ...]:
    """Останні фактичні результати для всіх ще живих зареєстрованих вікон."""
    return tuple(
        _capture_results[w]
        for w in list(_capture_windows)
        if w in _capture_results
    )


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


def _free_clipboard_handle(handle) -> bool:
    """Звільнити ще не переданий системі HGLOBAL рівно один раз."""
    free_result = _kernel32.GlobalFree(handle)
    if free_result:
        error = _kernel32.GetLastError()
        log.warning("GlobalFree clipboard handle не вдався (error=%s)", error)
        return False
    return True


def _set_clipboard_bytes(fmt: int, payload: bytes) -> bool:
    """Передати один GMEM_MOVEABLE payload системному clipboard."""
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not handle:
        return False

    transferred = False
    try:
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return False

        copied = False
        unlocked = False
        try:
            ctypes.memmove(pointer, payload, len(payload))
            copied = True
        finally:
            unlock_result = _kernel32.GlobalUnlock(handle)
            if not unlock_result:
                unlock_error = _kernel32.GetLastError()
                unlocked = unlock_error == 0

        if not copied or not unlocked:
            return False
        if not _user32.SetClipboardData(fmt, handle):
            return False
        transferred = True
        return True
    finally:
        if not transferred:
            _free_clipboard_handle(handle)


def set_clipboard_text_excluded(text: str, owner_hwnd: int) -> bool:
    """Записати текст у буфер обміну з маркерами виключення з історії та хмари.

    Встановлює:
      • CF_UNICODETEXT — сам текст
      • ExcludeClipboardContentFromMonitorProcessing — глобальний прапор виключення
      • CanIncludeInClipboardHistory = 0 — заборона локальної історії
      • CanUploadToCloudClipboard = 0 — заборона хмарної синхронізації
    """
    if not is_windows() or not owner_hwnd:
        return False
    if text is None:
        text = ""

    try:
        fmt_exclude = _user32.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
        fmt_history = _user32.RegisterClipboardFormatW("CanIncludeInClipboardHistory")
        fmt_cloud = _user32.RegisterClipboardFormatW("CanUploadToCloudClipboard")
        if not fmt_exclude:
            return False

        if not _user32.OpenClipboard(wintypes.HWND(int(owner_hwnd))):
            return False

        operation_ok = False
        try:
            if _user32.EmptyClipboard():
                encoded = text.encode("utf-16-le") + b"\x00\x00"
                text_ok = _set_clipboard_bytes(CF_UNICODETEXT, encoded)
                if text_ok:
                    exclude_ok = _set_clipboard_bytes(fmt_exclude, b"1\x00")
                    zero_dword = b"\x00" * ctypes.sizeof(wintypes.DWORD)
                    if fmt_history and not _set_clipboard_bytes(fmt_history, zero_dword):
                        log.warning("CanIncludeInClipboardHistory marker не записано")
                    if fmt_cloud and not _set_clipboard_bytes(fmt_cloud, zero_dword):
                        log.warning("CanUploadToCloudClipboard marker не записано")
                    operation_ok = exclude_ok
        except Exception as e:
            log.warning("set_clipboard_text_excluded не вдався: %s", e)
        finally:
            close_result = _user32.CloseClipboard()
            if not close_result:
                close_error = _kernel32.GetLastError()
                log.warning("CloseClipboard не вдався (error=%s)", close_error)
                operation_ok = False
        return operation_ok
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
