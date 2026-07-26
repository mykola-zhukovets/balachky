"""feature/filler-cleanup — авточистка слів-паразитів (opt-in, ЕКСПЕРИМЕНТАЛЬНА).

Чиста функція без Qt і без стану. РІВЕНЬ 1 ТІЛЬКИ, консервативно:
  (а) вокалізації-хезитації як ОКРЕМІ слова (е, е-е, еее, ем, ммм, гм, а-а…);
  (б) той самий кириличний токен двічі підряд через пробіл → одне
      («я я хотів» → «я хотів»).

Свідомо НЕ чіпаємо: змістовні слова (ну, типу, коротше, значить) — це рівень 2,
його не робимо; навмисні дефісні повтори («дуже-дуже») — це один токен;
числа — вони взагалі не токени-слова.

Порядок проходів має значення:
  1) СПЕРШУ стиск повторів — на ОРИГІНАЛЬНИХ сусідствах. Тому філер, вирізаний
     між двома однаковими словами, НЕ робить їх «повтором»: «так е так» → «так так»
     (емфатичний повтор зберігається, а не тихо злипається в «так»).
  2) ПОТІМ видалення філерів — при цьому термінальна пунктуація (. ! ? …), що
     стоїть одразу за філером наприкінці речення, ЗБЕРІГАЄТЬСЯ: філер вирізаємо
     як слово, зайвий пробіл прибираємо, а сам знак лишається на місці
     («Готово е-е. Почнемо.» → «Готово. Почнемо.», а не «Готово Почнемо.»).

Якщо чистити нічого — повертаємо текст незмінним (ранній вихід, байт-у-байт).
"""
import re

# feature/filler-cleanup
# Кириличні літери укр. абетки (діапазон а-я НЕ покриває і/ї/є/ґ — вони в окремих
# позиціях блоку U+0400, тож додаємо явно).
_UK = "а-яА-ЯіїєґІЇЄҐёЁ"

# Токен-слово: будь-який ряд літер/цифр із внутрішніми дефісами й апострофами
# (щоб «дуже-дуже», «будь-який», «з'їв» лишались ОДНИМ токеном). Усе між токенами —
# пробіли й розділові — потрапляє в «проміжки», тож видалення філера ніколи не
# зачепить сусіднє слово чи число.
_WORD_RE = re.compile(r"\w+(?:[-’'ʼ]\w+)*", re.UNICODE)

# Вокалізації рівня 1. fullmatch: весь токен = один із варіантів (незалежно від
# регістру). «3+» узагальнення — лише там, де вони явно задані у ТЗ; решта —
# точні форми, зайвого не прибираємо.
_FILLER_RE = re.compile(
    r"е|ее|е{3,}|е-е|ем|емм|м-м|м{3,}|гм|гмм|а-а|а{3,}",
    re.IGNORECASE,
)

# Кириличний токен (для звірки повторів): лише укр. літери з внутрішніми
# дефісами/апострофами. Латиниця й числа сюди не підходять — їх не збираємо.
_CYR_WORD_RE = re.compile(rf"[{_UK}]+(?:[-’'ʼ][{_UK}]+)*")

# feature/clean-mix: рівні агресивності автоочистки.
#   off    — не чіпаємо;
#   light  — лише вокалізації-хезитації (е-е, ммм, гм…);
#   medium — light + стиск повторів слів (як історична «повна» чистка);
#   strong — medium + дискурсивні вставні («ну, типу, коротше…») — переформулювання пауз.
LEVELS = ("off", "light", "medium", "strong")

# Дискурсивні слова-паразити рівня «Сильна»: вставні, що позначають перезапуск
# чи паузу думки. Прибираємо лише як ОКРЕМІ токени (fullmatch), не всередині слів.
_DISCOURSE_RE = re.compile(
    r"ну|типу|тіпа|коротше|значить|тобто|отже|власне|наче",
    re.IGNORECASE,
)

# Термінальна пунктуація — знаки кінця речення. Проміжок за філером, що містить
# такий знак, НЕ їмо: інакше зникає межа речення (DEFECT 1).
_TERMINAL_RE = re.compile(r"[.!?…]")

_SPACES_RE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r" +([,.!?:;)])")


def _is_filler(word: str, include_discourse: bool = False) -> bool:
    if _FILLER_RE.fullmatch(word) is not None:
        return True
    return include_discourse and _DISCOURSE_RE.fullmatch(word) is not None


def _is_cyrillic(word: str) -> bool:
    return _CYR_WORD_RE.fullmatch(word) is not None


def _is_space_only(gap: str) -> bool:
    return bool(gap) and gap.strip(" \t") == ""


def _has_terminal(gap: str) -> bool:
    return _TERMINAL_RE.search(gap) is not None


def _same_repeat(prev: str, cur: str) -> bool:
    """Чи два кириличні токени — той самий повтор для стиску?

    Регістронечутливо для слів у 2+ літери («Привіт привіт» → стутер, стискаємо).
    Для ОДНОБУКВЕНИХ вимагаємо ТОЧНИЙ регістр: інакше сентенс-початкова велика
    зливається з лексемою-сполучником («А а потім» помилково → «А потім»,
    з'їдаючи сполучник «а») — DEFECT 3. Тож «я я» стискаємо, «А а» лишаємо.
    """
    if prev.lower() != cur.lower():
        return False
    return len(cur) > 1 or prev == cur


def _tokenize(text: str):
    """text → впорядкований список (is_word, s): слова та проміжки між ними."""
    items = []
    last = 0
    for m in _WORD_RE.finditer(text):
        if m.start() > last:
            items.append((False, text[last:m.start()]))
        items.append((True, m.group()))
        last = m.end()
    if last < len(text):
        items.append((False, text[last:]))
    return items


def _tidy(text: str, lead_upper: bool) -> str:
    """Пост-обробка: стиснути подвійні пробіли, прибрати пробіл перед розділовими,
    зрізати краї; повернути велику літеру на початку, якщо вона там була."""
    text = _SPACES_RE.sub(" ", text)
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    text = text.strip(" \t")
    if lead_upper and text[:1].islower():
        text = text[0].upper() + text[1:]
    return text


def apply_filler_cleanup(text: str, level: str = "medium") -> str:
    """text → text без слів-паразитів згідно з рівнем агресивності (LEVELS).
    «off» або порожній вхід повертаємо незмінним; якщо чистити нічого — теж
    ранній вихід байт-у-байт."""
    level = level if level in LEVELS else "medium"
    if not text or level == "off":
        return text
    collapse_repeats = level in ("medium", "strong")   # стиск повторів — з «Середня»
    include_discourse = level == "strong"              # дискурсивні вставні — лише «Сильна»

    items = _tokenize(text)
    changed = False

    # Прохід 1 — стиснути повтор того самого кириличного слова через ПРОБІЛ.
    # Робимо ЦЕ ДО видалення філерів, щоб філер, що стоїть МІЖ двома однаковими
    # словами, не робив їх сусідами-«повтором» (DEFECT 2): «так е так» → «так так»,
    # а не «так». Кома/крапка між словами = намір, не заїкання: такі не чіпаємо.
    # Рівень «Легка» пропускає цей прохід — лише вирізає вокалізації.
    collapsed = []
    for is_word, s in items:
        if (collapse_repeats and is_word and _is_cyrillic(s) and len(collapsed) >= 2
                and not collapsed[-1][0] and _is_space_only(collapsed[-1][1])
                and collapsed[-2][0] and _same_repeat(collapsed[-2][1], s)):
            changed = True
            collapsed.pop()                 # прибрати проміжок; перший повтор лишаємо
            continue
        collapsed.append((is_word, s))

    # Прохід 2 — прибрати філери-вокалізації. Разом із філером зʼїдаємо ОДИН
    # сусідній проміжок (наступний, а на кінці — попередній), щоб не лишалось ні
    # подвійних пробілів, ні осиротілих ком. АЛЕ: якщо проміжок за філером несе
    # термінальний знак (. ! ? …) і перед філером стоїть слово — знак ЗБЕРІГАЄМО
    # (він закриває попереднє речення), прибираємо лише зайвий пробіл (DEFECT 1).
    stripped = []
    i, n = 0, len(collapsed)
    while i < n:
        is_word, s = collapsed[i]
        if is_word and _is_filler(s, include_discourse):
            changed = True
            nxt_gap = collapsed[i + 1][1] if (i + 1 < n and not collapsed[i + 1][0]) else None
            if (nxt_gap is not None and _has_terminal(nxt_gap)
                    and len(stripped) >= 2 and stripped[-2][0]
                    and not stripped[-1][0] and _is_space_only(stripped[-1][1])):
                stripped.pop()              # прибрати лише пробіл перед філером
                i += 1                      # слово геть; проміжок-зі-знаком лишається
            elif nxt_gap is not None:
                i += 2                      # слово + звичайний проміжок за ним
            else:
                if stripped and not stripped[-1][0]:
                    stripped.pop()          # філер у кінці → проміжок перед ним
                i += 1
            continue
        stripped.append((is_word, s))
        i += 1

    if not changed:
        return text                         # філерів немає — не чіпаємо

    lead_upper = text.lstrip()[:1].isupper()
    return _tidy("".join(s for _, s in stripped), lead_upper)
