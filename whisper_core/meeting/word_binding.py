"""Прив'язка слів системної доріжки до глобальних спанів мовців (Slice 3).

Чистий цілочисловий binder: БЕЗ sherpa, БЕЗ Qt, БЕЗ numpy. Кожному
``WordRecord`` доріжки ``sys`` призначає ``speaker_id`` за найбільшим СУМАРНИМ
перекриттям семплів зі спанами діаризації. Binder ніколи не видаляє й не дублює
слово: на виході рівно один ``SpeakerAssignment`` на кожне вхідне слово, у тому ж
порядку. Слова інших доріжок (mic/multimic) залишаються без мітки мовця.

Семантика перекриття свідомо у цілих семплах (16 кГц): жодної похибки float,
детермінований tie-break. Спани sherpa не перетинаються між собою
(``OfflineSpeakerDiarization`` віддає ексклюзивні відрізки), але binder стійкий і
до штучно накладених спанів у тестах — слово однаково лишається рівно раз.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .word_ledger import SpeakerAssignment, SpeakerCandidate, WordRecord

SYS_TRACK = "sys"

# Причини призначення (порівнюємо в коді, у діагностиці).
REASON_OVERLAP = "overlap"
REASON_NO_SPAN = "no_span"
REASON_NOT_SYS = "not_sys_track"


@dataclass(frozen=True)
class DiarizationSpan:
    """Глобальний відрізок одного мовця у семплах 16 кГц.

    ``speaker_id`` — стабільний анонімний ідентифікатор наради (``speaker_01``…).
    """

    start_sample: int
    end_sample: int
    speaker_id: str

    def __post_init__(self):
        if self.end_sample < self.start_sample:
            raise ValueError("некоректний sample range спану мовця")


def bind_words(words: Sequence[WordRecord],
               spans: Sequence[DiarizationSpan]) -> tuple[SpeakerAssignment, ...]:
    """Призначити мовця кожному слову ``sys`` за найбільшим сумарним перекриттям.

    Повертає кортеж ``SpeakerAssignment`` — рівно один на кожне вхідне слово, у
    вхідному порядку. Слово без жодного перетину або поза доріжкою ``sys`` дістає
    ``speaker_id=None``. Гейт нуль-втрат/нуль-дублювання: ``len(out) == len(words)``
    і кожен ``word_id`` присутній рівно раз.
    """
    words = list(words)
    sys_indices = [i for i, w in enumerate(words) if w.track == SYS_TRACK]

    # Спани відсортовані за початком — це дозволяє sweep і ранній вихід.
    sorted_spans = sorted(spans, key=lambda s: (s.start_sample, s.end_sample))

    # Слова sys відсортовані за початком; sweep-вказівник lo відсікає спани, що
    # завершилися лівіше за поточне слово (і тим паче за всі наступні).
    order = sorted(
        sys_indices,
        key=lambda i: (words[i].start_sample, words[i].end_sample, words[i].word_id),
    )
    assignment_by_index: dict[int, SpeakerAssignment] = {}
    lo = 0
    for i in order:
        word = words[i]
        while lo < len(sorted_spans) and sorted_spans[lo].end_sample <= word.start_sample:
            lo += 1
        sums: dict[str, int] = {}
        first_start: dict[str, int] = {}
        for j in range(lo, len(sorted_spans)):
            span = sorted_spans[j]
            if span.start_sample >= word.end_sample:
                break  # спани впорядковані за початком — далі перетину не буде
            inter = min(word.end_sample, span.end_sample) - max(
                word.start_sample, span.start_sample)
            if inter <= 0:
                continue
            sums[span.speaker_id] = sums.get(span.speaker_id, 0) + inter
            if span.speaker_id not in first_start:
                first_start[span.speaker_id] = span.start_sample
            else:
                first_start[span.speaker_id] = min(
                    first_start[span.speaker_id], span.start_sample)
        assignment_by_index[i] = _assignment_from_sums(word.word_id, sums, first_start)

    out = []
    for i, word in enumerate(words):
        if i in assignment_by_index:
            out.append(assignment_by_index[i])
        else:
            out.append(SpeakerAssignment(
                word_id=word.word_id, speaker_id=None,
                assignment_reason=REASON_NOT_SYS, candidates=(),
                overlap_suspected=False, confidence_class="deterministic"))
    return tuple(out)


def _assignment_from_sums(word_id: str, sums: dict[str, int],
                          first_start: dict[str, int]) -> SpeakerAssignment:
    if not sums:
        return SpeakerAssignment(
            word_id=word_id, speaker_id=None, assignment_reason=REASON_NO_SPAN,
            candidates=(), overlap_suspected=False,
            confidence_class="deterministic")
    # Перемога: найбільша сума; tie-break — найраніший початок спану мовця, тоді
    # лексикографічний speaker_id (повністю детерміновано).
    winner = min(
        sums,
        key=lambda spk: (-sums[spk], first_start[spk], spk),
    )
    candidates = tuple(
        SpeakerCandidate(speaker_id=spk, overlap_samples=sums[spk])
        for spk in sorted(sums, key=lambda s: (-sums[s], first_start[s], s))
    )
    return SpeakerAssignment(
        word_id=word_id, speaker_id=winner, assignment_reason=REASON_OVERLAP,
        candidates=candidates,
        # sherpa не повертає одночасних мовців — накладання не декларуємо.
        overlap_suspected=False,
        confidence_class="deterministic")


__all__ = [
    "DiarizationSpan", "bind_words", "SYS_TRACK",
    "REASON_OVERLAP", "REASON_NO_SPAN", "REASON_NOT_SYS",
]
