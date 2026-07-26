"""Правило-базований нормалізатор тексту перед G2P рушія (§5 спеки).

Чисте ядро БЕЗ Qt і БЕЗ важких залежностей. Викликається у воркері ПЕРЕД
G2P рушія. Нейровербалізатор відкинуто (завеликий для frozen + бреше на
абревіатурах: розгортає їх у вигадані слова).

`normalize(text, *, abbrev_map)` повертає `NormResult` з нормалізованим текстом і
offset-map (сирий↔нормалізований) — перша ланка наскрізного span-map (§8.2), який
у повному обсязі добудовує Хвиля 2. Ідемпотентний на вже-словесному тексті.

Обсяг правил v1 — рівно §5.1, не більше. Узгодження роду/відмінка для звичайних
цілих НЕ робимо (читаємо в називному) — це прийнятно для вичитки на слух.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- числівники (називний відмінок) ------------------------------------------

_ONES = ["нуль", "один", "два", "три", "чотири", "п'ять", "шість", "сім",
         "вісім", "дев'ять"]
_ONES_F = {1: "одна", 2: "дві"}          # жіночий рід (тисячі, ціла частина дробу)
_TEENS = ["десять", "одинадцять", "дванадцять", "тринадцять", "чотирнадцять",
          "п'ятнадцять", "шістнадцять", "сімнадцять", "вісімнадцять", "дев'ятнадцять"]
_TENS = {2: "двадцять", 3: "тридцять", 4: "сорок", 5: "п'ятдесят", 6: "шістдесят",
         7: "сімдесят", 8: "вісімдесят", 9: "дев'яносто"}
_HUNDREDS = {1: "сто", 2: "двісті", 3: "триста", 4: "чотириста", 5: "п'ятсот",
             6: "шістсот", 7: "сімсот", 8: "вісімсот", 9: "дев'ятсот"}

_MONTHS_GEN = {1: "січня", 2: "лютого", 3: "березня", 4: "квітня", 5: "травня",
               6: "червня", 7: "липня", 8: "серпня", 9: "вересня", 10: "жовтня",
               11: "листопада", 12: "грудня"}

# порядкові (день — середній рід називний): «двадцять третє»
_ORD_N = {1: "перше", 2: "друге", 3: "третє", 4: "четверте", 5: "п'яте",
          6: "шосте", 7: "сьоме", 8: "восьме", 9: "дев'яте", 10: "десяте",
          11: "одинадцяте", 12: "дванадцяте", 13: "тринадцяте", 14: "чотирнадцяте",
          15: "п'ятнадцяте", 16: "шістнадцяте", 17: "сімнадцяте", 18: "вісімнадцяте",
          19: "дев'ятнадцяте", 20: "двадцяте", 30: "тридцяте"}
# порядкові (година — жіночий рід називний): «чотирнадцята»
_ORD_F = {0: "нульова", 1: "перша", 2: "друга", 3: "третя", 4: "четверта",
          5: "п'ята", 6: "шоста", 7: "сьома", 8: "восьма", 9: "дев'ята",
          10: "десята", 11: "одинадцята", 12: "дванадцята", 13: "тринадцята",
          14: "чотирнадцята", 15: "п'ятнадцята", 16: "шістнадцята", 17: "сімнадцята",
          18: "вісімнадцята", 19: "дев'ятнадцята", 20: "двадцята", 30: "тридцята"}
# порядкові (рік — чоловічий родовий, «...шостого року»)
_ORD_G = {1: "першого", 2: "другого", 3: "третього", 4: "четвертого", 5: "п'ятого",
          6: "шостого", 7: "сьомого", 8: "восьмого", 9: "дев'ятого", 10: "десятого",
          11: "одинадцятого", 12: "дванадцятого", 13: "тринадцятого",
          14: "чотирнадцятого", 15: "п'ятнадцятого", 16: "шістнадцятого",
          17: "сімнадцятого", 18: "вісімнадцятого", 19: "дев'ятнадцятого",
          20: "двадцятого", 30: "тридцятого", 40: "сорокового", 50: "п'ятдесятого",
          60: "шістдесятого", 70: "сімдесятого", 80: "вісімдесятого",
          90: "дев'яностого"}

_UNITS = {"%": "відсотків", "№": "номер", "°": "градусів"}
_MEASURE = {"грн": "гривень", "км": "кілометрів", "кг": "кілограмів", "м": "метрів"}
_FRACTIONS = {"1/2": "одна друга", "1/3": "одна третя", "2/3": "дві третіх",
              "1/4": "одна четверта", "3/4": "три четвертих", "1/5": "одна п'ята"}

# українські назви літер (для побуквеного читання невідомих абревіатур)
_LETTER_NAMES = {
    "А": "а", "Б": "бе", "В": "ве", "Г": "ге", "Ґ": "ґе", "Д": "де", "Е": "е",
    "Є": "є", "Ж": "же", "З": "зе", "И": "и", "І": "і", "Ї": "ї", "Й": "йот",
    "К": "ка", "Л": "ел", "М": "ем", "Н": "ен", "О": "о", "П": "пе", "Р": "ер",
    "С": "ес", "Т": "те", "У": "у", "Ф": "еф", "Х": "ха", "Ц": "це", "Ч": "че",
    "Ш": "ша", "Щ": "ща", "Ю": "ю", "Я": "я",
}

# базовий словник загальновійськових/цивільних скорочень (редагований per-профіль
# зверху). Вимова або побуквено — тут кладемо найпоширеніше.
_BUILTIN_ABBREV = {
    "ЗСУ": "збройні сили україни",
    "ДПСУ": "державна прикордонна служба україни",
    "БПЛА": "безпілотний літальний апарат",
    "КП": "командний пункт",
    "РЛС": "радіолокаційна станція",
    "НП": "спостережний пункт",
    "ОК": "оперативне командування",
}


def _below_1000(n: int, feminine: bool = False) -> list:
    out = []
    h, rem = divmod(n, 100)
    if h:
        out.append(_HUNDREDS[h])
    t, u = divmod(rem, 10)
    if t == 1:
        out.append(_TEENS[u])
    else:
        if t >= 2:
            out.append(_TENS[t])
        if u:
            if feminine and u in _ONES_F:
                out.append(_ONES_F[u])
            else:
                out.append(_ONES[u])
    return out


def _group_noun(n: int, forms: tuple) -> str:
    """Узгодити іменник групи (тисяча/тисячі/тисяч) з числом n (1..999)."""
    one, few, many = forms
    if n % 100 in (11, 12, 13, 14):
        return many
    r = n % 10
    if r == 1:
        return one
    if r in (2, 3, 4):
        return few
    return many


def cardinal(n: int) -> str:
    """Ціле 0..999_999_999 → українські числівники (називний відмінок)."""
    if n == 0:
        return "нуль"
    if n < 0 or n > 999_999_999:
        return str(n)
    parts = []
    millions, rem = divmod(n, 1_000_000)
    thousands, units = divmod(rem, 1000)
    if millions:
        parts += _below_1000(millions)
        parts.append(_group_noun(millions, ("мільйон", "мільйони", "мільйонів")))
    if thousands:
        parts += _below_1000(thousands, feminine=True)
        parts.append(_group_noun(thousands, ("тисяча", "тисячі", "тисяч")))
    if units:
        parts += _below_1000(units)
    return " ".join(parts)


def _ordinal_small(n: int, table: dict) -> "str | None":
    """Порядковий для 0..99 за таблицею одиниць/десятків (складене: десяток
    кардинально + одиниця порядково)."""
    if n in table:
        return table[n]
    t, u = divmod(n, 10)
    if 2 <= t <= 9 and u and u in table and t in _TENS:
        return f"{_TENS[t]} {table[u]}"
    return None


def _year_words(year: int) -> str:
    """Рік → «дві тисячі двадцять шостого» (остання ненульова ланка — порядкова
    родового відмінка, решта — кардинально). Best-effort для 1000..2999; інакше
    кардинально + без порядкового."""
    if year < 1000 or year > 2999:
        return cardinal(year)
    th, rem = divmod(year, 1000)
    prefix = _below_1000(th, feminine=True) + [_group_noun(th, ("тисяча", "тисячі", "тисяч"))]
    if rem == 0:
        # рівні тисячі — рідкісний випадок; читаємо кардинально
        return cardinal(year)
    h, low = divmod(rem, 100)
    words = list(prefix)
    if h:
        words.append(_HUNDREDS[h])
    if low == 0:
        # рік типу 2100 — сотня порядкова, спрощуємо до кардинального
        return cardinal(year)
    ordg = _ordinal_small(low, _ORD_G)
    if ordg is None:
        return cardinal(year)
    words.append(ordg)
    return " ".join(words)


# --- токенізатор із побудовою span-map ---------------------------------------

@dataclass
class NormResult:
    """Нормалізований текст + offset-map (перша ланка наскрізного span-map).

    `spans` — список (out_start, out_end, raw_start, raw_end): діапазон у
    нормалізованому тексті → діапазон у СИРОМУ тексті. Розгорнуте число/абревіатура
    дає ОДИН сирий діапазон на весь свій вихід (§8.2). Незмінені літерні пробіги
    також мають span (тотожне відображення)."""
    text: str
    spans: list = field(default_factory=list)

    def raw_span_at(self, out_pos: int) -> "tuple | None":
        """Сирий діапазон, що покриває позицію out_pos у нормалізованому тексті."""
        for os_, oe, rs, re_ in self.spans:
            if os_ <= out_pos < oe:
                return (rs, re_)
        return None


# порядок альтернатив важливий: час і масштаб — перед цілими; дата — перед DD.MM;
# діапазон — перед окремими числами.
_TOKEN_RE = re.compile(
    r"(?P<date_full>\b\d{1,2}\.\d{1,2}\.\d{4}\b)"
    r"|(?P<time>\b\d{1,2}:\d{2}(?::\d{2})?\b)"
    r"|(?P<ratio>\b\d+:\d+\b)"
    r"|(?P<decimal>\b\d+,\d+\b)"
    r"|(?P<fraction>\b\d+/\d+\b)"
    r"|(?P<range>\b\d+\s*[-–]\s*\d+\b)"
    r"|(?P<date_short>\b\d{1,2}\.\d{1,2}\b)"
    r"|(?P<measure>\b\d+\s?(?:грн|км|кг|м)\b)"
    r"|(?P<integer>\b\d+\b)"
    r"|(?P<abbrev>\b[А-ЯІЇЄҐ]{2,5}\b)"
    r"|(?P<symbol>[%№°+])"
)


def _is_cyr(ch: str) -> bool:
    return bool(ch) and ("а" <= ch.lower() <= "я" or ch in "ґєїіҐЄЇІ")


def _spell_letters(word: str) -> str:
    return " ".join(_LETTER_NAMES.get(ch, ch) for ch in word)


def _render_time(tok: str) -> str:
    parts = tok.split(":")
    h = int(parts[0])
    mnt = int(parts[1])
    hour = _ORD_F.get(h) or _ordinal_small(h, _ORD_F) or cardinal(h)
    out = [hour, "година", cardinal(mnt), "хвилин"]
    if len(parts) == 3:
        out += [cardinal(int(parts[2])), "секунд"]
    return " ".join(out)


def _render_date(tok: str) -> str:
    bits = tok.split(".")
    day = int(bits[0])
    month = int(bits[1])
    day_w = _ORD_N.get(day) or _ordinal_small(day, _ORD_N) or cardinal(day)
    out = [day_w, _MONTHS_GEN.get(month, cardinal(month))]
    if len(bits) == 3:
        out += [_year_words(int(bits[2])), "року"]
    return " ".join(out)


def _render_ratio(tok: str) -> str:
    a, b = tok.split(":")
    return f"{cardinal(int(a))} до {cardinal(int(b))}"


def _render_decimal(tok: str) -> str:
    a, b = tok.split(",")
    whole = _ONES_F.get(int(a)) if int(a) in _ONES_F else cardinal(int(a))
    return f"{whole} кома {cardinal(int(b))}"


def _render_fraction(tok: str) -> str:
    if tok in _FRACTIONS:
        return _FRACTIONS[tok]
    a, b = tok.split("/")
    return f"{cardinal(int(a))} дріб {cardinal(int(b))}"


def _render_range(tok: str) -> str:
    m = re.match(r"(\d+)\s*[-–]\s*(\d+)", tok)
    return f"від {cardinal(int(m.group(1)))} до {cardinal(int(m.group(2)))}"


def _render_measure(tok: str) -> str:
    m = re.match(r"(\d+)\s?(грн|км|кг|м)", tok)
    return f"{cardinal(int(m.group(1)))} {_MEASURE[m.group(2)]}"


def _render_abbrev(tok: str, abbrev_map: dict) -> str:
    key = tok
    if abbrev_map and key in abbrev_map:
        return str(abbrev_map[key])
    if key in _BUILTIN_ABBREV:
        return _BUILTIN_ABBREV[key]
    return _spell_letters(key)


def normalize(text: str, *, abbrev_map: "dict | None" = None) -> NormResult:
    """Нормалізувати `text` за правилами v1 (§5.1). Повертає NormResult зі
    span-map. Порожній рядок → порожній результат."""
    text = text or ""
    abbrev_map = abbrev_map or {}
    out_parts = []
    spans = []
    out_len = 0
    idx = 0
    n = len(text)

    def emit(chunk: str, raw_start: int, raw_end: int):
        nonlocal out_len
        if chunk == "":
            return
        out_parts.append(chunk)
        spans.append((out_len, out_len + len(chunk), raw_start, raw_end))
        out_len += len(chunk)

    for m in _TOKEN_RE.finditer(text):
        start, end = m.span()
        if start > idx:
            emit(text[idx:start], idx, start)     # незмінений літерний пробіг
        kind = m.lastgroup
        tok = m.group()
        # порядковий елемент списку «1.» на початку рядка — лишаємо як номер пункту
        if kind == "integer":
            prev = text[start - 1] if start > 0 else ""
            after = text[end:end + 2]
            at_line_start = (start == 0) or text[start - 1] == "\n" or \
                (start >= 1 and text[:start].rstrip(" ").endswith("\n"))
            if after.startswith(".") and (at_line_start or prev in ("", "\n")):
                emit(tok, start, end)             # номер пункту «1.» — не розгортаємо
                idx = end
                continue
        if kind == "date_full":
            rendered = _render_date(tok)
        elif kind == "time":
            rendered = _render_time(tok)
        elif kind == "ratio":
            rendered = _render_ratio(tok)
        elif kind == "decimal":
            rendered = _render_decimal(tok)
        elif kind == "fraction":
            rendered = _render_fraction(tok)
        elif kind == "range":
            rendered = _render_range(tok)
        elif kind == "date_short":
            d, mo = tok.split(".")
            if 1 <= int(d) <= 31 and 1 <= int(mo) <= 12:
                rendered = _render_date(tok)
            else:
                rendered = tok
        elif kind == "measure":
            rendered = _render_measure(tok)
        elif kind == "integer":
            rendered = cardinal(int(tok))
        elif kind == "abbrev":
            rendered = _render_abbrev(tok, abbrev_map)
        elif kind == "symbol":
            if tok == "+":
                # КОНФЛІКТ §4.2 (StyleTTS2: «+» ПІСЛЯ складу = наголос) vs §5.1.8
                # («+» → «плюс»). Резервуємо «+» між кириличними літерами під
                # МАЙБУТНІЙ маркер наголосу (Хвиля 4 — словник вимови): не чіпаємо,
                # лишаємо як є. «Плюс» — лише коли «+» не всередині слова.
                prev_ch = text[start - 1] if start > 0 else ""
                next_ch = text[end] if end < n else ""
                if _is_cyr(prev_ch) and _is_cyr(next_ch):
                    rendered = tok            # наголос-маркер — не читаємо як «плюс»
                else:
                    rendered = "плюс"
            else:
                rendered = _UNITS.get(tok, tok)
        else:
            rendered = tok
        emit(rendered, start, end)
        idx = end
    if idx < n:
        emit(text[idx:], idx, n)
    return NormResult(text="".join(out_parts), spans=spans)
