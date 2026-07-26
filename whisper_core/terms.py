"""Словник термінів: biasing (hotwords/initial_prompt) + детермінована заміна.

Шлях до terms.toml — параметр (на Етапі 3 стане шляхом активного профілю).
Формат terms.toml НЕ змінюється.
"""
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import dict_backup


@dataclass
class Terms:
    hotwords: str = ""
    initial_prompt: str = ""
    pattern: object = None          # скомпільований re.Pattern або None
    variant_map: dict = field(default_factory=dict)


def read_terms_dict(terms_path, *, on_recovered=None) -> dict:
    """Об'єднаний словник: terms.toml (людський, з коментарями) + terms.auto.toml
    (машинні доповнення від вікна/learn) + terms.learned.toml (згенерована проєкція
    самонавчання, whisper_core.self_learning). Битий файл пропускається з
    попередженням. Порядок: людський → машинний → вивчений.

    on_recovered — опційний колбек(шлях), кличеться, якщо terms.auto.toml був
    побитий і піднято резервну копію (whisper_core.dict_backup) — вікно
    програми може показати користувачу повідомлення про відновлення."""
    merged = {}
    terms_path = Path(terms_path)
    auto_path = terms_path.with_name("terms.auto.toml")
    for p in (terms_path, auto_path, terms_path.with_name("terms.learned.toml")):
        if p == auto_path:
            data = _read_auto(terms_path, on_recovered=on_recovered)
        else:
            if not p.exists():
                continue
            try:
                data = tomllib.loads(p.read_text(encoding="utf-8")).get("terms", {})
            except tomllib.TOMLDecodeError as e:
                print(f"Помилка синтаксису в {p}: {e} — файл пропущено")
                continue
        for canon, variants in data.items():
            vs = merged.setdefault(canon, [])
            for v in variants:
                # порожній варіант дав би нуль-ширинну альтернативу в regex
                # (\b(?:слово|)\b) → канон вставлявся б на КОЖНІЙ межі слова
                # й тихо псував увесь текст; відкидаємо
                if not v.strip():
                    continue
                if v.lower() not in [x.lower() for x in vs]:
                    vs.append(v)
    return merged


def _auto_path(terms_path) -> Path:
    return Path(terms_path).with_name("terms.auto.toml")


def _parse_auto_text(text: str) -> dict:
    return dict(tomllib.loads(text).get("terms", {}))


def _read_auto(terms_path, *, on_recovered=None) -> dict:
    """Прочитати ЛИШЕ машинний terms.auto.toml → {канон: [варіанти]}.
    Немає файлу → {}. Битий TOML → пробуємо підняти останню цілу резервну
    копію (whisper_core.dict_backup); якщо піднялась — кличемо on_recovered
    (якщо задано) і повертаємо ЇЇ дані; жодної цілої копії нема → {}."""
    auto = _auto_path(terms_path)
    if not auto.exists():
        return {}
    try:
        return _parse_auto_text(auto.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        data, recovered = dict_backup.recover(auto, _parse_auto_text)
        if recovered:
            if on_recovered:
                on_recovered(auto)
            return data
        return {}


def _write_auto(terms_path, data: dict) -> None:
    """Перезаписати terms.auto.toml з нашого dict (формат наш, тож round-trip
    безпечний — коментарів тут не буває). Перед перезаписом (і перед
    видаленням, коли data порожній) попередню версію відсуває в резервну
    копію (whisper_core.dict_backup) — інакше збій запису чи випадкове
    очищення втратили б словник без сліду. Порожньо → сам файл не
    відтворюємо, щоб не лишати осиротілий [terms]."""
    auto = _auto_path(terms_path)
    dict_backup.rotate_before_write(auto)
    if not data:
        return
    lines = ["# Машинні доповнення словника (керує вікно програми та learn).",
             "# Людський файл зі своїми коментарями — terms.toml поруч.",
             "", "[terms]"]
    for c, variants in sorted(data.items()):
        key = c if re.fullmatch(r"[A-Za-z0-9_\-]+", c) else '"' + c.replace('"', '\\"') + '"'
        arr = ", ".join('"' + x.replace('"', '\\"') + '"' for x in variants)
        lines.append(f"{key} = [{arr}]")
    auto.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_term(terms_path, canon: str, variant: str = "") -> None:
    """Дописати термін у terms.auto.toml (машинний файл, формат наш).
    Людський terms.toml НЕ чіпаємо — інакше втратили б коментарі користувача."""
    data = _read_auto(terms_path)
    vs = data.setdefault(canon, [])
    v = variant.strip()
    if v and v.lower() not in [x.lower() for x in vs]:
        vs.append(v)
    _write_auto(terms_path, data)


# --- feature/vocab-manage: правка/видалення машинної частини словника ---
def editable_canons(terms_path) -> set:
    """Канони, які МОЖНА безпечно правити чи видаляти з вікна: ті, що живуть
    лише у машинному terms.auto.toml. Терміни з людського terms.toml (навіть
    якщо канон є і там, і там) лишаються read-only — там бувають коментарі й
    форматування, а tomllib уміє лише читати, тож round-trip не гарантований."""
    human = {}
    tp = Path(terms_path)
    if tp.exists():
        try:
            human = tomllib.loads(tp.read_text(encoding="utf-8")).get("terms", {})
        except tomllib.TOMLDecodeError:
            human = {}
    return set(_read_auto(terms_path)) - set(human)


def delete_term(terms_path, canon: str) -> bool:
    """Видалити канон із машинного terms.auto.toml. → True, якщо було що видаляти.
    Людський terms.toml не чіпаємо (див. editable_canons)."""
    data = _read_auto(terms_path)
    if canon not in data:
        return False
    del data[canon]
    _write_auto(terms_path, data)
    return True


def rename_term(terms_path, old_canon: str, new_canon: str) -> bool:
    """Змінити канонічну форму терміна у машинному terms.auto.toml, зберігши його
    варіанти. Якщо new_canon вже є в auto — зливаємо варіанти в один запис.
    → True, якщо змінено. Людський terms.toml не чіпаємо (див. editable_canons)."""
    new_canon = new_canon.strip()
    if not new_canon or new_canon == old_canon:
        return False
    data = _read_auto(terms_path)
    if old_canon not in data:
        return False
    variants = data.pop(old_canon)
    target = data.setdefault(new_canon, [])
    for v in variants:
        if v.lower() not in [x.lower() for x in target]:
            target.append(v)
    _write_auto(terms_path, data)
    return True


# feature/bulk-import
def parse_bulk_terms(text: str, existing: dict = None) -> tuple:
    """Розібрати багаторядковий список термінів для масового імпорту.

    Кожен непорожній, некоментарний рядок — один з двох форматів:
      слово               → канонічна форма без заміни (лише біасинг)
      почуто = друкувати   → заміна: "почуто" (як почулось) на "друкувати" (канон)
    Пробіли довкола «=» довільні. Порожні рядки та рядки, що починаються
    з "#", пропускаються мовчки (не рахуються як пропущені).

    `existing` — злитий словник canon -> [variants] (як з read_terms_dict),
    проти якого перевіряються дублікати; None/{} означає порожній словник.
    Дублікати (без урахування регістру) — і в межах вставленого тексту, і
    проти наявних — відкидаються та рахуються.

    Повертає (нові_терміни, кількість_пропущених): нові_терміни — список
    (canon, variant), variant="" якщо заміни нема.
    """
    existing_norm = {
        canon.strip().lower(): {v.strip().lower() for v in variants}
        for canon, variants in (existing or {}).items()
    }

    seen = set()
    new_terms = []
    skipped = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            heard, _, printed = line.partition("=")
            variant, canon = heard.strip(), printed.strip()
        else:
            canon, variant = line, ""
        if not canon:
            continue

        key = (canon.lower(), variant.lower())
        if key in seen:
            skipped += 1
            continue
        known_variants = existing_norm.get(canon.lower())
        if known_variants is not None and (not variant or variant.lower() in known_variants):
            skipped += 1
            continue

        seen.add(key)
        new_terms.append((canon, variant))
    return new_terms, skipped


def parse_csv_terms(text: str, existing: dict = None) -> tuple:
    """Розібрати CSV словника для імпорту з файлу.

    Кожен непорожній, некоментарний рядок — «термін;вимова1;вимова2…»:
    перша колонка це канонічний термін, решта — почуті варіанти, що на нього
    замінюються (кілька на один термін). Роздільник — «;». Рядок лише з
    терміном (без варіантів) → біасинг без заміни. Порожні клітинки, порожні
    рядки та рядки з «#» пропускаються мовчки (не рахуються як пропущені).

    Дублікати (в межах файлу й проти `existing`, без урахування регістру)
    відкидаються та рахуються. Контракт повернення той самий, що
    parse_bulk_terms: (нові_терміни, к-сть_пропущених), нові_терміни —
    список (canon, variant).
    """
    existing_norm = {
        canon.strip().lower(): {v.strip().lower() for v in variants}
        for canon, variants in (existing or {}).items()
    }

    seen = set()
    new_terms = []
    skipped = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cells = [c.strip() for c in line.split(";")]
        canon = cells[0]
        if not canon:
            continue
        variants = [c for c in cells[1:] if c] or [""]
        for variant in variants:
            key = (canon.lower(), variant.lower())
            if key in seen:
                skipped += 1
                continue
            known = existing_norm.get(canon.lower())
            if known is not None and (not variant or variant.lower() in known):
                skipped += 1
                continue
            seen.add(key)
            new_terms.append((canon, variant))
    return new_terms, skipped


def build_terms(data: dict) -> Terms:
    """Побудувати Terms із готового словника {канон: [варіанти]}.

    Виділено з load_terms, щоб ту саму машинерію (hotwords + initial_prompt +
    скомпільований pattern заміни) міг перевикористати білінгвальний phrasebook
    (whisper_core.phrasebook) без дублювання regex-логіки. Порожньо → порожній
    Terms (словник вимкнено, але застосунок живе)."""
    if not data:
        return Terms()
    canons = list(data)
    hotwords = ", ".join(canons)
    initial_prompt = "Технічна розмова українською. Терміни: " + ", ".join(canons) + "."
    variant_map = {v.lower(): canon for canon, variants in data.items()
                   for v in variants if v.strip()}    # без порожніх (див. read_terms_dict)
    # довші варіанти першими, щоб «ко ворк» матчився раніше за можливий «ворк»
    alts = "|".join(sorted((re.escape(v) for v in variant_map), key=len, reverse=True))
    pattern = re.compile(rf"\b(?:{alts})\b", re.IGNORECASE) if alts else None
    return Terms(hotwords, initial_prompt, pattern, variant_map)


def load_terms(terms_path) -> Terms:
    """terms.toml (+auto) → Terms. Нема файлів / порожньо / битий TOML → порожній
    Terms (словник вимкнено, але застосунок живе)."""
    return build_terms(read_terms_dict(terms_path))


def merge_terms_data(*dicts) -> dict:
    """Злити кілька словників {канон: [варіанти]} в один (для транскрипції одним
    проходом). Варіанти зливаються без регістрових дублікатів; порядок канонів —
    за першою появою. Використовується, щоб підмішати білінгвальну пам'ять фраз
    у словник термінів профілю, коли ввімкнено відповідний тумблер."""
    merged: dict = {}
    for data in dicts:
        for canon, variants in (data or {}).items():
            vs = merged.setdefault(canon, [])
            for v in variants:
                if v.strip() and v.lower() not in [x.lower() for x in vs]:
                    vs.append(v)
    return merged


def apply_glossary(text: str, terms: Terms) -> str:
    """Шар B: детермінована заміна чутих кирилізацій на правильну форму."""
    if not terms.pattern:
        return text
    return terms.pattern.sub(lambda m: terms.variant_map[m.group(0).lower()], text)
