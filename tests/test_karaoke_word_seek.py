"""Тести для Кандидата 4: караоке-підсвічування та перехід за кліком на слово (POST-30)."""
import os
import unittest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fronts.desktop.meeting_transcript_panel import (
    _word_spans_for, UtteranceListModel, TranscriptPanel
)
from whisper_core.meeting.postprocess import Utterance


class WordSpansTests(unittest.TestCase):
    def test_word_spans_uniform_fallback(self):
        # 2 слова на 2 секунди (0 - 2000 мс)
        spans = _word_spans_for("Слава Україні", 0.0, 2.0)
        self.assertEqual(len(spans), 2)
        # span 0: (0, 5, 0, 1000)
        self.assertEqual(spans[0][:2], (0, 5))
        self.assertEqual(spans[0][2:], (0, 1000))
        # span 1: (6, 7, 1000, 2000)
        self.assertEqual(spans[1][:2], (6, 7))
        self.assertEqual(spans[1][2:], (1000, 2000))

    def test_word_spans_with_exact_timestamps(self):
        words = [
            {"word": "Слава", "start": 0.2, "end": 0.8},
            {"word": "Україні", "start": 1.1, "end": 1.9},
        ]
        spans = _word_spans_for("Слава Україні", 0.0, 2.0, words=words)
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0][2:], (200, 800))
        self.assertEqual(spans[1][2:], (1100, 1900))


class UtteranceListModelWordCharTests(unittest.TestCase):
    def test_word_start_ms_for_char(self):
        u = Utterance(0.0, 2.0, "me", "Слава Україні")
        model = UtteranceListModel([u])
        # У слові "Слава" (char 0..4)
        self.assertEqual(model.word_start_ms_for_char(0, 0), 0)
        self.assertEqual(model.word_start_ms_for_char(0, 3), 0)
        # Пробіл між словами (char 5)
        self.assertIsNone(model.word_start_ms_for_char(0, 5))
        # У слові "Україні" (char 6..12)
        self.assertEqual(model.word_start_ms_for_char(0, 6), 1000)
        self.assertEqual(model.word_start_ms_for_char(0, 10), 1000)


class TranscriptPanelSeekWordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_click_seeks_to_word_or_fallback(self):
        u = Utterance(0.0, 2.0, "me", "Слава Україні")
        panel = TranscriptPanel([u])
        # fallback when pos is None
        fallback_ms = panel._word_seek_ms(panel._view.model().index(0, 0), None, 0, u)
        self.assertEqual(fallback_ms, 0)


if __name__ == "__main__":
    unittest.main()
