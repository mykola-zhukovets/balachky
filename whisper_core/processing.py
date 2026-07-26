"""feature/processing-slider — рівень обробки тексту (Т39).

Три позиції повзунка на вкладках Диктування та Нарада:
  • Дослівно            — вивід дорівнює розпізнаному тексту без наших замін;
  • Без слів-паразитів  — лише консервативна чистка вокалізацій/повторів;
  • З пунктуацією        — детермінована обробка «під документ»: словники, сильна
                          чистка, автокорекція, пунктуатор, форматування за контекстом
                          (для наради «Під документ» — структурований протокол).
                          Генеративне переписування локальною LLM — окремий пізніший
                          етап (спека §11 стадія 3), у цьому конвеєрі його ще нема, тож
                          третю позицію на Диктуванні названо чесно — «З пунктуацією».

Чистий модуль без Qt і без стану: ЄДИНЕ джерело правди про те, ЩО кожен рівень
дозволяє змінювати у тексті-виводі. Повзунок обирає рівень; конвеєр читає звідси
політику й вмикає/вимикає етапи. Ключова гарантія (скарга користувачів №1 «ШІ сам
перефразовує»): рівень — це ВЕРХНЯ МЕЖА змін. Контекстні профілі, макроси чи старі
налаштування не можуть тихо зробити нижчий рівень агресивнішим. Повзунок міняє лише
текст-ВИВІД: запис, VAD, розпізнавання, розрізнення голосів і сирий транскрипт в
історії — поза його впливом (дослівний завжди відновлюваний).

«Дослівно» означає незмінений вивід розпізнавання, а не акустичний оригінал: сама
модель може дати регістр чи пунктуацію.
"""
from dataclasses import dataclass
from enum import Enum

#: версія політики — лягає в метадані записів/артефактів для детермінованого
#: повтору/ретраю (спека §5).
PROCESSING_POLICY_VERSION = 1

# Поверхні (surfaces): у диктування й наради — НЕЗАЛЕЖНІ рівні, щоб протокол наради
# не змінював поведінку вставки диктування (спека §5).
DICTATION = "dictation"
MEETING = "meeting"
SURFACES = (DICTATION, MEETING)


class ProcessingMode(str, Enum):
    """Позиція повзунка. str-Enum, тож значення прямо серіалізується у profile.json."""
    VERBATIM = "verbatim"     # Дослівно
    FILLERS = "fillers"       # Без слів-паразитів
    DOCUMENT = "document"     # Диктування: «З пунктуацією»; Нарада: «Під документ»


#: порядок позицій повзунка (індекси 0..2).
MODES = (ProcessingMode.VERBATIM, ProcessingMode.FILLERS, ProcessingMode.DOCUMENT)

#: дефолт для нових профілів — найбезпечніша відповідь на скаргу «ШІ перефразовує».
DEFAULT_MODE = ProcessingMode.VERBATIM


@dataclass(frozen=True)
class ProcessingPolicy:
    """Незмінна, серіалізовна політика обробки для однієї позиції повзунка.

    Кожне поле — ВЕРХНЯ МЕЖА: етап виконається, лише якщо І політика дозволяє,
    І користувач його окремо ввімкнув (напр. cfg.autocorrect_enabled). Тому нижчі
    рівні неможливо зробити агресивнішими старим прапорцем чи контекстним профілем.
    """
    mode: ProcessingMode
    source: str             # "raw" (обхід словників) | "glossary"
    cleanup_level: str      # "off" | "medium" | "strong" (whisper_core.fillers)
    voice_commands: bool    # голосова навігація полями + голосова пунктуація
    macros: bool            # заміна голосових макросів
    autocorrect: bool       # автокорекція одруків
    punctuator: bool        # пунктуатор / ITN
    context_formatting: bool  # детермінований формат за контекстним профілем
    document_rewrite: bool  # генеративне переписування локальною LLM — етап §11 стадія 3,
                            # у конвеєрі ще не реалізований, тож наразі False на всіх
                            # позиціях (поле лишаємо для контракту з майбутнім етапом)


# Точна відповідність «позиція → етапи» (спека §3). Це таблиця-джерело правди:
# і конвеєр, і тести читають рівно її, тож розбіжності бути не може.
_POLICIES = {
    ProcessingMode.VERBATIM: ProcessingPolicy(
        mode=ProcessingMode.VERBATIM, source="raw", cleanup_level="off",
        voice_commands=False, macros=False, autocorrect=False, punctuator=False,
        context_formatting=False, document_rewrite=False),
    # Середня позиція — «medium», НЕ «strong»: дискурсивні слова («ну», «тобто»)
    # можуть нести зміст, тож «Без слів-паразитів» їх лишає (спека §2).
    ProcessingMode.FILLERS: ProcessingPolicy(
        mode=ProcessingMode.FILLERS, source="raw", cleanup_level="medium",
        voice_commands=False, macros=False, autocorrect=False, punctuator=False,
        context_formatting=False, document_rewrite=False),
    # Третя позиція («З пунктуацією» на Диктуванні): максимум ДЕТЕРМІНОВАНОЇ обробки —
    # словники, сильна чистка, голосові команди/макроси, автокорекція, пунктуатор,
    # форматування за контекстом. document_rewrite=False: генеративного переписування
    # локальною LLM у конвеєрі ще нема (спека §11 стадія 3), тож не заявляємо його
    # прапорцем, який ніхто не читає (інакше — «тихий no-op під виглядом фічі»).
    ProcessingMode.DOCUMENT: ProcessingPolicy(
        mode=ProcessingMode.DOCUMENT, source="glossary", cleanup_level="strong",
        voice_commands=True, macros=True, autocorrect=True, punctuator=True,
        context_formatting=True, document_rewrite=False),
}


def normalize_mode(mode) -> ProcessingMode:
    """Рядок / ProcessingMode / None → ProcessingMode; невідоме → DEFAULT_MODE."""
    if isinstance(mode, ProcessingMode):
        return mode
    if isinstance(mode, str):
        try:
            return ProcessingMode(mode.strip().lower())
        except ValueError:
            return DEFAULT_MODE
    return DEFAULT_MODE


def policy_for_mode(mode) -> ProcessingPolicy:
    """Політика обробки для позиції (рядок/enum). Незмінна, спільна на всіх викликах."""
    return _POLICIES[normalize_mode(mode)]


def profile_mode(profile, surface: str) -> str:
    """Режим поверхні з профілю; getattr-захищено (тест-дублі без .processing_mode
    дають DEFAULT_MODE) — щоб побудова сторінки не залежала від справжнього профілю."""
    getter = getattr(profile, "processing_mode", None)
    return getter(surface) if callable(getter) else DEFAULT_MODE.value


def source_text(policy: ProcessingPolicy, raw: str, glossary_text: str) -> str:
    """Стартовий текст за політикою: сирий (Дослівно / Без-паразитів — обхід
    словників) чи вже словниковий (Під документ)."""
    return raw if policy.source == "raw" else glossary_text


def mode_index(mode) -> int:
    """Позиція повзунка 0..2 для режиму."""
    return MODES.index(normalize_mode(mode))


def mode_from_index(index: int) -> ProcessingMode:
    """Режим за позицією повзунка; поза діапазоном → DEFAULT_MODE."""
    try:
        index = int(index)
    except (TypeError, ValueError):
        return DEFAULT_MODE
    return MODES[index] if 0 <= index < len(MODES) else DEFAULT_MODE


def migrate_dictation_mode(cleanup_level: str, *, preserve_speech: bool,
                           autocorrect_enabled: bool,
                           punctuator_enabled: bool) -> ProcessingMode:
    """Одноразова міграція старих глобальних прапорців → режим диктування (спека §5).

    cleanup_level — уже обчислений чинний рівень (whisper_core.config.
    cleanup_level_for_cfg), щоб цей модуль не залежав від Config. «Сильна» стара
    чистка навмисно мапиться на «Без слів-паразитів» (medium): рівень «strong»
    лишається лише всередині явно перетворювального «Під документ» (спека §2)."""
    if preserve_speech and cleanup_level == "off":
        return ProcessingMode.VERBATIM
    if autocorrect_enabled or punctuator_enabled:
        return ProcessingMode.DOCUMENT
    if cleanup_level != "off":
        return ProcessingMode.FILLERS
    return ProcessingMode.VERBATIM
