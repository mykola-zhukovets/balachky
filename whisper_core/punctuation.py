"""Голосова пунктуація: сказані слова-команди → розділові знаки.

Опційна (opt-in) фіча ДИКТУВАННЯ. Чиста функція без Qt і без стану: на вхід —
розпізнаний текст і мова диктування, на вихід — текст із підставленими знаками.
Якщо в тексті НЕМАЄ жодної команди — повертаємо його незмінним (байт-у-байт),
бо слово «кома» може бути змістом, а не командою (тому фіча й вимкнена типово).
"""
import re

# feature/voice-punctuation
# Набори команд за мовою диктування. Значення — рядок-замінник рівно в тому
# вигляді, як його треба вставити (пробіли в ньому — частина канонічної форми;
# зайві пробіли навколо приберемо на пост-обробці _tidy).
_COMMANDS = {
    "uk": {
        "кома": ", ",
        "крапка": ". ",
        "знак питання": "? ",
        "знак оклику": "! ",
        "двокрапка": ": ",
        "тире": " — ",
        "новий рядок": "\n",
        "з нового рядка": "\n",
        "дужка відкривається": " (",
        "дужка закривається": ") ",
    },
    "en": {
        "comma": ", ",
        "period": ". ",
        "full stop": ". ",
        "question mark": "? ",
        "exclamation mark": "! ",
        "colon": ": ",
        "dash": " — ",
        "new line": "\n",
    },
}

_PATTERNS = {}   # мова → скомпільований re.Pattern (лінива, одноразова компіляція)


def _pattern_for(language: str):
    pattern = _PATTERNS.get(language)
    if pattern is None:
        # довші (багатослівні) команди першими, щоб «знак питання» матчився
        # раніше за будь-яке коротше входження
        alts = "|".join(sorted((re.escape(cmd) for cmd in _COMMANDS[language]),
                               key=len, reverse=True))
        pattern = re.compile(rf"\b(?:{alts})\b", re.IGNORECASE)
        _PATTERNS[language] = pattern
    return pattern


_RE_SPACES = re.compile(r"[ \t]+")
_RE_NEWLINES = re.compile(r" *\n *")
_RE_PUNCT_BEFORE = re.compile(r" ([,.!?:)])")
_RE_OPEN_PAREN = re.compile(r"(\() ")
_RE_DUPE_PUNCT = re.compile(r"([,.!?:])\1+")
_RE_CAPITALIZE_AFTER = re.compile(r"([.!?] )(\w)")


def _tidy(text: str) -> str:
    """Прибрати артефакти підстановки: зайві пробіли навколо знаків, подвоєні
    знаки; після «. », «? », «! » — наступне слово з великої літери."""
    text = _RE_SPACES.sub(" ", text)                 # подвоєні пробіли → один
    text = _RE_NEWLINES.sub("\n", text)              # пробіли навколо \n геть
    text = _RE_PUNCT_BEFORE.sub(r"\1", text)         # пробіл перед знаком геть
    text = _RE_OPEN_PAREN.sub(r"\1", text)           # пробіл після «(» геть
    text = _RE_DUPE_PUNCT.sub(r"\1", text)           # подвоєні однакові знаки
    text = _RE_CAPITALIZE_AFTER.sub(
        lambda m: m.group(1) + m.group(2).upper(), text)
    return text.rstrip(" \t")


def apply_voice_punctuation(text: str, language: str = "uk") -> str:
    """text → text із підставленими знаками. language: 'uk' | 'en'.

    Без команд у тексті — повертаємо його недоторканим. Невідома мова — теж
    (набору команд для неї немає)."""
    commands = _COMMANDS.get(language)
    if not commands or not text:
        return text
    pattern = _pattern_for(language)
    if not pattern.search(text):
        return text                                     # команд нема — не чіпаємо
    result = pattern.sub(lambda m: commands[m.group(0).lower()], text)
    return _tidy(result)
