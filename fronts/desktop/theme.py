"""Дизайн-токени «Balachky» — єдина гама застосунку (денна + нічна).

Джерело канону — внутрішній дизайн-гайд проєкту.
Правила денної теми: золото — єдиний акцент (дозовано); жодних градієнтів/teal/
синього. Свідоме відхилення: крапка запису — червона (#E52421): активний мікрофон
= семантично критичний стан, не декор.

НІЧНИЙ/ЧЕРВОНИЙ режим (feature/night-mode, мілітарі-розвідка 22.07): МОНО-ЧЕРВОНА
палітра на майже-чорному тлі для сумісності з приладами нічного бачення — зелене/
синє/біле світло видно ворогу і псує адаптацію ока, червоне довгохвильове — ні
(усталена практика містків/підводних човнів + ATAK Night Vision; джерело:
research/2026-07-22-MIL-польові-умови.md §2). У нічній темі ЖОДНОГО не-червоного
світла: кожен видимий колір — червоно-домінантний, білий текст замінено на
світло-червоний, оливкове тло — на чорно-червоне, тайл «Б з жуком» вимкнено на
користь суцільного майже-чорного.

Архітектура: усі токени живуть у палітрах ``_DAY``/``_NIGHT``; ``_install`` кладе
активну палітру в глобалі модуля, а ``build_qss``/``build_qss_mica`` збирають
таблицю стилів з ПОТОЧНИХ значень. Геометрія (розміри/відступи/радіуси) — та сама
в обох темах, тож візуальний гейт (обрізання) зелений в обох за побудовою.

Редизайн «Просторий editorial»: багато повітря, впевнена типографіка, мінімум
рамок. Ключове технічне рішення: фон малюють лише «поверхні» (вікно, стек
сторінок, сайдбар, картки, поля) — QLabel/радіо/чекбокси прозорі, інакше
кожен напис несе за собою непрозорий прямокутник (артефакт «сирого Qt»).
"""
import os
import weakref

from PySide6.QtCore import QEvent, QObject, Qt
from whisper_core.paths import asset_root

# QSS url() вимагає прямі слеші навіть на Windows
_UI = asset_root() / "assets" / "ui"
_BG_TILE   = (_UI / "bg-tile.png").as_posix()   # тло «Б з жуком» (scripts/gen_bg_tile.py)

# SVG-іконки контролів мають ДВА варіанти файлу: денний (тепла гама) і нічний
# (`*-night.svg`, червоно-монохромний). Статичний SVG не читає палітру, тож
# правильний варіант ОБИРАЄМО за активною темою у _install (інакше золота крапка
# радіо / кремовий шеврон світилися б не-червоним у нічному режимі). Ключ QSS →
# базове імʼя файлу без розширення.
_ICON_TOKENS = {
    "_CHECK": "check", "_CHEVRON": "chevron-down", "_RADIO_OFF": "radio-off",
    "_RADIO_ON": "radio-on", "_RADIO_HOV": "radio-hover",
    "_RADIO_DIS": "radio-off-disabled",
}


def _icon(base: str, night: bool) -> str:
    """Posix-шлях (для QSS url()) до денного чи нічного варіанта SVG-іконки."""
    name = f"{base}-night.svg" if night else f"{base}.svg"
    return (_UI / name).as_posix()

_R_CARD = "22px"                            # картки/панелі
_R_CTRL = "12px"                            # кнопки/поля/списки

# ─────────────────────────── ПАЛІТРИ ───────────────────────────
# Кожен ключ — токен, що стає глобалом модуля (див. _install). Денні значення
# збігаються байт-у-байт зі старою темою (жодних регресів існуючих тестів/гейта).
# Нічні — червоно-монохромні (R домінує; зелене/синє тримаємо низько; білого нема).
_DAY = {
    "SURFACE":       "#4D4634",   # фон (хакі-олива)
    "CARD":          "#3A3528",   # картка
    "DEEP":          "#2E2A1F",   # глибша панель (сайдбар, поля вводу)
    "GOLD":          "#F39200",   # єдиний хроматичний акцент
    "GOLD_EYEBROW":  "#FFC766",   # теплий золотий для дрібних лейблів
    "TEXT_STRONG":   "#FFFFFF",
    "TEXT_BODY":     "#E6E5D1",   # тепло-білий: контраст на оливі без напруги
    "TEXT_MUTED":    "#D6CDB8",   # приглушений, читатись чітко
    "TEXT_ON_GOLD":  "#2E2A1F",
    "ALERT":         "#E52421",   # запис (REC) / криза
    "ALERT_TEXT":    "#E6E5D1",   # дрібний error-текст (5.35:1 на CARD)
    "FOCUS":         "#F39200",
    "IDLE":          "#D6CDB8",   # хакі-мутед для стану «готовий»
    "SUCCESS":       "#F39200",   # успіх (готово) — золото канонічного акценту
    "SUCCESS_EYEBROW": "#F39200", # світліше золото для підпису «готово»
    "GOLD_HOVER":    "#FFD387",   # accent hover — світліше золото
    "GOLD_PRESSED":  "#E2AB4A",   # accent pressed — глибше золото
    "DANGER_MUTED":  "#CF7B62",   # теракот для тексту/рамки деструктив-кнопки
    # rgba-токени (прозорі відтінки)
    "HOVER_OVERLAY": "rgba(255,255,255,0.05)",  # ghost hover — тихе біле світло
    "PRESS_OVERLAY": "rgba(0,0,0,0.22)",        # натиск базової/ghost
    "TEXT_DISABLED": "rgba(230,229,209,0.36)",  # текст вимкненого контрола
    "_DANGER_45":    "rgba(207,123,98,0.45)",   # рамка деструктиву у спокої
    "_DANGER_12":    "rgba(207,123,98,0.12)",   # hover-підсвіт деструктиву
    "_DANGER_22":    "rgba(207,123,98,0.22)",   # натиск деструктиву
    "_TILE_BASE":    "#2c2718",                 # база тайла (під bg-tile.png)
    "_GLASS_HI":     "rgba(109,102,86,0.34)",   # верхній-лівий кремовий відблиск скла
    "_GLASS_TINT":   "rgba(46,42,31,0.26)",     # база плашки (.26 — тайл просвічує)
    "_GLASS_EDGE":   "rgba(255,255,255,0.16)",  # кромка скла 1px
    "_GLASS_TOP":    "rgba(255,255,255,0.24)",  # inset-світло по верхній кромці
    "_HAIRLINE":     "rgba(255,255,255,0.09)",  # тонкі розділювачі
    "_LINE_SOFT":    "rgba(255,255,255,0.14)",  # межі контролів
    "_GOLD_08":      "rgba(243,146,0,0.08)",
    "_GOLD_10":      "rgba(243,146,0,0.10)",
    "_GOLD_15":      "rgba(243,146,0,0.15)",
    "_GOLD_16":      "rgba(243,146,0,0.16)",
    "_GOLD_22":      "rgba(243,146,0,0.22)",
    "_GOLD_35":      "rgba(243,146,0,0.35)",
    "_GOLD_65":      "rgba(243,146,0,0.65)",
    "_WHITE_04":     "rgba(255,255,255,0.04)",  # hover рядка словника
    "_WHITE_06":     "rgba(255,255,255,0.06)",  # межа рядка таблиці
    "_WHITE_16":     "rgba(255,255,255,0.16)",  # ручка скролбара
    "_WHITE_28":     "rgba(255,255,255,0.28)",  # ручка скролбара :hover
    "_PANEL_35":     "rgba(46,42,31,0.35)",     # dropzone / disabled-фон
    "_PANEL_45":     "rgba(46,42,31,0.45)",     # disabled-фон полів/кнопок
    "_PANEL_65":     "rgba(46,42,31,0.65)",     # текст на вимкненій accent-кнопці
    "_INK_35":       "rgba(230,229,209,0.35)",  # вимкнений повзунок
    "_INK_45":       "rgba(230,229,209,0.45)",  # рамка чекбокса / disabled-текст
    "_ERR_EDGE":     "rgba(229,36,33,0.55)",    # рамка error-бейджа
    "_DROPZONE_DASH": "rgba(214,205,184,0.32)", # пунктир зони скидання
    "_LINE_HILITE":  "#523A1E",                 # підсвіт активного рядка розшифровки
    "SIDEBAR_GLASS": "rgba(46,42,31,0.72)",     # тонування сайдбара під Mica
    "_TILE_IMG":     f'url("{_BG_TILE}")',      # тло стека сторінок
    # RGB-кортежі для власних малювальників (glass/pill/level/splash): ті самі
    # альфи, лише змінюється відтінок між темами.
    "LIGHT_RGB":     (255, 255, 255),           # скляне світло/кромки (світло, не колір)
    "ACCENT_RGB":    (243, 146, 0),             # золото у власному малюванні
    "ALERT_RGB":     (229, 36, 33),             # REC-пульс / кліп рівня
    "PANEL_RGB":     (46, 42, 31),              # тіло пілюлі/смужки рівня
    "DISABLED_RGB":  (230, 229, 209),           # тьмяний вміст вимкненого контрола
}

_NIGHT = {
    "SURFACE":       "#0F0303",   # майже-чорне тепле тло
    "CARD":          "#1B0808",
    "DEEP":          "#170606",
    "GOLD":          "#FF4D4D",   # акцент — середньо-яскравий червоний
    "GOLD_EYEBROW":  "#FF9E9E",   # світло-червоний для дрібних лейблів
    "TEXT_STRONG":   "#FF8F8F",   # заголовки — світлий червоний (замість білого)
    "TEXT_BODY":     "#FF6B6B",   # тіло тексту — червоний (7.3:1 на тлі)
    "TEXT_MUTED":    "#D96C6C",   # приглушений червоний (5.8:1 на тлі)
    "TEXT_ON_GOLD":  "#1A0000",   # темний текст на червоному акценті (6.1:1)
    "ALERT":         "#FF3B3B",   # REC — насичений червоний
    "ALERT_TEXT":    "#FF8484",
    "FOCUS":         "#FF6161",   # фокус-рамка — яскравіший червоний
    "IDLE":          "#C86A6A",   # тьмяний червоний для «готовий»
    "SUCCESS":       "#FF6B6B",   # у моно-червоному успіх = світлий червоний
    "SUCCESS_EYEBROW": "#FF9E9E",
    "GOLD_HOVER":    "#FF7A7A",   # accent hover — світліший червоний
    "GOLD_PRESSED":  "#E04A4A",   # accent pressed — глибший червоний
    "DANGER_MUTED":  "#E06A6A",   # деструктив — теж червоний (яскравіший відтінок)
    "HOVER_OVERLAY": "rgba(255,90,90,0.06)",
    "PRESS_OVERLAY": "rgba(0,0,0,0.22)",        # чорний натиск світла не дає — лишаємо
    "TEXT_DISABLED": "rgba(255,107,107,0.34)",
    "_DANGER_45":    "rgba(224,106,106,0.50)",
    "_DANGER_12":    "rgba(224,106,106,0.14)",
    "_DANGER_22":    "rgba(224,106,106,0.24)",
    "_TILE_BASE":    "#0C0303",                 # майже-чорне тло стека сторінок
    "_GLASS_HI":     "rgba(150,50,50,0.30)",    # відблиск скла — червоний
    "_GLASS_TINT":   "rgba(30,8,8,0.34)",       # база плашки — чорно-червона
    "_GLASS_EDGE":   "rgba(255,100,100,0.18)",  # кромка скла — червона
    "_GLASS_TOP":    "rgba(255,120,120,0.22)",  # inset-світло — червоне
    "_HAIRLINE":     "rgba(255,90,90,0.12)",
    "_LINE_SOFT":    "rgba(255,90,90,0.20)",
    "_GOLD_08":      "rgba(255,77,77,0.10)",
    "_GOLD_10":      "rgba(255,77,77,0.14)",
    "_GOLD_15":      "rgba(255,77,77,0.18)",
    "_GOLD_16":      "rgba(255,77,77,0.20)",
    "_GOLD_22":      "rgba(255,77,77,0.28)",
    "_GOLD_35":      "rgba(255,77,77,0.42)",
    "_GOLD_65":      "rgba(255,90,90,0.70)",
    "_WHITE_04":     "rgba(255,90,90,0.06)",
    "_WHITE_06":     "rgba(255,90,90,0.09)",
    "_WHITE_16":     "rgba(255,100,100,0.20)",
    "_WHITE_28":     "rgba(255,120,120,0.32)",
    "_PANEL_35":     "rgba(28,8,8,0.50)",
    "_PANEL_45":     "rgba(28,8,8,0.55)",
    "_PANEL_65":     "rgba(26,0,0,0.65)",
    "_INK_35":       "rgba(255,107,107,0.32)",
    "_INK_45":       "rgba(255,107,107,0.40)",
    "_ERR_EDGE":     "rgba(255,70,70,0.60)",
    "_DROPZONE_DASH": "rgba(255,107,107,0.30)",
    "_LINE_HILITE":  "#4A1616",                 # підсвіт активного рядка — темно-червоний
    "SIDEBAR_GLASS": "rgba(18,6,6,0.82)",
    "_TILE_IMG":     "none",                    # тайл вимкнено — суцільне майже-чорне
    "LIGHT_RGB":     (255, 110, 110),           # «світло» скла — червоне
    "ACCENT_RGB":    (255, 77, 77),
    "ALERT_RGB":     (255, 59, 59),
    "PANEL_RGB":     (24, 8, 8),
    "DISABLED_RGB":  (255, 107, 107),
}


# ───────────────── персоналізація кольору (рішення Миколи 25.07) ──────────────
# Цитата власника: «зроби червоний режим. І так само, щоб людина могла з
# палітри будь-який інший колір вибрати» — і окремо: «Тільки не називається
# нічним режимом. А просто вибір кольору». Це ПЕРСОНАЛІЗАЦІЯ ВИГЛЯДУ, а не
# мілітарі-фіча: червоний — лише один із варіантів.
#
# Механіка: _NIGHT лишається ЕТАЛОНОМ (байт-у-байт, як і раніше) — «червоний»
# пресет. Решта відтінків — той самий _NIGHT із заміненим ТОНОМ (H у HSV) у
# кожному колірному токені; насиченість і яскравість (S, V) лишаються, тому
# геометрія контрасту між сусідніми токенами приблизно зберігається. Нейтральні
# кольори (S=0: чорний/білий/сірий) від зсуву тону НЕ змінюються — це властивість
# самого HSV→RGB (при S=0 результат завжди (V,V,V) незалежно від H).
#
# Контраст — не побічний ефект, а вимога: та сама яскравість на різних тонах
# читається по-різному (зелений важить у формулі яскравості 0.7152, синій —
# лише 0.0722), тож після зсуву кожен готовий пресет проганяється через
# _fix_contrast, що ПІДТЯГУЄ яскравість (не тон!) токенів, які не дотягують до
# WCAG 2.1 AA (4.5:1), і чесно кидає RuntimeError, якщо це неможливо.
import colorsys
import re

_HEX_RE = re.compile(r"#([0-9A-Fa-f]{6})\b")
_RGBA_RE = re.compile(
    r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(,\s*[\d.]+\s*)?\)")

#: токени палітри, що НЕ є кольором — зсув тону їх не торкає
_NON_COLOR_TOKENS = {"_TILE_IMG"}

_CONTRAST_MIN = 4.5   # WCAG 2.1 AA
# (текстовий токен, токен тла, підпис для помилки) — саме ці пари мусять
# читатись у КОЖНОМУ готовому кольоровому варіанті.
_CONTRAST_PAIRS = [
    ("TEXT_BODY",    "CARD",    "тіло тексту на картці"),
    ("TEXT_BODY",    "SURFACE", "тіло тексту на тлі вікна"),
    ("TEXT_STRONG",  "CARD",    "заголовок на картці"),
    ("TEXT_STRONG",  "SURFACE", "заголовок на тлі вікна"),
    ("TEXT_MUTED",   "CARD",    "приглушений текст на картці"),
    ("TEXT_MUTED",   "SURFACE", "приглушений текст на тлі вікна"),
    ("TEXT_ON_GOLD", "GOLD",    "текст на акцентній кнопці"),
]


def _hex_to_rgb(h: str):
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(rgb) -> str:
    r, g, b = (max(0, min(255, round(c))) for c in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def _shift_rgb(rgb, hue_deg: float):
    """(r,g,b 0-255) → той самий колір із тоном, заміненим на ``hue_deg``
    (0-359); насиченість і яскравість НЕ чіпаємо."""
    r, g, b = rgb
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    nr, ng, nb = colorsys.hsv_to_rgb((hue_deg % 360) / 360, s, v)
    return round(nr * 255), round(ng * 255), round(nb * 255)


def _token_rgb(value):
    """Розібрати значення токена (hex / rgba-рядок / RGB-кортеж) → (r,g,b).
    Нерозпізнане (шляхи, url, розміри) → None."""
    if isinstance(value, tuple):
        return tuple(value[:3])
    if not isinstance(value, str):
        return None
    m = _HEX_RE.fullmatch(value.strip())
    if m:
        return _hex_to_rgb(m.group(1))
    m = _RGBA_RE.fullmatch(value.strip())
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def _repack_like(original, rgb):
    """Зібрати ``rgb`` у ТОМУ САМОМУ вигляді, що ``original`` — hex / rgba-
    рядок (альфа копіюється буквально, байт-у-байт) / RGB-кортеж."""
    r, g, b = (max(0, min(255, round(c))) for c in rgb)
    if isinstance(original, tuple):
        return (r, g, b)
    m = _RGBA_RE.fullmatch(original.strip())
    if m:
        alpha = m.group(4) or ""
        return f"rgba({r},{g},{b}{alpha})"
    return _rgb_to_hex((r, g, b))


def _shift_token(value, hue_deg: float):
    """Зсунути тон ОДНОГО значення палітри, зберігши формат і альфу. Значення,
    що не є кольором (None із _token_rgb), повертає без змін."""
    rgb = _token_rgb(value)
    if rgb is None:
        return value
    return _repack_like(value, _shift_rgb(rgb, hue_deg))


def _linear_channel(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(rgb) -> float:
    r, g, b = rgb
    return (0.2126 * _linear_channel(r) + 0.7152 * _linear_channel(g)
            + 0.0722 * _linear_channel(b))


def _contrast_ratio(fg_rgb, bg_rgb) -> float:
    l1, l2 = _relative_luminance(fg_rgb), _relative_luminance(bg_rgb)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _boost_contrast(token_value, bg_rgb, target: float = _CONTRAST_MIN, label: str = ""):
    """Двійковий пошук по V (HSV) токена, поки контраст із ``bg_rgb`` не
    досягне ``target``. Тон і насиченість НЕ чіпаємо — лише яскравість.

    Однакова V «світиться» по-різному на різних тонах (синій/фіолетовий
    важать у формулі яскравості лише 0.0722 проти 0.7152 зеленого), тож
    напрям «темніше» (V→0), що працює для більшості тонів, у вузькій
    синьо-фіолетовій смузі не дотягує НАВІТЬ на суцільно чорному — там
    рятує лише світліший бік (V→1). Тому перевіряємо ОБИДВА краї діапазону
    й обираємо той, що дотягує (за рівних умов — ближчий до вихідного
    значення, найменша видима зміна). Якщо жоден край не дотягує — чесна
    RuntimeError із назвою токена й досягнутим числом, а не занижений поріг.

    Бісекція тримає інваріант «good_v завжди задовольняє поріг» напряму
    (перевіряється на кожному кроці), тож коректна незалежно від того, чи
    контраст монотонний по всьому шляху від вихідного V до краю.

    Повністю НАСИЧЕНИЙ токен (S=1, як-от TEXT_ON_GOLD у _NIGHT) на вузькій
    синьо-фіолетовій смузі не дотягує НАВІТЬ на власних крайніх V=0/V=1 (там
    крайні точки — чорний і чистий тон, а не чорний/БІЛИЙ). Математична
    гарантія: для БУДЬ-ЯКОГО тла принаймні один із двох СПРАВЖНІХ крайніх
    кольорів (чорний S=0/V=0, білий S=0/V=1) дотягує до 4.5:1 — зони, де
    провалюються ОБИДВА, не існує (умови Lbg<0.175 і Lbg>0.183 несумісні).
    Тож коли зсув тону+V не рятує, останній крок — знебарвити (S→0) і
    повторити пошук по V серед SPRAVDI чорного/білого."""
    fg_rgb = _token_rgb(token_value)
    if fg_rgb is None:
        return token_value
    if _contrast_ratio(fg_rgb, bg_rgb) >= target:
        return token_value
    h, s, v0 = colorsys.rgb_to_hsv(fg_rgb[0] / 255, fg_rgb[1] / 255, fg_rgb[2] / 255)

    def rgb_at(vv, ss):
        return tuple(c * 255 for c in colorsys.hsv_to_rgb(h, ss, vv))

    def contrast_at(vv, ss):
        # округлюємо ДО каналів 0-255, як і фінальний _repack_like — інакше
        # бісекція може збігтись у точку, чий округлений колір знову
        # проскакує під поріг (ефект «безперервна оптимізація, дискретний
        # результат»).
        rounded = tuple(round(max(0, min(255, c))) for c in rgb_at(vv, ss))
        return _contrast_ratio(rounded, bg_rgb)

    def resolve(ss):
        """Знайти V (за фіксованої насиченості ``ss``), що дотягує до порогу,
        або None, якщо жоден з країв не дотягує за цієї насиченості."""
        c_low, c_high = contrast_at(0.0, ss), contrast_at(1.0, ss)
        ok_low, ok_high = c_low >= target, c_high >= target
        if not ok_low and not ok_high:
            return None, max(c_low, c_high)
        if ok_low and ok_high:
            edge = 0.0 if abs(0.0 - v0) <= abs(1.0 - v0) else 1.0
        else:
            edge = 0.0 if ok_low else 1.0
        good_v, bad_v = edge, v0      # good_v ЗАВЖДИ задовольняє поріг (інваріант)
        for _ in range(40):
            mid = (good_v + bad_v) / 2
            if contrast_at(mid, ss) >= target:
                good_v = mid
            else:
                bad_v = mid
        return good_v, None

    best_v, achieved = resolve(s)
    final_s = s
    if best_v is None:
        best_v, achieved = resolve(0.0)   # останній крок: знебарвити до чорного/білого
        final_s = 0.0
    if best_v is None:
        raise RuntimeError(
            f"контраст не вдалося підтягнути ({label or 'токен'}): "
            f"досягнуто {achieved:.2f}:1, потрібно {target}:1")
    return _repack_like(token_value, rgb_at(best_v, final_s))


def _fix_contrast(palette: dict) -> dict:
    """Прогнати всі пари з ``_CONTRAST_PAIRS`` і підтягнути текстові токени,
    що не дотягують до WCAG AA. Тло (CARD/SURFACE/GOLD) не чіпаємо."""
    out = dict(palette)
    for fg_key, bg_key, label in _CONTRAST_PAIRS:
        bg_rgb = _token_rgb(out[bg_key])
        out[fg_key] = _boost_contrast(out[fg_key], bg_rgb, label=f"{label} ({fg_key})")
    return out


def build_palette_for_hue(hue: int, base: dict = None) -> dict:
    """Нова моно-палітра зі зсувом тону в ``hue`` (ціле 0-359) на основі
    ``base`` (за замовч. еталонна _NIGHT). Контраст WCAG AA підтягується
    автоматично — результат завжди читний, інакше RuntimeError."""
    if not isinstance(hue, int) or isinstance(hue, bool) or not (0 <= hue < 360):
        raise ValueError(f"hue має бути цілим 0-359, отримано {hue!r}")
    base = _NIGHT if base is None else base
    shifted = {
        key: (value if key in _NON_COLOR_TOKENS else _shift_token(value, hue))
        for key, value in base.items()
    }
    return _fix_contrast(shifted)


#: іменовані відтінки поза еталонним червоним (той — точно 0°, без перерахунку)
NAMED_HUES = {
    "amber":  40,
    "green":  135,
    "teal":   178,
    "blue":   212,
    "purple": 268,
    "pink":   330,
}

# Готові варіанти кольору. "classic" і "red" — наявні _DAY/_NIGHT БЕЗ ЗМІН
# (байт-у-байт, жодного регресу); решта — build_palette_for_hue на старті модуля.
PRESETS: dict = {"classic": dict(_DAY), "red": dict(_NIGHT)}
PRESETS.update({name: build_palette_for_hue(hue) for name, hue in NAMED_HUES.items()})


def _hue_for(color):
    """``color`` → тон (0-359) для вибору/генерації іконок, або None для
    класичної (денної) теми."""
    if color == "classic":
        return None
    if color == "red":
        return 0
    if isinstance(color, bool):
        raise ValueError(f"невідомий колір інтерфейсу: {color!r}")
    if isinstance(color, int):
        if not (0 <= color < 360):
            raise ValueError(f"hue має бути 0-359, отримано {color!r}")
        return color
    if color in NAMED_HUES:
        return NAMED_HUES[color]
    raise ValueError(f"невідомий колір інтерфейсу: {color!r}")


def palette_for(color) -> dict:
    """Палітра для ``color``: 'classic' / 'red' / назва пресету (NAMED_HUES) /
    довільне ціле 0-359."""
    if isinstance(color, str) and color in PRESETS:
        return PRESETS[color]
    return build_palette_for_hue(_hue_for(color))


_ICON_CACHE_SUBDIR = "ui_icons"


def _icon_path_for(base: str, hue) -> str:
    """Posix-шлях до SVG-іконки контрола для активного кольору. ``hue`` — те,
    що повертає ``_hue_for``: None → денний файл (base.svg); 0 (еталонний
    червоний) → наявний base-night.svg без змін; інший тон — перефарбований
    файл, кешований у теці користувача (не перегенеровуємо, якщо кеш не
    старіший за джерело). Помилка запису тихо повертає червоний варіант —
    непрочитний інтерфейс гірший за неідеальний колір іконки."""
    if hue is None:
        return (_UI / f"{base}.svg").as_posix()
    src = _UI / f"{base}-night.svg"
    if hue == 0:
        return src.as_posix()
    try:
        from whisper_core.paths import user_dir
        out_dir = user_dir() / _ICON_CACHE_SUBDIR
        out_path = out_dir / f"{base}-hue{hue}.svg"
        if out_path.exists() and out_path.stat().st_mtime >= src.stat().st_mtime:
            return out_path.as_posix()
        text = src.read_text(encoding="utf-8")
        recolored = _HEX_RE.sub(
            lambda m: _rgb_to_hex(_shift_rgb(_hex_to_rgb(m.group(1)), hue)), text)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(recolored, encoding="utf-8")
        return out_path.as_posix()
    except OSError:
        return src.as_posix()          # тихий відкат на червоні іконки


def _startup_color_from_env():
    """Активний колір інтерфейсу на старті модуля (до будь-якого конфігу/UI) —
    для гейта й розвідки руками. Два джерела:
      BALACHKY_UI_COLOR — новий, параметричний: назва пресету ('classic',
        'red', 'amber', 'green', 'teal', 'blue', 'purple', 'pink') або ціле
        0-359 (рядком) для довільного тону. Має пріоритет.
      BALACHKY_FORCE_NIGHT — старий бінарний прапорець (лишений: ним могли
        вже користуватись скрипти) — вмикає еталонний червоний, як і раніше.
    Жодного зі змінних → 'classic'."""
    env_color = os.environ.get("BALACHKY_UI_COLOR", "").strip()
    if env_color:
        return int(env_color) if env_color.lstrip("-").isdigit() else env_color
    return "red" if os.environ.get("BALACHKY_FORCE_NIGHT") else "classic"


_ACTIVE_COLOR = _startup_color_from_env()


# Активна палітра як словник (для build_qss — щоб не спиратись на динамічні
# глобалі, яких статичний аналіз pyflakes не бачить). Заповнює _install.
_P: dict = {}


def _install(palette: dict, hue=None) -> None:
    """Покласти активну палітру в глобалі модуля + перебудувати похідні токени.
    Модуль-глобалі (theme.GOLD тощо) читають острівці наживо; словник ``_P`` —
    самодостатнє джерело для build_qss (без «undefined name» у лінтера)."""
    g = globals()
    g.update(palette)
    # _GLASS_FILL — одна радіальна заливка (база-тінт + кутовий відблиск): Qt QSS не
    # стекає два фони, тож складаємо математичний еквівалент композиту макета.
    glass_fill = (
        f"qradialgradient(cx:0.14, cy:-0.02, radius:1.2, fx:0.14, fy:-0.02,"
        f" stop:0 {palette['_GLASS_HI']}, stop:0.55 {palette['_GLASS_TINT']},"
        f" stop:1 {palette['_GLASS_TINT']})")
    g["_GLASS_FILL"] = glass_fill
    p = dict(palette)
    p["_GLASS_FILL"] = glass_fill
    # статичні (не-палітрові) токени, на які теж спирається QSS. Шляхи до SVG-
    # іконок — за активним кольором (день/червоний/перефарбовані), решта
    # геометрії — та сама завжди.
    p.update({tok: _icon_path_for(base, hue) for tok, base in _ICON_TOKENS.items()})
    p.update(_R_CARD=_R_CARD, _R_CTRL=_R_CTRL)
    _P.clear()
    _P.update(p)


def _apply_color(color) -> None:
    """Внутрішнє: встановити активний колір у глобалі модуля (без перебудови
    готових рядків QSS — те робить set_ui_color/set_mode)."""
    global _ACTIVE_COLOR
    _ACTIVE_COLOR = color
    _install(palette_for(color), _hue_for(color))


_apply_color(_ACTIVE_COLOR)


def is_night() -> bool:
    """Сумісність: чи активний саме еталонний червоний варіант (не персоналізація
    в інший відтінок і не класика)."""
    return _ACTIVE_COLOR == "red"


def current_ui_color():
    """Активний колір інтерфейсу: 'classic' / назва пресету / int-тон 0-359."""
    return _ACTIVE_COLOR


# Фіча повертається у збірку (рішення Миколи 25.07, скасовує Т50 від 23.07):
# «просто вибір кольору», не мілітарі-нічний режим. Прапорець лишається
# ІМЕНЕМ для сумісності з наявними if theme.NIGHT_MODE_AVAILABLE (settings.py) —
# тепер завжди True, тобто заслінки більше нема. Наступний агент (інтерфейс),
# що замінить одиночний тумблер на вибір кольору з PRESETS, може прибрати
# сам прапорець і цю перевірку остаточно.
NIGHT_MODE_AVAILABLE = True


def resolve_ui_color(cfg):
    """Активний колір інтерфейсу для цього конфігу: нове поле ``cfg.ui_color``
    (назва пресету з PRESETS або int 0-359). Якщо його нема (старий конфіг) —
    міграція зі старого ``cfg.night_mode``: True → 'red', інакше 'classic'."""
    value = getattr(cfg, "ui_color", None)
    if value not in (None, ""):
        return value
    return "red" if getattr(cfg, "night_mode", False) else "classic"


def night_enabled_for(cfg) -> bool:
    """Сумісність зі старим бінарним викликом (fronts/desktop/app.py): чи
    стартувати НЕ в класичній темі. Читає resolve_ui_color (нове поле
    cfg.ui_color, з міграцією старого night_mode)."""
    return resolve_ui_color(cfg) != "classic"


def set_ui_color(color) -> None:
    """Перемкнути активний колір інтерфейсу НАЖИВО (без перезапуску) і
    перебудувати готові рядки QSS. ``color`` — 'classic', назва пресету
    (red/amber/green/teal/blue/purple/pink) або ціле 0-359 (довільний тон)."""
    global QSS, QSS_MICA
    _apply_color(color)
    QSS = build_qss()
    QSS_MICA = build_qss_mica()


def set_mode(night: bool) -> None:
    """Стара бінарна назва (сумісність з наявними викликами й тестами):
    True → еталонний червоний, False → класична денна."""
    set_ui_color("red" if night else "classic")


def load_fonts() -> None:
    """Підключити бандл-шрифти Fixel (MacPaw, SIL OFL — ліцензія поруч у
    assets/fonts/OFL.txt). Викликати ПІСЛЯ створення QApplication, ДО QSS.
    Файлів нема / не читаються — тихий фолбек: у QSS системні родини.
    Основний UI-текст спирається на рідний Segoe UI (див. QSS);
    Fixel Display лишається лише для бренд-мітки сайдбара."""
    from PySide6.QtGui import QFontDatabase
    fonts_dir = asset_root() / "assets" / "fonts"
    for ttf in sorted(fonts_dir.glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(ttf))


def spaced(label, pct: int = 160, center: bool = False, rich: bool = False):
    """Збільшити міжрядковий інтервал QLabel. У Qt-QSS немає line-height, тож
    обгортаємо наявний текст у rich-text <p> з line-height — QLabel це шанує в
    рендері (емпірично: +70% висоти багаторядкового блоку). Викликати ПІСЛЯ
    setText(). За замовч. текст ПЛОСКИЙ (екранується). rich=True — текст уже
    містить розмітку (<b>/<br>), не екранувати (напр. опис «Про програму»)."""
    import html as _html
    from PySide6.QtCore import Qt
    label.setTextFormat(Qt.RichText)
    label.setWordWrap(True)
    align = "text-align:center; " if center else ""
    inner = label.text() if rich else _html.escape(label.text())
    label.setText(f'<p style="{align}line-height:{pct}%; margin:0;">'
                  + inner + "</p>")
    return label


def build_qss() -> str:
    """Зібрати таблицю стилів з ПОТОЧНОЇ активної палітри."""
    p = _P
    return f"""
/* --- база: колір і шрифт успадковуються, фон — лише у поверхонь ---
   Основа — класичний Segoe UI. Fixel більше не тримає UI (лише на лого). */
QWidget {{
    color: {p["TEXT_BODY"]};
    font-family: "Segoe UI", system-ui, sans-serif;
    font-size: 15px;
    background: transparent;
}}
QMainWindow, QDialog, QMessageBox, QInputDialog, QFileDialog {{
    background: {p["SURFACE"]};
}}
/* Тло «Б з жуком» малює НЕ QSS, а _TiledStack.paintEvent (main_window):
   QSS background-repeat не повторює тайл вертикально на високому QStackedWidget
   (Qt-квірк — лишає пласку базу внизу). Тут стек ПРОЗОРИЙ, щоб вкладені стеки
   (стрічка диктування, вкладки Налаштувань тощо) не перекривали намальований
   тайл сторінкового стека. Тайл/базу з палітри читає _TiledStack наживо. */
QStackedWidget {{ background: transparent; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QToolTip {{
    background: {p["DEEP"]};
    color: {p["TEXT_BODY"]};
    border: 1px solid {p["_LINE_SOFT"]};
    border-radius: 4px;
    padding: 5px 9px;
    max-width: 520px;
}}
QMenu {{
    background: {p["DEEP"]};
    border: 1px solid {p["_LINE_SOFT"]};
    border-radius: 6px;
    padding: 5px;
}}
QMenu::item {{ padding: 7px 16px; border-radius: 4px; }}
QMenu::item:selected {{ background: {p["_GOLD_16"]}; color: {p["TEXT_STRONG"]}; }}
QMenu::item:disabled {{ color: {p["TEXT_MUTED"]}; }}
QMenu::separator {{ height: 1px; background: {p["_HAIRLINE"]}; margin: 5px 8px; }}

/* --- сайдбар: логотип + навігація (GlassButton у glass.py) + слоган --- */
QFrame#sidebar {{
    background: {p["DEEP"]};
    border: none;
    border-right: 1px solid {p["_HAIRLINE"]};
}}

/* --- типографічні ролі --- */
QLabel {{ background: transparent; }}
/* ЄДИНЕ джерело правди типографіки (docs/DESIGN-TYPOGRAPHY.md §4): одна
   властивість `level` покриває всі 5 рівнів + `stat`. Старі властивості нижче
   (h1/strong/muted/eyebrow) лишаються сумісними аліасами на час міграції. */
QLabel[level="h1"]      {{
    color: {p["TEXT_STRONG"]}; font-size: 24px; font-weight: 600;
    font-family: "Segoe UI", system-ui, sans-serif;
}}
QLabel[level="eyebrow"] {{
    color: {p["GOLD_EYEBROW"]}; font-size: 12px; font-weight: 600;
}}
QLabel[level="block"]   {{ color: {p["TEXT_STRONG"]}; font-size: 16px; font-weight: 600; }}
QLabel[level="body"]    {{ color: {p["TEXT_BODY"]}; font-size: 15px; font-weight: 400; }}
QLabel[level="hint"]    {{ color: {p["TEXT_MUTED"]}; font-size: 13px; font-weight: 400; }}
QLabel[level="stat"]    {{ color: {p["TEXT_STRONG"]}; font-size: 28px; font-weight: 600; }}
QLabel[h1="true"]      {{
    color: {p["TEXT_STRONG"]}; font-size: 24px; font-weight: 600;
    font-family: "Segoe UI", system-ui, sans-serif;
}}
QLabel[pagesub="true"] {{ color: {p["TEXT_MUTED"]}; font-size: 14px; }}
QLabel[eyebrow="true"] {{
    color: {p["GOLD_EYEBROW"]}; font-size: 12px; font-weight: 600;
}}
QLabel[strong="true"]  {{ color: {p["TEXT_STRONG"]}; font-weight: 600; font-size: 16px; }}
QLabel[gold="true"]    {{ color: {p["GOLD"]}; font-weight: 600; }}
QLabel[muted="true"]   {{ color: {p["TEXT_MUTED"]}; font-size: 13px; }}
QLabel[formlabel="true"] {{ color: {p["TEXT_MUTED"]}; font-size: 14px; }}
/* бренд-мітка сайдбара — єдиний свідомий дотик Fixel Display (фолбек — рідний) */
QLabel[logo="true"]    {{
    color: {p["TEXT_STRONG"]}; font-size: 18px; font-weight: 600;
    font-family: "Segoe UI", system-ui, sans-serif;
}}
QLabel[logosub="true"] {{
    color: {p["GOLD"]}; font-size: 14px; font-weight: 500;
}}
QLabel[slogan="true"]  {{ color: {p["TEXT_MUTED"]}; font-size: 12px; }}
QLabel[version="true"] {{ color: {p["IDLE"]}; font-size: 12px; }}
QLabel[emptytitle="true"] {{ color: {p["TEXT_BODY"]}; font-size: 17px; font-weight: 600; }}
QLabel[kbd="true"] {{
    background: {p["DEEP"]};
    color: {p["TEXT_STRONG"]};
    font-weight: 600;
    border: 1px solid {p["_LINE_SOFT"]};
    border-radius: 4px;
    padding: 4px 12px;
}}
QFrame[divider="true"] {{
    background: {p["_HAIRLINE"]}; border: none; min-height: 1px; max-height: 1px;
}}

/* --- картки-скло: напівпрозоре скло над тайлом (тайл просвічує) --- */
QFrame[card="true"] {{
    background: {p["_GLASS_FILL"]};
    border: 1px solid {p["_GLASS_EDGE"]};
    border-top-color: {p["_GLASS_TOP"]};
    border-radius: {p["_R_CARD"]};
}}
/* активний словник — як активний пункт сайдбара: золота рамка + заливка 10%.
   Радіус успадковується від [card] (22px). */
QFrame[card="true"][active="true"] {{
    background: {p["_GOLD_10"]};
    border: 1px solid {p["GOLD"]};
}}
QFrame[dropzone="true"] {{
    border: 1px dashed {p["_DROPZONE_DASH"]};
    border-radius: {p["_R_CARD"]};
    background: {p["_PANEL_35"]};
}}

/* скляна підложка панелей: та сама формула скла, що й картки */
QFrame[glasspanel="true"] {{
    background: {p["_GLASS_FILL"]};
    border: 1px solid {p["_GLASS_EDGE"]};
    border-top-color: {p["_GLASS_TOP"]};
    border-radius: {p["_R_CARD"]};
}}

/* badge-чіпи статусу черги файлів: готово / розпізнаю / у черзі */
QLabel[badge="done"] {{
    color: {p["GOLD_EYEBROW"]}; font-size: 13px; font-weight: 600;
    border: 1px solid {p["GOLD"]}; border-radius: 6px; padding: 3px 10px;
}}
QLabel[badge="busy"] {{
    color: {p["GOLD_EYEBROW"]}; font-size: 13px; font-weight: 600;
    background: {p["_GOLD_15"]};
    border: 1px solid {p["_GOLD_35"]}; border-radius: 6px; padding: 3px 10px;
}}
QLabel[badge="queued"] {{
    color: {p["TEXT_MUTED"]}; font-size: 13px;
    border: 1px solid {p["_LINE_SOFT"]}; border-radius: 6px; padding: 3px 10px;
}}
QLabel[badge="error"] {{
    color: {p["ALERT_TEXT"]}; font-size: 13px; font-weight: 600;
    border: 1px solid {p["_ERR_EDGE"]}; border-radius: 6px; padding: 3px 10px;
}}
QFrame[profileRow="true"]:focus {{ border: 2px solid {p["FOCUS"]}; }}
/* наведення на рядок словника — МИТТЄВИЙ QSS-hover. Активний рядок (золота
   заливка) на hover не гасне — його правило специфічніше. */
QFrame[profileRow="true"]:hover {{ background: {p["_WHITE_04"]}; }}
QFrame[card="true"][active="true"]:hover {{ background: {p["_GOLD_10"]}; border: 1px solid {p["GOLD"]}; }}

/* --- контроли: кожен має hover / pressed / keyboard focus / disabled --- */
QPushButton {{
    background: {p["DEEP"]}; color: {p["TEXT_BODY"]}; border: 1px solid {p["_LINE_SOFT"]};
    border-radius: {p["_R_CTRL"]}; padding: 8px 16px; min-height: 24px; min-width: 96px;
}}
QPushButton:hover {{ border-color: {p["_GOLD_65"]}; color: {p["TEXT_STRONG"]}; }}
QPushButton:pressed {{ background: {p["PRESS_OVERLAY"]}; }}
/* фокус: НЕ outline (Qt QSS малює його поверх контенту і він лягає на текст —
   вердикти судів 24.07 двічі), а потовщена рамка з компенсацією паддінга,
   щоб кнопка не «стрибала»: 1px+8/16 -> 2px+7/15. Рамка малюється ЛИШЕ при
   клавіатурному фокусі ([kbfocus="true"]). Правила для accent/ghost/danger —
   нижче їхніх базових, інакше програють у каскаді. */
QPushButton[kbfocus="true"] {{ border: 2px solid {p["FOCUS"]}; padding: 7px 15px; }}
QPushButton:disabled {{ color: {p["TEXT_DISABLED"]}; border-color: {p["_HAIRLINE"]}; background: {p["_PANEL_45"]}; }}
QPushButton[accent="true"] {{ background: {p["GOLD"]}; color: {p["TEXT_ON_GOLD"]}; font-weight: 600; border: 1px solid {p["GOLD"]}; padding: 8px 16px; min-height: 24px; }}
QPushButton[accent="true"]:hover {{ background: {p["GOLD_HOVER"]}; border-color: {p["GOLD_HOVER"]}; color: {p["TEXT_ON_GOLD"]}; }}
QPushButton[accent="true"]:pressed {{ background: {p["GOLD_PRESSED"]}; border-color: {p["GOLD_PRESSED"]}; }}
QPushButton[accent="true"]:disabled {{ background: {p["_GOLD_35"]}; border-color: {p["_GOLD_35"]}; color: {p["_PANEL_65"]}; }}
QPushButton[ghost="true"] {{ background: transparent; border: 1px solid transparent; color: {p["TEXT_MUTED"]}; min-width: 0; }}
QPushButton[ghost="true"]:hover {{ background: {p["HOVER_OVERLAY"]}; color: {p["TEXT_STRONG"]}; border-color: transparent; }}
QPushButton[ghost="true"]:pressed {{ background: {p["PRESS_OVERLAY"]}; }}
QPushButton[ghost="true"]:disabled {{ background: transparent; color: {p["TEXT_DISABLED"]}; }}
/* деструктив: вторинна кнопка з тонкою рамкою і стриманим теракотом (НЕ ALERT-
   заливка). Обов'язково супроводжується QMessageBox-підтвердженням. */
QPushButton[danger="true"] {{ background: transparent; color: {p["DANGER_MUTED"]}; border: 1px solid {p["_DANGER_45"]}; min-width: 0; }}
QPushButton[danger="true"]:hover {{ background: {p["_DANGER_12"]}; border-color: {p["DANGER_MUTED"]}; color: {p["DANGER_MUTED"]}; }}
QPushButton[danger="true"]:pressed {{ background: {p["_DANGER_22"]}; }}
QPushButton[danger="true"]:disabled {{ background: transparent; color: {p["TEXT_DISABLED"]}; border-color: {p["_HAIRLINE"]}; }}
/* accent: FOCUS у денній темі == GOLD (#F39200) — рамка кольору заливки
   невидима (суд-3, 0/5460 пікселів). Тому контрастна ТЕМНА рамка
   TEXT_ON_GOLD — той самий патерн, що фокус-перстень чіп-слайдера. */
QPushButton[accent="true"][kbfocus="true"] {{ border: 2px solid {p["TEXT_ON_GOLD"]}; padding: 7px 15px; }}
QPushButton[ghost="true"][kbfocus="true"] {{ border: 2px solid {p["FOCUS"]}; padding: 7px 15px; }}
QPushButton[danger="true"][kbfocus="true"] {{ border: 2px solid {p["FOCUS"]}; padding: 7px 15px; }}

QComboBox, QAbstractSpinBox {{ background: {p["DEEP"]}; border: 1px solid {p["_LINE_SOFT"]}; border-radius: {p["_R_CTRL"]}; padding: 7px 12px; min-height: 24px; }}
QComboBox:hover, QAbstractSpinBox:hover {{ border-color: {p["_GOLD_65"]}; }}
QComboBox:pressed, QAbstractSpinBox:pressed {{ background: {p["_GOLD_08"]}; }}
QComboBox:focus, QAbstractSpinBox:focus {{ border: 1px solid {p["FOCUS"]}; }}
QComboBox:disabled, QAbstractSpinBox:disabled {{ color: {p["_INK_45"]}; background: {p["_PANEL_45"]}; border-color: {p["_HAIRLINE"]}; }}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox::down-arrow {{ image: url("{p["_CHEVRON"]}"); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {p["DEEP"]}; color: {p["TEXT_BODY"]}; border: 1px solid {p["_LINE_SOFT"]}; border-radius: 6px; padding: 4px; selection-background-color: {p["_GOLD_22"]}; selection-color: {p["TEXT_STRONG"]}; }}

QLineEdit, QTextEdit, QPlainTextEdit {{ background: {p["DEEP"]}; border: 1px solid {p["_LINE_SOFT"]}; border-radius: {p["_R_CTRL"]}; padding: 8px 12px; color: {p["TEXT_BODY"]}; selection-background-color: {p["_GOLD_22"]}; }}
QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover {{ border-color: {p["_GOLD_65"]}; }}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {p["FOCUS"]}; }}
QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled {{ color: {p["_INK_45"]}; background: {p["_PANEL_45"]}; border-color: {p["_HAIRLINE"]}; }}

/* редактор розшифровки (feature/transcript-editing) — як QLineEdit, лише вищий */
QPlainTextEdit {{
    background: {p["DEEP"]};
    border: 1px solid {p["_LINE_SOFT"]};
    border-radius: {p["_R_CTRL"]};
    padding: 8px 12px;
    color: {p["TEXT_BODY"]};
    selection-background-color: {p["_GOLD_22"]};
}}
QPlainTextEdit:focus {{ border: 2px solid {p["FOCUS"]}; }}

/* уніфіковані стани QToolButton (feature/design-polish) */
QToolButton {{ border: 1px solid transparent; border-radius: 6px; }}
QToolButton:hover {{ background: {p["_GOLD_08"]}; border-color: {p["_GOLD_65"]}; }}
QToolButton:pressed {{ background: rgba(0,0,0,0.28); }}
QToolButton:focus {{ border: 1px solid {p["FOCUS"]}; }}
QToolButton:disabled {{ color: {p["_INK_45"]}; }}

/* --- вкладки Налаштувань: виразні пігулки --- */
QTabWidget::pane {{ border: none; background: transparent; top: 0px; }}
QTabWidget::tab-bar {{ left: 0; }}
QTabBar {{ background: transparent; qproperty-drawBase: 0; }}
QTabBar::tab {{
    background: {p["DEEP"]}; color: {p["TEXT_MUTED"]};
    padding: 8px 16px; margin-right: 6px;
    border: 1px solid {p["_LINE_SOFT"]};
    border-radius: {p["_R_CTRL"]};
    font-size: 14px;
}}
QTabBar::tab:hover {{ color: {p["TEXT_STRONG"]}; background: {p["_GOLD_08"]}; border-color: {p["_GOLD_65"]}; }}
QTabBar::tab:selected {{
    color: {p["TEXT_ON_GOLD"]}; background: {p["GOLD"]};
    border: 1px solid {p["GOLD"]};
    font-weight: 600;
}}
QTabBar::tab:focus {{ border-color: {p["FOCUS"]}; }}
QTabBar::tab:disabled {{ color: {p["TEXT_DISABLED"]}; border-color: {p["_HAIRLINE"]}; background: {p["_PANEL_45"]}; }}

QRadioButton, QCheckBox {{ color: {p["TEXT_BODY"]}; spacing: 10px; background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 3px 4px; }}
QRadioButton:hover, QCheckBox:hover {{ background: {p["_GOLD_08"]}; }}
QRadioButton:pressed, QCheckBox:pressed {{ background: rgba(0,0,0,0.18); }}
QRadioButton:focus, QCheckBox:focus {{ border: 1px solid {p["FOCUS"]}; }}
QRadioButton:disabled, QCheckBox:disabled {{ color: {p["_INK_45"]}; }}
QRadioButton::indicator {{ width: 18px; height: 18px; border: none; image: url("{p["_RADIO_OFF"]}"); }}
QRadioButton::indicator:hover:!checked {{ image: url("{p["_RADIO_HOV"]}"); }}
QRadioButton::indicator:checked {{ image: url("{p["_RADIO_ON"]}"); }}
QRadioButton::indicator:disabled {{ image: url("{p["_RADIO_DIS"]}"); }}
QCheckBox::indicator {{ width: 18px; height: 18px; border: 1px solid {p["_INK_45"]}; border-radius: 6px; background: transparent; }}
QCheckBox::indicator:hover {{ border-color: {p["GOLD_EYEBROW"]}; }}
QCheckBox::indicator:checked {{ background: {p["GOLD"]}; border-color: {p["GOLD"]}; image: url("{p["_CHECK"]}"); }}

QSlider::groove:horizontal {{ height: 2px; background: {p["_LINE_SOFT"]}; border: none; border-radius: 1px; }}
QSlider::sub-page:horizontal {{ background: {p["_LINE_SOFT"]}; border-radius: 1px; }}
QSlider::add-page:horizontal {{ background: {p["_LINE_SOFT"]}; border-radius: 1px; }}
QSlider::handle:horizontal {{ width: 22px; margin: -6px 0; border-radius: 7px; background: {p["GOLD"]}; border: none; }}
QSlider::handle:horizontal:hover {{ background: {p["GOLD_EYEBROW"]}; }}
QSlider::handle:horizontal:pressed {{ background: {p["GOLD"]}; }}
QSlider:disabled::groove:horizontal {{ border-color: {p["_HAIRLINE"]}; }}
QSlider:disabled::sub-page:horizontal, QSlider:disabled::handle:horizontal {{ background: {p["_INK_35"]}; border-color: {p["_HAIRLINE"]}; }}

/* --- таблиця термінів: без сітки, тонкі горизонтальні лінії --- */
QTableWidget {{
    background: {p["CARD"]};
    border: 1px solid {p["_HAIRLINE"]};
    border-radius: 6px;
    padding: 4px;
    gridline-color: transparent;
}}
QTableWidget:focus {{ border: 1px solid {p["FOCUS"]}; }}
QTableWidget::item {{
    padding: 7px 10px;
    border: none;
    border-bottom: 1px solid {p["_WHITE_06"]};
}}
QTableWidget::item:selected {{ background: {p["_GOLD_16"]}; color: {p["TEXT_STRONG"]}; }}
QHeaderView::section {{
    background: transparent;
    color: {p["TEXT_MUTED"]};
    font-size: 12px;
    font-weight: 600;
    border: none;
    border-bottom: 1px solid {p["_LINE_SOFT"]};
    padding: 7px 10px;
}}
QTableCornerButton::section {{ background: transparent; border: none; }}

QProgressBar {{
    background: {p["DEEP"]};
    border: none;
    border-radius: 6px;
    min-height: 12px;
}}
QProgressBar::chunk {{ background: {p["GOLD"]}; border-radius: 6px; }}

QDialog, QMessageBox, QInputDialog {{ border: 1px solid {p["_LINE_SOFT"]}; border-radius: 6px; }}
QDialog QPushButton, QMessageBox QPushButton, QInputDialog QPushButton {{ min-height: 24px; }}

/* --- тонкі скролбари без жолоба --- */
QScrollBar:vertical {{
    background: transparent; width: 6px; border: none; margin: 2px 0;
}}
QScrollBar::handle:vertical {{
    background: {p["_WHITE_16"]}; border-radius: 3px; min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {p["_WHITE_28"]}; }}
QScrollBar::handle:vertical:pressed {{ background: {p["_GOLD_65"]}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{
    background: transparent; height: 6px; border: none; margin: 0 2px;
}}
QScrollBar::handle:horizontal {{
    background: {p["_WHITE_16"]}; border-radius: 3px; min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{ background: {p["_WHITE_28"]}; }}
QScrollBar::handle:horizontal:pressed {{ background: {p["_GOLD_65"]}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
"""


def build_qss_mica() -> str:
    """Оверлей поверх QSS: ставиться на MainWindow ЛИШЕ після успішного вмикання
    Mica (fronts.desktop.backdrop). Вікно й host прозорі — DWM малює скло;
    сторінки контенту примусово тверді (тайл/майже-чорне)."""
    p = _P
    return f"""
QMainWindow {{ background: transparent; }}
QWidget#centralHost {{ background: transparent; }}
QFrame#sidebar {{ background: {p["SIDEBAR_GLASS"]}; }}
QStackedWidget {{ background: transparent; }}
"""


# Готові рядки для сумісності зі старим імпортом `from .theme import QSS`.
QSS = build_qss()
QSS_MICA = build_qss_mica()


# ─────────────────────── жива зміна теми ───────────────────────
# Власні малювальники (glass/level/pill/splash) читають токени через `theme.X`
# ПРИ малюванні — після зміни палітри достатньо їх перемалювати. Віджети з власним
# setStyleSheet або qta-іконкою (плеєри/нотатка/прев'ю/декор-іконки) реєструють
# колбек перебудови тут. Зберігаємо (СЛАБКЕ посилання на власника, дія): власник
# може вільно вмерти — його запис відсіється при наступному прогоні. Дію тримаємо
# так, щоб вона НЕ утримувала власника (bound-метод за іменем, або fn(owner)).
_restyle_hooks = []   # list[ (weakref.ref(owner), name_or_fn) ]


def register_restyle(bound_method) -> None:
    """Зареєструвати метод перебудови стилю віджета (без аргументів). Викликається
    у __init__ віджета з власним setStyleSheet; спрацьовує при зміні теми.
    Зберігаємо ІМʼЯ методу (рядок), а не сам bound-метод, щоб не тримати власника."""
    _restyle_hooks.append((weakref.ref(bound_method.__self__), bound_method.__name__))


def register_restyle_call(owner, fn) -> None:
    """Як register_restyle, але дія — вільна функція ``fn(owner)`` (для віджетів
    без власного методу, напр. декоративних QLabel із qta-іконкою). ``fn`` НЕ має
    захоплювати ``owner`` (його передамо аргументом), інакше власник не вмре."""
    _restyle_hooks.append((weakref.ref(owner), fn))


def _run_restyle_hooks() -> None:
    alive = []
    for ref, action in _restyle_hooks:
        owner = ref()
        if owner is None:
            continue                # власник помер — відсіюємо запис
        alive.append((ref, action))
        try:
            if isinstance(action, str):
                getattr(owner, action)()
            else:
                action(owner)
        except RuntimeError:
            pass                    # C++-обʼєкт знищено між ref() і викликом
    _restyle_hooks[:] = alive


def apply_link_colors(app) -> None:
    """Прив'язати роль QPalette.Link (колір `<a>`-посилань без власного inline-
    кольору) до акценту активної теми. Без цього QLabel малює посилання типовим
    синім (#0000FF) — заборонене не-червоне світло в нічному режимі. Кликати на
    старті ПІСЛЯ можливого нічного свопу і при кожній зміні теми."""
    from PySide6.QtGui import QColor, QPalette
    pal = app.palette()
    c = QColor(_P["GOLD"])            # активна палітра (не бара-глобаль — чисто для лінтера)
    pal.setColor(QPalette.ColorRole.Link, c)
    pal.setColor(QPalette.ColorRole.LinkVisited, c)
    app.setPalette(pal)


class _KbFocusFilter(QObject):
    """Фільтр подій для відстеження клавіатурного фокуса (властивість kbfocus)."""
    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn:
            reason = event.reason()
            if reason == Qt.FocusReason.ActiveWindowFocusReason:
                # повернення до вікна (Alt+Tab): стан рамки був вирішений
                # раніше — не чіпаємо, інакше після кліку мишею рамка
                # з'являлась би сама собою при поверненні у вікно
                return super().eventFilter(obj, event)
            quiet = (Qt.FocusReason.MouseFocusReason,
                     Qt.FocusReason.PopupFocusReason)
            self._update_focus(obj, reason not in quiet)
        elif event.type() == QEvent.FocusOut:
            self._update_focus(obj, False)
        return super().eventFilter(obj, event)

    def _update_focus(self, widget, enabled: bool):
        # фільтр стоїть на QApplication, тож ловить події і не-віджетів
        # (QWindow теж отримує FocusIn) — у них немає style()/setProperty
        if not hasattr(widget, "style"):
            return
        val = "true" if enabled else None
        if widget.property("kbfocus") != val:
            if val:
                widget.setProperty("kbfocus", val)
            else:
                widget.setProperty("kbfocus", None)
            st = widget.style()
            if st:
                st.unpolish(widget)
                st.polish(widget)


_kb_filter_instance = None


def install_keyboard_focus_filter(app) -> None:
    """Увімкнути відстеження клавіатурного фокуса для всього застосунку."""
    global _kb_filter_instance
    if _kb_filter_instance is None:
        _kb_filter_instance = _KbFocusFilter(app)
        app.installEventFilter(_kb_filter_instance)


def apply_theme(app, night: bool) -> None:
    """Змінити тему НА ЛЬОТУ (без перезапуску): свопнути палітру, перевстановити
    таблицю стилів застосунку, перебудувати стилі острівців і перемалювати все.

    Оверлей Mica (QSS_MICA) на головне вікно перевстановлює сам викликач (він
    знає, чи його вікно під склом) — див. MainWindow.reapply_theme."""
    from PySide6.QtWidgets import QApplication
    install_keyboard_focus_filter(app)
    set_mode(night)
    app.setStyleSheet(QSS)
    apply_link_colors(app)          # роль Link → акцент нової теми (не синій)
    _run_restyle_hooks()
    # Власні малювальники читають токени при paint → форсуємо повний перемал.
    for w in QApplication.allWidgets():
        try:
            w.update()
        except RuntimeError:
            pass


def setup_table_header(header) -> None:
    """Уніфіковане стилювання шапки таблиці (QHeaderView):
    канонічна роль 'table_header' та розріджені капітелі."""
    if header is None:
        return
    header.setProperty("canon_role", "table_header")
    try:
        from PySide6.QtGui import QFont
        font = header.font()
        font.setCapitalization(QFont.AllUppercase)
        header.setFont(font)
    except Exception:
        pass


def setup_table(table) -> None:
    """Уніфіковане стилювання таблиці та її шапки."""
    if table is None:
        return
    table.setProperty("canon_role", "table")
    setup_table_header(table.horizontalHeader())

