"""Юніт-тести швидкого пошуку в нараді (Ctrl+F):

- Пре-індексація збігів у ``UtteranceListModel.set_search_query`` — без
  урахування регістру, хронологічний плаский список.
- Навігація ``navigate_match`` по колу + лічильник ``search_status``.
- Синхронна прокрутка/перемотка ``TranscriptPanel._reveal_match_row`` через
  той самий канал ``seekRequested``, що й клацання по репліці.

Стиль pytest (голі функції, без ``unittest.TestCase``) — файл НЕ бачить
``unittest discover``, тому вписаний у ``dev/pytest_only_modules.txt``
(як і сусідній ``test_transcript_panel_words.py``)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest
from PySide6.QtWidgets import QApplication

from fronts.desktop.meeting_transcript_panel import TranscriptPanel, UtteranceListModel
from whisper_core.meeting import postprocess as mpost


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    # UtteranceListModel — QObject (QAbstractListModel); dataChanged — сигнал
    # Qt, потребує живого QCoreApplication навіть у офскрін-режимі.
    yield QApplication.instance() or QApplication([])


def _model():
    utterances = [
        mpost.Utterance(0.0, 2.0, mpost.SPK_SINGLE,
                        "Колеги, починаємо обговорення критичних строки випуску"),
        mpost.Utterance(2.0, 4.0, mpost.SPK_SINGLE,
                        "Нагадую, що наші строки зафіксовані у наказі"),
        mpost.Utterance(4.0, 6.0, mpost.SPK_SINGLE,
                        "По першому блоку питань строки витримуємо"),
        mpost.Utterance(6.0, 8.0, mpost.SPK_SINGLE, "Дякую всім за увагу"),
    ]
    return UtteranceListModel(utterances)


# ---------------------------------------------------------------------------
# Пре-індексація збігів: без регістру, хронологічний порядок
# ---------------------------------------------------------------------------

def test_search_finds_all_case_insensitive_matches_in_order():
    m = _model()
    m.set_search_query("Строки")   # інший регістр, ніж у тексті
    assert [row for row, _, _ in m._matches] == [0, 1, 2]
    assert m.search_status() == (1, 3)


def test_empty_query_clears_matches_without_error():
    m = _model()
    m.set_search_query("строки")
    assert m.search_status() == (1, 3)
    m.set_search_query("")
    assert m.search_status() == (0, 0)
    assert m._matches == []


def test_whitespace_only_query_behaves_like_empty():
    m = _model()
    m.set_search_query("   ")
    assert m.search_status() == (0, 0)


def test_query_with_no_matches_gives_zero_of_zero():
    m = _model()
    m.set_search_query("параграф")
    assert m.search_status() == (0, 0)


def test_multiple_matches_within_a_single_utterance_are_all_indexed():
    utterances = [mpost.Utterance(0.0, 1.0, mpost.SPK_SINGLE, "строки і строки і строки")]
    m = UtteranceListModel(utterances)
    m.set_search_query("строки")
    assert len(m._matches) == 3
    assert [start for _, start, _ in m._matches] == [0, 9, 18]


# ---------------------------------------------------------------------------
# Навігація по колу + лічильник
# ---------------------------------------------------------------------------

def test_navigate_next_wraps_around_from_last_to_first():
    m = _model()
    m.set_search_query("строки")
    assert m.search_status() == (1, 3)
    m.navigate_match(1)
    assert m.search_status() == (2, 3)
    m.navigate_match(1)
    assert m.search_status() == (3, 3)
    m.navigate_match(1)             # з 3-го на 1-й по колу
    assert m.search_status() == (1, 3)


def test_navigate_prev_wraps_around_from_first_to_last():
    m = _model()
    m.set_search_query("строки")
    assert m.search_status() == (1, 3)
    m.navigate_match(-1)            # з 1-го на 3-й по колу
    assert m.search_status() == (3, 3)


def test_navigate_active_row_matches_the_active_match_index():
    m = _model()
    m.set_search_query("строки")
    assert m.active_match_row() == 0
    m.navigate_match(1)
    assert m.active_match_row() == 1
    m.navigate_match(1)
    assert m.active_match_row() == 2


def test_navigate_with_no_matches_is_a_safe_noop():
    m = _model()
    m.set_search_query("параграф")
    assert m.navigate_match(1) is None
    assert m.search_status() == (0, 0)


# ---------------------------------------------------------------------------
# _SEARCH_SPANS_ROLE: делегат читає позиції збігів рядка
# ---------------------------------------------------------------------------

def test_search_spans_role_marks_only_the_active_match_as_active():
    from fronts.desktop.meeting_transcript_panel import _SEARCH_SPANS_ROLE

    m = _model()
    m.set_search_query("строки")
    idx0 = m.index(0, 0)
    idx1 = m.index(1, 0)
    spans0 = m.data(idx0, _SEARCH_SPANS_ROLE)
    spans1 = m.data(idx1, _SEARCH_SPANS_ROLE)
    assert len(spans0) == 1 and spans0[0][2] is True     # активний перший збіг
    assert len(spans1) == 1 and spans1[0][2] is False

    m.navigate_match(1)             # активний переходить на рядок 1
    spans0 = m.data(idx0, _SEARCH_SPANS_ROLE)
    spans1 = m.data(idx1, _SEARCH_SPANS_ROLE)
    assert spans0[0][2] is False
    assert spans1[0][2] is True


def test_search_spans_role_empty_for_row_without_match():
    from fronts.desktop.meeting_transcript_panel import _SEARCH_SPANS_ROLE

    m = _model()
    m.set_search_query("строки")
    idx3 = m.index(3, 0)            # "Дякую всім за увагу" — без "строки"
    assert m.data(idx3, _SEARCH_SPANS_ROLE) == []


# ---------------------------------------------------------------------------
# TranscriptPanel: рядок пошуку відкриває/закриває Ctrl+F/Esc-канал
# ---------------------------------------------------------------------------

def test_open_search_shows_and_focuses_the_bar():
    # Панель ніколи не .show()-иться в тесті (без живого вікна), тож
    # isVisible() завжди False через невидимих предків — перевіряємо
    # ВЛАСНИЙ explicit-стан рядка пошуку через isHidden().
    panel = TranscriptPanel(_model()._utterances)
    assert panel._search_bar.isHidden()
    panel.open_search()
    assert not panel._search_bar.isHidden()


def test_close_search_hides_bar_and_clears_query():
    panel = TranscriptPanel(_model()._utterances)
    panel.open_search()
    panel._on_search_query("строки")
    assert panel._model.search_status() == (1, 3)
    panel.close_search()
    assert panel._search_bar.isHidden()
    assert panel._model.search_status() == (0, 0)


def test_search_query_emits_seek_to_first_match_start_ms():
    panel = TranscriptPanel(_model()._utterances)
    seeks = []
    panel.seekRequested.connect(seeks.append)
    panel._on_search_query("строки")
    assert seeks == [0]             # репліка 0 починається з 0 мс


def test_navigate_next_emits_seek_to_next_match_start_ms():
    panel = TranscriptPanel(_model()._utterances)
    seeks = []
    panel._on_search_query("строки")
    panel.seekRequested.connect(seeks.append)
    panel._navigate_search(1)
    assert seeks == [2000]          # репліка 1 починається з 2000 мс


def test_empty_query_via_panel_leaves_status_at_zero_of_zero():
    panel = TranscriptPanel(_model()._utterances)
    panel._on_search_query("")
    assert panel._model.search_status() == (0, 0)
