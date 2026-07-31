"""Кошик нарад: м'яке видалення з поверненням для тек наради усередині сховища застосунку.

Мінімальний обсяг (тільки наради): видалення наради
переносить її теку в ``<root>/.trash/<назва>-<мітка часу>`` замість негайного
``rmtree``. Перенесення — ``shutil.move`` У МЕЖАХ ТОГО САМОГО кореня сховища:
на одному томі це rename (0 мс, без копіювання гігабайтних WAV).

Журнал цілісності (``audit.jsonl``) сесії НЕ чіпається: тека переїжджає
цілком разом з журналом, подій про перенесення/повернення до нього НЕ
дописуємо (журнал лишається чистим списком подій самої наради — soft-delete
не є остаточним видаленням).
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

_TRASH_DIRNAME = ".trash"
_INFO_NAME = "trash_info.json"
DEFAULT_MAX_AGE_DAYS = 7


def trash_root(root) -> Path:
    """Тека кошика всередині кореня сховища (``<root>/.trash``)."""
    return Path(root) / _TRASH_DIRNAME


def _unique_dir(parent: Path, name: str) -> Path:
    """``parent/name``, а за зайнятості — ``parent/name (2)``, ``(3)``...

    Наявну теку НІКОЛИ не затираємо (конфлікт імен при відновленні — розділ
    «конфлікт імен» завдання)."""
    candidate = parent / name
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = parent / f"{name} ({n})"
        if not candidate.exists():
            return candidate
        n += 1


def soft_delete(session_dir, root, *, now: "float | None" = None) -> Path:
    """Перенести теку сесії у кошик. Повертає шлях у кошику.

    ``root`` — корінь сховища нарад (``sessions/``), кошик — його підтека,
    тож ``shutil.move`` лишається в межах одного тому й не копіює дані.
    """
    session_dir = Path(session_dir)
    root = Path(root)
    if not session_dir.is_dir():
        raise FileNotFoundError(session_dir)
    tdir = trash_root(root)
    tdir.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    dest = _unique_dir(tdir, f"{session_dir.name}-{stamp}")
    shutil.move(str(session_dir), str(dest))
    info = {"original_name": session_dir.name, "trashed_at": now}
    try:
        (dest / _INFO_NAME).write_text(
            json.dumps(info, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logging.exception(
            "soft_delete: не вдалося записати %s у %s", _INFO_NAME, dest)
    return dest


def _read_info(trashed_dir: Path) -> dict:
    try:
        data = json.loads((trashed_dir / _INFO_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def original_name(trashed_dir) -> str:
    """Назва теки до видалення (з ``trash_info.json``, без побічних дій).

    Потрібна перед ``restore()`` — той сам прибирає ``trash_info.json``, тож
    після виклику оригінальну назву вже ніде взяти (кошик 2026-07-31,
    прибирання осиротілого tombstone при конфлікті імені)."""
    info = _read_info(Path(trashed_dir))
    return info.get("original_name") or Path(trashed_dir).name


def restore(trashed_dir, root) -> Path:
    """Повернути теку з кошика на місце (``root/<оригінальна назва>``).

    Конфлікт із наявною текою — суфікс " (2)", " (3)"... наявне НЕ затираємо.
    """
    trashed_dir = Path(trashed_dir)
    root = Path(root)
    if not trashed_dir.is_dir():
        raise FileNotFoundError(trashed_dir)
    info = _read_info(trashed_dir)
    name = info.get("original_name") or trashed_dir.name
    # trash_info.json — дані з диска, їм не можна вірити як шляху: отруєне
    # "original_name" на кшталт "..\\..\\інша-тека" перетягло б теку ЗА МЕЖІ
    # root (аудит релізу 31.07). Роздільники/".."/абсолютний шлях → фолбек
    # на фактичну назву теки в кошику, вона суфіксована й безпечна.
    if (not isinstance(name, str) or Path(name).is_absolute()
            or len(Path(name).parts) != 1 or name in (".", "..")):
        name = trashed_dir.name
    info_path = trashed_dir / _INFO_NAME
    if info_path.exists():
        try:
            info_path.unlink()
        except OSError:
            logging.exception("restore: не вдалося прибрати %s", info_path)
    dest = _unique_dir(root, name)
    shutil.move(str(trashed_dir), str(dest))
    return dest


def purge_expired(root, *, max_age_days: float = DEFAULT_MAX_AGE_DAYS,
                  now: "float | None" = None) -> list:
    """Остаточно видалити теки кошика, старші за ``max_age_days``.

    Best-effort: збій прибирання однієї теки не спиняє решту. Повертає перелік
    фізично видалених шляхів (для суміжного прибирання voice_memory тощо)."""
    root = Path(root)
    tdir = trash_root(root)
    if not tdir.is_dir():
        return []
    now = time.time() if now is None else now
    cutoff = now - max_age_days * 86400
    purged = []
    for entry in sorted(tdir.iterdir()):
        if not entry.is_dir():
            continue
        info = _read_info(entry)
        trashed_at = info.get("trashed_at")
        if not isinstance(trashed_at, (int, float)):
            try:
                trashed_at = entry.stat().st_mtime
            except OSError:
                continue
        if trashed_at > cutoff:
            continue
        try:
            shutil.rmtree(entry)
        except OSError:
            logging.exception("purge_expired: не вдалося прибрати %s", entry)
        else:
            purged.append(entry)
    return purged
