"""Зведення по історії диктувань: «скільки я наговорив».

Чиста функція над history.jsonl — рахує лише з полів, що ВЖЕ є у схемі
(ts, final/raw). Тривалості аудіо у схемі немає, тож її тут немає теж.
Читання (і пропуск битих рядків) — через whisper_core.history.read_recent.
"""
import time
from datetime import date

from .history import read_recent

#: типова швидкість набору руками (слів/хв) для оцінки зекономленого часу.
#: Джерело припущення підписуємо в UI-підказці (feature/ux-center).
TYPING_WPM = 40


def estimate_saved_minutes(words: int, wpm: int = TYPING_WPM) -> float:
    """Груба оцінка зекономленого часу проти набору руками, у хвилинах: words/wpm.

    ПРИПУЩЕННЯ (підписуємо в UI): типова швидкість набору ~40 слів/хв; фактичний
    час диктування НЕ віднімається, бо в історії немає тривалості аудіо. Тож це
    оцінка зверху — «стільки б зайняв набір цих слів руками».
    """
    if wpm <= 0:
        return 0.0
    return max(0, int(words)) / float(wpm)


def _day_ordinal(ts) -> int:
    lt = time.localtime(ts)
    return date(lt.tm_year, lt.tm_mon, lt.tm_mday).toordinal()


def streak_days(source, now=None) -> int:
    """Скільки днів поспіль (включно з сьогодні) є хоча б один запис у історії.

    Розрив = день без записів. Якщо сьогодні записів ще немає, але вчора були —
    стрік рахуємо від учора (щоб ранок без диктування не обнуляв учорашній
    результат). Записи без числового ts у стрік не входять.
    """
    if now is None:
        now = time.time()
    days = set()
    for _line, rec in read_recent(source):
        ts = rec.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            days.add(_day_ordinal(ts))
    if not days:
        return 0
    today = _day_ordinal(now)
    if today in days:
        cur = today
    elif (today - 1) in days:
        cur = today - 1
    else:
        return 0
    count = 0
    while cur in days:
        count += 1
        cur -= 1
    return count


def summarize(source, now=None):
    """Підсумок історії: сьогодні / останні 7 днів / за весь час.

    source — Profile (має .history_path), Path або str зі шляхом до history.jsonl.
    now — поточний unix-час (для тестів); None → time.time().

    Повертає dict {'today', 'week', 'all'}, де кожен — {'records', 'words'}:
    кількість записів і кількість слів (просте розбиття по пробілах у final,
    фолбек — raw). «today» — від локальної півночі; «week» — останні 7 діб.
    Записи без числового ts потрапляють лише у «all». Биті рядки jsonl
    пропускає read_recent.
    """
    if now is None:
        now = time.time()
    lt = time.localtime(now)
    day_start = now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    week_start = now - 7 * 86400

    buckets = {
        "today": {"records": 0, "words": 0},
        "week": {"records": 0, "words": 0},
        "all": {"records": 0, "words": 0},
    }
    for _line, rec in read_recent(source):
        text = rec.get("final") or rec.get("raw") or ""
        words = len(text.split())
        buckets["all"]["records"] += 1
        buckets["all"]["words"] += words
        ts = rec.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        if ts >= week_start:
            buckets["week"]["records"] += 1
            buckets["week"]["words"] += words
        if ts >= day_start:
            buckets["today"]["records"] += 1
            buckets["today"]["words"] += words
    return buckets
