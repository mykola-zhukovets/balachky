"""learn.py — pattern-mining пам'яті Балачок.

Аналізує історію активного (чи вказаного) профілю і пропонує нові терміни:
часті кириличні токени, яких нема ні у словнику, ні у стоп-списку звичайної
лексики, ні у твоєму персональному ignore-списку.

    python learn.py                          # кандидати активного профілю
    python learn.py --profile kolega         # інший профіль
    python learn.py --min 3                  # поріг частоти (типово 2)
    python learn.py --ignore слово інше      # більше не пропонувати ці слова

Ти підтверджуєш кожен термін сам: система вчиться, контроль лишається за тобою.
"""
import json
import re
import sys
import tomllib
from collections import Counter
from pathlib import Path

from whisper_core import profiles
from whisper_core.uk_stopwords import UK_STOPWORDS

MIN_COUNT = 2    # скільки разів має зустрітись токен, щоб стати кандидатом
TOP_N = 30
# кириличний токен від 3 літер, апостроф всередині слова дозволений
_TOKEN_RE = re.compile(r"[а-щьюяіїєґ][а-щьюяіїєґ']{2,}")


def load_known_variants(terms_path) -> set:
    """Усе, що вже у словнику — людський terms.toml + машинний terms.auto.toml."""
    from whisper_core.terms import read_terms_dict
    known = set()
    for canon, variants in read_terms_dict(terms_path).items():
        known.add(canon.lower())
        known.update(v.lower() for v in variants)
    return known


def analyze(history_path, known=None, stopwords=UK_STOPWORDS, min_count=MIN_COUNT):
    """→ [(токен, частота), ...] спадно: часті кириличні токени поза відомим."""
    known = known or set()
    history_path = Path(history_path)
    if not history_path.exists():
        return []
    counter = Counter()
    for line in history_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        for tok in _TOKEN_RE.findall((rec.get("raw") or "").lower()):
            tok = tok.strip("'")
            if tok not in stopwords and tok not in known:
                counter[tok] += 1
    return [(t, c) for t, c in counter.most_common() if c >= min_count]


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])

    prof = None
    if "--profile" in argv:
        i = argv.index("--profile")
        prof = profiles.get(name=argv[i + 1]) if i + 1 < len(argv) else None
        if prof is None:
            print("Нема такого профілю. Список: python -m whisper_core.profiles list")
            return 1
        del argv[i:i + 2]
    prof = prof or profiles.get_active()

    if "--ignore" in argv:
        words = argv[argv.index("--ignore") + 1:]
        if not words:
            print("Вжиток: python learn.py --ignore <слово> [ще слова]")
            return 1
        prof.add_ignored(words)
        print(f"Додано в ignore профілю «{prof.name}»: {', '.join(words)}")
        return 0

    min_count = MIN_COUNT
    if "--min" in argv:
        i = argv.index("--min")
        try:
            min_count = int(argv[i + 1])
        except (IndexError, ValueError):
            print("Вжиток: python learn.py --min <число>")
            return 1

    known = load_known_variants(prof.terms_path) | prof.ignored_words()
    candidates = analyze(prof.history_path, known=known, min_count=min_count)
    if not candidates:
        print(f"Кандидатів немає (профіль «{prof.name}») — замало історії, "
              f"або все вже у словнику/ignore.")
        return 0

    print(f"Кандидати в терміни (профіль «{prof.name}», ≥{min_count}×):\n")
    for tok, cnt in candidates[:TOP_N]:
        print(f"  «{tok}»  ×{cnt}")
    print(f"\nДодати термін:   у {prof.terms_path}")
    print('                 ПравильнаФорма = ["варіант зі списку"]')
    print("Прибрати шум:    python learn.py --ignore <слово>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
