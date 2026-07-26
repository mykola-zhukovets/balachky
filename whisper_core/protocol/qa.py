"""Q&A-чат по нараді: постав питання своєму запису → відповідь із таймкод-цитатами,
повністю офлайн. Той самий sidecar/бекенд, що AI-протокол (generate.py) — інший лише
промт і постобробка: вільна відповідь + клікабельні цитати замість структурованого
протоколу. Тому is_valid_protocol сюди НЕ застосовний (це інший вихід).

Межа модуля: БЕЗ Qt і БЕЗ мережі. Виклик LLM інжектиться (`generate_fn`) — у проді це
sidecar.generate, у тестах фейк. Тож увесь конвеєр (промт → виклик → парсинг цитат,
і чанкування) покрито юнітами без моделі.

Довгі наради чанкуються тим самим динамічним механізмом, що протокол (plan_context):
питання ставиться до кожного чанка (часткові відповіді), потім окремий прохід зшиває
їх в одну (map → synthesis), а не конкатенує.
"""
from __future__ import annotations

import re

from . import generate as _gen

# --- промт українською ------------------------------------------------------

_SYSTEM = """Ти помічник, що відповідає на питання ЛИШЕ за транскриптом наради нижче.

Правила:
- Використовуй ТІЛЬКИ інформацію з транскрипту. Нічого не вигадуй і не додумуй.
- Цитуй короткий фрагмент транскрипту, на якому ґрунтується відповідь, і став поруч
  його таймкод у квадратних дужках [MM:SS].
- Якщо у транскрипті немає відповіді на питання — так і напиши: «У записі немає
  відповіді на це питання», і не вигадуй.

Пиши українською, стисло й по суті."""


def build_qa_prompt(question: str, transcript_text: str) -> str:
    """Системний промт + транскрипт наради + питання користувача."""
    return (
        f"{_SYSTEM}\n\n"
        f"=== Транскрипт наради ===\n{transcript_text}\n\n"
        f"=== Питання ===\n{question}\n\n"
        f"Відповідь:\n")


_SYNTHESIS = """Нижче — відповіді на ОДНЕ Й ТЕ САМЕ питання, отримані за різними
послідовними частинами однієї довгої наради. Зший їх в ОДНУ зв'язну відповідь.
Збережи таймкод-цитати [MM:SS]. Не дублюй однакове. Якщо в жодній частині відповіді
немає — напиши «У записі немає відповіді на це питання». Пиши українською.

Питання: {question}

"""


def _synthesis_prompt(question: str, partials) -> str:
    body = "\n\n".join(f"=== Частина {i + 1} ===\n{p}" for i, p in enumerate(partials))
    return f"{_SYNTHESIS.format(question=question)}{body}\n\nЗведена відповідь:\n"


# --- парсинг таймкод-цитат у клікабельні ------------------------------------

_CITE_RE = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]")


def parse_citations(answer: str):
    """Знайти таймкод-цитати «[MM:SS]»/«[HH:MM:SS]» у відповіді → список
    (seconds, timecode) у порядку появи, без повторів. Для клікабельних чіпів:
    клік стрибає плеєр на момент (як Smart Chapters). Некоректні токени пропускаємо."""
    out, seen = [], set()
    for m in _CITE_RE.finditer(answer or ""):
        tc = m.group(1)
        secs = _gen._parse_timecode(tc)
        if secs is None or secs in seen:
            continue
        seen.add(secs)
        out.append((secs, tc))
    return out


# --- оркестрація ------------------------------------------------------------

def answer_question(question: str, utterances, generate_fn, *, me_label: str = "Я",
                    others_label: str = "Співрозмовники", speaker_names=None,
                    max_tokens: int = 1024) -> str:
    """Головна точка входу: питання + репліки наради → текстова відповідь із цитатами.

    generate_fn(prompt, max_tokens=...) -> str — інжектований виклик LLM (у проді
    sidecar.generate із прив'язаним model_path). Порожнє питання або порожній
    транскрипт → порожній рядок. Довга нарада (за planned n_ctx) → map+synthesis.
    """
    question = (question or "").strip()
    utterances = list(utterances or [])
    if not question or not utterances:
        return ""
    transcript = _gen.render_transcript(
        utterances, me_label=me_label, others_label=others_label,
        speaker_names=speaker_names)
    # Динамічний контекст (той самий механізм, що протокол): оверхед — QA-промт без
    # транскрипту (менший за протокольний: без few-shot), +32 на chat-обгортку.
    overhead = _gen.estimate_tokens(build_qa_prompt(question, "")) + 32
    plan = _gen.plan_context(_gen.estimate_tokens(transcript),
                             overhead=overhead, max_tokens=max_tokens)
    if plan.mode == "single":
        raw = generate_fn(build_qa_prompt(question, transcript),
                          max_tokens=max_tokens, n_ctx=plan.n_ctx)
        return _gen.postprocess(raw)
    # рідкісний шлях: чанки → часткові відповіді → синтез (n_ctx стабільний на прогін)
    partials = []
    for chunk in _gen._chunk_utterances(utterances, plan.chunk_tokens, _gen.OVERLAP_TOKENS):
        ctext = _gen.render_transcript(
            chunk, me_label=me_label, others_label=others_label,
            speaker_names=speaker_names)
        partials.append(_gen.postprocess(
            generate_fn(build_qa_prompt(question, ctext),
                        max_tokens=max_tokens, n_ctx=plan.n_ctx)))
    return _gen.postprocess(
        generate_fn(_synthesis_prompt(question, partials),
                    max_tokens=max_tokens, n_ctx=plan.n_ctx))
