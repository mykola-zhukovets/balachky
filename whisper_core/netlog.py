"""Журнал мережевої активності — легкий монітор наших вихідних з'єднань.

Доказова офлайновість (мілітарі-довіра): записуємо ЛИШЕ факт спроби + хост +
тип, БЕЗ вмісту (ані тексту, ані аудіо, ані заголовків запиту). У нормі журнал
порожній: єдиний легітимний вихід у мережу — завантаження моделей/компонентів
та (за окремою згодою) перевірка й завантаження оновлень. Саме ці записи
позначаються allowed=True.

Це НЕ системний сніфер: перехоплення на рівні НАШИХ власних точок виходу (там,
де ми самі кличемо urllib / huggingface). Ми не читаємо чужий трафік і не
чіпаємо мережу ОС. Користувач може незалежно звірити журнал із Resource Monitor
чи Wireshark — довідка «Як перевірити самому» у діалозі журналу.

Модуль — ЧИСТЕ ЯДРО (без Qt). Джерело правди — файл network_log.jsonl у теці
користувача: диктування-потоки (запис) і головний потік (показ) бачать
однаковий стан без спільної пам'яті. Логування — best-effort: будь-яка помилка
запису НЕ зупиняє саме завантаження.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from pathlib import Path

from . import paths

#: типи легітимного виходу
MODEL = "model"      #: завантаження моделі / компонента постобробки
UPDATE = "update"    #: перевірка або завантаження оновлення програми
OTHER = "other"      #: будь-що інше → allowed=False (у нормі не трапляється)

#: скільки останніх записів тримаємо (журнал у нормі майже порожній)
_MAX = 500

_LOCK = threading.Lock()


def _default_path() -> Path:
    return paths.user_dir() / "network_log.jsonl"


def _resolve(path) -> Path:
    return Path(path) if path is not None else _default_path()


def record(host, *, kind: str = OTHER, allowed: bool = False,
           detail: str = "", path=None) -> dict:
    """Записати одну спробу вихідного з'єднання (факт + хост + тип, без вмісту).

    Ніколи не кидає: помилку запису ковтаємо, щоб логування не зламало саме
    завантаження. Повертає створений запис (для тестів/діагностики)."""
    entry = {
        "ts": time.time(),
        "host": str(host or "?"),
        "kind": kind if kind in (MODEL, UPDATE, OTHER) else OTHER,
        "allowed": bool(allowed),
        "detail": str(detail or ""),
    }
    line = json.dumps(entry, ensure_ascii=False)
    target = _resolve(path)
    with _LOCK:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            _trim_locked(target)
        except OSError:
            pass  # best-effort: журнал не критичний для роботи
    return entry


def record_url(url, *, kind: str, allowed: bool = True,
               detail: str = "", path=None) -> dict:
    """Як record(), але хост дістаємо з URL. Наші легітимні точки виходу знають
    саме URL, тож це найзручніший вхід. allowed=True за замовчуванням — усі наші
    власні виклики (моделі/оновлення) очікувані."""
    host = None
    try:
        # .hostname (не .netloc): відкидає шлях, параметри запиту й будь-який
        # user:pass@ — у журнал НЕ потрапляє ні вміст, ні локальний шлях. Немає
        # хоста (file://, схема відсутня) → "?", а не сирий URL зі шляхом.
        host = urllib.parse.urlparse(str(url)).hostname
    except (TypeError, ValueError):
        host = None
    return record(host or "?", kind=kind, allowed=allowed,
                  detail=detail, path=path)


def entries(path=None) -> list:
    """Усі записи журналу в хронологічному порядку (старі → нові). Немає файлу —
    порожній список (нормальний стан офлайн-роботи). Биті рядки пропускаємо."""
    target = _resolve(path)
    out = []
    with _LOCK:
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(rec, dict) and "host" in rec:
            out.append(rec)
    return out[-_MAX:]


def summary(path=None) -> dict:
    """Зведення: скільки всього записів, скільки дозволених, скільки
    непередбачених (allowed=False), і час останнього запису (epoch або None)."""
    rows = entries(path)
    allowed = sum(1 for r in rows if r.get("allowed"))
    flagged = len(rows) - allowed
    last_ts = rows[-1].get("ts") if rows else None
    return {"total": len(rows), "allowed": allowed,
            "flagged": flagged, "last_ts": last_ts}


def clear(path=None) -> None:
    """Очистити журнал (кнопка користувача / ізоляція тестів). Немає файлу —
    no-op."""
    target = _resolve(path)
    with _LOCK:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass


def _trim_locked(target: Path) -> None:
    """Тримати файл у межах _MAX рядків (перезапис лише коли реально розрісся —
    у нормі журнал крихітний, тож це майже ніколи не спрацьовує). Викликається
    під _LOCK."""
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= _MAX:
        return
    try:
        target.write_text("\n".join(lines[-_MAX:]) + "\n", encoding="utf-8")
    except OSError:
        pass
