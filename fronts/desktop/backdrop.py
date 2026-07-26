"""Mica-бекдроп Windows 11 (22H2+) через DWM.

Скло — лише навігаційний шар (канон «Balachky»): вмикається тільки якщо ОБИДВА
DWM-виклики успішні; будь-яка помилка → False → вікно лишається твердим.
"""
import ctypes

DWMWA_SYSTEMBACKDROP_TYPE = 38   # Win11 22H2+
DWMSBT_MAINWINDOW = 2            # Mica


class _MARGINS(ctypes.Structure):
    _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]


def enable_mica(hwnd: int) -> bool:
    """Розтягнути DWM-рамку на весь клієнт + увімкнути Mica. True — лише
    якщо обидва виклики повернули S_OK (0); інакше тихий фолбек у клієнта."""
    try:
        dwm = ctypes.windll.dwmapi
        margins = _MARGINS(-1, -1, -1, -1)
        if dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins)) != 0:
            return False
        value = ctypes.c_int(DWMSBT_MAINWINDOW)
        return dwm.DwmSetWindowAttribute(
            hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
            ctypes.byref(value), ctypes.sizeof(value)) == 0
    except Exception:
        return False
