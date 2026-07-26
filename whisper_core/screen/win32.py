"""Малі Win32-обгортки для вибору й захоплення окремого вікна."""
from __future__ import annotations
import ctypes
import os
from dataclasses import dataclass
import numpy as np

PW_RENDERFULLCONTENT = 0x00000002
GWL_EXSTYLE, WS_EX_TOOLWINDOW = -20, 0x00000080

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
    return getattr(ctypes, "windll", None) and ctypes.windll.user32

def list_windows() -> list[WindowInfo]:
    """Видимі, не-системні top-level-вікна для вибору джерела."""
    user32 = _user32()
    if not user32:
        return []
    found = []
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
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
        user32.EnumWindows(enum_proc(collect), 0)
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
    user32, gdi32 = _user32(), ctypes.windll.gdi32
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
