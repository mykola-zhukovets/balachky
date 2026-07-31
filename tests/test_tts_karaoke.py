"""Хвиля 2: караоке-підсвічування (§8.5, §11.2).

setExtraSelections, UTF-16 на Qt-межі (emoji), document anchor (source_start_cp),
перемикач слово/речення, зупинка при редагуванні, стійкість до зуму/шрифту."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTextEdit

from fronts.desktop.tts_karaoke import (GRANULARITY_SENTENCE, GRANULARITY_WORD,
                                        KaraokeHighlighter)

_APP = QApplication.instance() or QApplication([])


def _editor(text):
    e = QTextEdit()
    e.setPlainText(text)
    return e


class TestWordHighlight(unittest.TestCase):
    def test_word_utf16_range(self):
        e = _editor("привіт світло тут")
        h = KaraokeHighlighter(e)
        wt = [{"start_ms": 0, "raw_start": 0, "raw_end": 6},
              {"start_ms": 50, "raw_start": 7, "raw_end": 13}]
        h.start(wt)
        h.update(50)
        self.assertEqual(h.current_selection_utf16(), (7, 13))   # «світло»

    def test_emoji_shifts_utf16(self):
        e = _editor("👍 слово")
        h = KaraokeHighlighter(e)
        # code-point raw 2..7 («слово») → UTF-16 3..8 (👍 = 2 units)
        h.start([{"start_ms": 0, "raw_start": 2, "raw_end": 7}])
        h.update(0)
        self.assertEqual(h.current_selection_utf16(), (3, 8))

    def test_source_start_cp_absolute(self):
        # редактор має префікс; слово з абсолютним raw (як зробив worker) лягає точно
        e = _editor("ПРЕФІКС: привіт")
        h = KaraokeHighlighter(e)
        h.start([{"start_ms": 0, "raw_start": 9, "raw_end": 15}])   # «привіт»
        h.update(0)
        start, end = h.current_selection_utf16()
        cur_text = e.toPlainText()[start:end]
        self.assertEqual(cur_text, "привіт")

    def test_advances_between_words(self):
        e = _editor("одне друге третє")
        h = KaraokeHighlighter(e)
        wt = [{"start_ms": 0, "raw_start": 0, "raw_end": 4},
              {"start_ms": 100, "raw_start": 5, "raw_end": 10},
              {"start_ms": 200, "raw_start": 11, "raw_end": 16}]
        h.start(wt)
        h.update(0)
        self.assertEqual(h.current_selection_utf16(), (0, 4))
        h.update(150)
        self.assertEqual(h.current_selection_utf16(), (5, 10))


class TestSentenceGranularity(unittest.TestCase):
    def test_sentence_highlights_whole(self):
        e = _editor("Перше речення. Друге речення.")
        h = KaraokeHighlighter(e)
        # два речення; слова тегнуті ТЕКСТОВОЮ належністю (як merge_sentences)
        wt = [{"start_ms": 0, "raw_start": 0, "raw_end": 5, "sentence": 0},
              {"start_ms": 100, "raw_start": 6, "raw_end": 13, "sentence": 0},
              {"start_ms": 300, "raw_start": 15, "raw_end": 20, "sentence": 1},
              {"start_ms": 400, "raw_start": 21, "raw_end": 28, "sentence": 1}]
        starts = [0, 300]
        h.set_granularity(GRANULARITY_SENTENCE)
        h.start(wt, starts)
        h.update(100)
        s, en = h.current_selection_utf16()
        self.assertEqual((s, en), (0, 13))       # усе перше речення
        h.update(400)
        s2, en2 = h.current_selection_utf16()
        self.assertEqual((s2, en2), (15, 28))    # усе друге речення

    def test_boundary_word_stays_in_own_sentence(self):
        # межове «тут.» перед крапкою: його start_ms пізній (близько до наступного
        # sentence_start через hifigan-дрейф), АЛЕ текстово воно в реченні 0. За
        # ms-вікном воно втекло б у речення 1; за членством — лишається в 0 (§8.5).
        e = _editor("Перше слово тут. Друге.")
        h = KaraokeHighlighter(e)
        wt = [{"start_ms": 0, "raw_start": 0, "raw_end": 5, "sentence": 0},
              {"start_ms": 100, "raw_start": 6, "raw_end": 11, "sentence": 0},
              # «тут» start_ms=295 — майже на межі речення 1 (300), але sentence=0
              {"start_ms": 295, "raw_start": 12, "raw_end": 15, "sentence": 0},
              {"start_ms": 300, "raw_start": 17, "raw_end": 22, "sentence": 1}]
        starts = [0, 300]
        h.set_granularity(GRANULARITY_SENTENCE)
        h.start(wt, starts)
        h.update(100)                            # грає речення 0
        s, en = h.current_selection_utf16()
        self.assertEqual((s, en), (0, 15))       # «тут» ВКЛЮЧЕНО у речення 0 (до крапки)
        h.update(300)                            # грає речення 1
        s2, en2 = h.current_selection_utf16()
        self.assertEqual((s2, en2), (17, 22))    # лише «Друге», без витоку «тут»


class TestRevisionStop(unittest.TestCase):
    def test_edit_during_playback_stops(self):
        e = _editor("привіт світло")
        stopped = []
        h = KaraokeHighlighter(e, on_stopped=lambda: stopped.append(True))
        h.start([{"start_ms": 0, "raw_start": 0, "raw_end": 6}])
        h.update(0)
        self.assertTrue(h.is_active())
        e.setPlainText("зовсім інший текст")     # редагування під час відтворення
        h.update(10)
        self.assertFalse(h.is_active())          # зупинено (raw-діапазони застаріли)
        self.assertTrue(stopped)
        self.assertIsNone(h.current_selection_utf16())

    def test_font_change_keeps_char_ranges(self):
        # межі в СИМВОЛАХ, не пікселях → зміна шрифту не рухає діапазон (баг Thorium)
        e = _editor("привіт світло тут")
        h = KaraokeHighlighter(e)
        wt = [{"start_ms": 0, "raw_start": 7, "raw_end": 13}]
        h.start(wt)
        h.update(0)
        before = h.current_selection_utf16()
        f = e.font(); f.setPointSize(f.pointSize() + 6); e.setFont(f)
        h._last_key = None
        h.update(0)
        self.assertEqual(h.current_selection_utf16(), before)   # ті самі символи


class TestLiveStreamIntegration(unittest.TestCase):
    """БЛОКЕР 1 рецензії: ЖИВА зв'язка controller→panel→highlighter. Наявний
    test-компонент тестує highlighter ізольовано з ручними даними; тут — реальний
    потік чанків з контролера в панель: nav ◀/▶ стрибає між реченнями, гранулярність
    «речення» підсвічує ціле речення, _sentence_starts не порожній."""

    def _run_controller_capture(self):
        # контролер із FakeSidecar, що дає 2 чанки з per-chunk word_timings
        import os as _os
        import tempfile as _tf
        from types import SimpleNamespace
        from fronts.desktop.tts_controller import TtsController

        class TwoChunkSidecar:
            def load_voice(self, *a, **k):
                pass

            def synthesize_stream(self, *, text, voice_id, wav_dir, on_event=None, **k):
                from whisper_core.tts import (MSG_ACCEPTED, MSG_CHUNK_READY,
                                              MSG_RESULT)
                if on_event:
                    on_event({"type": MSG_ACCEPTED, "id": "r"})
                # речення 0: слова raw 0..5, 6..13 (media-ms 0,100); речення 1: 15..20,21..28
                data = [
                    ([{"word_index": 0, "raw_start": 0, "raw_end": 5,
                       "start_ms": 0, "end_ms": 90},
                      {"word_index": 1, "raw_start": 6, "raw_end": 13,
                       "start_ms": 100, "end_ms": 200}]),
                    ([{"word_index": 0, "raw_start": 15, "raw_end": 20,
                       "start_ms": 0, "end_ms": 90},
                      {"word_index": 1, "raw_start": 21, "raw_end": 28,
                       "start_ms": 100, "end_ms": 200}]),
                ]
                for i, wt in enumerate(data):
                    p = _os.path.join(wav_dir, f"s{i}.wav")
                    with open(p, "wb") as f:
                        f.write(b"\x00" * 8)
                    if on_event:
                        on_event({"type": MSG_CHUNK_READY, "wav_path": p,
                                  "timings": wt})
                if on_event:
                    on_event({"type": MSG_RESULT})
                return "r"

        class FakeTemp:
            def __init__(self):
                self.path = _tf.mkdtemp(prefix="live-")

            def cleanup(self):
                import shutil
                shutil.rmtree(self.path, ignore_errors=True)
                return True

        rv = SimpleNamespace(id="v", engine_kind="fake",
                             manifest_path=_tf.mkdtemp(), languages=("uk",),
                             available=lambda: True,
                             integrity_available=lambda: True)
        cfg = SimpleNamespace(tts_enabled=True, tts_voice_uk="v",
                              tts_voice_en="v", ui_language="uk")
        captured = []
        ctrl = TtsController(
            cfg=cfg, coordinator=SimpleNamespace(acquire_tts=lambda: object()),
            resolve_voice=lambda vid, lang: rv,
            sidecar_factory=lambda: TwoChunkSidecar(), temp_factory=FakeTemp,
            combine=lambda w, o: o,
            on_chunk_playable=lambda tok, p, t, first: captured.append((p, t, first)))
        ctrl.play_text("Перше речення тут. Друге речення знову.")
        w = ctrl._worker
        if w:
            w.join(5)
        return captured

    def test_stream_fills_nav_and_sentence(self):
        captured = self._run_controller_capture()
        self.assertGreaterEqual(len(captured), 2)   # 2 речення з контролера
        # згодовуємо захоплені чанки в РЕАЛЬНУ панель (як робить app-сигнал у GUI-потоці)
        from fronts.desktop.tts_panel import ListenPanel
        panel = ListenPanel(None, has_voice=True,
                            text="Перше речення тут. Друге речення знову.")
        for p, t, first in captured:
            panel.enqueue_chunk(p, t, first)
        # _sentence_starts НЕ порожній (жива зв'язка, не visual_gate-синтетика)
        self.assertTrue(panel._sentence_starts)
        self.assertEqual(len(panel._chunks), 2)
        self.assertEqual(panel.current_sentence_index(), 0)
        # ◀/▶ реально стрибають між реченнями
        panel._next_sentence()
        self.assertEqual(panel.current_sentence_index(), 1)
        panel._prev_sentence()
        self.assertEqual(panel.current_sentence_index(), 0)
        # гранулярність «речення»: підсвічує ЦІЛЕ речення 0 (слова з членством sentence=0)
        panel._granularity = GRANULARITY_SENTENCE
        hl = panel.highlighter()
        hl.set_granularity(GRANULARITY_SENTENCE)
        hl.start(panel._chunks[0][1], [0])
        hl.update(0)
        sel = hl.current_selection_utf16()
        self.assertEqual(sel, (0, 13))              # усе перше речення, не порожньо


class TestReverseResume(unittest.TestCase):
    """Хвиля 5 §9.2: продовження з того ж речення після правки (reverse-resume)."""

    def test_panel_resumes_at_saved_sentence(self):
        from fronts.desktop.tts_panel import ListenPanel
        panel = ListenPanel(None, has_voice=True, text="Одне. Друге. Третє.")
        panel.set_resume_index(1)                # продовжити з речення 1
        # стрімляться 3 чанки; playback має початися з індексу 1, не 0
        panel.enqueue_chunk("s0.wav", [], True)
        panel.enqueue_chunk("s1.wav", [], False)
        panel.enqueue_chunk("s2.wav", [], False)
        self.assertEqual(panel.current_sentence_index(), 1)   # почали з речення 1
        self.assertEqual(len(panel._chunks), 3)               # усі збережені для ◀

    def test_normal_start_at_zero(self):
        from fronts.desktop.tts_panel import ListenPanel
        panel = ListenPanel(None, has_voice=True, text="Одне. Друге.")
        panel.enqueue_chunk("s0.wav", [], True)
        panel.enqueue_chunk("s1.wav", [], False)
        self.assertEqual(panel.current_sentence_index(), 0)   # норма — з початку


class TestPanelDoubleClickFix(unittest.TestCase):
    """Хвіст рецензії хв.4: double-click по слову в караоке-панелі → діалог вимови з
    передзаповненим словом (не лише ручний fix_word)."""

    def test_double_click_routes_to_on_fix_word(self):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from fronts.desktop.tts_panel import ListenPanel
        fixed = []
        panel = ListenPanel(None, has_voice=True, text="Коростень тут",
                            on_fix_word=lambda w: fixed.append(w))
        vp = panel._editor.viewport()
        # синтетичний double-click на початку тексту (перше слово)
        ev = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(3, 3),
                         Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier)
        handled = panel.eventFilter(vp, ev)
        self.assertTrue(handled)                 # подія оброблена
        self.assertTrue(fixed)                   # on_fix_word викликано
        self.assertEqual(fixed[0], "Коростень")  # слово під курсором

    def test_fix_word_public(self):
        from fronts.desktop.tts_panel import ListenPanel
        fixed = []
        panel = ListenPanel(None, has_voice=True, text="замок",
                            on_fix_word=lambda w: fixed.append(w))
        panel.fix_word("замок")
        self.assertEqual(fixed, ["замок"])


if __name__ == "__main__":
    unittest.main()
