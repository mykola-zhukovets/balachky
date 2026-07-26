"""Білінгвальна пам'ять фраз: пари «як чується кирилицею → як писати».

Микола диктує українською з англійськими термінами (git, worktree, deploy, pull
request…), а STT калічить їх кирилицею. Словник термінів (whisper_core.terms)
уже вміє одиничні власні назви; ця пам'ять — про БАГАТОСЛІВНІ укр/англ-суміші
(«ворктрі» → «worktree», «пул реквест» → «pull request»), які користувач
затвердив сам.

Сховище — ОКРЕМЕ від terms.toml: phrases.toml поряд із профілем, керується
власним тумблером config.phrase_memory_enabled (незалежним від preserve_speech —
це терміни, а не стиль мовлення). Коли тумблер увімкнено, застосунок підмішує ці
пари у словник термінів на етапі завантаження (terms.merge_terms_data), тож
рушій застосовує їх ТИМ САМИМ детермінованим проходом (terms.apply_glossary)
ПІСЛЯ STT і ДО вставки, а канонічні (латинські) форми додатково живлять hotwords.

Формат phrases.toml — той самий, що й terms.toml (щоб можна було перевикористати
машинерію):

    [phrases]
    "pull request" = ["пул реквест", "пулреквест"]
    worktree       = ["ворктрі", "ворк трі"]

Ключ — як ПИСАТИ (латиниця/канон), список — як ЧУЄТЬСЯ кирилицею.

ОБМЕЖЕННЯ заміни (успадковані від terms.apply_glossary, документовано свідомо):
  • Заміняються лише ЦІЛІ токени за межами слова (\\b…\\b): «ворктрі» в
    «ворктрійка» не чіпається — і це навмисно, щоб не псувати сусідні слова.
  • Відмінки НЕ згортаються автоматично: «ворктрію», «ворктрієм» — це окремі
    почуті форми; додай кожну потрібну як окремий варіант.
  • Багатослівна фраза матчиться лише за точної послідовності токенів із тими
    самими пробілами, що у варіанті («пул реквест», не «пул  реквест»).

Приватність — канон: усе локально, нічого не покидає комп'ютер.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from .terms import Terms, build_terms

_TABLE = "phrases"


def read_phrases(phrases_path) -> dict:
    """phrases.toml → {канон: [варіанти]}. Немає файлу / битий TOML → {}
    (пам'ять фраз просто вимкнена, застосунок живе). Порожні варіанти
    відкидаються — вони дали б нуль-ширинну альтернативу в regex і псували б
    увесь текст (та сама пастка, що в terms.read_terms_dict)."""
    p = Path(phrases_path)
    if not p.exists():
        return {}
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8")).get(_TABLE, {})
    except tomllib.TOMLDecodeError as e:
        print(f"Помилка синтаксису в {p}: {e} — файл пропущено")
        return {}
    out: dict = {}
    for canon, variants in data.items():
        vs = out.setdefault(canon, [])
        for v in variants:
            if v.strip() and v.lower() not in [x.lower() for x in vs]:
                vs.append(v)
    return out


def read_learned_phrases(phrases_path) -> dict:
    """phrases.learned.toml (згенерована проєкція самонавчання, поруч із phrases.toml)
    → {канон: [варіанти]}. Немає файлу / битий TOML → {}. Читається ОКРЕМО від
    ручного phrases.toml: вивчені пари підмішуються ЗАВЖДИ (незалежно від тумблера
    phrase_memory_enabled), бо це вже підтверджені виправлення користувача, а не
    опційна пам'ять стилю."""
    return read_phrases(Path(phrases_path).with_name("phrases.learned.toml"))


def write_phrases(phrases_path, data: dict) -> None:
    """Перезаписати phrases.toml з нашого dict (формат наш → round-trip
    безпечний, коментарів тут не буває). Порожньо → файл видаляємо, щоб не
    лишати осиротілий [phrases]."""
    p = Path(phrases_path)
    if not data:
        p.unlink(missing_ok=True)
        return
    lines = ["# Білінгвальна пам'ять фраз (керує вікно програми).",
             "# Ключ — як ПИСАТИ (латиниця), список — як ЧУЄТЬСЯ кирилицею.",
             "", f"[{_TABLE}]"]
    for canon, variants in sorted(data.items()):
        key = canon if re.fullmatch(r"[A-Za-z0-9_\-]+", canon) \
            else '"' + canon.replace('"', '\\"') + '"'
        arr = ", ".join('"' + x.replace('"', '\\"') + '"' for x in variants)
        lines.append(f"{key} = [{arr}]")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_phrase(phrases_path, write: str, heard: str) -> bool:
    """Додати пару «heard (як чується) → write (як писати)». → True, якщо
    щось додалося (нова пара або новий варіант до наявного канону). Порожній
    write або heard ігнорується (обидва обов'язкові — інакше пара нінащо)."""
    write, heard = write.strip(), heard.strip()
    if not write or not heard:
        return False
    data = read_phrases(phrases_path)
    vs = data.setdefault(write, [])
    if heard.lower() in [x.lower() for x in vs]:
        return False
    vs.append(heard)
    write_phrases(phrases_path, data)
    return True


def delete_phrase(phrases_path, write: str) -> bool:
    """Видалити канон (усі його варіанти). → True, якщо було що видаляти."""
    data = read_phrases(phrases_path)
    if write not in data:
        return False
    del data[write]
    write_phrases(phrases_path, data)
    return True


def list_phrases(phrases_path) -> list:
    """[(write, [heard, ...]), ...], відсортовано за write — для таблиці UI."""
    return sorted(read_phrases(phrases_path).items())


def phrase_like(heard: str, write: str, *, max_words: int = 4,
                max_chars: int = 40) -> bool:
    """Чи схоже виправлення «heard → write» на КОРОТКУ пару-фразу, яку варто
    запам'ятати у пам'яті фраз (зворотне диктування). True лише коли обидва боки
    непорожні, різні, короткі (≤ max_words слів і ≤ max_chars символів) — тобто
    це один термін/фраза, а не переписане речення. Речення сюди не потрапляють:
    їх недоречно тримати як пару підстановки (та сама логіка, що add_phrase у
    словнику термінів — цілі короткі токени)."""
    h, w = (heard or "").strip(), (write or "").strip()
    if not h or not w or h.lower() == w.lower():
        return False
    if len(h) > max_chars or len(w) > max_chars:
        return False
    return len(h.split()) <= max_words and len(w.split()) <= max_words


def load_phrasebook(phrases_path) -> Terms:
    """phrases.toml → Terms (той самий тип, що й словник термінів). Дозволяє
    застосовувати пам'ять фраз окремо (apply_glossary) у тестах і будь-де."""
    return build_terms(read_phrases(phrases_path))


_LATIN_RE = re.compile(r"[A-Za-z]")


def bilingual_suggestions(root=None, *, samples=None, n: int = 8,
                          min_count: int = 2, profile=None) -> list:
    """Авто-навчання: кандидати у пам'ять фраз із мовного щоденника помилок.

    Беремо повторювані виправлення (error_diary.aggregate) і лишаємо ті, де
    ПРАВИЛЬНА форма («now») містить латиницю — це ознака білінгвальної фрази
    («як писати латиницею»: worktree, pull request). Так одномовні кириличні
    виправлення лишаються словнику термінів, а укр/англ-суміші пропонуються
    сюди — без дублювання того самого рядка у двох списках з однаковою дією.

    profile — пробрасуємо в error_diary.aggregate: підказки фраз лишаються в межах
    свого словника (feature/selflearn-dict), не пропонують чужу пару в клік.

    Контракт повернення — як у error_diary.top_suggestions:
    [{"was": почуто, "now": як_писати, "count": N}], найчастіші перші.
    samples — для тестів (передається далі в error_diary.aggregate)."""
    from . import error_diary
    rows = error_diary.aggregate(root, samples=samples, profile=profile)
    picked = [r for r in rows
              if r["count"] >= min_count and _LATIN_RE.search(r["now"])]
    return picked[:n]
