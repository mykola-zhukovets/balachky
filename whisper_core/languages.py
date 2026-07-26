"""feature/multilang-asr — мови розпізнавання Whisper (Т44).

Модель Whisper розпізнає ~99 мов; на вхід приймає код мови (напр. "uk") або
None — тоді визначає мову сама. Тут — ЄДИНЕ джерело правди: перелік кодів із
людськими назвами двома мовами інтерфейсу (укр + англ) і нормалізація вибору
користувача в аргумент faster-whisper ``transcribe(language=)``.

Чистий модуль-дані без Qt і без важких залежностей (не тягне faster_whisper),
щоб його однаково легко імпортували і рушій, і сторінка Налаштувань.

Джерело кодів: faster_whisper.tokenizer._LANGUAGE_CODES (звірено 2026-07).
Оновлюючи faster-whisper — перезвір цей перелік тестом test_multilang.py.
"""

# Значення cfg.language для «Автоматично (визначати)» — рушій передасть None.
AUTO = "auto"

# (код, назва_укр, назва_англ). Порядок — за кодом (легко звіряти з переліком
# faster-whisper); для показу сортуємо назвою через ordered_for_ui().
LANGUAGES = (
    ("af", "Африкаанс", "Afrikaans"),
    ("am", "Амхарська", "Amharic"),
    ("ar", "Арабська", "Arabic"),
    ("as", "Ассамська", "Assamese"),
    ("az", "Азербайджанська", "Azerbaijani"),
    ("ba", "Башкирська", "Bashkir"),
    ("be", "Білоруська", "Belarusian"),
    ("bg", "Болгарська", "Bulgarian"),
    ("bn", "Бенгальська", "Bengali"),
    ("bo", "Тибетська", "Tibetan"),
    ("br", "Бретонська", "Breton"),
    ("bs", "Боснійська", "Bosnian"),
    ("ca", "Каталонська", "Catalan"),
    ("cs", "Чеська", "Czech"),
    ("cy", "Валлійська", "Welsh"),
    ("da", "Данська", "Danish"),
    ("de", "Німецька", "German"),
    ("el", "Грецька", "Greek"),
    ("en", "Англійська", "English"),
    ("es", "Іспанська", "Spanish"),
    ("et", "Естонська", "Estonian"),
    ("eu", "Баскська", "Basque"),
    ("fa", "Перська", "Persian"),
    ("fi", "Фінська", "Finnish"),
    ("fo", "Фарерська", "Faroese"),
    ("fr", "Французька", "French"),
    ("gl", "Галісійська", "Galician"),
    ("gu", "Гуджараті", "Gujarati"),
    ("ha", "Хауса", "Hausa"),
    ("haw", "Гавайська", "Hawaiian"),
    ("he", "Іврит", "Hebrew"),
    ("hi", "Гінді", "Hindi"),
    ("hr", "Хорватська", "Croatian"),
    ("ht", "Гаїтянська креольська", "Haitian Creole"),
    ("hu", "Угорська", "Hungarian"),
    ("hy", "Вірменська", "Armenian"),
    ("id", "Індонезійська", "Indonesian"),
    ("is", "Ісландська", "Icelandic"),
    ("it", "Італійська", "Italian"),
    ("ja", "Японська", "Japanese"),
    ("jw", "Яванська", "Javanese"),
    ("ka", "Грузинська", "Georgian"),
    ("kk", "Казахська", "Kazakh"),
    ("km", "Кхмерська", "Khmer"),
    ("kn", "Каннада", "Kannada"),
    ("ko", "Корейська", "Korean"),
    ("la", "Латина", "Latin"),
    ("lb", "Люксембурзька", "Luxembourgish"),
    ("ln", "Лінгала", "Lingala"),
    ("lo", "Лаоська", "Lao"),
    ("lt", "Литовська", "Lithuanian"),
    ("lv", "Латвійська", "Latvian"),
    ("mg", "Малагасійська", "Malagasy"),
    ("mi", "Маорі", "Maori"),
    ("mk", "Македонська", "Macedonian"),
    ("ml", "Малаялам", "Malayalam"),
    ("mn", "Монгольська", "Mongolian"),
    ("mr", "Маратхі", "Marathi"),
    ("ms", "Малайська", "Malay"),
    ("mt", "Мальтійська", "Maltese"),
    ("my", "Бірманська", "Burmese"),
    ("ne", "Непальська", "Nepali"),
    ("nl", "Нідерландська", "Dutch"),
    ("nn", "Норвезька (нюношк)", "Norwegian Nynorsk"),
    ("no", "Норвезька", "Norwegian"),
    ("oc", "Окситанська", "Occitan"),
    ("pa", "Панджабі", "Punjabi"),
    ("pl", "Польська", "Polish"),
    ("ps", "Пушту", "Pashto"),
    ("pt", "Португальська", "Portuguese"),
    ("ro", "Румунська", "Romanian"),
    ("ru", "Російська", "Russian"),
    ("sa", "Санскрит", "Sanskrit"),
    ("sd", "Синдхі", "Sindhi"),
    ("si", "Сингальська", "Sinhala"),
    ("sk", "Словацька", "Slovak"),
    ("sl", "Словенська", "Slovenian"),
    ("sn", "Шона", "Shona"),
    ("so", "Сомалійська", "Somali"),
    ("sq", "Албанська", "Albanian"),
    ("sr", "Сербська", "Serbian"),
    ("su", "Сунданська", "Sundanese"),
    ("sv", "Шведська", "Swedish"),
    ("sw", "Суахілі", "Swahili"),
    ("ta", "Тамільська", "Tamil"),
    ("te", "Телугу", "Telugu"),
    ("tg", "Таджицька", "Tajik"),
    ("th", "Тайська", "Thai"),
    ("tk", "Туркменська", "Turkmen"),
    ("tl", "Тагальська", "Tagalog"),
    ("tr", "Турецька", "Turkish"),
    ("tt", "Татарська", "Tatar"),
    ("uk", "Українська", "Ukrainian"),
    ("ur", "Урду", "Urdu"),
    ("uz", "Узбецька", "Uzbek"),
    ("vi", "Вʼєтнамська", "Vietnamese"),
    ("yi", "Їдиш", "Yiddish"),
    ("yo", "Йоруба", "Yoruba"),
    ("yue", "Кантонська", "Cantonese"),
    ("zh", "Китайська", "Chinese"),
)

# код → (назва_укр, назва_англ)
_BY_CODE = {code: (uk, en) for code, uk, en in LANGUAGES}

# Головні для користувача мови — першими у селекторі (решта за абеткою).
_PINNED = ("uk", "en")

# Абетка для коректного українського сортування назв (стандартний сорт рядків
# ставить і/ї/є/ґ поза блоком а-я, тож без цього порядок був би хибний).
_UK_ALPHABET = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
_UK_INDEX = {ch: i for i, ch in enumerate(_UK_ALPHABET)}


def is_supported(code) -> bool:
    """Чи це відомий код мови розпізнавання Whisper."""
    return isinstance(code, str) and code in _BY_CODE


def display_name(code: str, ui_language: str = "uk") -> str:
    """Людська назва мови для показу; невідомий код повертаємо як є."""
    names = _BY_CODE.get(code)
    if names is None:
        return code
    return names[1] if ui_language == "en" else names[0]


def _uk_sort_key(name: str):
    return [_UK_INDEX.get(ch, len(_UK_ALPHABET)) for ch in name.lower()]


def ordered_for_ui(ui_language: str = "uk") -> list:
    """Список (код, назва) для селектора: uk та en першими (головні сценарії),
    решта — за абеткою назви поточної мови інтерфейсу. «Автоматично» додає UI
    окремим верхнім пунктом (це не мова, а режим)."""
    tail = [code for code, *_ in LANGUAGES if code not in _PINNED]
    if ui_language == "en":
        tail.sort(key=lambda c: _BY_CODE[c][1].lower())
    else:
        tail.sort(key=lambda c: _uk_sort_key(_BY_CODE[c][0]))
    order = list(_PINNED) + tail
    return [(code, display_name(code, ui_language)) for code in order]


def transcribe_language_arg(language):
    """cfg.language → аргумент faster-whisper ``transcribe(language=)``.

    "auto" / порожнє / None / невідомий код → None (модель визначає мову сама).
    Відомий код повертаємо як є. Це ЄДИНЕ місце, де вибір «Автоматично»
    перетворюється на None — рушій і всі фронти кличуть звідси."""
    if not language:
        return None
    code = str(language).strip().lower()
    if code == AUTO or code not in _BY_CODE:
        return None
    return code
