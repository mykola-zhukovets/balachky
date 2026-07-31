"""Легкий парсер CHANGELOG.md для картки «Що нового» вкладки «Про програму».

Не тягне ввесь файл (23 КБ) у пам'ять UI одразу довгим рядком — читає його
раз, ділить на версійні блоки за заголовками ``## [X.Y.Z] - дата`` (без
"Unreleased") і повертає лише марковані пункти обраної мови для перших
``max_versions`` версій. Заголовки розділів ("### Added"/"Changed"/"Fixed")
та мовні підзаголовки ("**Українською:**"/"**In English:**") у показ не
йдуть — інтерфейс дає людині суцільний список, без сирої структури файлу.
"""
from __future__ import annotations

import re
from pathlib import Path

_VERSION_HEADER = re.compile(r"^## \[(?P<ver>[^\]]+)\](?:\s*-\s*(?P<date>.+))?$")
_SECTION_HEADER = re.compile(r"^### (?P<name>Added|Changed|Fixed)\s*$")
_LANG_HEADER = re.compile(r"^\*\*(?P<lang>Українською|In English):\*\*\s*$")
_BULLET = re.compile(r"^-\s+(?P<text>.+)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _strip_markdown_bold(text: str) -> str:
    """"**Заголовок.** решта" → "<b>Заголовок.</b> решта" (легкий HTML для QLabel)."""
    return _BOLD.sub(r"<b>\1</b>", text)


def _parse_entry_body(body_lines):
    """{"Added": [(lang, text), ...], "Changed": [...], "Fixed": [...]}."""
    sections = {"Added": [], "Changed": [], "Fixed": []}
    section = None
    lang = None
    for line in body_lines:
        stripped = line.strip()
        m = _SECTION_HEADER.match(stripped)
        if m:
            section, lang = m.group("name"), None
            continue
        m = _LANG_HEADER.match(stripped)
        if m:
            lang = "uk" if m.group("lang") == "Українською" else "en"
            continue
        m = _BULLET.match(stripped)
        if m and section and lang:
            sections[section].append((lang, m.group("text")))
    return sections


def _parse_versions(text: str):
    """[(version, date, sections), ...] у порядку файлу, без "Unreleased"."""
    lines = text.splitlines()
    headers = []
    for i, line in enumerate(lines):
        m = _VERSION_HEADER.match(line.strip())
        if m and m.group("ver").strip().lower() != "unreleased":
            headers.append((i, m.group("ver").strip(), (m.group("date") or "").strip()))
    entries = []
    for idx, (start, ver, date) in enumerate(headers):
        end = headers[idx + 1][0] if idx + 1 < len(headers) else len(lines)
        entries.append((ver, date, _parse_entry_body(lines[start + 1:end])))
    return entries


def latest_entries(path, lang: str = "uk", max_versions: int = 2):
    """Останні ``max_versions`` версій із CHANGELOG.md за ``path``, готові до
    показу: [{"version": str, "date": str, "items": [html-рядок, ...]}, ...].
    items — злиті розділи Added/Changed/Fixed обраної мови, без заголовків
    розділів (короткий людяний перелік, не технічна структура файлу).
    Файл відсутній/нечитний → порожній список (виклик має показати запасний
    текст, а не впасти)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for ver, date, sections in _parse_versions(text)[:max_versions]:
        items = [
            _strip_markdown_bold(item_text)
            for section_name in ("Added", "Changed", "Fixed")
            for item_lang, item_text in sections[section_name]
            if item_lang == lang
        ]
        out.append({"version": ver, "date": date, "items": items})
    return out
