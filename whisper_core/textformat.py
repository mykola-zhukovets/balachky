"""feature/output-formats — детерміновані профілі форматування виводу диктування.

Чиста функція без Qt, без ШІ і без стану: надиктований текст → переформатований за
обраним профілем. Правила навмисно прості й передбачувані (не модель): той самий
вхід завжди дає той самий вихід, тож кожен режим покривається юнітом на прикладі.

Режими:
  * ``plain``    — звичайний: нормалізувати пробіли, без структурних змін.
  * ``markdown`` — переліки: кожне речення стає пунктом списку «- …».
  * ``code``     — код: дослівно, зі збереженням відступів; без автопунктуації
                   (не додаємо крапок, не змінюємо регістр), лише прибираємо
                   хвостові пробіли рядків.
  * ``letter``   — лист: кожен рядок вводу стає окремим абзацом (порожній рядок
                   між абзацами), пробіли всередині абзацу нормалізуються.

Застосовується у конвеєрі виводу диктування (fronts/desktop/app.py) залежно від
поведінки контекстного профілю (Behavior.formatting).
"""
from __future__ import annotations

import re

PLAIN = "plain"
MARKDOWN = "markdown"
CODE = "code"
LETTER = "letter"

#: усі підтримувані режими (порядок = порядок у випадному списку UI)
MODES = (PLAIN, MARKDOWN, CODE, LETTER)

#: межа речення: термінальний знак (. ! ? …) + пробіл(и). lookbehind лишає знак
#: у складі речення, split ковтає пробіли.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_SPACES = re.compile(r"[ \t]+")


def _norm_spaces(text: str) -> str:
    """Стиснути пробіли/таби в один пробіл і обрізати краї."""
    return _SPACES.sub(" ", text).strip()


def split_sentences(text: str) -> list[str]:
    """Розбити текст на речення за термінальними знаками. Порожні — відкидаємо."""
    return [s for s in (p.strip() for p in _SENTENCE_SPLIT.split(text or "")) if s]


def _plain(text: str) -> str:
    return _norm_spaces(text)


def _markdown(text: str) -> str:
    sentences = split_sentences(text)
    if not sentences:
        return ""
    return "\n".join(f"- {s}" for s in sentences)


def _code(text: str) -> str:
    # Дослівно: зберігаємо провідні відступи, прибираємо лише хвостові пробіли
    # рядків. Нічого не додаємо (без автопунктуації) і не змінюємо регістр.
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _letter(text: str) -> str:
    # Кожен непорожній рядок вводу — окремий абзац; пробіли всередині абзацу
    # нормалізуємо, абзаци розділяємо порожнім рядком.
    paras = [_norm_spaces(ln) for ln in (text or "").splitlines()]
    paras = [p for p in paras if p]
    return "\n\n".join(paras)


_APPLY = {
    PLAIN: _plain,
    MARKDOWN: _markdown,
    CODE: _code,
    LETTER: _letter,
}


def apply_format(text: str, mode: str) -> str:
    """Переформатувати ``text`` за режимом ``mode``. Невідомий режим або порожній
    текст → ``plain`` (нормалізація без структурних змін). Функція чиста."""
    if not text:
        return ""
    return _APPLY.get(mode, _plain)(text)
