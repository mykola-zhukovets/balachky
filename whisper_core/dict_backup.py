"""Резервні копії словників користувача (виправлення, скорочення, імена) +
автовідновлення побитого файла.

Одне місце для всієї логіки копій — виклики зі словникових модулів (terms.py
тощо) лише кличуть rotate_before_write/recover навколо свого write_text/read,
самі копії не переізобретають.

Формат: <файл>.bak1 (найсвіжіша) … <файл>.bakN (найстаріша), N=MAX_BACKUPS.
"""
from __future__ import annotations

from pathlib import Path

MAX_BACKUPS = 3


def _backup_path(path: Path, n: int) -> Path:
    return path.with_name(path.name + f".bak{n}")


def rotate_before_write(path) -> None:
    """Перед перезаписом path: зсунути .bak1→.bak2→.bak3 (найстаріша .bak3
    зникає), а нинішній вміст path → .bak1. Файла ще нема (перший запис) —
    нема що копіювати, тихо виходимо. Викликач сам пише нове path після
    цього виклику."""
    path = Path(path)
    if not path.exists():
        return
    _backup_path(path, MAX_BACKUPS).unlink(missing_ok=True)
    for n in range(MAX_BACKUPS - 1, 0, -1):
        src = _backup_path(path, n)
        if src.exists():
            src.replace(_backup_path(path, n + 1))
    path.replace(_backup_path(path, 1))


def recover(path, loads_fn):
    """path не читається (побитий) → шукаємо першу цілу резервну копію від
    найсвіжішої (.bak1) до найстарішої (.bak3): loads_fn(текст) не кидає
    виняток. Знайшли → піднімаємо її вміст назад у path (програма й далі
    пише/читає той самий шлях) і повертаємо (дані, True). Жодної цілої копії
    нема → (None, False) — виклик сам вирішує, як деградувати (порожній
    словник)."""
    path = Path(path)
    for n in range(1, MAX_BACKUPS + 1):
        bak = _backup_path(path, n)
        if not bak.exists():
            continue
        try:
            text = bak.read_text(encoding="utf-8")
            data = loads_fn(text)
        except Exception:                                  # noqa: BLE001
            continue
        path.write_text(text, encoding="utf-8")
        return data, True
    return None, False
