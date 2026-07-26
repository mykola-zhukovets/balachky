"""Мінімальний парсер iCalendar (.ics) для авто-назви наради.

Тільки stdlib — без залежностей. Витягуємо з VEVENT-блоків три поля: SUMMARY,
DTSTART, DTEND. Мета вузька: коли користувач зберігає нараду, підказати назву тієї
події календаря, що покриває час запису (suggest_meeting_name).

Часові зони:
  • «…Z» — UTC (aware);
  • «;TZID=Область/Місто:…» — через zoneinfo, якщо зона відома (aware);
  • без суфікса й TZID — локальний час (naive).
Порівняння часу — через POSIX-timestamp: naive-datetime Python рахує як локальний,
aware — за своєю зоною, тож наведення до спільної шкали виходить безкоштовно.

RFC 5545: довгі рядки складають (folding) — продовження починається з пробілу
або табуляції; перед розбором розкладаємо назад (_unfold).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:                     # pragma: no cover — Python < 3.9
    ZoneInfo = None


def _unfold(text: str) -> list:
    """Розкласти складені рядки RFC 5545 назад в один логічний рядок кожен.
    Продовження (рядок, що починається з пробілу/табуляції) приклеюється до
    попереднього, а той один пробіл/таб відкидається."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for line in text.split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _split_prop(line: str) -> "tuple | None":
    """Рядок «NAME;PARAM=val:VALUE» → (name, {params}, value). Без ':' → None."""
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].upper()
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v
    return name, params, value.strip()


def _parse_dt(value: str, params: dict) -> "datetime | None":
    """Розібрати DTSTART/DTEND-значення у datetime. Формати:
    20260717T090000Z (UTC) / 20260717T090000 (+TZID або локальний) / 20260717."""
    v = value.strip()
    try:
        if v.endswith("Z"):
            return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc)
        if "T" in v:
            dt = datetime.strptime(v, "%Y%m%dT%H%M%S")
            tzid = params.get("TZID")
            if tzid and ZoneInfo is not None:
                try:
                    dt = dt.replace(tzinfo=ZoneInfo(tzid))
                except Exception:
                    pass                # невідома зона → лишаємо локальний naive
            return dt
        return datetime.strptime(v, "%Y%m%d")   # подія на цілий день
    except ValueError:
        return None


def parse_ics(text: str) -> list:
    """Розібрати текст .ics у список подій. Кожна подія — dict:
    {"summary": str, "start": datetime|None, "end": datetime|None}. Події поза
    VEVENT або без SUMMARY ігноруємо."""
    events = []
    cur = None
    for line in _unfold(text):
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            cur = {"summary": "", "start": None, "end": None}
            continue
        if stripped == "END:VEVENT":
            if cur is not None and cur["summary"]:
                events.append(cur)
            cur = None
            continue
        if cur is None:
            continue
        parsed = _split_prop(line)
        if parsed is None:
            continue
        name, params, value = parsed
        if name == "SUMMARY":
            cur["summary"] = value
        elif name == "DTSTART":
            cur["start"] = _parse_dt(value, params)
        elif name == "DTEND":
            cur["end"] = _parse_dt(value, params)
    return events


def parse_ics_file(path) -> list:
    """parse_ics для файлу. Нема файлу / не читається → []."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    return parse_ics(text)


def _ts(dt: datetime) -> float:
    """POSIX-timestamp: naive → локальний час, aware → за своєю зоною."""
    return dt.timestamp()


def suggest_meeting_name(ics_path, at_time: datetime) -> "str | None":
    """Назва події календаря, що покриває момент at_time (start <= at_time < end).
    Кілька подій покривають → перша за DTSTART. Немає покриття / файлу → None."""
    at = _ts(at_time)
    hits = []
    for ev in parse_ics_file(ics_path):
        start, end = ev["start"], ev["end"]
        if start is None or end is None:
            continue
        if _ts(start) <= at < _ts(end):
            hits.append((_ts(start), ev["summary"]))
    if not hits:
        return None
    hits.sort(key=lambda h: h[0])
    return hits[0][1]
