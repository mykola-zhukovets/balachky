"""Юніт-тести навігації та фільтра за мовцем у нараді (issue #19):

- чисті функції ``whisper_core.meeting.speaker_nav`` (склеювання пауз,
  сусідня репліка обраного мовця) — без Qt;
- ``TranscriptFilterProxyModel`` над ``UtteranceListModel`` — приховування
  чужих реплік без дублювання даних джерела;
- ``SpeakerFilterBar``/``TranscriptPanel`` — чіпи фільтра, кнопки
  Попередня/Наступна, перемикач «Грати лише обраного», зазор;
- ``VideoPlayerDialog._solo_target``/``_maybe_solo_jump`` — авто-стрибок
  через чужі репліки без реального ``QMediaPlayer``.

Qt-тести: один ``QApplication`` на модуль, offscreen-платформа (як
``tests/render_meeting_smoke.py``).
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtWidgets import QApplication, QToolButton

from whisper_core.meeting import postprocess as mpost
from whisper_core.meeting.speaker_nav import (
    next_speaker_utterance_ms, playback_segments, prev_speaker_utterance_ms,
)

_APP = QApplication.instance() or QApplication([])


def _u(start, end, speaker, text="."):
    return mpost.Utterance(start, end, speaker, text)


# ---------------------------------------------------------------------------
# playback_segments — склеювання паузи (спека 31.07 §3)
# ---------------------------------------------------------------------------

class PlaybackSegmentsTests(unittest.TestCase):
    def test_merges_when_gap_within_threshold(self):
        utterances = [_u(0.0, 1.0, "a"), _u(3.0, 4.0, "a")]   # пауза 2с <= 5с
        self.assertEqual(playback_segments(utterances, "a", gap_s=5.0),
                         [(0, 4000)])

    def test_breaks_when_gap_above_threshold(self):
        utterances = [_u(0.0, 1.0, "a"), _u(10.0, 11.0, "a")]  # пауза 9с > 5с
        self.assertEqual(playback_segments(utterances, "a", gap_s=5.0),
                         [(0, 1000), (10000, 11000)])

    def test_gap_exactly_at_threshold_merges(self):
        utterances = [_u(0.0, 1.0, "a"), _u(6.0, 7.0, "a")]    # пауза рівно 5с
        self.assertEqual(playback_segments(utterances, "a", gap_s=5.0),
                         [(0, 7000)])

    def test_empty_for_speaker_with_no_utterances(self):
        utterances = [_u(0.0, 1.0, "a"), _u(2.0, 3.0, "b")]
        self.assertEqual(playback_segments(utterances, "c", gap_s=5.0), [])

    def test_empty_input_gives_empty_segments(self):
        self.assertEqual(playback_segments([], "a"), [])

    def test_ignores_other_speakers_and_keeps_chronological_order(self):
        utterances = [
            _u(0.0, 1.0, "a"), _u(1.5, 2.0, "b"),
            _u(2.5, 3.0, "a"), _u(20.0, 21.0, "a"),
        ]
        self.assertEqual(
            playback_segments(utterances, "a", gap_s=5.0),
            [(0, 3000), (20000, 21000)])

    def test_none_speaker_merges_across_all_speakers(self):
        utterances = [_u(0.0, 1.0, "a"), _u(1.5, 2.5, "b")]
        self.assertEqual(playback_segments(utterances, None, gap_s=5.0),
                         [(0, 2500)])

    def test_ms_values_are_ints(self):
        segs = playback_segments([_u(0.0, 1.0, "a")], "a")
        start, end = segs[0]
        self.assertIsInstance(start, int)
        self.assertIsInstance(end, int)


# ---------------------------------------------------------------------------
# next/prev_speaker_utterance_ms
# ---------------------------------------------------------------------------

class SpeakerNavTests(unittest.TestCase):
    def test_next_skips_utterance_already_playing(self):
        # Запас 50 мс: кнопка “Наступна” не має перемотувати на репліку,
        # яка вже звучить (позиція за 20 мс до її старту).
        us = [_u(4.0, 5.0, "a"), _u(8.0, 9.0, "a")]
        self.assertEqual(next_speaker_utterance_ms(3980, us, "a"), 8000)

    def test_prev_skips_utterance_already_playing(self):
        us = [_u(4.0, 5.0, "a"), _u(8.0, 9.0, "a")]
        self.assertEqual(prev_speaker_utterance_ms(8020, us, "a"), 4000)

    def setUp(self):
        self.utterances = [
            _u(0.0, 1.0, "a"), _u(2.0, 2.5, "b"),
            _u(4.0, 4.5, "a"), _u(6.0, 6.5, "b"),
        ]

    def test_next_skips_other_speakers(self):
        self.assertEqual(
            next_speaker_utterance_ms(0, self.utterances, "a"), 4000)

    def test_prev_skips_other_speakers(self):
        self.assertEqual(
            prev_speaker_utterance_ms(6500, self.utterances, "a"), 4000)

    def test_next_none_speaker_matches_any_utterance(self):
        self.assertEqual(
            next_speaker_utterance_ms(0, self.utterances, None), 2000)

    def test_prev_none_speaker_matches_any_utterance(self):
        self.assertEqual(
            prev_speaker_utterance_ms(6500, self.utterances, None), 6000)

    def test_next_returns_none_past_last_matching_utterance(self):
        self.assertIsNone(next_speaker_utterance_ms(4000, self.utterances, "a"))

    def test_prev_returns_none_before_first_matching_utterance(self):
        self.assertIsNone(prev_speaker_utterance_ms(0, self.utterances, "a"))


# ---------------------------------------------------------------------------
# UtteranceListModel.speaker_counts / set_speaker_names
# ---------------------------------------------------------------------------

class SpeakerCountsTests(unittest.TestCase):
    def test_counts_in_first_seen_order(self):
        from fronts.desktop.meeting_transcript_panel import UtteranceListModel
        utterances = [
            _u(0.0, 1.0, "b"), _u(1.0, 2.0, "a"),
            _u(2.0, 3.0, "b"), _u(3.0, 4.0, "a"), _u(4.0, 5.0, "a"),
        ]
        model = UtteranceListModel(utterances)
        self.assertEqual(model.speaker_counts(), [("b", 2), ("a", 3)])

    def test_rename_updates_label_but_not_speaker_code(self):
        from fronts.desktop.meeting_transcript_panel import UtteranceListModel
        utterances = [_u(0.0, 1.0, "speaker_01")]
        model = UtteranceListModel(utterances)
        self.assertIsNone(model.speaker_label("speaker_01"))
        model.set_speaker_names({"speaker_01": "Олексій"})
        self.assertEqual(model.speaker_label("speaker_01"), "Олексій")
        # той самий код мовця й далі визначає лічильники — перейменування
        # не переносить репліки на інший ключ
        self.assertEqual(model.speaker_counts(), [("speaker_01", 1)])


# ---------------------------------------------------------------------------
# TranscriptFilterProxyModel
# ---------------------------------------------------------------------------

class ProxyModelTests(unittest.TestCase):
    def setUp(self):
        from fronts.desktop.meeting_transcript_panel import (
            TranscriptFilterProxyModel, UtteranceListModel,
        )
        self.utterances = [
            _u(0.0, 1.0, "a", "перша"), _u(1.0, 2.0, "b", "друга"),
            _u(2.0, 3.0, "a", "третя"), _u(3.0, 4.0, "b", "четверта"),
            _u(4.0, 5.0, "a", "п'ята"),
        ]
        self.model = UtteranceListModel(self.utterances)
        self.proxy = TranscriptFilterProxyModel()
        self.proxy.setSourceModel(self.model)

    def test_unfiltered_shows_all_rows(self):
        self.assertEqual(self.proxy.rowCount(), len(self.utterances))

    def test_filtered_row_count_matches_speaker_utterance_count(self):
        self.proxy.set_speaker_filter("a")
        self.assertEqual(self.proxy.rowCount(), 3)
        self.proxy.set_speaker_filter("b")
        self.assertEqual(self.proxy.rowCount(), 2)

    def test_map_to_source_gives_correct_utterance(self):
        self.proxy.set_speaker_filter("b")
        source_index = self.proxy.mapToSource(self.proxy.index(1, 0))
        u = self.model.utterance_at(source_index.row())
        self.assertEqual(u.text, "четверта")

    def test_utterance_at_forwards_through_mapping(self):
        self.proxy.set_speaker_filter("a")
        self.assertEqual(self.proxy.utterance_at(0).text, "перша")
        self.assertEqual(self.proxy.utterance_at(2).text, "п'ята")

    def test_speaker_label_forwards_to_source(self):
        self.model.set_speaker_names({"a": "Микола"})
        self.assertEqual(self.proxy.speaker_label("a"), "Микола")


# ---------------------------------------------------------------------------
# TranscriptPanel: чіпи, навігація, accessibleName
# ---------------------------------------------------------------------------

class TranscriptPanelSpeakerFilterTests(unittest.TestCase):
    def setUp(self):
        from fronts.desktop.meeting_transcript_panel import TranscriptPanel
        self.utterances = [
            _u(0.0, 1.0, "a", "перша"), _u(1.0, 2.0, "b", "друга"),
            _u(2.0, 3.0, "a", "третя"), _u(10.0, 11.0, "b", "четверта"),
        ]
        self.panel = TranscriptPanel(
            self.utterances, {"a": "Микола", "b": "Олексій"})
        self.seeks = []
        self.panel.seekRequested.connect(self.seeks.append)

    def tearDown(self):
        self.panel.deleteLater()
        _APP.processEvents()

    def test_all_chip_selected_by_default_and_shows_every_row(self):
        self.assertEqual(self.panel.current_speaker_filter(), None)
        self.assertEqual(self.panel._view.model().rowCount(), 4)

    def test_clicking_speaker_chip_filters_view_to_that_speaker(self):
        self.panel._filter_bar._chips["a"].click()
        self.assertEqual(self.panel.current_speaker_filter(), "a")
        self.assertEqual(self.panel._view.model().rowCount(), 2)

    def test_clicking_all_chip_again_restores_full_list(self):
        self.panel._filter_bar._chips["a"].click()
        self.panel._filter_bar._chips[None].click()
        self.assertIsNone(self.panel.current_speaker_filter())
        self.assertEqual(self.panel._view.model().rowCount(), 4)

    def test_clicking_row_in_filtered_list_emits_correct_seek_ms(self):
        self.panel._filter_bar._chips["b"].click()
        proxy = self.panel._view.model()
        self.panel._on_clicked(proxy.index(1, 0))    # друга репліка "b" — старт 10с
        self.assertEqual(self.seeks, [10000])

    def test_every_chip_has_nonempty_accessible_name(self):
        for chip in self.panel._filter_bar._chips.values():
            self.assertTrue(chip.accessibleName())

    def test_nav_and_solo_controls_have_nonempty_accessible_names(self):
        self.assertTrue(self.panel._speaker_prev_btn.accessibleName())
        self.assertTrue(self.panel._speaker_next_btn.accessibleName())
        self.assertTrue(self.panel._solo_btn.accessibleName())
        self.assertTrue(self.panel._gap_combo.accessibleName())

    def test_next_prev_buttons_respect_speaker_filter(self):
        self.panel._filter_bar._chips["a"].click()
        self.panel.set_active_ms(0)               # позиція на початку
        self.panel._speaker_next_btn.click()
        self.assertEqual(self.seeks, [2000])       # третя репліка "a", не друга "b"

    def test_solo_toggle_and_gap_change_emit_playback_settings_changed(self):
        calls = []
        self.panel.playbackSettingsChanged.connect(lambda: calls.append(1))
        self.panel._solo_btn.setChecked(True)
        self.assertTrue(self.panel.solo_enabled())
        self.assertEqual(len(calls), 1)
        self.panel._gap_combo.setCurrentIndex(0)   # 1 с
        self.assertEqual(self.panel.gap_seconds(), 1.0)
        self.assertEqual(len(calls), 2)

    def test_gap_combo_defaults_to_five_seconds(self):
        self.assertEqual(self.panel.gap_seconds(), 5.0)

    def test_rename_speaker_updates_chip_label_keeps_filter_by_code(self):
        self.panel._filter_bar._chips["a"].click()
        self.panel.set_speaker_names({"a": "Директор", "b": "Олексій"})
        self.assertIn("Директор", self.panel._filter_bar._chips["a"].text())
        # фільтр і далі активний за кодом "a", перейменування його не скидає
        self.assertEqual(self.panel.current_speaker_filter(), "a")
        self.assertEqual(self.panel._view.model().rowCount(), 2)


# ---------------------------------------------------------------------------
# VideoPlayerDialog._solo_target / _maybe_solo_jump — без реального медіа
# ---------------------------------------------------------------------------

class VideoPlayerSoloJumpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtMultimedia import QMediaPlayer
            _ = QMediaPlayer
        except Exception:                       # pragma: no cover
            raise unittest.SkipTest("QtMultimedia недоступний")

    def setUp(self):
        from fronts.desktop.video_player import VideoPlayerDialog
        self.dlg = VideoPlayerDialog(None, None)
        self.addCleanup(self.dlg.deleteLater)
        self.addCleanup(_APP.processEvents)

    def test_returns_none_when_inside_a_segment(self):
        self.dlg._solo_segments = [(1000, 2000), (5000, 6000)]
        self.assertIsNone(self.dlg._solo_target(1500))

    def test_returns_next_segment_start_when_in_a_gap(self):
        self.dlg._solo_segments = [(1000, 2000), (5000, 6000)]
        self.assertEqual(self.dlg._solo_target(3000), 5000)

    def test_returns_next_segment_start_when_before_first_segment(self):
        self.dlg._solo_segments = [(1000, 2000)]
        self.assertEqual(self.dlg._solo_target(0), 1000)

    def test_returns_sentinel_when_past_last_segment(self):
        self.dlg._solo_segments = [(1000, 2000)]
        self.assertEqual(self.dlg._solo_target(2500), -1)

    def test_maybe_solo_jump_seeks_to_next_segment_start(self):
        from unittest.mock import MagicMock
        self.dlg._solo_enabled = True
        self.dlg._solo_segments = [(1000, 2000), (5000, 6000)]
        self.dlg._player.setPosition = MagicMock()
        self.dlg._player.pause = MagicMock()
        self.dlg._maybe_solo_jump(3000)
        self.dlg._player.setPosition.assert_called_once_with(5000)
        self.dlg._player.pause.assert_not_called()

    def test_maybe_solo_jump_pauses_when_no_more_segments(self):
        from unittest.mock import MagicMock
        self.dlg._solo_enabled = True
        self.dlg._solo_segments = [(1000, 2000)]
        self.dlg._player.setPosition = MagicMock()
        self.dlg._player.pause = MagicMock()
        self.dlg._maybe_solo_jump(2500)
        self.dlg._player.pause.assert_called_once()
        self.dlg._player.setPosition.assert_not_called()

    def test_maybe_solo_jump_pauses_only_once_after_last_segment(self):
        # Соло дограло останню репліку мовця. Якщо тиснути паузу на КОЖЕН тік
        # позиції, людина більше не зможе натиснути відтворення.
        from unittest.mock import MagicMock
        self.dlg._solo_enabled = True
        self.dlg._solo_segments = [(1000, 2000)]
        self.dlg._player.pause = MagicMock()
        for pos in range(2500, 3500, 100):
            self.dlg._maybe_solo_jump(pos)
        self.assertEqual(self.dlg._player.pause.call_count, 1)

    def test_solo_pause_latch_resets_when_playback_returns_to_a_segment(self):
        # Користувач перемотав назад у межі відрізка — соло знову живе,
        # і наступний вихід за останній відрізок дасть нову паузу.
        from unittest.mock import MagicMock
        self.dlg._solo_enabled = True
        self.dlg._solo_segments = [(1000, 2000)]
        self.dlg._player.pause = MagicMock()
        self.dlg._maybe_solo_jump(2500)
        self.dlg._maybe_solo_jump(1500)          # усередині відрізка
        self.dlg._maybe_solo_jump(2500)
        self.assertEqual(self.dlg._player.pause.call_count, 2)

    def test_maybe_solo_jump_does_nothing_when_disabled(self):
        from unittest.mock import MagicMock
        self.dlg._solo_enabled = False
        self.dlg._solo_segments = [(1000, 2000)]
        self.dlg._player.setPosition = MagicMock()
        self.dlg._player.pause = MagicMock()
        self.dlg._maybe_solo_jump(3000)
        self.dlg._player.setPosition.assert_not_called()
        self.dlg._player.pause.assert_not_called()

    def test_maybe_solo_jump_respects_300ms_seek_hysteresis(self):
        from unittest.mock import MagicMock
        self.dlg._solo_enabled = True
        self.dlg._solo_segments = [(1000, 2000), (2100, 2200), (5000, 6000)]
        self.dlg._player.setPosition = MagicMock()
        self.dlg._player.pause = MagicMock()
        self.dlg._maybe_solo_jump(2050)     # -> перескочити на 2100
        self.dlg._maybe_solo_jump(2250)     # одразу після — гістерезис блокує
        self.dlg._player.setPosition.assert_called_once_with(2100)

    def test_solo_settings_changed_recomputes_segments_from_panel_state(self):
        from fronts.desktop.video_player import VideoPlayerDialog
        utterances = [
            _u(0.0, 1.0, "a"), _u(1.5, 2.0, "a"), _u(20.0, 21.0, "a"),
        ]
        dlg = VideoPlayerDialog(None, None, utterances=utterances,
                                speaker_names={"a": "Микола"})
        self.addCleanup(dlg.deleteLater)
        panel = dlg._transcript_panel
        panel._filter_bar._chips["a"].click()
        panel._solo_btn.setChecked(True)
        self.assertEqual(dlg._solo_segments, [(0, 2000), (20000, 21000)])


# ---------------------------------------------------------------------------
# Пошук (Ctrl+F) + фільтр мовця разом (Етап 4 спеки, issue #19)
# ---------------------------------------------------------------------------

class SearchWithSpeakerFilterTests(unittest.TestCase):
    def setUp(self):
        from fronts.desktop.meeting_transcript_panel import TranscriptPanel
        # Спільне слово “мова” у репліках трьох різних мовців: лічильник
        # пошуку під фільтром має рахувати лише видимі збіги.
        self.utterances = [
            _u(0.0, 1.0, "a", "перша мова"), _u(1.0, 2.0, "b", "друга мова"),
            _u(2.0, 3.0, "a", "третя мова"), _u(3.0, 4.0, "b", "четверта фраза"),
        ]
        self.panel = TranscriptPanel(self.utterances)

    def tearDown(self):
        self.panel.deleteLater()
        _APP.processEvents()

    def test_search_under_filter_counts_only_visible_matches(self):
        self.panel._filter_bar._chips["a"].click()   # видно 2 репліки "a", обидві з "мова"
        self.panel._on_search_query("мова")
        self.assertEqual(self.panel._model.search_status(), (1, 2))

    def test_navigate_match_stays_within_visible_rows(self):
        self.panel._filter_bar._chips["a"].click()
        self.panel._on_search_query("мова")
        row = self.panel._model.navigate_match(1)
        self.assertIn(row, (0, 2))   # рядки мовця "a" (0-та й 2-га в джерелі)
        self.assertNotEqual(self.panel._model.utterance_at(row).speaker, "b")

    def test_clearing_filter_restores_full_match_count(self):
        self.panel._filter_bar._chips["a"].click()
        self.panel._on_search_query("мова")
        self.assertEqual(self.panel._model.search_status(), (1, 2))
        self.panel._filter_bar._chips[None].click()   # знято фільтр
        self.assertEqual(self.panel._model.search_status(), (1, 3))  # усі 3 "мова"


# ---------------------------------------------------------------------------
# Клік по бейджу мовця в рядку списку (спека §4.2 п.2)
# ---------------------------------------------------------------------------

class BadgeClickTests(unittest.TestCase):
    def setUp(self):
        from fronts.desktop.meeting_transcript_panel import TranscriptPanel
        self.utterances = [
            _u(0.0, 1.0, "a", "перша"), _u(1.0, 2.0, "b", "друга"),
        ]
        self.panel = TranscriptPanel(
            self.utterances, {"a": "Микола", "b": "Олексій"})
        self.seeks = []
        self.panel.seekRequested.connect(self.seeks.append)

    def tearDown(self):
        self.panel.deleteLater()
        _APP.processEvents()

    def test_click_on_speaker_badge_enables_filter_without_seeking(self):
        from fronts.desktop.meeting_transcript_panel import _badge_rect
        index = self.panel._model.index(0, 0)
        rect = self.panel._view.visualRect(index)
        badge = _badge_rect(rect)
        self.panel._on_clicked(index, badge.center())
        self.assertEqual(self.panel.current_speaker_filter(), "a")
        self.assertTrue(self.panel._filter_bar._chips["a"].isChecked())
        self.assertEqual(self.seeks, [])

    def test_click_elsewhere_in_row_seeks_and_keeps_filter_unchanged(self):
        index = self.panel._model.index(1, 0)
        rect = self.panel._view.visualRect(index)
        self.panel._on_clicked(index, rect.center())   # центр рядка — не бейдж
        self.assertEqual(self.seeks, [1000])
        self.assertIsNone(self.panel.current_speaker_filter())


# ---------------------------------------------------------------------------
# Ряд чипів не роздуває мінімальну ширину панелі
# ---------------------------------------------------------------------------

class SpeakerFilterRowGeometryTests(unittest.TestCase):
    """Ряд чипів у прокручуваній області: чипи не обрізаються скролбаром і
    чіп у фокусі завжди у видимій частині."""

    def _panel(self, speakers: int):
        from fronts.desktop.meeting_transcript_panel import TranscriptPanel
        utterances = [_u(i, i + 1, f"speaker_{i:02d}", f"репліка {i}")
                      for i in range(speakers)]
        names = {f"speaker_{i:02d}": f"Учасник із довгим ім’ям {i}" for i in range(speakers)}
        panel = TranscriptPanel(utterances, names)
        panel.resize(402, 600)
        panel.show()
        QApplication.processEvents()
        return panel

    def test_chips_keep_full_height_when_scrollbar_appears(self):
        panel = self._panel(12)
        try:
            area = panel._filter_scroll
            chip = panel._filter_bar.findChildren(QToolButton)[0]
            self.assertGreater(area.horizontalScrollBar().maximum(), 0,
                               "на 12 мовцях ряд має прокручуватись")
            self.assertGreaterEqual(area.viewport().height(), chip.sizeHint().height(),
                                    "скролбар не має з’їдати висоту чипів")
        finally:
            panel.deleteLater()

    def test_no_spare_height_without_scrollbar(self):
        panel = self._panel(2)
        try:
            area = panel._filter_scroll
            self.assertEqual(area.horizontalScrollBar().maximum(), 0)
            self.assertEqual(area.height(), panel._filter_bar.sizeHint().height())
        finally:
            panel.deleteLater()

    def test_focused_chip_is_scrolled_into_view(self):
        panel = self._panel(12)
        try:
            area = panel._filter_scroll
            chips = panel._filter_bar.findChildren(QToolButton)
            last = chips[-1]
            last.setFocus()
            QApplication.processEvents()
            left = last.mapTo(area.viewport(), last.rect().topLeft()).x()
            self.assertGreaterEqual(left, 0)
            self.assertLessEqual(left + last.width(), area.viewport().width(),
                                 "чіп у фокусі має бути повністю видимим")
        finally:
            panel.deleteLater()


class SpeakerFilterBarWidthTests(unittest.TestCase):
    def _panel_with_speakers(self, n):
        from fronts.desktop.meeting_transcript_panel import TranscriptPanel
        utterances = [
            _u(float(i), float(i) + 1.0, f"speaker_{i:02d}", f"репліка {i}")
            for i in range(n)]
        return TranscriptPanel(utterances)

    def test_min_width_does_not_grow_with_speaker_count(self):
        panel2 = self._panel_with_speakers(2)
        panel12 = self._panel_with_speakers(12)
        try:
            w2 = panel2.minimumSizeHint().width()
            w12 = panel12.minimumSizeHint().width()
            self.assertLessEqual(w12, w2)
        finally:
            panel2.deleteLater()
            panel12.deleteLater()
            _APP.processEvents()


if __name__ == "__main__":
    unittest.main()
