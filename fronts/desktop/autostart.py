"""Автозапуск разом із Windows: .bat у теці автозавантаження користувача.

dev: файл запускає застосунок з кореня репозиторію через venv-python.
frozen (PyInstaller): запускає безпосередньо Balachky.exe (sys.executable).
Параметр startup_dir — для тестів у тимчасовій теці.
"""
import os
import sys
from pathlib import Path

from whisper_core import paths

STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / \
    "Microsoft/Windows/Start Menu/Programs/Startup"
BAT_NAME = "balachky-autostart.bat"

_REPO = paths.APP_ROOT


def is_enabled(startup_dir=STARTUP_DIR) -> bool:
    return (Path(startup_dir) / BAT_NAME).exists()


def enable(startup_dir=STARTUP_DIR) -> None:
    # лапки — шлях може мати пробіли; mbcs (Windows ANSI) — cmd.exe читає .bat
    # в ANSI, тож кирилиця у шляху виживає (utf-8 тут ламався б мовчки)
    if paths.FROZEN:
        # збірка: жодного python — просто стартуємо exe згорнутим
        bat = "\n".join([
            "@echo off",
            f'start "" /min "{sys.executable}" --autostart',
        ]) + "\n"
    else:
        venv = _REPO / ".venv" / "Scripts" / "python.exe"
        exe = r".venv\Scripts\python.exe" if venv.exists() else "python"
        bat = "\n".join([
            "@echo off",
            f'cd /d "{_REPO}"',
            f'start "" /min "{exe}" -m fronts.desktop --autostart',
        ]) + "\n"
    try:
        (Path(startup_dir) / BAT_NAME).write_text(bat, encoding="mbcs")
    except LookupError:  # не-Windows (тести на іншій ОС)
        (Path(startup_dir) / BAT_NAME).write_text(bat, encoding="utf-8")


def refresh_if_enabled(startup_dir=STARTUP_DIR) -> bool:
    """Оновити наявний BAT до поточного формату, не вмикаючи автозапуск самовільно."""
    if not is_enabled(startup_dir):
        return False
    enable(startup_dir)
    return True


def disable(startup_dir=STARTUP_DIR) -> None:
    (Path(startup_dir) / BAT_NAME).unlink(missing_ok=True)
