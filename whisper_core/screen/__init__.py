"""Незалежний запис екрана: джерела Win32 та відеорекордер."""
from .recorder import ScreenRecorder, ScreenRecordOptions, available_formats
from .win32 import WindowInfo, list_windows
__all__ = ["ScreenRecorder", "ScreenRecordOptions", "WindowInfo", "available_formats", "list_windows"]
