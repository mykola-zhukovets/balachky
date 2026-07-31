"""Рантайм-хук PyInstaller: реєстрація _MEIPASS/PySide6 у search path DLL Windows.

feature/player-recordings (дефект frozen-збірки):
Qt Multimedia у frozen onedir-збірці вантажить ffmpegmediaplugin.dll з теки
_MEIPASS/PySide6/plugins/multimedia. Сам плагін залежить від Qt6Multimedia.dll,
Qt6Core.dll та ffmpeg-бібліотек (avcodec-61.dll, avutil-59.dll тощо), які
PyInstaller пакує в _MEIPASS/PySide6.

На Windows (Python >= 3.8) Windows DLL loader НЕ шукає залежності у підтеці
sys._MEIPASS/PySide6 (SetDllDirectory bootloader-а налаштовано лише на корінь
_MEIPASS). Без явного os.add_dll_directory(_MEIPASS/PySide6) завантаження
ffmpegmediaplugin.dll падало з ERROR_MOD_NOT_FOUND (126). Qt Multimedia мовчки
відкочувався на windowsmediaplugin (Media Foundation), який при спробі відтворити
VP9 WebM записи екрана видавав у лог «Cannot allocate memory».

Цей хук реєструє _MEIPASS/PySide6 у DLL search path до старту Qt, що дозволяє
Windows DLL loader знайти всі залежності ffmpegmediaplugin.dll і відтворювати
відеозаписи через FFmpeg-бекенд у frozen-збірці.
"""
import os
import sys
from pathlib import Path

if os.name == "nt" and getattr(sys, "frozen", False):
    _meipass = Path(getattr(sys, "_MEIPASS", ""))
    _pyside_dir = _meipass / "PySide6"
    if _pyside_dir.is_dir():
        try:
            sys._pyside_dll_cookie = os.add_dll_directory(str(_pyside_dir))
        except (OSError, AttributeError):
            pass
        os.environ["PATH"] = str(_pyside_dir) + os.pathsep + os.environ.get("PATH", "")
