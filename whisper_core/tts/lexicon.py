"""Словник наголосів і вимови (§6, Т58) — «почув → виправив → запамʼятав назавжди».

Per-профіль, ОКРЕМО від STT-словника самонавчання (Т36). Трирівнева архітектура
(розвідка 2026-07-23-RESEARCH-TTS-словник-наголосів):
  1. text_replace — заміна слова на інше написання ДО Stressifier/G2P (80% випадків);
  2. stress — U+0301-наголошений варіант ПІСЛЯ Stressifier, ДО ipa() (StyleTTS2);
  3. phonetic — готовий IPA ПЕРЕД акустичною моделлю (у сховищі/конвеєрі; НЕ в
     acceptance Хвилі 4 — прибрано з обіцянок, §6.4).

Жоден рушій не дає lexicon-API винятків (розвідка §1) → власний шар перехоплення.
Сховище — append-only pronunciation.jsonl + реконструкція активних (як self_learning).
Ревізія словника інвалідує WAV/timing-кеш (cache_key Хвилі 2 має lexicon_rev).

БЕЗ Qt. Span-map (Хвиля 2) лишається коректним після text_replace: розгорнуте/замінене
слово мапиться на raw-діапазон ОРИГІНАЛУ (критично для караоке)."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field

from .normalize import NormResult

# --- рівні виправлення / режими збігу ----------------------------------------
CORRECTION_TEXT_REPLACE = "text_replace"
CORRECTION_STRESS = "stress"
CORRECTION_PHONETIC = "phonetic"
_CORRECTION_TYPES = {CORRECTION_TEXT_REPLACE, CORRECTION_STRESS, CORRECTION_PHONETIC}

MATCH_WORD = "word"
MATCH_ANYWHERE = "anywhere"
MATCH_REGEX = "regex"
_MATCH_MODES = {MATCH_WORD, MATCH_ANYWHERE, MATCH_REGEX}

SCOPE_GLOBAL = "global"

_STRESS_MARK = "́"          # combining acute accent (стандарт lang-uk stress.txt)


class RuleError(ValueError):
    """Правило словника вимови невалідне (reason_key — i18n-ключ причини). НІКОЛИ
    не втрачаємо введене користувачем — лише відмовляємо в збереженні з поясненням."""

    def __init__(self, reason_key: str, detail: str = ""):
        super().__init__(detail or reason_key)
        self.reason_key = reason_key


@dataclass(frozen=True)
class PronRule:
    id: str
    match: str
    value: str
    correction_type: str = CORRECTION_TEXT_REPLACE
    match_mode: str = MATCH_WORD
    case_sensitive: bool = False
    scope: str = SCOPE_GLOBAL
    forms: tuple = field(default_factory=tuple)
    created_at: str = ""
    source: str = "manual"

    def to_dict(self) -> dict:
        return {"v": 1, "op": "learn", "id": self.id, "match": self.match,
                "value": self.value, "correction_type": self.correction_type,
                "match_mode": self.match_mode, "case_sensitive": self.case_sensitive,
                "scope": self.scope, "forms": list(self.forms),
                "created_at": self.created_at, "source": self.source}


# --- валідація правила ПЕРЕД збереженням (§6.1, §6.4) -------------------------

def validate_rule(match: str, value: str, *, match_mode: str = MATCH_WORD,
                  correction_type: str = CORRECTION_TEXT_REPLACE) -> None:
    """Кинути RuleError, якщо правило криве. Викликається ДО збереження — щоб криве
    правило не зʼїло введене мовчки (NVDA #11407) і не заморозило воркер (regex)."""
    if not (match or "").strip():
        raise RuleError("tts_pron_regex_bad", "порожнє слово")
    if not (value or "").strip():
        raise RuleError("tts_pron_regex_bad", "порожня вимова")
    if correction_type not in _CORRECTION_TYPES:
        raise RuleError("tts_pron_regex_bad", f"невідомий тип: {correction_type}")
    if match_mode not in _MATCH_MODES:
        raise RuleError("tts_pron_regex_bad", f"невідомий режим: {match_mode}")
    if match_mode == MATCH_REGEX:
        # v1: ВІЛЬНИЙ regex НЕ підтримується (суд хвилі 4 БЛОКЕР 3). Статичний детектор
        # ReDoS-повноти недосяжний: ловив nested-quantifier ((a+)+), але пропускав
        # ambiguous alternation ((a|aa)+, (a|a)*) — теж експоненційні. re тримає GIL,
        # тож runtime-timeout не перериває зависання воркера. word/anywhere покривають
        # 99% виправлень вимови. regex — лише пізніше з РЕАЛЬНИМ пісочним підпроцесом.
        raise RuleError("tts_pron_regex_bad",
                        "регулярні вирази поки не підтримуються")


# --- pymorphy3 відмінкові форми (з ПІДТВЕРДЖЕННЯМ, §6.4) ----------------------

def generate_forms(word: str) -> list:
    """Відмінкові форми слова через pymorphy3 (для «поширити на відмінки?»). pymorphy3
    опційний — без нього повертаємо лише базову форму (захисний fallback, §6.4). Форми
    показуються користувачу на ПІДТВЕРДЖЕННЯ, не авто-поширюються."""
    word = (word or "").strip()
    if not word:
        return []
    import importlib.util
    if importlib.util.find_spec("pymorphy3") is None:
        return [word]                            # лише база (pymorphy3 недоступний)
    try:
        morph = _get_morph()
        if morph is None:
            return [word]
        parsed = morph.parse(word)
        if not parsed:
            return [word]
        lexeme = parsed[0].lexeme
        seen = {}
        for p in lexeme:
            seen.setdefault(p.word, None)
        forms = list(seen.keys())
        # pymorphy3 повертає форми в нижньому регістрі; застосовуємо регістр ОРИГІНАЛУ
        # (§6.1 приклад капіталізований: «Коростень»→«Коростеня», не «коростеня»)
        if word[:1].isupper():
            forms = [f[:1].upper() + f[1:] if f else f for f in forms]
        return forms or [word]
    except Exception:                            # noqa: BLE001 — pymorphy може падати на власних назвах
        return [word]


_MORPH = None


def _get_morph():
    global _MORPH
    if _MORPH is None:
        try:
            import pymorphy3
        except ImportError:
            return None
        _MORPH = pymorphy3.MorphAnalyzer(lang="uk")
    return _MORPH


# --- конвеєр перехоплення (застосовується у воркері/адаптері) -----------------

class PronPipeline:
    """Побудований із активних правил профілю; передається воркеру в IPC
    (lexicon_snapshot) і застосовується перед/усередині синтезу."""

    def __init__(self, rules):
        self._rules = list(rules or [])
        # компільовані матчери для text_replace (стрес/фонетика застосовуються в адаптері)
        self._tr = [(r, self._compile(r)) for r in self._rules
                    if r.correction_type == CORRECTION_TEXT_REPLACE]
        self._stress = {self._key(r): r for r in self._rules
                        if r.correction_type == CORRECTION_STRESS}
        self._phonetic = {self._key(r): r for r in self._rules
                          if r.correction_type == CORRECTION_PHONETIC}

    @staticmethod
    def _key(r) -> str:
        return r.match if r.case_sensitive else r.match.lower()

    @staticmethod
    def _compile(r):
        flags = 0 if r.case_sensitive else re.IGNORECASE
        if r.match_mode == MATCH_REGEX:
            return None                          # v1: regex-правила НЕ застосовуємо
            #                                      (ReDoS; суд хвилі 4). from_ipc теж безпечний.
        pat = re.escape(r.match)
        if r.match_mode == MATCH_WORD:
            pat = r"(?<!\w)" + pat + r"(?!\w)"
        return re.compile(pat, flags)

    def is_empty(self) -> bool:
        return not self._rules

    def revision(self) -> str:
        """Хеш правил → lexicon_rev для cache_key (Хвиля 2). Зміна словника → інший
        ключ → старі WAV/timings не переграються."""
        payload = json.dumps([r.to_dict() for r in self._rules],
                             ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    # --- рівень 1: text_replace зі ЗБЕРЕЖЕННЯМ span-map --------------------
    def apply_text_replace(self, norm: NormResult) -> NormResult:
        """Замінити слова за text_replace-правилами у нормалізованому тексті,
        ЗБЕРІГАЮЧИ span-map: замінене слово мапиться на raw-діапазон ОРИГІНАЛУ
        (критично для караоке Хвилі 2). Без text_replace-правил — той самий NormResult."""
        if not self._tr:
            return norm
        text = norm.text
        # знайти всі непересічні збіги (перший-виграє за порядком правил)
        matches = []
        occupied = [False] * (len(text) + 1)
        for rule, pat in self._tr:
            if pat is None:
                continue
            for m in pat.finditer(text):
                s, e = m.start(), m.end()
                if s == e or any(occupied[s:e]):
                    continue
                for i in range(s, e):
                    occupied[i] = True
                matches.append((s, e, rule.value))
        if not matches:
            return norm
        matches.sort()
        new_parts, new_spans, out, idx = [], [], 0, 0
        for s, e, value in matches:
            if s > idx:                          # незмінений проміжок — копіюємо span-и
                out = _copy_spans(norm, idx, s, out, new_parts, new_spans, text)
            raw_s = _raw_start(norm, s)
            raw_e = _raw_end(norm, e)
            new_parts.append(value)
            new_spans.append((out, out + len(value), raw_s, raw_e))
            out += len(value)
            idx = e
        if idx < len(text):
            out = _copy_spans(norm, idx, len(text), out, new_parts, new_spans, text)
        return NormResult(text="".join(new_parts), spans=new_spans)

    # --- рівень 2: stress-override (для StyleTTS2 між Stressifier і ipa) ---
    def apply_stress(self, text: str) -> str:
        """Замінити наголошені форми зі словника (U+0301). Викликається в адаптері
        StyleTTS2 ПІСЛЯ Stressifier, ДО ipa(). Перекриває словникову неоднозначність
        ukrainian-word-stress (власного API винятків у lang-uk немає — розвідка §1а)."""
        if not self._stress:
            return text
        return _replace_words(text, self._stress)

    # --- рівень 3: phonetic-override (у сховищі/конвеєрі; НЕ в acceptance) --
    def apply_phonetic(self, ipa_text: str) -> str:
        if not self._phonetic:
            return ipa_text
        return _replace_words(ipa_text, self._phonetic)

    # --- IPC ---
    def to_ipc(self) -> list:
        return [r.to_dict() for r in self._rules]

    @classmethod
    def from_ipc(cls, data) -> "PronPipeline":
        rules = []
        for d in data or []:
            try:
                rules.append(PronRule(
                    id=str(d.get("id", "")), match=str(d.get("match", "")),
                    value=str(d.get("value", "")),
                    correction_type=str(d.get("correction_type", CORRECTION_TEXT_REPLACE)),
                    match_mode=str(d.get("match_mode", MATCH_WORD)),
                    case_sensitive=bool(d.get("case_sensitive", False)),
                    scope=str(d.get("scope", SCOPE_GLOBAL)),
                    forms=tuple(d.get("forms", ()))))
            except Exception:                    # noqa: BLE001
                continue
        return cls(rules)


def _replace_words(text: str, rules_by_key: dict) -> str:
    def _sub(m):
        w = m.group(0)
        r = rules_by_key.get(w) or rules_by_key.get(w.lower())
        return r.value if r is not None else w
    return re.sub(r"\w+", _sub, text)


# --- span-map композиція (identity-aware, як timings.normalized_word_raw_spans) ---

def _raw_start(nr: NormResult, out_pos: int) -> int:
    for os_, oe, rs, re_ in nr.spans:
        if os_ <= out_pos < oe:
            return rs + (out_pos - os_) if (oe - os_) == (re_ - rs) else rs
    return nr.spans[-1][3] if nr.spans else out_pos


def _raw_end(nr: NormResult, out_pos_excl: int) -> int:
    p = out_pos_excl - 1
    for os_, oe, rs, re_ in nr.spans:
        if os_ <= p < oe:
            return rs + (p - os_) + 1 if (oe - os_) == (re_ - rs) else re_
    return nr.spans[-1][3] if nr.spans else out_pos_excl


def _copy_spans(nr: NormResult, a: int, b: int, out_base: int,
                parts: list, spans: list, text: str) -> int:
    """Скопіювати незмінений проміжок [a,b) з ре-базуванням out-позицій; identity-
    span-и мапляться позиційно (per-char granularity збережена для караоке)."""
    parts.append(text[a:b])
    for os_, oe, rs, re_ in nr.spans:
        lo, hi = max(os_, a), min(oe, b)
        if lo >= hi:
            continue
        new_os = out_base + (lo - a)
        new_oe = out_base + (hi - a)
        if (oe - os_) == (re_ - rs):
            spans.append((new_os, new_oe, rs + (lo - os_), rs + (hi - os_)))
        else:
            spans.append((new_os, new_oe, rs, re_))
    return out_base + (b - a)


# --- сховище per-профіль (append-only + реконструкція, як self_learning) -----

@dataclass
class LearnResult:
    status: str                 # learned | updated | not_learned | failed
    rule: "PronRule | None" = None


def _journal_path(profile):
    from pathlib import Path
    return Path(getattr(profile, "dir", profile)) / "pronunciation.jsonl"


def _read_events(profile) -> list:
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
            continue
    return out


def _active_rules(events: list) -> list:
    active, order = {}, []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        op = ev.get("op")
        if op == "learn":
            eid = ev.get("id")
            if not eid:
                continue
            if eid not in active:
                order.append(eid)
            active[eid] = PronRule(
                id=eid, match=ev.get("match", ""), value=ev.get("value", ""),
                correction_type=ev.get("correction_type", CORRECTION_TEXT_REPLACE),
                match_mode=ev.get("match_mode", MATCH_WORD),
                case_sensitive=bool(ev.get("case_sensitive", False)),
                scope=ev.get("scope", SCOPE_GLOBAL),
                forms=tuple(ev.get("forms", ())),
                created_at=ev.get("created_at", ""), source=ev.get("source", "manual"))
        elif op == "revoke":
            active.pop(ev.get("id"), None)
    return [active[eid] for eid in order if eid in active]


def _append_event(profile, event: dict) -> None:
    path = _journal_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


def learn(profile, match: str, value: str, *, match_mode: str = MATCH_WORD,
          correction_type: str = CORRECTION_TEXT_REPLACE, case_sensitive: bool = False,
          scope: str = SCOPE_GLOBAL, forms=None, source: str = "manual") -> LearnResult:
    """Зберегти правило вимови (валідація ПЕРЕД записом; RuleError — не зберігаємо,
    введене не втрачаємо). UPSERT: правило з тим самим (match, correction_type, scope)
    ОНОВЛЮЄ значення на нове (status="updated"); нове слово → status="learned"."""
    validate_rule(match, value, match_mode=match_mode, correction_type=correction_type)
    existing = _active_rules(_read_events(profile))
    key = (match if case_sensitive else match.lower(), correction_type, scope)
    existing_rule = None
    for r in existing:
        if (r.match if r.case_sensitive else r.match.lower(),
                r.correction_type, r.scope) == key:
            existing_rule = r
            break
    # UPSERT (суд хвилі 4 БЛОКЕР 1): існуюче правило ОНОВЛЮЄМО значенням на нове —
    # той самий id, нова подія learn (append-only; реконструкція бере останню за id).
    # Раніше поверталось already_learned зі СТАРИМ значенням → застрягав назавжди.
    import secrets
    rid = existing_rule.id if existing_rule else secrets.token_hex(8)
    rule = PronRule(
        id=rid, match=match, value=value,
        correction_type=correction_type, match_mode=match_mode,
        case_sensitive=case_sensitive, scope=scope,
        forms=tuple(forms or ()), created_at=_now_iso(), source=source)
    _append_event(profile, rule.to_dict())
    return LearnResult("updated" if existing_rule else "learned", rule)


def revoke(profile, rule_id: str) -> LearnResult:
    active = {r.id: r for r in _active_rules(_read_events(profile))}
    if rule_id not in active:
        return LearnResult("not_learned")
    _append_event(profile, {"v": 1, "op": "revoke", "id": rule_id,
                            "created_at": _now_iso()})
    return LearnResult("learned", active[rule_id])


def list_rules(profile) -> list:
    return _active_rules(_read_events(profile))


def active_pipeline(profile, voice_id: "str | None" = None) -> PronPipeline:
    """Побудувати PronPipeline з активних правил профілю (scope global + voice:<id>)."""
    rules = _active_rules(_read_events(profile))
    scoped = [r for r in rules
              if r.scope == SCOPE_GLOBAL or r.scope == f"voice:{voice_id}"]
    return PronPipeline(scoped)
