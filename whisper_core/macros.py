"""Голосові макроси: сказаний тригер → розгортка з підстановками дати/часу.

Референс — Dragon NaturallySpeaking (без ML). Це ЄДИНА фіча підстановок у
застосунку: колишні глобальні «сніпети» злиті сюди (див. migrate_snippets).
Ключове:
  • розгортка може містити плейсхолдери {дата} і {час} (та англ. аліаси
    {date}/{time}), які підставляються В МОМЕНТ вставки поточними датою/часом;
  • сховище — ПЕР-ПРОФІЛЬНЕ: macros.toml лежить поруч зі словником профілю
    (whisper_core.profiles.Profile.macros_path), а не глобально.

Матчинг — безпечний контракт: розгортка вставляється ЛИШЕ коли нормалізований
текст диктування ТОЧНО збігається з тригером; часткові чи вбудовані збіги
ігноруються (щоб макрос не спрацював посеред довшої фрази).

Формат/серіалізацію toml та нормалізацію тригера переюзуємо зі snippets (тепер
це просто спільний TOML-бекенд) — щоб не дублювати логіку читання-запису. Тут —
лише те, що макроси додають зверху: підстановка плейсхолдерів, матчинг і
одноразова міграція старих сніпетів.
"""
import re
from datetime import datetime
from pathlib import Path

from .snippets import (
    normalize_trigger,
    load_snippets as _load_toml,
    save_snippets as _save_toml,
    add_snippet as _add,
    delete_snippet as _delete,
)

# {дата}/{date} → дд.мм.рррр, {час}/{time} → гг:хв. Регістр і мова аліасів
# не мають значення (Микола може написати {Дата} чи {TIME}). Фігурні дужки
# екрануємо у власному тексті користувача НЕ треба — {інше} лишається як є.
_DATE_TOKENS = ("дата", "date")
_TIME_TOKENS = ("time", "час")
_TOKEN_RE = re.compile(r"\{(" + "|".join(_DATE_TOKENS + _TIME_TOKENS) + r")\}",
                       re.IGNORECASE)


def expand_placeholders(text: str, now: datetime = None) -> str:
    """Підставити {дата}/{час} (та англ. аліаси) поточними датою/часом.
    `now` — для тестів; типово datetime.now(). Невідомі {токени} лишаються
    дослівно (не наш плейсхолдер — не чіпаємо)."""
    if not text or "{" not in text:
        return text
    stamp = now or datetime.now()

    def _sub(m):
        token = m.group(1).lower()
        if token in _DATE_TOKENS:
            return stamp.strftime("%d.%m.%Y")
        return stamp.strftime("%H:%M")

    return _TOKEN_RE.sub(_sub, text)


def load_macros(path) -> dict:
    """macros.toml → {нормалізований_тригер: розгортка}. Нема файлу / порожньо /
    битий TOML → порожній dict (фіча тихо вимикається, програма живе)."""
    return _load_toml(path)


def apply_macro(text: str, macros: dict, now: datetime = None) -> str:
    """text → розгортка з підставленими датою/часом, ЯКЩО нормалізований text
    ТОЧНО збігається з тригером. Інакше — text незмінним (часткові/вбудовані
    збіги не замінюються навмисно — безпека від випадкових підстановок)."""
    if not text or not macros:
        return text
    key = normalize_trigger(text)
    if key not in macros:
        return text
    return expand_placeholders(macros[key], now)


# --- програмний запис (переформатовує файл, як у snippets) ---
def save_macros(path, macros: dict) -> None:
    """Перезаписати macros.toml зі словника (наш формат, без коментарів)."""
    _save_toml(path, macros)


def add_macro(path, trigger: str, expansion: str) -> None:
    """Додати/оновити макрос (тригер нормалізується). Порожній тригер — no-op."""
    _add(path, trigger, expansion)


def delete_macro(path, trigger: str) -> bool:
    """Видалити макрос за (нормалізованим) тригером. → True, якщо було що видаляти."""
    return _delete(path, trigger)


def migrate_snippets(snippets_path, macros_path) -> int:
    """Одноразова міграція злиття: колишні глобальні голосові шаблони (сніпети)
    переносяться в macros.toml профілю. Наявний макрос має пріоритет — сніпет із
    тим самим тригером його НЕ перезаписує. Після переносу snippets.toml
    видаляється: його відсутність і є маркером «вже мігровано», тож повторні
    запуски проходять повз (нема файлу → 0, no-op). → кількість перенесених.

    Викликається на старті ОДИН фактичний раз (поки файл сніпетів існує)."""
    sp = Path(snippets_path)
    if not sp.exists():
        return 0
    snippets = _load_toml(sp)          # битий/порожній → {} (фіча й так була off)
    macros = _load_toml(macros_path)
    migrated = 0
    for trigger, text in snippets.items():
        if trigger not in macros:
            macros[trigger] = text
            migrated += 1
    if migrated:
        _save_toml(macros_path, macros)
    try:
        sp.unlink()                    # мігровано — прибрати, щоб не повторювати
    except OSError:
        pass
    return migrated
