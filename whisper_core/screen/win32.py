"""Малі Win32-обгортки для вибору й захоплення окремого вікна."""
from __future__ import annotations
import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
import numpy as np

PW_RENDERFULLCONTENT = 0x00000002
GWL_EXSTYLE, WS_EX_TOOLWINDOW = -20, 0x00000080
_WNDENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    left: int
    top: int
    width: int
    height: int
    @property
    def label(self):
        return f"{self.title} ({self.width}×{self.height})"

def _user32():
    user32 = getattr(ctypes, "windll", None) and ctypes.windll.user32
    if not user32:
        return None
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (
        wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = (
        wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = (_WNDENUMPROC, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowDC.argtypes = (wintypes.HWND,)
    user32.GetWindowDC.restype = wintypes.HDC
    user32.PrintWindow.argtypes = (
        wintypes.HWND, wintypes.HDC, wintypes.UINT)
    user32.PrintWindow.restype = wintypes.BOOL
    user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    user32.ReleaseDC.restype = ctypes.c_int
    return user32


def _gdi32():
    gdi32 = ctypes.windll.gdi32
    gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = (
        wintypes.HDC, ctypes.c_int, ctypes.c_int)
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.GetBitmapBits.argtypes = (
        wintypes.HBITMAP, wintypes.LONG, ctypes.c_void_p)
    gdi32.GetBitmapBits.restype = wintypes.LONG
    gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = (wintypes.HDC,)
    gdi32.DeleteDC.restype = wintypes.BOOL
    return gdi32

def list_windows() -> list[WindowInfo]:
    """Видимі, не-системні top-level-вікна для вибору джерела."""
    user32 = _user32()
    if not user32:
        return []
    found = []
    def collect(hwnd, _):
        hwnd = int(hwnd)
        if (not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4)
                or user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        rect = ctypes.wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w > 1 and h > 1:
                found.append(WindowInfo(hwnd, title.value.strip(), rect.left, rect.top, w, h))
        return True
    try:
        user32.EnumWindows(_WNDENUMPROC(collect), 0)
    except Exception:
        return []
    return sorted(found, key=lambda item: item.title.casefold())

def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    user32 = _user32()
    if not user32:
        raise RuntimeError("Захоплення вікон доступне лише у Windows")
    rect = ctypes.wintypes.RECT()
    if not user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
        raise RuntimeError("Вікно більше не доступне")
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

def print_window(hwnd: int) -> np.ndarray | None:
    """BGRA через PrintWindow, або None щоб рекордер застосував mss fallback."""
    if os.name != "nt" or not _user32():
        return None
    user32, gdi32 = _user32(), _gdi32()
    _, _, width, height = window_rect(hwnd)
    if width < 2 or height < 2:
        raise RuntimeError("Вікно має неприпустимий розмір")
    hwnd_dc = user32.GetWindowDC(int(hwnd))
    if not hwnd_dc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    old = gdi32.SelectObject(mem_dc, bitmap)
    try:
        if not user32.PrintWindow(int(hwnd), mem_dc, PW_RENDERFULLCONTENT):
            return None
        raw = ctypes.create_string_buffer(width * height * 4)
        if gdi32.GetBitmapBits(bitmap, len(raw), raw) != len(raw):
            return None
        return np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 4)).copy()
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(int(hwnd), hwnd_dc)
