"""Позиція плаваючого індикатора диктування (feature/ux-center).

Чисті функції (без Qt) — щоб тестувати без екрана. Екрани подаються списком
прямокутників (x, y, w, h) у глобальних координатах (як QScreen.geometry()).
"""


def rect_contains_point(rect, x, y) -> bool:
    rx, ry, rw, rh = rect
    return rx <= x <= rx + rw and ry <= y <= ry + rh


def is_visible(x, y, screens) -> bool:
    """Чи лежить лівий-верхній кут пілюлі бодай на якомусь екрані. Монітор міг
    зникнути / роздільність змінитись → збережена позиція «за кадром»."""
    return any(rect_contains_point(s, x, y) for s in screens)


def resolved_position(saved, screens, default):
    """saved=(x, y)|None; default=(x, y). Повертає позицію для показу: збережену,
    якщо її кут ще у межах якогось екрана; інакше — типову (монітор від'єднали)."""
    if saved is not None and saved[0] is not None and saved[1] is not None:
        x, y = int(saved[0]), int(saved[1])
        if is_visible(x, y, screens):
            return (x, y)
    return (int(default[0]), int(default[1]))
