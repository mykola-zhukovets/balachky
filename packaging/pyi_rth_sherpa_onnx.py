"""Рантайм-хук PyInstaller: вантажити onnxruntime САМЕ зі sherpa_onnx/lib.

Windows може підхопити старий ``onnxruntime.dll`` із System32 і впасти з
«requested API version [23]». Одного лише збору DLL у дистрибутив НЕ досить —
треба виправити ПОРЯДОК пошуку до перших імпортів застосунку. Тому тут, ще до
завантаження коду Балачок, ми:

  • тримаємо ``os.add_dll_directory`` на ``_MEIPASS/sherpa_onnx/lib`` (cookie
    зберігаємо на весь процес — інакше тека прибереться при GC);
  • ставимо цю теку на ПОЧАТОК ``PATH``;
  • предзавантажуємо DLL абсолютними шляхами у порядку залежностей:
    ``onnxruntime`` → ``sherpa-onnx-c-api`` → ``sherpa-onnx-cxx-api``;
  • тримаємо ``ctypes.WinDLL``-хендли на весь час життя процесу.

Dev-еквівалент — ``whisper_core.meeting.diarize.prepare_windows_dlls()``.
"""
import ctypes
import os
import sys
from pathlib import Path

if os.name == "nt" and getattr(sys, "frozen", False):
    _lib = Path(getattr(sys, "_MEIPASS", "")) / "sherpa_onnx" / "lib"
    if _lib.is_dir():
        _cookie = None
        try:
            _cookie = os.add_dll_directory(str(_lib))
        except (OSError, AttributeError):
            _cookie = None
        os.environ["PATH"] = str(_lib) + os.pathsep + os.environ.get("PATH", "")
        _handles = []
        for _name in ("onnxruntime.dll", "sherpa-onnx-c-api.dll",
                      "sherpa-onnx-cxx-api.dll"):
            _dll = _lib / _name
            if _dll.is_file():
                try:
                    _handles.append(ctypes.WinDLL(str(_dll)))
                except OSError:
                    pass
        # Тримати cookie й хендли на весь процес — інакше DLL-каталог/бібліотеки
        # можуть вивантажитись до першого справжнього використання sherpa.
        sys._sherpa_dll_cookie = _cookie
        sys._sherpa_dll_handles = _handles
