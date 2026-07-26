"""Голосова навігація полями зовнішніх документів (feature/office-voice-nav).

Референс — Dragon Medical One «next field»/«first field» (навігація по полях
шаблону голосом) і прогалина Excel Dictate, який НЕ вміє переходити між
комірками голосом (див. RESEARCH-office-надбудова-продукт). БЕЗ панелі/надбудови:
командний шар поверх наявного механізму вставки. Микола диктує рапорт у Word чи
Excel і керує курсором голосом — «наступне поле» тисне Tab у документі, «комірка
Б7» переходить у комірку через Go To (Ctrl+G).

Чиста логіка без Qt і без надсилання клавіш: на вхід — розпізнаний текст диктанту
й мова, на вихід — NavAction або None. Переклад дії у клавіші — у wininput;
безпека цілі й доставка — у paste. Розбір команд повторює контракт макросів і
formfill: команда розпізнається ЛИШЕ при ТОЧНОМУ збігу нормалізованої фрази з
відомою командою (часткові/вбудовані збіги — ні). Виняток — адреса комірки:
«комірка <адреса>» матчиться за префіксом, бо адреса змінна.

── Межа «команда vs звичайний текст» (рішення й документація) ──────────────────
Фіча — opt-in (cfg.voice_nav_enabled, типово ВИМК). Це і є «явний режим»: доки
режим вимкнено, «наступне поле» завжди лишається текстом. Коли режим увімкнено,
діє контракт ТОЧНОГО-ЗБІГУ-ЦІЛОЇ-ФРАЗИ (той самий, що вже стереже макроси й
formfill): нормалізований текст усього диктанту має дорівнювати команді. Тобто
«наступне поле» окремою фразою → команда; «наступне поле буде складним» →
звичайний текст (вставляється). Це свідомий компроміс, ідентичний макросам:
доки режим увімкнено, продиктувати рівно фразу-команду як буквальний зміст
не можна (вимкни режим або додай слово). Обрано саме цей контракт, а не
слово-префікс, щоб не плодити нову модель поведінки поряд із наявними
макросами/formfill і щоб команда звучала природно («наступне поле», не
«команда наступне поле»).
"""
import re

from . import paths

# NavAction — простий кортеж (kind, arg):
#   ("key",  <ім'я-клавіші>)   — надіслати клавішу/акорд: tab | shift_tab | enter
#                                 | up | down | left | right (розуміє wininput)
#   ("goto", "<АДРЕСА>")        — перейти в комірку Excel (Ctrl+G + адреса + Enter)

# Ідентифікатори дій → NavAction. Через id (а не пряму дію) описуються і вбудовані
# фрази, і користувацькі аліаси — одне джерело правди, аліас просто вказує id.
_ACTIONS = {
    "next_field": ("key", "tab"),
    "prev_field": ("key", "shift_tab"),
    "next_cell": ("key", "tab"),        # Excel: Tab — природна «наступна комірка»
    "cell_down": ("key", "down"),
    "cell_up": ("key", "up"),
    "cell_left": ("key", "left"),
    "cell_right": ("key", "right"),
    "confirm": ("key", "enter"),
}

# Вбудовані фрази за мовою диктування → id дії. Нормалізовані (нижній регістр, без
# кінцевої пунктуації). Синоніми — на випадок різного розпізнавання ASR. НЕ беремо
# фрази, що збігаються з голосовою пунктуацією («новий рядок») — щоб дві фічі не
# конфліктували за одну фразу; для Enter — окремі «підтвердити»/«готово».
_PHRASES = {
    "uk": {
        "наступне поле": "next_field",
        "попереднє поле": "prev_field",
        "наступна комірка": "next_cell",
        "наступна клітинка": "next_cell",
        "комірка нижче": "cell_down",
        "клітинка нижче": "cell_down",
        "комірка вище": "cell_up",
        "клітинка вище": "cell_up",
        "комірка ліворуч": "cell_left",
        "комірка праворуч": "cell_right",
        "підтвердити": "confirm",
        "готово": "confirm",
    },
    "en": {
        "next field": "next_field",
        "previous field": "prev_field",
        "next cell": "next_cell",
        "cell below": "cell_down",
        "cell down": "cell_down",
        "cell above": "cell_up",
        "cell up": "cell_up",
        "cell left": "cell_left",
        "cell right": "cell_right",
        "confirm": "confirm",
        "done": "confirm",
    },
}

# Префікс адресної команди «комірка <адреса>» / «cell <адреса>» за мовою.
_CELL_PREFIXES = {
    "uk": ("комірка", "клітинка"),
    "en": ("cell",),
}

# Кирилиця → латинська літера стовпця Excel. Стовпці Excel завжди латиницею, але
# ASR укр. диктанту часто повертає кирилицю. Мапимо два класи:
#   • візуальні двійники (той самий гліф): А В Е І К М Н О Р С Т У Х;
#   • фонетичні (як укр. читається латинська літера): Б→B (приклад Миколи «Б7»),
#     Д→D, Ф→F, Г/Ґ→G, З→Z, Л→L.
# Неоднозначність B/V вирішена на користь конвенції Миколи: і Б, і В → B (стовпець
# V — рідкісний; для нього продиктуйте латинську «V»). Для стовпців без певного
# двійника вимовляйте латинську літеру, як показано в шапці Excel. Ключі —
# ВЕЛИКІ літери; вхід підіймаємо у регістр ДО translate.
_CYR_TO_LAT = str.maketrans({
    "А": "A", "Б": "B", "В": "B", "Г": "G", "Ґ": "G", "Д": "D", "Е": "E",
    "З": "Z", "І": "I", "К": "K", "Л": "L", "М": "M", "Н": "H", "О": "O",
    "П": "P", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Ф": "F", "Х": "X",
})

_TRAILING = " \t.!?,"
_CELL_RE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]{0,6})$")


def _normalize(text: str) -> str:
    """Канонічна форма фрази: нижній регістр, стиснуті пробіли, без кінцевої
    пунктуації (той самий контракт, що normalize_trigger у сніпетах/formfill)."""
    s = re.sub(r"\s+", " ", (text or "").strip().lower())
    return s.rstrip(_TRAILING)


def parse_cell_address(raw: str):
    """Сказана адреса комірки → канонічна «B7» або None.

    Прибираємо пробіли («б 7» → «б7»), мапимо кириличні літери-двійники в
    латиницю, підіймаємо регістр і звіряємо форму [літери][цифри] (стовпець
    A..ZZZ, рядок 1..9999999). Невалідне (порожнє, лише літери, чужі символи) →
    None. Провідний нуль у рядку («B07») відкидаємо — Excel рядка «07» не має."""
    if not raw:
        return None
    s = re.sub(r"\s+", "", raw).upper().translate(_CYR_TO_LAT)
    m = _CELL_RE.match(s)
    return m.group(1) + m.group(2) if m else None


def match(text: str, language: str = "uk", aliases: dict = None):
    """Розпізнаний диктант → NavAction або None.

    Порядок: (1) точна фраза серед вбудованих + користувацьких аліасів; (2)
    адресна команда «комірка <адреса>». Порожній текст / невідома мова → None.
    aliases — {нормалізована_фраза: id_дії} (див. load_aliases); користувацький
    аліас перекриває вбудовану фразу з тим самим текстом."""
    norm = _normalize(text)
    if not norm:
        return None

    table = dict(_PHRASES.get(language, {}))
    if aliases:
        table.update(aliases)
    action_id = table.get(norm)
    if action_id and action_id in _ACTIONS:
        return _ACTIONS[action_id]

    # адресна команда: «комірка Б7» → ("goto", "B7"). Точні фрази (як-от «комірка
    # нижче») уже відсіклись вище, тож сюди доходить лише «комірка <адреса>».
    for prefix in _CELL_PREFIXES.get(language, ()):
        head = prefix + " "
        if norm.startswith(head):
            addr = parse_cell_address(norm[len(head):])
            if addr:
                return ("goto", addr)
            return None   # «комірка …» без валідної адреси — не команда й не текст
    return None


# --- користувацькі аліаси (розширення списку команд у налаштуваннях) ----------
def load_aliases(path) -> dict:
    """navcommands.toml → {нормалізована_фраза: id_дії}. Формат:

        [aliases]
        "далі" = "next_field"
        "назад" = "prev_field"

    Приймаємо ЛИШЕ фрази, чий id є серед відомих дій (_ACTIONS) — невідомий id
    тихо ігнорується (друкарська помилка не має ламати фічу). Нема файлу /
    порожньо / битий TOML → {} (фіча працює лише на вбудованих командах)."""
    import tomllib
    from pathlib import Path
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return {}
    raw = data.get("aliases", data)
    if not isinstance(raw, dict):
        return {}
    out = {}
    for phrase, action_id in raw.items():
        if isinstance(action_id, str) and action_id in _ACTIONS:
            key = _normalize(phrase)
            if key:
                out[key] = action_id
    return out


def aliases_path():
    """Шлях до navcommands.toml (поруч із config.toml). Окремий файл — бо
    серіалізатор config.py скалярний і не пише таблиці; лишається редагованим
    руками, як context_profiles.toml/macros.toml."""
    return paths.user_dir() / "navcommands.toml"


# --- довідка команд (сторінка-шпаргалка, поповнюється разом із таблицями) ------
# id дії → i18n-ключ опису ефекту (UI резолвить tr()). Порядок — логічні групи.
_REFERENCE_ORDER = (
    ("next_field", "nav_ref_next_field"),
    ("prev_field", "nav_ref_prev_field"),
    ("next_cell", "nav_ref_next_cell"),
    ("cell_down", "nav_ref_cell_down"),
    ("cell_up", "nav_ref_cell_up"),
    ("cell_left", "nav_ref_cell_left"),
    ("cell_right", "nav_ref_cell_right"),
    ("confirm", "nav_ref_confirm"),
)


def command_reference(language: str = "uk", aliases: dict = None) -> list:
    """Список рядків шпаргалки: [(фраза, i18n_ключ_ефекту), …] для мови.

    Для кожної дії беремо першу вбудовану фразу цієї мови; далі — адресна команда
    (окремий i18n-ключ) і, якщо є, користувацькі аліаси (їхня фраза + ефект тієї
    дії). Шпаргалка так «поповнюється» новими командами автоматично."""
    phrases = _PHRASES.get(language, {})
    # id → перша вбудована фраза цієї мови (для показу канонічного прикладу)
    first_phrase = {}
    for phrase, action_id in phrases.items():
        first_phrase.setdefault(action_id, phrase)

    rows = []
    for action_id, effect_key in _REFERENCE_ORDER:
        phrase = first_phrase.get(action_id)
        if phrase:
            rows.append((phrase, effect_key))

    # адресна команда (приклад із першим префіксом мови)
    prefixes = _CELL_PREFIXES.get(language, ())
    if prefixes:
        rows.append((f"{prefixes[0]} B7", "nav_ref_goto"))

    # користувацькі аліаси — показуємо з ефектом їхньої дії
    effect_by_id = {aid: key for aid, key in _REFERENCE_ORDER}
    for phrase, action_id in (aliases or {}).items():
        key = effect_by_id.get(action_id)
        if key:
            rows.append((phrase, key))
    return rows
