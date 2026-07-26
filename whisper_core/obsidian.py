"""feature/obsidian-channel: доставка готового Markdown наради у сховище Obsidian.

Балачки вже будують Markdown транскрипту наради (postprocess.to_transcript_markdown
з YAML-frontmatter). Цей модуль — тонкий КАНАЛ доставки того Markdown у вибрану
користувачем папку сховища Obsidian; сам Obsidian підхоплює новий файл своїм
файловим watcher-ом (окремий плагін не потрібен — див. розвідку 2026-07-18).

Тут ЛИШЕ чиста логіка (без Qt), щоб її можна було покрити тестами:
  * render_filename  — імʼя файлу за шаблоном із плейсхолдерами {дата}{назва}{час};
  * resolve_collision — вільне імʼя (суфікс «-2», без перезапису оригіналу);
  * write_markdown   — безпечний запис ЛИШЕ в межах вибраної папки (safe_under);
  * open_uri         — obsidian://open?path=… для відкриття файлу в Obsidian.
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

from . import paths

#: Заборонені у Windows-іменах символи + керівні коди → замінюємо на «-».
#: Серед них роздільники шляху / і \ — тому імʼя за визначенням не вийде з папки.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Дефолтний шаблон імені: «РРРР-ММ-ДД-назва-наради.md» (розширення додаємо самі).
DEFAULT_TEMPLATE = "{дата}-{назва}"


def _sanitize(text: str) -> str:
    """Довільний рядок → безпечний компонент імені файлу.

    Заборонені символи (у т. ч. роздільники шляху) → «-»; злиплі дефіси/пробіли
    стягуємо в один; краї від дефісів/пробілів/крапок чистимо (Windows не любить
    хвостову крапку). Порожній результат → «»."""
    text = _ILLEGAL.sub("-", str(text or ""))
    text = re.sub(r"[-\s]{2,}", "-", text)
    return text.strip(" .-")


def render_filename(template: str, *, date: str = "", name: str = "",
                    time: str = "") -> str:
    """Шаблон імені → безпечне імʼя файлу з розширенням .md.

    Плейсхолдери (укр і англ-синоніми): {дата}/{date}, {назва}/{name},
    {час}/{time}. Спершу підставляємо значення, далі санітизуємо ВЕСЬ результат
    (щоб і сам шаблон не проніс роздільник шляху). Порожній результат (усі
    плейсхолдери порожні) → запасне «нарада». Розширення .md додаємо, якщо його
    ще немає (без урахування регістру)."""
    template = template or DEFAULT_TEMPLATE
    subs = {
        "{дата}": date, "{date}": date,
        "{назва}": name, "{name}": name,
        "{час}": time, "{time}": time,
    }
    out = template
    for key, val in subs.items():
        out = out.replace(key, str(val or ""))
    out = _sanitize(out)
    if not out:
        out = "нарада"
    if not out.lower().endswith(".md"):
        out += ".md"
    return out


def resolve_collision(directory, filename: str) -> Path:
    """Повний шлях у ``directory``, якого ще НЕ існує на диску.

    Якщо ``filename`` вільний — повертаємо ``directory/filename``. Якщо зайнятий —
    додаємо суфікс «-2», «-3»… перед розширенням («імʼя.md» → «імʼя-2.md»), доки
    не знайдемо вільне. Оригінал НЕ перезаписуємо (вимога: колізія → суфікс)."""
    directory = Path(directory)
    stem = Path(filename).stem
    suffix = Path(filename).suffix or ".md"
    candidate = directory / filename
    n = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{n}{suffix}"
        n += 1
    return candidate


def write_markdown(vault_dir, filename: str, markdown: str) -> Path:
    """Записати ``markdown`` файлом ``filename`` у папку ``vault_dir``; вернути шлях.

    Безпека (вимога): пишемо ЛИШЕ в межах вибраної папки. Кінцевий шлях звіряємо
    ``paths.safe_under(vault_dir, target)`` — навіть якщо ``filename`` якось проніс
    роздільник чи «..», за межі папки ми не пишемо (ValueError). Колізію імені
    розвʼязує ``resolve_collision`` (оригінал недоторканий). Папка має існувати —
    інакше OSError спливе до викликача, який покаже підказку."""
    # Нестроковий/порожній шлях (битий конфіг) → ValueError, яку викликачі
    # ловлять разом з OSError — не TypeError повз їхній except (звірка №4 18.07)
    if not isinstance(vault_dir, (str, Path)) or not str(vault_dir).strip():
        raise ValueError(f"Папку Obsidian не вибрано або шлях некоректний: {vault_dir!r}")
    vault_dir = Path(vault_dir)
    target = resolve_collision(vault_dir, filename)
    if not paths.safe_under(vault_dir, target):
        raise ValueError(f"Шлях поза вибраною папкою: {target}")
    # newline="" — рядки Markdown уже з чистим LF; без цього Windows подвоїв би CR.
    with open(target, "w", encoding="utf-8", newline="") as f:
        f.write(markdown)
    return target


def open_uri(file_path) -> str:
    """obsidian://open?path=<абсолютний шлях, url-encoded> — відкрити файл у Obsidian.

    Схема ``path=`` не потребує знати назву сховища: Obsidian сам відкриє файл у
    тому сховищі, що його містить. Якщо Obsidian не встановлений — ОС не обробить
    схему, і викликач (контролер) робить graceful-fallback на відкриття папки."""
    p = Path(file_path).resolve()
    return "obsidian://open?path=" + urllib.parse.quote(str(p), safe="")
