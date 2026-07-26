# feature/watch-folder
"""Спостереження за текою — ЧИСТІ хелпери без Qt.

Тут живе спільний список аудіо-розширень (одне джерело правди для drag&drop
сторінки «Файли» і для watch-логіки) та рішення «файл готовий / це нове аудіо».
Оркестрація (QFileSystemWatcher / робочий потік) — у fronts.desktop.app; тут
лише те, що покривається unittest без PySide6 (mock таймерів, tempfile).
"""
import os
import time
from pathlib import Path

# Той самий список, що приймає drag&drop сторінки «Файли» (main_window.FilesPage
# імпортує саме звідси — не дублюємо). Без Qt, щоб і вікно, і тести брали одне.
AUDIO_EXT = {".ogg", ".oga", ".opus", ".mp3", ".wav", ".m4a",
             ".mp4", ".webm", ".flac", ".aac", ".wma"}


def is_supported_audio(path, exts=AUDIO_EXT) -> bool:
    """Чи має файл підтримуване аудіо-розширення (регістр не важливий)."""
    return Path(path).suffix.lower() in exts


def new_audio_files(files, seen, exts=AUDIO_EXT):
    """Нові аудіофайли: підтримуване розширення і ще НЕ в `seen`.

    Порядок збережено; `seen` — множина вже врахованих шляхів (str). Так уже
    оброблені (чи в роботі) файли не потрапляють у чергу повторно.
    """
    return [f for f in files if str(f) not in seen and is_supported_audio(f, exts)]


def size_is_stable(prev_size: int, cur_size: int) -> bool:
    """Файл дописано, коли два послідовні заміри розміру збіглися і він не
    порожній. `prev_size < 0` — перший замір (ще нема з чим порівнювати)."""
    return cur_size > 0 and cur_size == prev_size


def wait_until_stable(path, *, getsize=os.path.getsize, sleep=time.sleep,
                      interval: float = 1.0, timeout: float = 120.0) -> bool:
    """Дочекатись, поки файл повністю з'явиться (його ще могли копіювати).

    Розмір читається повторно з паузою `interval`; щойно два послідовні заміри
    збіглися (і файл не порожній) — True. Файл зник/недоступний або вичерпано
    `timeout` — False. `getsize`/`sleep` ін'єктуються, тож тест мокає таймери й
    читає tempfile без реальних пауз.
    """
    prev = -1
    waited = 0.0
    while waited <= timeout:
        try:
            cur = getsize(path)
        except OSError:
            return False                 # зник під час копіювання — облишаємо
        if size_is_stable(prev, cur):
            return True
        prev = cur
        sleep(interval)
        waited += interval
    return False
