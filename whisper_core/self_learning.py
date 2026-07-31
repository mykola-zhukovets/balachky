"""Самонавчання словника: «виправив раз — назавжди», ПІД КОЖЕН СЛОВНИК окремо.

Найчастіша скарга ринку — «виправив слово, а програма знову його калічить». Тут
одне виправлення користувача (з редактора історії / зворотного диктування чи з
діалогу «Виправити розпізнавання») САМЕ навчає АКТИВНИЙ профіль, без походу в
налаштування. Навчання строго пер-профільне: «вдома» і «робота» можуть дати різні
результати для того самого почутого — ізоляція гарантована теками профілів.

Модуль — ЧИСТЕ ЯДРО (без Qt). Він володіє:
  • витягом diff одного виправленого фрагмента (token-level, рівно один hunk);
  • класифікацією (bias / точна заміна терміна / пара-фраза / нічого) за
    консервативними правилами, які НЕ перетворюють нормальне слово на інше
    нормальне слово (захист «гартати» ≠ «гортати»);
  • захистом від засмічення (ідемпотентність, суперечність, перекриття, ліміти);
  • персистом: append-only журнал self-learning.jsonl + згенеровані проєкції
    terms.learned.toml / phrases.learned.toml (атомарно, під пер-профільним локом);
  • переліком/видаленням (undo через подію revoke).

Механіка навчання:
  | Виправлення                                   | Механізм         | Результат            |
  | одне слово, почуте може бути легітимним       | terms bias       | лише hotword-нудж    |
  | однозначний спецтокен (ворктрі → worktree)    | terms replace    | bias + точна заміна  |
  | 2+ слова або багатослівна ціль (пул реквест)  | пара-фраза       | точна заміна фрази   |
  | небезпечно/суперечливо/кілька правок          | —                | зберегти, не вчити   |

Проєкції підмішуються рушієм ТИМ САМИМ детермінованим проходом (terms.apply_glossary
+ hotwords/initial_prompt), що й людський словник; людські файли НЕ переписуються.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .history import history_lock

# --- ліміти захисту від засмічення (константи, не налаштування в MVP) ---
MAX_ACTIVE = 200            # активних вивчених правил на профіль
MAX_BIAS_CHARS = 2000       # Unicode-символів підмішаного bias-тексту на профіль
SRC_MAX_TOKENS = 4          # почуте: 1-4 лексичні токени
TGT_MAX_TOKENS = 6          # написане: 1-6 лексичних токенів

_ALLOWED_KINDS = ("term-bias", "term-replace", "phrase-replace")

# токен = послідовність літер/цифр/підкреслень/дефісів/апострофів (Unicode \w).
# Пунктуація й пробіли — роздільники (не творять межу слова для порівняння).
_TOKEN_RE = re.compile(r"[\w’'\-]+", re.UNICODE)
_LATIN_RE = re.compile(r"[A-Za-z]")
_DIGIT_RE = re.compile(r"[0-9]")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁёЇїІіЄєҐґ]")
# URL / шлях у зміненому фрагменті — не вчимо (це не слово)
_URL_PATH_RE = re.compile(r"https?://|www\.|[\\/]|\.[A-Za-z]{2,4}(?:$|[\\/])")

_UNSET = object()


# ─────────────────────────── типи результату ───────────────────────────
@dataclass
class LearnResult:
    """Підсумок спроби навчання — для тосту й undo у UI."""
    status: str                 # learned | already_learned | not_learned | failed
    reason: str = ""            # код причини (для i18n), коли не вчили
    kind: str = ""              # term-bias | term-replace | phrase-replace
    heard: str = ""
    write: str = ""
    entry_id: str = ""          # id події learn (для «Скасувати»)
    history_updated: bool = True  # виставляє контролер (чи оновлено запис історії)


@dataclass
class LearnedEntry:
    """Активне вивчене правило (реконструйоване з журналу)."""
    id: str
    kind: str
    heard: str
    write: str
    created_at: str = ""
    history_id: str = ""


@dataclass
class ProfileContext:
    """Знімок профілю для класифікації: що вже відоме цьому словнику."""
    canons: set = field(default_factory=set)
    variants: set = field(default_factory=set)
    ignores: set = field(default_factory=set)


# ─────────────────────────── нормалізація/токени ───────────────────────────
def norm(s: str) -> str:
    """NFC + curly-апостроф→', згортання внутрішніх пробілів, casefold. Для
    ПОРІВНЯННЯ й ключів; показуємо/зберігаємо завжди обрізане авторське написання."""
    s = unicodedata.normalize("NFC", s or "")
    s = s.replace("’", "'").replace("ʼ", "'").replace("`", "'")
    s = re.sub(r"\s+", " ", s).strip()
    return s.casefold()


def _tokens(s: str):
    """[(текст, start, end)] лексичних токенів; пунктуація/пробіли — роздільники."""
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(s or "")]


def _token_texts(s: str):
    return [t[0] for t in _tokens(s)]


# ─────────────────────────── diff одного виправлення ───────────────────────────
def diff_correction(before: str, after: str):
    """Token-level diff показаного ДО-тексту й відредагованого. Приймаємо РІВНО
    один непорожній replace-hunk (1-4 почутих токени → 1-6 написаних). Повертає
    (heard, write, "") на успіх або (None, None, код-причини):
      identical    — нема лексичної зміни / та сама нормалізована форма;
      multi_hunk   — 2+ змінених фрагменти;
      not_replace  — чиста вставка/видалення (не заміна);
      newline      — у зміненому фрагменті перенос рядка;
      url_or_path  — URL/шлях у фрагменті;
      too_long     — фрагмент перевищує ліміт токенів."""
    before, after = before or "", after or ""
    bt, at = _tokens(before), _tokens(after)
    bn = [norm(t[0]) for t in bt]
    an = [norm(t[0]) for t in at]
    sm = difflib.SequenceMatcher(a=bn, b=an, autojunk=False)
    changed = [op for op in sm.get_opcodes() if op[0] != "equal"]
    if not changed:
        return None, None, "identical"
    if len(changed) > 1:
        return None, None, "multi_hunk"
    tag, i1, i2, j1, j2 = changed[0]
    if tag != "replace":
        return None, None, "not_replace"
    heard_toks, write_toks = bt[i1:i2], at[j1:j2]
    if not heard_toks or not write_toks:
        return None, None, "not_replace"
    heard_span = before[heard_toks[0][1]:heard_toks[-1][2]]
    write_span = after[write_toks[0][1]:write_toks[-1][2]]
    if "\n" in heard_span or "\n" in write_span:
        return None, None, "newline"
    if _URL_PATH_RE.search(heard_span) or _URL_PATH_RE.search(write_span):
        return None, None, "url_or_path"
    if len(heard_toks) > SRC_MAX_TOKENS or len(write_toks) > TGT_MAX_TOKENS:
        return None, None, "too_long"
    heard = " ".join(t[0] for t in heard_toks).strip()
    write = " ".join(t[0] for t in write_toks).strip()
    if norm(heard) == norm(write):
        return None, None, "identical"
    return heard, write, ""


# ─────────────────────────── класифікація ───────────────────────────
def is_term_like(text: str, ctx: "ProfileContext | None" = None) -> bool:
    """«Термін-подібне» написане: латиниця/цифри/_/-, змішані скрипти, все-капс
    токен, або вже наявний канон профілю. Лише такі цілі отримують hotword-bias
    за замовчуванням — щоб звичайна проза не роздувала prompt."""
    t = text or ""
    if _LATIN_RE.search(t) or _DIGIT_RE.search(t) or "_" in t or "-" in t:
        return True
    if _LATIN_RE.search(t) and _CYRILLIC_RE.search(t):
        return True
    for tok in _token_texts(t):
        if len(tok) >= 2 and tok == tok.upper() and tok.upper() != tok.lower():
            return True
    if ctx is not None and norm(t) in ctx.canons:
        return True
    return False


def is_possibly_legit(heard: str, ctx: "ProfileContext | None",
                      lexicon: "set | None") -> bool:
    """Чи почуте може бути ЛЕГІТИМНИМ окремим словом (яке не можна нищити глобальною
    заміною). True, якщо воно є в канонах/варіантах/ігнорі профілю або в частотному
    лексиконі української. Лексикон недоступний → КОНСЕРВАТИВНО True (не ставимо
    ризикової заміни). Саме це тримає «гартати» недоторканим."""
    nh = norm(heard)
    if ctx is not None and (nh in ctx.canons or nh in ctx.variants or nh in ctx.ignores):
        return True
    if lexicon is None:
        return True
    return nh in lexicon


def classify(heard: str, write: str, ctx: "ProfileContext | None" = None,
             lexicon: "set | None" = None):
    """Обрати тип правила за diff-фрагментом. Повертає (rule|None, reason), де
    rule = {"kind","heard","write"}. reason (коли rule=None): identical | ordinary_word."""
    nh, nw = norm(heard), norm(write)
    if not nh or not nw or nh == nw:
        return None, "identical"
    src_multi = len(_token_texts(heard)) > 1
    tgt_multi = len(_token_texts(write)) > 1
    term_like = is_term_like(write, ctx)

    # 2+ токени в почутому АБО багатослівна ціль → пара-фраза (точна заміна цілої
    # послідовності токенів; не може змінити слово всередині іншого слова).
    if src_multi or tgt_multi:
        return {"kind": "phrase-replace", "heard": heard.strip(),
                "write": write.strip()}, ""

    # одне слово → одне слово
    if is_possibly_legit(heard, ctx, lexicon):
        # почуте може бути справжнім словом → НІКОЛИ не ставимо заміну.
        if term_like:
            return {"kind": "term-bias", "heard": heard.strip(),
                    "write": write.strip()}, ""
        return None, "ordinary_word"     # нормальне слово → нормальне слово: без правила
    # почуте НЕ схоже на легітимне слово
    if term_like:
        return {"kind": "term-replace", "heard": heard.strip(),
                "write": write.strip()}, ""
    return None, "ordinary_word"


# ─────────────────────────── шляхи профілю ───────────────────────────
def _journal_path(profile) -> Path:
    return _dir(profile) / "self-learning.jsonl"


def _learned_terms_path(profile) -> Path:
    return _dir(profile) / "terms.learned.toml"


def _learned_phrases_path(profile) -> Path:
    return _dir(profile) / "phrases.learned.toml"


def _dir(profile) -> Path:
    return Path(getattr(profile, "dir", profile))


# ─────────────────────────── журнал (read/append) ───────────────────────────
def _read_events_unlocked(profile) -> list:
    path = _journal_path(profile)
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # битий фінальний рядок (обірваний запис) — пропускаємо, як історія
            continue
    return out


def _active_from_events(events: list) -> list:
    """Реконструкція активних правил: learn додає, revoke прибирає (за id).
    Порядок — за першою появою learn (стабільно)."""
    active = {}
    order = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        op = ev.get("op")
        if op == "learn":
            eid = ev.get("id")
            kind = ev.get("kind")
            if not eid or kind not in _ALLOWED_KINDS:
                continue
            if eid not in active:
                order.append(eid)
            active[eid] = LearnedEntry(
                id=eid, kind=kind, heard=ev.get("heard", ""),
                write=ev.get("write", ""), created_at=ev.get("created_at", ""),
                history_id=ev.get("history_id", ""))
        elif op == "revoke":
            eid = ev.get("id")
            if eid in active:
                del active[eid]
    return [active[eid] for eid in order if eid in active]


def _append_event_unlocked(profile, event: dict) -> None:
    path = _journal_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _lock(profile):
    return history_lock(_journal_path(profile))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─────────────────────────── проєкції (toml) ───────────────────────────
def _projection_data(entries: list):
    """(terms_data, phrases_data): {канон: [варіанти]} для двох проєкцій."""
    terms_data: dict = {}
    phrases_data: dict = {}
    for e in entries:
        if e.kind == "term-bias":
            terms_data.setdefault(e.write, [])
        elif e.kind == "term-replace":
            vs = terms_data.setdefault(e.write, [])
            if e.heard.lower() not in [x.lower() for x in vs]:
                vs.append(e.heard)
        elif e.kind == "phrase-replace":
            vs = phrases_data.setdefault(e.write, [])
            if e.heard.lower() not in [x.lower() for x in vs]:
                vs.append(e.heard)
    return terms_data, phrases_data


def _write_toml_atomic(path: Path, table: str, data: dict) -> None:
    """Атомарний перезапис проєкції (fsync + os.replace). Порожньо → файл прибираємо,
    щоб read_terms_dict/read_learned_phrases нічого не підмішували."""
    if not data:
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        return
    lines = ["# Згенеровано автоматично (самонавчання словника). НЕ редагувати вручну —",
             "# джерело істини self-learning.jsonl; правити правила у вкладці «Словники».",
             "", f"[{table}]"]
    for canon, variants in sorted(data.items()):
        key = canon if re.fullmatch(r"[A-Za-z0-9_\-]+", canon) \
            else '"' + canon.replace('"', '\\"') + '"'
        arr = ", ".join('"' + v.replace('"', '\\"') + '"' for v in variants)
        lines.append(f"{key} = [{arr}]")
    text = "\n".join(lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _rebuild_projections_unlocked(profile, entries: list) -> None:
    terms_data, phrases_data = _projection_data(entries)
    _write_toml_atomic(_learned_terms_path(profile), "terms", terms_data)
    _write_toml_atomic(_learned_phrases_path(profile), "phrases", phrases_data)


def _is_stale(profile) -> bool:
    """Проєкції відстали від журналу (без лока — лише stat)."""
    journal = _journal_path(profile)
    try:
        if not journal.exists():
            return False
        jm = journal.stat().st_mtime
    except OSError:
        return False
    lt, lp = _learned_terms_path(profile), _learned_phrases_path(profile)
    # який стан журналу має бути в проєкціях
    terms_data, phrases_data = _projection_data(_active_from_events(
        _read_events_unlocked(profile)))
    for path, data in ((lt, terms_data), (lp, phrases_data)):
        exists = path.exists()
        if data and not exists:
            return True
        if not data and exists:
            return True
        if exists:
            try:
                if path.stat().st_mtime < jm:
                    return True
            except OSError:
                return True
    return False


def ensure_projections(profile) -> None:
    """Перебудувати проєкції, якщо вони відстали/зникли/побилися. Кличеться на
    старті та при завантаженні словника профілю (дешево: спершу stat без лока)."""
    if not _is_stale(profile):
        return
    with _lock(profile):
        if not _is_stale(profile):
            return
        _rebuild_projections_unlocked(
            profile, _active_from_events(_read_events_unlocked(profile)))


# ─────────────────────────── контекст профілю ───────────────────────────
def load_context(profile) -> ProfileContext:
    """Знімок профілю: канони/варіанти (людський словник + auto + вивчене) і
    ігнор-слова. Для класифікації (term-like / possibly-legit)."""
    from . import terms as terms_mod, phrasebook
    tdict = terms_mod.read_terms_dict(profile.terms_path)   # включає terms.learned.toml
    canons = {norm(c) for c in tdict}
    variants = {norm(v) for vs in tdict.values() for v in vs}
    pdict = phrasebook.read_phrases(profile.phrases_path)
    canons |= {norm(c) for c in pdict}
    variants |= {norm(v) for vs in pdict.values() for v in vs}
    try:
        ignores = {norm(w) for w in profile.ignored_words()}
    except Exception:
        ignores = set()
    return ProfileContext(canons=canons, variants=variants, ignores=ignores)


def _existing_heard_map(profile, entries: list) -> dict:
    """{нормалізоване_почуте: нормалізоване_написане} з усіх ДЕТЕРМІНОВАНИХ замін
    профілю: вивчені replace + людський словник (варіант→канон) + ручні фрази.
    Для перевірки суперечності/дубля. Bias сюди не входить (він не творить заміни)."""
    from . import terms as terms_mod, phrasebook
    m: dict = {}
    for e in entries:
        if e.kind in ("term-replace", "phrase-replace"):
            m[norm(e.heard)] = norm(e.write)
    for canon, variants in terms_mod.read_terms_dict(profile.terms_path).items():
        for v in variants:
            m.setdefault(norm(v), norm(canon))
    for canon, variants in phrasebook.read_phrases(profile.phrases_path).items():
        for v in variants:
            m.setdefault(norm(v), norm(canon))
    return m


def _is_subsequence(a: list, b: list) -> bool:
    """a — суцільна ціло-токенна підпослідовність b (або дорівнює b)."""
    if not a or len(a) > len(b):
        return False
    for i in range(len(b) - len(a) + 1):
        if b[i:i + len(a)] == a:
            return True
    return False


def _injected_bias_chars(entries: list) -> int:
    """Приблизний обсяг підмішаного bias-тексту (канони + варіанти проєкцій)."""
    terms_data, phrases_data = _projection_data(entries)
    total = 0
    for data in (terms_data, phrases_data):
        for canon, variants in data.items():
            total += len(canon) + sum(len(v) for v in variants)
    return total


def _conflict(profile, rule: dict, entries: list, ctx: ProfileContext):
    """Захист від засмічення. Повертає код-причини або None (можна вчити).
    already_learned — таке правило вже діє (не дублюємо)."""
    kind = rule["kind"]
    nh, nw = norm(rule["heard"]), norm(rule["write"])

    # ідемпотентність: ідентичне активне правило
    for e in entries:
        if e.kind == kind and norm(e.heard) == nh and norm(e.write) == nw:
            return "already_learned"

    # bias уже наявного канону — надлишковий
    if kind == "term-bias" and nw in ctx.canons:
        return "already_learned"

    # ліміт кількості
    if len(entries) >= MAX_ACTIVE:
        return "at_cap"

    # бюджет prompt-bias
    if _injected_bias_chars(entries + [LearnedEntry(
            id="", kind=kind, heard=rule["heard"], write=rule["write"])]) > MAX_BIAS_CHARS:
        return "at_cap"

    # ПРЯМА СУПЕРЕЧНІСТЬ — для БУДЬ-ЯКОГО типу: почуте вже мапиться на інше написане
    # (у вивчених замінах, людському словнику, auto чи ручних фразах). Перевірка не
    # залежить від того, чи вийшов bias, чи replace: сам факт «те саме почуте → інша
    # ціль» суперечливий. Інакше після вивчення «ворктрі→worktree» друге
    # «ворктрі→WorkSpace» тихо стало б нешкідливим bias і сховало б конфлікт.
    existing = _existing_heard_map(profile, entries)
    other = existing.get(nh)
    if other is not None:
        if other == nw:
            return "already_learned"           # та сама заміна вже є (напр. у словнику)
        return "contradiction"

    # ПЕРЕКРИТТЯ (для замін): почуте як ціло-токенна послідовність збігається,
    # містить або міститься в наявній заміні з ІНШИМ виходом → неоднозначно.
    if kind in ("term-replace", "phrase-replace"):
        nh_toks = nh.split()
        for k, v in existing.items():
            if v == nw:
                continue                       # той самий вихід — безпечно
            k_toks = k.split()
            if _is_subsequence(nh_toks, k_toks) or _is_subsequence(k_toks, nh_toks):
                return "overlap"
    return None


# ─────────────────────────── головна оркестрація ───────────────────────────
_lexicon_cache = _UNSET


def _uk_lexicon():
    """Частотний лексикон української (той, що качає автокорекція). Немає файлу
    → None (консервативний режим). Кешуємо: один раз на процес."""
    global _lexicon_cache
    if _lexicon_cache is _UNSET:
        _lexicon_cache = _load_lexicon()
    return _lexicon_cache


def _load_lexicon():
    from . import paths
    try:
        p = paths.autocorrect_dict_path()
        if not p.is_file() or p.stat().st_size == 0:
            return None
        words = set()
        with p.open(encoding="utf-8") as f:
            for line in f:
                w = line.split(" ", 1)[0].strip()
                if w:
                    words.add(w.casefold())
        return words or None
    except OSError:
        return None


def learn_from_correction(profile, before: str, after: str, *, history_id: str = "",
                          source: str = "history", lexicon=_UNSET) -> LearnResult:
    """Вивести й зберегти БЕЗПЕЧНЕ правило з одного виправлення (before→after) у
    ЦЕЙ профіль. lexicon=_UNSET → авто-лексикон; передай set/None у тестах."""
    heard, write, reason = diff_correction(before, after)
    if heard is None:
        return LearnResult(status="not_learned", reason=reason)
    ctx = load_context(profile)
    lex = _uk_lexicon() if lexicon is _UNSET else lexicon
    rule, creason = classify(heard, write, ctx, lex)
    if rule is None:
        return LearnResult(status="not_learned", reason=creason, heard=heard, write=write)

    try:
        with _lock(profile):
            entries = _active_from_events(_read_events_unlocked(profile))
            conflict = _conflict(profile, rule, entries, ctx)
            if conflict == "already_learned":
                return LearnResult(status="already_learned", reason="already_learned",
                                   kind=rule["kind"], heard=rule["heard"], write=rule["write"])
            if conflict:
                return LearnResult(status="not_learned", reason=conflict,
                                   kind=rule["kind"], heard=rule["heard"], write=rule["write"])
            entry_id = uuid.uuid4().hex
            _append_event_unlocked(profile, {
                "v": 1, "op": "learn", "id": entry_id, "kind": rule["kind"],
                "heard": rule["heard"], "write": rule["write"],
                "created_at": _now_iso(), "history_id": history_id or "",
                "source": source})
            try:
                _rebuild_projections_unlocked(
                    profile, _active_from_events(_read_events_unlocked(profile)))
            except OSError:
                # проєкція не вдалась: правило в журналі є, але словник не оновлено —
                # чесно звітуємо failed, виправлення (у контролері) лишається збереженим
                return LearnResult(status="failed", reason="projection_failed",
                                   kind=rule["kind"], heard=rule["heard"],
                                   write=rule["write"], entry_id=entry_id)
    except OSError:
        return LearnResult(status="failed", reason="journal_failed",
                           kind=rule["kind"], heard=rule["heard"], write=rule["write"])
    return LearnResult(status="learned", kind=rule["kind"],
                       heard=rule["heard"], write=rule["write"], entry_id=entry_id)


def revoke(profile, entry_id: str) -> bool:
    """Скасувати вивчене правило (undo / «Прибрати»): дописати revoke, перебудувати
    проєкції. → True, якщо правило було активним."""
    if not entry_id:
        return False
    with _lock(profile):
        active = _active_from_events(_read_events_unlocked(profile))
        if entry_id not in {e.id for e in active}:
            return False
        _append_event_unlocked(profile, {"v": 1, "op": "revoke", "id": entry_id,
                                          "created_at": _now_iso()})
        _rebuild_projections_unlocked(
            profile, _active_from_events(_read_events_unlocked(profile)))
    return True


def list_learned(profile) -> list:
    """Активні вивчені правила, найновіші першими (для менеджера у «Словниках»)."""
    with _lock(profile):
        entries = _active_from_events(_read_events_unlocked(profile))
    return list(reversed(entries))


def relearn(profile, kind: str, heard: str, write: str) -> str:
    """Повторно додати правило (undo видалення у менеджері). Обходить перевірки
    конфліктів — відновлюємо рівно те, що щойно прибрали. → новий id."""
    if kind not in _ALLOWED_KINDS:
        return ""
    with _lock(profile):
        entry_id = uuid.uuid4().hex
        _append_event_unlocked(profile, {
            "v": 1, "op": "learn", "id": entry_id, "kind": kind,
            "heard": heard, "write": write, "created_at": _now_iso(),
            "history_id": "", "source": "undo-remove"})
        _rebuild_projections_unlocked(
            profile, _active_from_events(_read_events_unlocked(profile)))
    return entry_id


def validate_and_rebuild(profile) -> dict:
    """Імпорт профілю: перевірити схему журналу, відкинути невалідні/суперечливі
    події, перебудувати проєкції. Повертає {"kept": N, "dropped": M}. Ніколи не
    зливає в чужий профіль — працює лише в межах цього каталогу."""
    with _lock(profile):
        raw = _read_events_unlocked(profile)
        kept_events = []
        seen_replace: dict = {}
        active_ids: set = set()
        kept = dropped = 0
        for ev in raw:
            if not isinstance(ev, dict) or ev.get("v") != 1:
                dropped += 1
                continue
            op = ev.get("op")
            if op == "revoke":
                if ev.get("id") in active_ids:
                    kept_events.append(ev)
                    active_ids.discard(ev.get("id"))
                    kept += 1
                else:
                    dropped += 1
                continue
            if op != "learn":
                dropped += 1
                continue
            eid, kind = ev.get("id"), ev.get("kind")
            nh, nw = norm(ev.get("heard", "")), norm(ev.get("write", ""))
            if not eid or kind not in _ALLOWED_KINDS or not nh or not nw or nh == nw:
                dropped += 1
                continue
            if kind in ("term-replace", "phrase-replace"):
                other = seen_replace.get(nh)
                if other is not None and other != nw:
                    dropped += 1                    # суперечлива імпортована пара
                    continue
                seen_replace[nh] = nw
            kept_events.append(ev)
            active_ids.add(eid)
            kept += 1
        # переписати журнал лише валідними подіями + перебудувати проєкції
        journal = _journal_path(profile)
        if kept_events:
            text = "\n".join(json.dumps(e, ensure_ascii=False) for e in kept_events) + "\n"
            tmp = journal.with_name(journal.name + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, journal)
        else:
            journal.unlink(missing_ok=True)
        _rebuild_projections_unlocked(
            profile, _active_from_events(_read_events_unlocked(profile)))
    return {"kept": kept, "dropped": dropped}
