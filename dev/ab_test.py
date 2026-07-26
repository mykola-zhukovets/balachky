"""A/B-раннер точності: жене зібраний корпус через 2+ моделі й рахує WER/CER
проти виправленого людиною тексту. DEV-скрипт (не в UI, не в застосунку).

Метрики — прості, без важких залежностей: власна нормалізація (регістр/пунктуація/
пробіли) + відстань Левенштейна на словах (WER) і на символах (CER). Ці функції
імпортуються юнітами (tests/test_corpus.py), тож модуль тримаємо import-safe:
рушій Whisper вантажиться ЛИШЕ у run() (lazy), а не на верхньому рівні.

Запуск:
    python -m dev.ab_test --models large-v3 turbo --out ab_report.md
Без --models береться пара за замовчуванням (DEFAULT_MODELS). Звіт — markdown
у --out (типово ab_report.md у корені репо).
"""
from __future__ import annotations

import argparse
import re
import unicodedata
from pathlib import Path

# Дефолтна пара моделей для порівняння (передумова вибору: large-v3 vs turbo).
DEFAULT_MODELS = ["large-v3", "large-v3-turbo"]

# Пунктуація, яку нормалізація прибирає (лишаємо букви, цифри, пробіли й апостроф
# всередині слова знімаємо теж — «м'яч»→«мяч», щоб одрук апострофа не карався).
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Знизити регістр, прибрати пунктуацію, злити пробіли. Стійка база для
    порівняння: різниця лише в комах/крапках/регістрі не має роздувати WER."""
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("’", "").replace("'", "")   # апостроф не рахуємо
    text = _PUNCT_RE.sub(" ", text.lower())
    return _WS_RE.sub(" ", text).strip()


def _levenshtein(a: list, b: list) -> int:
    """Відстань редагування між послідовностями (класичний DP, O(len(a)*len(b))).
    Працює і для списку слів, і для списку символів."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1,        # видалення
                           cur[j - 1] + 1,     # вставка
                           prev[j - 1] + cost))  # заміна/збіг
        prev = cur
    return prev[-1]


def wer(reference: str, hypothesis: str) -> float:
    """Word Error Rate: Левенштейн на словах / кількість слів еталона.
    Порожній еталон → 0.0 якщо гіпотеза теж порожня, інакше 1.0."""
    ref = normalize(reference).split()
    hyp = normalize(hypothesis).split()
    if not ref:
        return 0.0 if not hyp else 1.0
    return _levenshtein(ref, hyp) / len(ref)


def cer(reference: str, hypothesis: str) -> float:
    """Character Error Rate: Левенштейн на символах / довжина еталона (пробіли
    прибираємо, щоб CER не залежав від їх кількості)."""
    ref = normalize(reference).replace(" ", "")
    hyp = normalize(hypothesis).replace(" ", "")
    if not ref:
        return 0.0 if not hyp else 1.0
    return _levenshtein(list(ref), list(hyp)) / len(ref)


def _transcribe(model_name: str, wav_path: Path) -> str:
    """Розшифрувати один WAV заданою моделлю (свіжий рушій на модель). Lazy-
    імпорти всередині: модуль лишається import-safe для юнітів метрик."""
    from copy import copy as _copy

    from whisper_core.config import Config
    from whisper_core.engine import Engine

    cfg = Config.load()
    cfg = _copy(cfg)
    cfg.model_name = model_name
    engine = Engine(cfg)
    _raw, final, _dur, _words, _segs = engine.transcribe(str(wav_path), None)[:5]
    return final or ""


def run(models=None, out_path=None) -> Path:
    """Прогнати корпус через кожну модель, порахувати середні WER/CER, записати
    markdown-звіт. Повертає шлях до звіту. Зразки без WAV чесно пропускаються."""
    from whisper_core import corpus

    models = models or DEFAULT_MODELS
    out_path = Path(out_path) if out_path else Path("ab_report.md")

    samples = [s for s in corpus.load_samples()
               if s.get("wav_path") and Path(s["wav_path"]).is_file()]
    per_model = {m: {"wer": [], "cer": []} for m in models}
    rows = []   # (recognized, corrected, {model: (wer, cer, hyp)})

    for s in samples:
        ref = s.get("corrected", "")
        cell = {}
        for m in models:
            try:
                hyp = _transcribe(m, Path(s["wav_path"]))
            except Exception as e:                       # noqa: BLE001
                print(f"[!] {m} впала на {s['wav']}: {e}")
                continue
            w, c = wer(ref, hyp), cer(ref, hyp)
            per_model[m]["wer"].append(w)
            per_model[m]["cer"].append(c)
            cell[m] = (w, c, hyp)
        rows.append((s.get("recognized", ""), ref, cell))

    _write_report(out_path, models, samples, per_model, rows)
    return out_path


def _avg(xs) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _write_report(out_path: Path, models, samples, per_model, rows) -> None:
    lines = ["# A/B точності розпізнавання", ""]
    lines.append(f"Зразків із аудіо: **{len(samples)}**  ·  моделей: "
                 f"**{len(models)}**")
    lines.append("")
    lines.append("## Підсумок (менше — краще)")
    lines.append("")
    lines.append("| Модель | середній WER | середній CER |")
    lines.append("|---|---|---|")
    for m in models:
        lines.append(f"| {m} | {_avg(per_model[m]['wer']):.3f} | "
                     f"{_avg(per_model[m]['cer']):.3f} |")
    lines.append("")
    lines.append("## По зразках")
    lines.append("")
    for i, (recognized, ref, cell) in enumerate(rows, 1):
        lines.append(f"### Зразок {i}")
        lines.append(f"- Виправлено (еталон): `{ref}`")
        for m in models:
            if m in cell:
                w, c, hyp = cell[m]
                lines.append(f"- {m}: WER={w:.3f} CER={c:.3f} → `{hyp}`")
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Звіт: {out_path.resolve()}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="A/B точності по зібраному корпусу")
    ap.add_argument("--models", nargs="+", default=None,
                    help="назви моделей (напр. large-v3 large-v3-turbo)")
    ap.add_argument("--out", default="ab_report.md", help="файл markdown-звіту")
    args = ap.parse_args(argv)
    run(models=args.models, out_path=args.out)


if __name__ == "__main__":
    main()
