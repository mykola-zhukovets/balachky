"""Пошук збігів у тексті для перегляду розшифровки (feature/transcript-editing).

Чиста логіка без Qt — щоб покривалась unittest discover. UI (підсвітка,
лічильник, кнопки) — у pages/edit_search.py, тут лише пошук позицій і
навігація по збігах.
"""


def find_matches(text: str, query: str) -> list:
    """Позиції (start, end) усіх збігів query у text, без урахування регістру.

    Порожній query (або порожній після strip нема — регістр лишаємо як є, але
    порожній рядок) → []. Збіги не перетинаються: далі шукаємо від кінця збігу.
    """
    if not query:
        return []
    hay = text.lower()
    needle = query.lower()
    out = []
    start = 0
    n = len(needle)
    while True:
        i = hay.find(needle, start)
        if i < 0:
            break
        out.append((i, i + n))
        start = i + n
    return out


def step_index(current: int, total: int, forward: bool) -> int:
    """Наступний індекс збігу з циклічним переходом через край.

    total == 0 → -1 (немає збігів). current поза межами → 0 (перший).
    """
    if total <= 0:
        return -1
    if current < 0 or current >= total:
        return 0
    return (current + (1 if forward else -1)) % total
