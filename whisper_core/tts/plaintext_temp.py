"""Володіння plaintext-аудіо у %TEMP% (§8.9, закриває блокер 6).

Озвучення розшифрованого протоколу із ЗАПЕЧАТАНОЇ наради пише plaintext-WAV у %TEMP%.
`InlinePlayer.stop()` лише відпускає handle — файл лишається; після cancel/крашу на
диску лишилась би аудіоверсія конфіденційного тексту. Модель володіння:

  • батько володіє TemporaryDirectory(prefix="balachky-tts-plain-") — це `wav_dir`,
    що передається воркеру; уся озвучка всередині;
  • прибирання на stop/error/cancel/exit; на СТАРТІ — cleanup_stale (crash-recovery);
  • у temp-імені й логах — НІКОЛИ текст/назва наради (лише випадковий суфікс).

БЕЗ Qt."""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from . import PLAINTEXT_TEMP_PREFIX

_log = logging.getLogger("balachky.tts")


class PlaintextAudioDir:
    """Тимчасова тека-власник озвучки одного відтворення/експорту. Контекст-менеджер:
    гарантує прибирання на виході (stop/error/cancel), навіть за винятку."""

    def __init__(self):
        self._dir = tempfile.mkdtemp(prefix=PLAINTEXT_TEMP_PREFIX)

    @property
    def path(self) -> str:
        return self._dir

    def cleanup(self) -> bool:
        """Прибрати теку. Повертає True, якщо тека фізично зникла; False — якщо
        лишилась (Windows-лок на handle плеєра) → викликач кладе в retry-list (§8.9)."""
        path = self._dir
        if not path:
            return True
        shutil.rmtree(path, ignore_errors=True)
        if os.path.isdir(path):
            _log.warning("не вдалося прибрати temp озвучення")   # БЕЗ вмісту в логу
            return False                   # лишилось (лок) — власник повторить пізніше
        self._dir = ""
        return True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()


def cleanup_stale(max_age_seconds: int = 3600) -> int:
    """Прибрати залишені крашем `balachky-tts-plain-*` теки (crash-recovery на старті).
    Повертає кількість прибраних. Тек молодших за max_age не чіпає (можлива активна)."""
    now = time.time()
    root = Path(tempfile.gettempdir())
    removed = 0
    for path in root.glob(PLAINTEXT_TEMP_PREFIX + "*"):
        try:
            if not path.is_dir():
                continue
            # max_age<=0 (старт застосунку) → прибираємо ВСІ (активної озвучки немає).
            # НЕ звіряємо вік: на Windows mtime щойно створеної теки може бути трохи
            # «в майбутньому» відносно time.time() (coarse-resolution) → age-перевірка
            # флакала й лишала свіжу crash-temp (конфіденційне аудіо).
            if max_age_seconds <= 0 or now - path.stat().st_mtime >= max_age_seconds:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            _log.warning("не вдалося прибрати застарілий temp озвучення")
    return removed
