"""Завантаження частотного словника української для автокорекції (symspellpy).

Компонент навмисно НЕ вшивається у білд (як моделі діаризації): мегабайти даних
качаються за явним натиском кнопки в Налаштуваннях.

Джерело — hermitdave/FrequencyWords (uk_50k, ліцензія MIT): формат «слово частота»
на рядок, рівно той, що очікує symspellpy. dict_uk (VESUM) сюди НЕ береться: це
морфологічний словник (.dic/.aff), а не частотний, і його тристороння ліцензія
(MPL/GPL/LGPL) складніша за MIT — при потребі його можна тримати окремим
завантажуваним файлом даних, не змінюючи цей код.

feature/punctuation-plus.
"""
from __future__ import annotations

import os
import tempfile
import urllib.request
from pathlib import Path

from . import netlog   # доказова офлайновість: журнал вихідних з'єднань

# hermitdave/FrequencyWords @ master, content/2018/uk/uk_50k.txt (MIT).
DICT_URL = ("https://raw.githubusercontent.com/hermitdave/FrequencyWords/"
            "master/content/2018/uk/uk_50k.txt")
# Мінімальний розмір валідного словника (реально ~870 КБ); захист від半-качаного
# чи недокачаного файлу, що symspellpy мовчки прийняв би за порожній словник.
MIN_DICT_BYTES = 100_000


class AutocorrectDownloadError(RuntimeError):
    pass


def _is_reparse_point(path: Path) -> bool:
    """True для symlink/junction (frozen exe не ходить по reparse-точках кешу)."""
    import stat
    try:
        info = path.lstat()
    except OSError:
        return True
    return (path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) &
                                      getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _download(url: str, destination: Path, progress_cb=None, cancel_check=None) -> None:
    netlog.record_url(url, kind=netlog.MODEL, detail="autocorrect")
    try:
        with urllib.request.urlopen(url, timeout=30) as response, destination.open("wb") as out:
            total, received = int(response.headers.get("Content-Length") or 0), 0
            while True:
                if cancel_check and cancel_check():
                    raise InterruptedError()
                part = response.read(1024 * 256)
                if not part:
                    break
                out.write(part)
                received += len(part)
                if progress_cb:
                    progress_cb(received, total)
    except InterruptedError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise AutocorrectDownloadError(
            f"Не вдалося завантажити словник автокорекції: {exc}") from exc


def download_and_install(target_path, progress_cb=None, cancel_check=None) -> None:
    """Докачати частотний словник і атомарно активувати його за target_path.

    Уже наявний валідний файл — no-op. Качаємо у тимчасовий файл поруч (той самий
    том → перейменування атомарне), перевіряємо розмір, тоді os.replace."""
    target = Path(target_path)
    if target.is_file() and not _is_reparse_point(target) and target.stat().st_size >= MIN_DICT_BYTES:
        return
    if target.exists() and _is_reparse_point(target):
        raise AutocorrectDownloadError("Файл словника не може бути symlink або reparse point")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="uk_freq-", suffix=".part", dir=target.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        _download(DICT_URL, tmp, progress_cb, cancel_check)
        if tmp.stat().st_size < MIN_DICT_BYTES:
            raise AutocorrectDownloadError("Завантажений словник надто малий або порожній")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
