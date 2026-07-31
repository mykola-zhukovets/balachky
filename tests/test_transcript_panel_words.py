"""Юніт-тести підсвічування СЛОВА в розшифровці наради (Етап 3 «Єдиного
робочого екрана наради»):

- Крок 1: символьні й часові межі слів (``_word_spans_for``), bisect-пошук
  активного слова за часом (``UtteranceListModel.word_for_ms``).
- Крок 2: кеш стану активного (рядок, слово) — ``set_active_pos`` мусить
  РАНО ВИХОДИТИ (``return False``, БЕЗ ``dataChanged``) на тіках, де активна
  пара не змінилась. З ~50 тиків ``positionChanged``/сек активне
  слово реально змінюється лише 2-5 разів — 90-95% подій кеш зобов'язаний
  відсікти.

Стиль pytest (голі функції, без ``unittest.TestCase``) — файл НЕ бачить
``unittest discover``, тому вписаний у ``dev/pytest_only_modules.txt``.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest
from PySide6.QtWidgets import QApplication

from fronts.desktop.meeting_transcript_panel import UtteranceListModel, _word_spans_for
from whisper_core.meeting import postprocess as mpost


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    # UtteranceListModel — QObject (QAbstractListModel); dataChanged — сигнал
    # Qt, потребує живого QCoreApplication навіть у офскрін-режимі.
    yield QApplication.instance() or QApplication([])


def _two_utterance_model():
    utterances = [
        mpost.Utterance(0.0, 2.0, mpost.SPK_SINGLE, "Перше слово тут"),   # 3 слова, 0-2000мс
        mpost.Utterance(2.0, 3.0, mpost.SPK_SINGLE, "Друга репліка"),      # 2 слова, 2000-3000мс
    ]
    return UtteranceListModel(utterances)


# ---------------------------------------------------------------------------
# Крок 1: символьні й часові межі слів
# ---------------------------------------------------------------------------

def test_word_spans_char_offsets_match_source_text():
    text = "Перше слово тут"
    spans = _word_spans_for(text, 0.0, 2.0)
    assert len(spans) == 3
    words = [text[cs:cs + cl] for cs, cl, _, _ in spans]
    assert words == ["Перше", "слово", "тут"]


def test_word_spans_evenly_divide_utterance_duration_in_ms():
    spans = _word_spans_for("а б в г", 0.0, 4.0)   # 4 слова на 4000мс → по 1000мс
    assert [(s, e) for _, _, s, e in spans] == [
        (0, 1000), (1000, 2000), (2000, 3000), (3000, 4000)]


def test_word_spans_empty_or_blank_text_gives_no_words():
    assert _word_spans_for("", 0.0, 1.0) == []
    assert _word_spans_for("   ", 0.0, 1.0) == []


def test_word_spans_last_word_reaches_exact_utterance_end_ms():
    # Цілочисельне ділення mid-слів не мусить «недотягувати» останнє слово
    # до кінця репліки через округлення вниз.
    spans = _word_spans_for("а б в", 0.0, 1.0)   # 3 слова на 1000мс → 333/333/334
    assert spans[-1][3] == 1000


# ---------------------------------------------------------------------------
# word_for_ms: bisect-пошук активного слова за часом
# ---------------------------------------------------------------------------

def test_word_for_ms_finds_word_matching_the_playback_position():
    m = _two_utterance_model()
    # Репліка 0: "Перше слово тут", 0-2000мс, межі по 667мс.
    assert m.word_for_ms(0, 0) == 0        # "Перше"
    assert m.word_for_ms(0, 900) == 1      # "слово"
    assert m.word_for_ms(0, 1999) == 2     # "тут"


def test_word_for_ms_unknown_row_returns_minus_one():
    m = _two_utterance_model()
    assert m.word_for_ms(-1, 100) == -1
    assert m.word_for_ms(99, 100) == -1


def test_word_for_ms_row_without_words_returns_minus_one():
    m = UtteranceListModel([mpost.Utterance(0.0, 1.0, mpost.SPK_SINGLE, "   ")])
    assert m.word_for_ms(0, 500) == -1


def test_gap_between_utterances_has_no_active_row_hence_no_active_word():
    # Синтезовані слова ЗАВЖДИ мають інтервал (немає паузи
    # МІЖ словами всередині репліки) — єдина реальна пауза лишається МІЖ
    # репліками, де активного рядка взагалі немає.
    utterances = [
        mpost.Utterance(0.0, 1.0, mpost.SPK_SINGLE, "Перша"),
        mpost.Utterance(2.0, 3.0, mpost.SPK_SINGLE, "Друга"),
    ]
    m = UtteranceListModel(utterances)
    assert m.row_for_ms(1500) == -1


# ---------------------------------------------------------------------------
# Крок 2: кеш стану — рахуємо dataChanged на серії тіків
# ---------------------------------------------------------------------------

def _logical_state_changes(model, ticks):
    """Незалежний від ``set_active_pos`` підрахунок: скільки РАЗІВ пара
    (row, word) справді змінюється вздовж ``ticks`` — рахує через голі
    ``row_for_ms``/``word_for_ms`` (без жодного кешу), щоб не бути
    тавтологічним порівнянням стану моделі із самим собою."""
    states = []
    for t in ticks:
        row = model.row_for_ms(t)
        word = model.word_for_ms(row, t) if row >= 0 else -1
        states.append((row, word))
    changes = 1
    for i in range(1, len(states)):
        if states[i] != states[i - 1]:
            changes += 1
    return changes


def test_dataChanged_cache_skips_ticks_where_active_word_did_not_change():
    m = _two_utterance_model()
    # 150 тіків @ 20мс (=50 Гц positionChanged) над двома репліками 0-3000мс.
    ticks = list(range(0, 3000, 20))

    emitted = {"count": 0}
    m.dataChanged.connect(lambda *_: emitted.__setitem__("count", emitted["count"] + 1))

    for t in ticks:
        m.set_active_pos(t)

    # Незалежно від dataChanged: скільки РАЗІВ пара (row, word) справді
    # змінюється вздовж тіків — рахує через голі row_for_ms/word_for_ms, не
    # через сигнал моделі (не тавтологія). 3 слова репліки 0 + 2 слова
    # репліки 1 = 5 реальних змін активного слова.
    distinct_states = _logical_state_changes(m, ticks)
    assert distinct_states == 5

    # Кожен перехід стану дає ЩОНАЙБІЛЬШЕ 2 сигнали dataChanged (перехід між
    # репліками перемальовує старий І новий рядок; зміна слова в тій самій
    # репліці — лише 1). Кеш зобов'язаний тримати кількість емісій БІЛЯ
    # distinct_states, а НЕ біля кількості тіків.
    assert emitted["count"] <= distinct_states * 2
    # Головна гарантія продуктивності: з 50 тіків/сек кеш
    # відсікає переважну більшість — тут 6 емісій супроти 150 сирих тіків.
    assert emitted["count"] < len(ticks) * 0.1


def test_set_active_pos_returns_false_and_emits_nothing_when_state_unchanged():
    m = _two_utterance_model()
    assert m.set_active_pos(100) is True     # перший тік завжди «змінює» стан (-1 → 0)

    emitted = {"count": 0}
    m.dataChanged.connect(lambda *_: emitted.__setitem__("count", emitted["count"] + 1))
    changed = m.set_active_pos(101)          # той самий (row, word) — ще «Перше»
    assert changed is False
    assert emitted["count"] == 0


def test_word_highlight_disabled_keeps_active_word_index_always_minus_one():
    m = _two_utterance_model()
    m.set_word_highlight_enabled(False)
    m.set_active_pos(900)     # всередині репліки 0, було б слово 1 ("слово")
    assert m._active_row == 0
    assert m._active_word_idx == -1
