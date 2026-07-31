"""E3: транскрипт наради → структурований протокол Markdown українською.

Вхід — репліки з transcript.json (Utterance: таймкоди + мітки мовців Я/Співрозмовники/
speaker_N). Вихід — Markdown із чотирьох секцій:
  ## Підсумок            — абзац
  ## Рішення             — список
  ## Задачі              — таблиця Хто | Що | Термін | Час у записі (таймкод сегмента-джерела)
  ## Розділи наради       — chapters з часовими межами

Межа модуля: БЕЗ Qt і БЕЗ мережі. Виклик LLM інжектиться (`generate_fn`) — у проді це
sidecar.generate, у тестах — фейк. Тож увесь конвеєр (промт → виклик → постобробка,
і чанкування) покрито юнітами без моделі.

Контекст добирається ДИНАМІЧНО під довжину транскрипту (plan_context): короткі наради
— один прохід із найменшим достатнім n_ctx; надто довгі для стелі VRAM — map-reduce
(чанки по межах реплік з overlap, часткові підсумки, окремий SYNTHESIS-прохід, не
конкатенація). Так закрито корінь бага «фіксований n_ctx=8192 + поріг 90K»: llama
кидала overflow ще ДО чанкування на ~15-хв нараді.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import DEFAULT_MAX_TOKENS
from ..meeting import postprocess as mpost

# Евристика Meetily: токенів ≈ символів × 0.35.
_TOKENS_PER_CHAR = 0.35
OVERLAP_TOKENS = 100

# --- динамічний контекст (під-хвиля n_ctx) ----------------------------------
# Корінь бага, що фіксується: n_ctx був фіксований 8192, а поріг чанкування —
# хардкод 90_000 (розрахунок під теоретичні 128K Gemma). Реально прогін ламався
# вже на ~5300 токенах транскрипту (~15 хв наради): llama-cpp кидав
# "Requested tokens exceed context window" ЩЕ ДО чанкування. Тепер контекст
# добирається під конкретний транскрипт, а поріг чанкування = реальна межа
# обраного n_ctx (не константа).
#
# Сходинки n_ctx: беремо найменшу, що покриває бюджет — щоб LlamaBackend кешував
# модель за стабільним (шлях, n_ctx) і не перевантажував її між чанками.
CTX_LADDER = (8192, 16384, 32768, 49152, 65536)
# Консервативна стеля контексту до живого калібрування VRAM (спека §Б): вміщає
# ~2-годинну нараду одним проходом, не роздуваючи KV-cache на 8 ГБ. Точні межі
# (fast/GPU з flash+q8 вище) підтверджує живий замір; максимум відкривається через
# Розширене. model_ctx_cap можна перевизначити викликом.
DEFAULT_MODEL_CTX_CAP = 32768
# Запас, який лишаємо в чанку понад overhead+max_tokens (уникнути крайового overflow).
_CHUNK_MARGIN = 512
# Мінімальний осмислений розмір чанка (нижче — фрагмент завеликий для обробки).
_MIN_CHUNK_TOKENS = 1024

SECTION_SUMMARY = "## Підсумок"
SECTION_DECISIONS = "## Рішення"
SECTION_TASKS = "## Задачі"
SECTION_CHAPTERS = "## Розділи наради"

# Таксономія рішень (патерн Google Meet Decisions): статус кожного рішення.
# Порядок = порядок груп у відрендереному протоколі.
DECISION_STATUSES = ("Узгоджено", "Потребує обговорення", "Відхилено", "Відкладено")
_TASKS_TABLE_HEADER = ("| Хто | Що | Термін | Час у записі |\n"
                       "|-----|-----|--------|--------------|")


def estimate_tokens(text: str) -> int:
    return int(len(text) * _TOKENS_PER_CHAR)


def render_transcript(utterances, *, me_label: str, others_label: str,
                      speaker_names=None) -> str:
    """Репліки → рядки «[MM:SS] Мітка: текст» (те, що читає промт). Мітки-джерела
    показуємо завжди: власник задачі береться саме з мітки мовця."""
    return mpost.to_transcript_text(
        utterances, me_label=me_label, others_label=others_label,
        speaker_names=speaker_names, show_source=True)


# --- промт українською (секретар, few-shot) ---------------------------------

_SYSTEM = """Ти — секретар наради. Склади протокол СТРОГО за транскриптом нижче.
Пиши українською, діловим стилем. Використовуй ЛИШЕ те, що є в транскрипті — нічого
не вигадуй. Якщо якоїсь інформації немає (наприклад, термін задачі) — залиш поле
порожнім, не додумуй. Власника задачі бери з мітки мовця, який її взяв або якому її
доручили. У колонці «Час у записі» став таймкод [MM:SS] сегмента, звідки взято задачу.

Кожне рішення почни зі статусу у квадратних дужках — рівно одне зі значень:
[Узгоджено] — сторони погодили й ухвалили;
[Потребує обговорення] — питання підняте, рішення ще не ухвалене;
[Відхилено] — пропозицію розглянули й відхилили;
[Відкладено] — рішення свідомо перенесли на потім.

Формат відповіді — рівно ці чотири секції Markdown, без вступів і пояснень:

## Підсумок
<один-два абзаци: про що була нарада та головний результат>

## Рішення
- [Статус] <ухвалене рішення>

## Задачі
| Хто | Що | Термін | Час у записі |
|-----|-----|--------|--------------|
| <виконавець> | <завдання> | <термін або порожньо> | <MM:SS> |

## Розділи наради
- [MM:SS–MM:SS] <назва теми, що обговорювалася>
"""

_FEWSHOT_1_IN = """[00:00] Я: Треба до п'ятниці підготувати звіт по пальному.
[00:12] Співрозмовники: Добре, я візьму цифри з першої роти.
[00:20] Я: Домовились, тоді зустрічаємось у понеділок."""

_FEWSHOT_1_OUT = """## Підсумок
Коротка нарада щодо звіту по пальному. Домовилися підготувати звіт і зустрітися наступного тижня.

## Рішення
- [Узгоджено] Провести наступну зустріч у понеділок.

## Задачі
| Хто | Що | Термін | Час у записі |
|-----|-----|--------|--------------|
| Співрозмовники | Зібрати цифри по пальному з першої роти | п'ятниця | 00:12 |

## Розділи наради
- [00:00–00:20] Підготовка звіту по пальному"""

_FEWSHOT_2_IN = """[00:00] Мовець 1: Нам бракує двох радіостанцій на напрямок.
[00:15] Мовець 2: Я подам заявку на постачання сьогодні.
[00:25] Мовець 1: Дякую, тримай мене в курсі."""

_FEWSHOT_2_OUT = """## Підсумок
Обговорення нестачі засобів зв'язку. Вирішено подати заявку на постачання радіостанцій.

## Рішення
- [Узгоджено] Подати заявку на дві радіостанції.

## Задачі
| Хто | Що | Термін | Час у записі |
|-----|-----|--------|--------------|
| Мовець 2 | Подати заявку на постачання радіостанцій | сьогодні | 00:15 |

## Розділи наради
- [00:00–00:25] Нестача засобів зв'язку"""


def build_prompt(transcript_text: str) -> str:
    """Системний промт + 2 few-shot приклади + фактичний транскрипт наради."""
    return (
        f"{_SYSTEM}\n"
        f"=== Приклад 1 ===\nТранскрипт:\n{_FEWSHOT_1_IN}\n\nПротокол:\n{_FEWSHOT_1_OUT}\n\n"
        f"=== Приклад 2 ===\nТранскрипт:\n{_FEWSHOT_2_IN}\n\nПротокол:\n{_FEWSHOT_2_OUT}\n\n"
        f"=== Нарада для протоколу ===\nТранскрипт:\n{transcript_text}\n\nПротокол:\n")


class ProtocolContextOverflow(ValueError):
    """Фрагмент наради не влазить у контекст навіть при обробці частинами
    (детермінований збій — повтор не поможе). Оркестрація дає чесну помилку,
    НЕ «спробуйте ще раз» (урок рецензента)."""


@dataclass(frozen=True)
class ContextPlan:
    """План одного прогону: обраний n_ctx, режим і розмір чанка (для map-reduce)."""
    n_ctx: int
    mode: str                     # "single" | "mapreduce"
    chunk_tokens: int = 0         # осмислений лише для mapreduce


def protocol_overhead_tokens() -> int:
    """Оверхед промту (системна інструкція + 2 few-shot + обгортка chat-turn),
    у токенах — рахуємо з РЕАЛЬНИХ констант через estimate_tokens, не хардкодимо."""
    # +64 приблизний бюджет на chat-обгортку шаблону (turn-теги, порожній канал думки).
    return estimate_tokens(build_prompt("")) + 64


def plan_context(transcript_tokens: int, *, model_ctx_cap: int = DEFAULT_MODEL_CTX_CAP,
                 overhead: "int | None" = None,
                 max_tokens: int = DEFAULT_MAX_TOKENS) -> ContextPlan:
    """Обрати n_ctx і режим під конкретний транскрипт (раз на весь прогін).

    single-pass бюджет = overhead + transcript + max_tokens. Беремо найменшу
    сходинку n_ctx, що покриває бюджет і не більша за стелю VRAM. Якщо бюджет
    перевищує стелю → map-reduce на стелі, з chunk_tokens, підібраним так, щоб
    overhead + chunk + max_tokens влазило зі запасом."""
    if overhead is None:
        overhead = protocol_overhead_tokens()
    reserve = overhead + max_tokens
    budget = reserve + max(0, int(transcript_tokens))
    steps = [s for s in CTX_LADDER if s <= model_ctx_cap]
    if not steps:                              # стеля нижча за першу сходинку
        steps = [max(1, model_ctx_cap)]
    n_ctx = next((s for s in steps if s >= budget), None)
    if n_ctx is not None:
        return ContextPlan(n_ctx=n_ctx, mode="single")
    cap = steps[-1]
    chunk_tokens = cap - reserve - _CHUNK_MARGIN
    if chunk_tokens < _MIN_CHUNK_TOKENS:
        # стеля майже повністю з'їдена оверхедом+відповіддю — фрагментами теж не вийде
        raise ProtocolContextOverflow(
            "Контекст моделі замалий навіть для обробки частинами")
    return ContextPlan(n_ctx=cap, mode="mapreduce", chunk_tokens=chunk_tokens)


# --- постобробка виходу LLM -------------------------------------------------

# Основне приборкання thinking — chat-шаблон (thinking off) у worker. Це —
# захист-глибина: якщо якийсь квант усе ж лишить ланцюг міркувань, вирізаємо його.
# Gemma 4 обрамляє reasoning як [Start thinking]…[End thinking]; Qwen-стиль — <think>.
_THINK_RE = re.compile(
    r"(<think>.*?</think>|\[\s*start thinking\s*\].*?\[\s*end thinking\s*\])",
    re.DOTALL | re.IGNORECASE)
# Незакритий ланцюг ([Start thinking] без пари) — зрізаємо все до першого заголовка секції.
_UNCLOSED_THINK_RE = re.compile(
    r"^.*?\[\s*start thinking\s*\](?:(?!\[\s*end thinking\s*\]).)*?(?=^\s*## )",
    re.DOTALL | re.IGNORECASE | re.MULTILINE)
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n```\s*$", re.DOTALL)


def postprocess(raw: str) -> str:
    """Прибрати ланцюги міркувань (<think>/[Start thinking]) і обгортку code-fence,
    підчистити пробіли. Порожній вихід → порожній рядок."""
    if not raw:
        return ""
    text = _THINK_RE.sub("", raw)
    if "## " in text:
        text = _UNCLOSED_THINK_RE.sub("", text, count=1)
    fence = _FENCE_RE.match(text.strip())
    if fence:
        text = fence.group(1)
    return text.strip()


def is_valid_protocol(text: str) -> bool:
    """Груба валідація: присутні хоча б Підсумок і одна зі змістовних секцій."""
    return SECTION_SUMMARY in text and (
        SECTION_TASKS in text or SECTION_DECISIONS in text)


# --- таксономія рішень і клікабельні розділи --------------------------------

def _section_body(text: str, header: str) -> str:
    """Тіло секонди Markdown між `header` і наступним `## ` (без самого заголовка).
    Секції немає → порожній рядок."""
    body, inside = [], False
    for line in (text or "").splitlines():
        if line.strip() == header:
            inside = True
            continue
        if inside and line.strip().startswith("## "):
            break
        if inside:
            body.append(line)
    return "\n".join(body)


_DECISION_RE = re.compile(r"^\s*[-*]\s*\[\s*([^\]]+?)\s*\]\s*(.*\S)?\s*$")


def parse_decisions(text: str):
    """Секція «Рішення» → список (status, decision). status — одне з
    DECISION_STATUSES (нормалізовано за регістром) або None, коли позначки немає
    чи вона невідома. Рядок без квадратних дужок теж повертається зі status=None."""
    lookup = {s.casefold(): s for s in DECISION_STATUSES}
    out = []
    for line in _section_body(text, SECTION_DECISIONS).splitlines():
        s = line.strip()
        if not (s.startswith("- ") or s.startswith("* ")):
            continue
        m = _DECISION_RE.match(line)
        if m and m.group(1).casefold() in lookup:
            out.append((lookup[m.group(1).casefold()], (m.group(2) or "").strip()))
        else:
            out.append((None, s[2:].strip()))
    return out


def render_decisions(decisions) -> str:
    """(status, decision) → рядки Markdown-списку, згруповані за канонним порядком
    статусів, кожен пункт із **[статус]**-позначкою. Без-статусні — вкінці."""
    lines = []
    for status in DECISION_STATUSES:
        for st, decision in decisions:
            if st == status:
                lines.append(f"- **[{status}]** {decision}".rstrip())
    for st, decision in decisions:
        if st is None:
            lines.append(f"- {decision}".rstrip())
    return "\n".join(lines)


def apply_decision_taxonomy(text: str) -> str:
    """Перебудувати секцію «Рішення»: розпарсити пункти в (status, decision) і
    відрендерити згрупованими з badge-позначками. Немає секції/пунктів → без змін.
    Заголовки секцій не чіпаємо, тож is_valid_protocol лишається чинним."""
    decisions = parse_decisions(text)
    if not decisions:
        return text
    new_body = render_decisions(decisions)
    lines = (text or "").splitlines()
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == SECTION_DECISIONS:
            out.append(line)
            out.append(new_body)
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("## "):
                i += 1
            if i < len(lines):
                out.append("")            # порожній рядок перед наступною секцією
        else:
            out.append(line)
            i += 1
    return "\n".join(out)


_CHAPTER_RE = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\s*(.*\S)?\s*$")


def _parse_timecode(token: str):
    """«MM:SS» або «HH:MM:SS» → секунди (int). Некоректний токен → None."""
    parts = token.strip().split(":")
    if not (2 <= len(parts) <= 3) or not all(p.strip().isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] * 3600 + nums[1] * 60 + nums[2]


def parse_chapters(text: str):
    """Секція «Розділи наради» → список (start, end, title) у секундах для
    клікабельної навігації. Формат пункту: «- [MM:SS–MM:SS] назва» (роздільник
    –/—/-; HH:MM:SS теж). end=None, якщо межа одна. Рядки без валідного стартового
    таймкоду пропускаємо."""
    out = []
    for line in _section_body(text, SECTION_CHAPTERS).splitlines():
        m = _CHAPTER_RE.match(line)
        if not m:
            continue
        bounds = re.split(r"\s*[–—-]\s*", m.group(1).strip())
        start = _parse_timecode(bounds[0])
        if start is None:
            continue
        end = _parse_timecode(bounds[1]) if len(bounds) > 1 else None
        out.append((start, end, (m.group(2) or "").strip()))
    return out


# --- чанкування довгих нарад (рідкісний шлях) -------------------------------

def _chunk_utterances(utterances, chunk_tokens: int, overlap_tokens: int):
    """Розбити репліки на чанки по межах реплік з overlap. Кожен чанк —
    список Utterance; overlap повторює хвіст попереднього для зв'язності."""
    chunks, cur, cur_tok = [], [], 0
    for u in utterances:
        t = estimate_tokens(u.text)
        if cur and cur_tok + t > chunk_tokens:
            chunks.append(cur)
            # overlap: хвіст попереднього чанка на початок наступного
            overlap, otok = [], 0
            for prev in reversed(cur):
                otok += estimate_tokens(prev.text)
                overlap.insert(0, prev)
                if otok >= overlap_tokens:
                    break
            cur, cur_tok = list(overlap), otok
        cur.append(u)
        cur_tok += t
    if cur:
        chunks.append(cur)
    return chunks


_SYNTHESIS = """Нижче — часткові підсумки послідовних частин однієї довгої наради.
Зший їх в ОДИН зв'язний протокол тим самим форматом (## Підсумок / ## Рішення /
## Задачі таблицею / ## Розділи наради). Кожне рішення лишай зі статусом у
квадратних дужках ([Узгоджено]/[Потребує обговорення]/[Відхилено]/[Відкладено]).
Не дублюй однакові задачі й рішення. Пиши українською, лише за наданими підсумками.

"""


def _synthesis_prompt(partials) -> str:
    body = "\n\n".join(f"=== Частина {i + 1} ===\n{p}" for i, p in enumerate(partials))
    return f"{_SYSTEM}\n{_SYNTHESIS}{body}\n\nЗведений протокол:\n"


def generate_protocol(utterances, generate_fn, *, me_label: str = "Я",
                      others_label: str = "Співрозмовники", speaker_names=None,
                      max_tokens: int = DEFAULT_MAX_TOKENS,
                      model_ctx_cap: int = DEFAULT_MODEL_CTX_CAP) -> str:
    """Головна точка входу: репліки → Markdown-протокол.

    generate_fn(prompt, *, max_tokens=..., n_ctx=...) -> str — інжектований виклик LLM
    (у проді sidecar.generate із прив'язаним model_path). Порожній транскрипт →
    порожній рядок. Довжина транскрипту визначає режим: короткий → один прохід із
    добраним n_ctx; надто довгий для стелі контексту → map-reduce (чанки по межах
    реплік → часткові підсумки → синтез). n_ctx СТАБІЛЬНИЙ на весь прогін (модель не
    перевантажується між чанками)."""
    utterances = list(utterances or [])
    if not utterances:
        return ""
    transcript = render_transcript(
        utterances, me_label=me_label, others_label=others_label,
        speaker_names=speaker_names)
    plan = plan_context(estimate_tokens(transcript), model_ctx_cap=model_ctx_cap,
                        max_tokens=max_tokens)
    if plan.mode == "single":
        raw = generate_fn(build_prompt(transcript), max_tokens=max_tokens,
                          n_ctx=plan.n_ctx)
        return apply_decision_taxonomy(postprocess(raw))
    # map-reduce: чанки по СМИСЛОВИХ межах реплік → часткові підсумки → синтез.
    chunks = _chunk_utterances(utterances, plan.chunk_tokens, OVERLAP_TOKENS)
    partials = []
    for chunk in chunks:
        ctext = render_transcript(
            chunk, me_label=me_label, others_label=others_label,
            speaker_names=speaker_names)
        # Одна безперервна репліка більша за чанк → чесний overflow (не «спробуйте ще раз»).
        if estimate_tokens(ctext) > plan.chunk_tokens + OVERLAP_TOKENS:
            raise ProtocolContextOverflow(
                "Фрагмент наради надто щільний навіть для обробки частинами")
        partials.append(postprocess(
            generate_fn(build_prompt(ctext), max_tokens=max_tokens, n_ctx=plan.n_ctx)))
    reduced = _reduce_partials(partials, generate_fn, plan, max_tokens)
    return apply_decision_taxonomy(postprocess(reduced))


def _reduce_partials(partials, generate_fn, plan: ContextPlan, max_tokens: int) -> str:
    """Ієрархічний reduce (спека §Г): якщо сума часткових підсумків сама не влазить у
    контекст (багато чанків) — зводимо їх ГРУПАМИ, доки фінальний синтез не вміститься.
    Так знімається останній край переповнення на synthesis-проході."""
    overhead = protocol_overhead_tokens()
    budget = plan.n_ctx - overhead - max_tokens - _CHUNK_MARGIN
    guard = 0
    while len(partials) > 1:
        joined = estimate_tokens(_synthesis_prompt(partials))
        if joined <= plan.n_ctx:
            break
        # згрупувати партіали так, щоб кожна група влазила в budget, і звести кожну
        groups, cur, cur_tok = [], [], 0
        for p in partials:
            pt = estimate_tokens(p)
            if cur and cur_tok + pt > max(_MIN_CHUNK_TOKENS, budget):
                groups.append(cur)
                cur, cur_tok = [], 0
            cur.append(p)
            cur_tok += pt
        if cur:
            groups.append(cur)
        if len(groups) >= len(partials):
            # не вдалося зменшити кількість (гігантський партіал) — чесний overflow
            raise ProtocolContextOverflow(
                "Часткові підсумки не зводяться в контекст навіть групами")
        partials = [postprocess(
            generate_fn(_synthesis_prompt(g), max_tokens=max_tokens, n_ctx=plan.n_ctx))
            for g in groups]
        guard += 1
        if guard > 8:                          # страхувальник від нескінченного дерева
            break
    return generate_fn(_synthesis_prompt(partials), max_tokens=max_tokens,
                       n_ctx=plan.n_ctx)
