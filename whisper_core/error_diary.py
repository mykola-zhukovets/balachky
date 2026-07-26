"""Мовний щоденник помилок: агрегатор повторюваних виправлень над корпусом.

Поверх whisper_core.corpus (пари «розпізнано → виправлено») виводить, які саме
виправлення користувач робить раз за разом: «було X → стало Y, N разів». Це
підказка, що варто додати правило у словник профілю, щоб рушій більше не помилявся.

Приватність — канон: усе локально, читаємо лише manifest.jsonl корпусу, нікуди
нічого не відправляємо. Модуль — ЧИСТЕ ЯДРО: без Qt, лише робота зі списком
зразків, які дає corpus.load_samples.
"""
from __future__ import annotations

from . import corpus


def aggregate(root=None, *, samples=None, profile=None) -> list:
    """Згрупувати повторювані виправлення корпусу.

    Повертає список dict-ів {"was": X, "now": Y, "count": N}, відсортований за
    спаданням частоти (N), далі за абеткою «було». Групування — регістро-
    незалежне (нормалізація casefold), тож «Свято» і «свято» — той самий запис;
    у відображенні лишаємо форму першого-побаченого зразка.

    Зразки, де розпізнане й виправлене збігаються (виправлення не було) або де
    один із текстів порожній, пропускаємо — правило з них не виведеш.

    profile — якщо задано (не None), агрегуємо ЛИШЕ зразки цього словника, щоб
    щоденник помилок не показував (і не пропонував у клік) чужу пару в іншому
    словнику (feature/selflearn-dict, спека «Poisoning… scale protection»).

    samples — для тестів: якщо передано, читаємо з нього, а не з диска; фільтр за
    профілем застосовується й до переданих зразків.
    """
    if samples is None:
        samples = corpus.load_samples(root, profile=profile)
    elif profile is not None:
        samples = [s for s in samples if (s.get("profile") or "") == profile]

    order = []            # ключі в порядку першої появи (стабільне сортування)
    agg = {}              # ключ → {"was", "now", "count"}
    for rec in samples:
        was = (rec.get("recognized") or "").strip()
        now = (rec.get("corrected") or "").strip()
        if not was or not now or was.casefold() == now.casefold():
            continue
        key = (was.casefold(), now.casefold())
        entry = agg.get(key)
        if entry is None:
            agg[key] = {"was": was, "now": now, "count": 1}
            order.append(key)
        else:
            entry["count"] += 1

    rows = [agg[k] for k in order]
    rows.sort(key=lambda e: (-e["count"], e["was"].casefold()))
    return rows


def top_suggestions(root=None, *, samples=None, n: int = 5,
                    min_count: int = 2, profile=None) -> list:
    """Найчастіші виправлення-кандидати в правило словника.

    Лишаємо тільки ті, що повторились щонайменше min_count разів (одноразове
    виправлення — це радше опечатка, а не системна помилка рушія), і віддаємо
    перші n. Порядок — той самий, що в aggregate (за спаданням частоти).

    profile — пробрасуємо в aggregate: підказки лишаються в межах свого словника.
    """
    rows = aggregate(root, samples=samples, profile=profile)
    picked = [r for r in rows if r["count"] >= min_count]
    return picked[:n]
