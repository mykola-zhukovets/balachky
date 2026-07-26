"""Сховище тригер→текст на TOML — спільний бекенд для голосових макросів
(whisper_core.macros). Історично це був окремий модуль «сніпетів»; після злиття
сніпетів і макросів в одну фічу «Макроси» тут лишилася лише чиста логіка
читання-запису й нормалізації тригера, яку макроси переюзовують:
  load_snippets(path)          — TOML {тригер = "текст"} → {нормалізований_тригер: текст}
  save/add/delete_snippet      — програмний запис (переформатовує файл)
  normalize_trigger(text)      — канонічна форма тригера для звірки

Файл — людський TOML: таблиця тригер = "текст", багаторядкові значення —
через TOML-рядки у три лапки.

Нормалізація тригера й тексту перед звіркою: нижній регістр, тримінг, стиснуті
внутрішні пробіли, зрізана кінцева пунктуація (.!?,). Тож «Встав підпис.» і
«встав  підпис» дадуть той самий тригер. Ключі у поверненому словнику вже
нормалізовані — і для звірки, і для показу у вікні.

ПРОГРАМНИЙ ЗАПИС (save_snippets/add_snippet/delete_snippet) ПЕРЕФОРМАТОВУЄ файл:
стандартний tomllib уміє лише читати, а tomli_w у залежностях немає — тому
записуємо власним серіалізатором. Наслідок: коментарі й ручне форматування
файлу втрачаються, а ключі зберігаються у нормалізованому вигляді. Хто хоче
зберегти коментарі — редагує файл руками (кнопка «Відкрити файл»).
"""
import re
import tomllib
from pathlib import Path

# кінцева пунктуація, яку зрізаємо при нормалізації (як у голосовій пунктуації —
# whisper часто додає крапку в кінці фрази, а тригер має матчитись без неї)
_TRAILING = " \t.!?,"


def normalize_trigger(text: str) -> str:
    """Канонічна форма тригера/тексту для звірки: нижній регістр, стиснуті
    пробіли, без кінцевої пунктуації та пробілів. Кирилиця не втрачається
    (str.lower її обробляє)."""
    s = re.sub(r"\s+", " ", text.strip().lower())
    return s.rstrip(_TRAILING)


def load_snippets(path) -> dict:
    """snippets.toml → {нормалізований_тригер: текст}. Нема файлу / порожньо /
    битий TOML → порожній dict (фіча тихо вимикається, застосунок живе).

    Значення не-рядки (напр. масив помилково) пропускаються. При колізії
    нормалізованих тригерів виграє останній у файлі."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeError, OSError) as e:
        # Windowed-exe без stdout: не print, а тихо порожньо (як config.load).
        import logging
        logging.getLogger(__name__).warning(
            "Не вдалося прочитати сніпети %s: %s — фіча вимкнена", p, e)
        return {}
    out = {}
    for trigger, value in data.items():
        if not isinstance(value, str):
            continue
        key = normalize_trigger(trigger)
        if key:
            out[key] = value
    return out


# --- програмний запис (переформатовує файл; див. докстрінг модуля) ---
def _dump_value(value: str) -> str:
    """Значення сніпета як TOML-рядок. Однорядкове — базовий рядок у лапках;
    багаторядкове — рядок у три лапки (кінцевий \\ зрізає останній перенос, тож
    round-trip не додає зайвого \\n)."""
    esc = value.replace("\\", "\\\\").replace('"', '\\"')
    if "\n" not in value:
        return '"' + esc + '"'
    # перший \n одразу після відкривних """ TOML прибирає сам; кінцевий \ гасить
    # перенос перед закривними """ — значення round-trip'иться байт-у-байт
    return '"""\n' + esc + '\\\n"""'


def _dump_key(trigger: str) -> str:
    """Ключ-тригер завжди як базовий рядок у лапках — у тригерах бувають пробіли
    й кирилиця, тож голий bare-key не годиться."""
    return '"' + trigger.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dumps(snippets: dict) -> str:
    """Словник {тригер: текст} → вміст snippets.toml (наш формат, без коментарів)."""
    lines = [
        "# Балачки — голосові шаблони (сніпети). Керується вікном; коментарі при",
        "# автозаписі не зберігаються. Довідка й приклади — snippets.example.toml.",
        "# Формат: \"тригер\" = \"текст\"  (багаторядковий текст — у три лапки).",
        "",
    ]
    for trigger in sorted(snippets):
        lines.append(f"{_dump_key(trigger)} = {_dump_value(snippets[trigger])}")
    return "\n".join(lines) + "\n"


def save_snippets(path, snippets: dict) -> None:
    """Перезаписати snippets.toml зі словника. ПЕРЕФОРМАТОВУЄ файл (див. модуль)."""
    Path(path).write_text(dumps(snippets), encoding="utf-8")


def add_snippet(path, trigger: str, text: str) -> None:
    """Додати/оновити шаблон (тригер нормалізується). Порожній тригер — no-op."""
    key = normalize_trigger(trigger)
    if not key:
        return
    data = load_snippets(path)
    data[key] = text
    save_snippets(path, data)


def delete_snippet(path, trigger: str) -> bool:
    """Видалити шаблон за (нормалізованим) тригером. → True, якщо було що видаляти."""
    key = normalize_trigger(trigger)
    data = load_snippets(path)
    if key not in data:
        return False
    del data[key]
    save_snippets(path, data)
    return True
