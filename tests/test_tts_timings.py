"""Хвиля 1: координатні хелпери таймінгів — document anchor + UTF-16."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.tts import timings as T


class TestAnchor(unittest.TestCase):
    def test_absolute_codepoint_from_zero(self):
        self.assertEqual(T.absolute_codepoint(0, 5), 5)

    def test_absolute_codepoint_with_selection_offset(self):
        # виділення не з позиції 0 → зсув додається (§8.2)
        self.assertEqual(T.absolute_codepoint(100, 5), 105)


class TestUtf16(unittest.TestCase):
    def test_ascii_offset_equals_codepoints(self):
        self.assertEqual(T.codepoint_to_utf16("hello world", 5), 5)

    def test_astral_emoji_shifts_utf16(self):
        # emoji — сурогатна пара (2 UTF-16 units, 1 code point). Слово ПІСЛЯ нього
        # зсувається в UTF-16, але не в code points.
        text = "👍 слово"
        cp = text.index("слово")               # code-point-позиція слова = 2
        self.assertEqual(cp, 2)
        u16 = T.codepoint_to_utf16(text, cp)
        self.assertEqual(u16, 3)               # 👍(2) + пробіл(1) = 3 UTF-16 units

    def test_ukrainian_bmp_no_shift(self):
        # кирилиця — BMP (1 UTF-16 unit кожна), зсуву немає
        self.assertEqual(T.codepoint_to_utf16("привіт", 6), 6)

    def test_utf16_length(self):
        self.assertEqual(T.utf16_length("привіт"), 6)
        self.assertEqual(T.utf16_length("👍"), 2)


from whisper_core.tts.normalize import normalize


class TestWordTimings(unittest.TestCase):
    def test_build_word_timings_basic(self):
        # 3 токени, 1 на слово, hop=25мс; тривалості 2,4,2 кадри
        wt = T.build_word_timings([2, 4, 2], [0, 1, 2], 25.0,
                                  [(0, 5), (6, 11), (12, 15)])
        self.assertEqual(len(wt), 3)
        self.assertEqual(wt[0]["start_ms"], 0)
        self.assertEqual(wt[0]["end_ms"], 50)      # 2*25
        self.assertEqual(wt[1]["start_ms"], 50)
        self.assertEqual(wt[1]["end_ms"], 150)     # 50 + 4*25
        self.assertEqual(wt[2]["raw_start"], 12)

    def test_multi_token_word_extends_end(self):
        # 2 токени одного слова (word 0), 1 токен слова 1
        wt = T.build_word_timings([2, 2, 3], [0, 0, 1], 25.0, [(0, 4), (5, 9)])
        self.assertEqual(len(wt), 2)
        self.assertEqual(wt[0]["start_ms"], 0)
        self.assertEqual(wt[0]["end_ms"], 100)     # два токени по 2 кадри
        self.assertEqual(wt[1]["start_ms"], 100)

    def test_source_start_cp_offsets_raw(self):
        wt = T.build_word_timings([1], [0], 25.0, [(3, 8)], source_start_cp=100)
        self.assertEqual(wt[0]["raw_start"], 103)
        self.assertEqual(wt[0]["raw_end"], 108)

    def test_active_word_index(self):
        wt = [{"start_ms": 0}, {"start_ms": 50}, {"start_ms": 150}]
        self.assertEqual(T.active_word_index(wt, 0), 0)
        self.assertEqual(T.active_word_index(wt, 49), 0)
        self.assertEqual(T.active_word_index(wt, 50), 1)
        self.assertEqual(T.active_word_index(wt, 200), 2)

    def test_active_word_before_first(self):
        self.assertEqual(T.active_word_index([{"start_ms": 100}], 50), -1)


class TestSpanMap(unittest.TestCase):
    def test_expanded_number_both_words_one_raw(self):
        # «23» → «двадцять три»: обидва нормалізовані слова → один сирий діапазон
        nr = normalize("23")
        spans = T.normalized_word_raw_spans(nr)
        self.assertEqual(len(spans), 2)            # двадцять, три
        self.assertEqual(spans[0], (0, 2))
        self.assertEqual(spans[1], (0, 2))

    def test_plain_words_map_directly(self):
        nr = normalize("привіт світ")
        spans = T.normalized_word_raw_spans(nr)
        self.assertEqual(spans[0], (0, 6))
        self.assertEqual(spans[1][0], 7)           # «світ» після пробілу


class TestMergeAndNav(unittest.TestCase):
    def test_merge_sentences_shifts(self):
        s0 = [{"word_index": 0, "start_ms": 0, "end_ms": 100,
               "raw_start": 0, "raw_end": 5}]
        s1 = [{"word_index": 0, "start_ms": 0, "end_ms": 80,
               "raw_start": 6, "raw_end": 10}]
        g, starts = T.merge_sentences([s0, s1], [200, 150])
        self.assertEqual(starts, [0, 200])
        self.assertEqual(g[0]["start_ms"], 0)
        self.assertEqual(g[1]["start_ms"], 200)    # друге речення зсунуте
        self.assertEqual(g[1]["end_ms"], 280)

    def test_sentence_navigation(self):
        starts = [0, 200, 500]
        self.assertEqual(T.sentence_at(starts, 250), 1)
        self.assertEqual(T.next_sentence_start(starts, 250), 500)
        self.assertEqual(T.prev_sentence_start(starts, 550), 200)
        self.assertIsNone(T.next_sentence_start(starts, 600))


class TestCacheKey(unittest.TestCase):
    def test_key_differs_on_voice(self):
        a = T.cache_key("текст", "v1", "r1", "e1", "l1", 0)
        b = T.cache_key("текст", "v2", "r1", "e1", "l1", 0)
        self.assertNotEqual(a, b)

    def test_key_differs_on_lexicon(self):
        a = T.cache_key("текст", "v1", "r1", "e1", "l1", 0)
        b = T.cache_key("текст", "v1", "r1", "e1", "l2", 0)
        self.assertNotEqual(a, b)

    def test_key_stable(self):
        a = T.cache_key("текст", "v1", "r1", "e1", "l1", 0)
        b = T.cache_key("текст", "v1", "r1", "e1", "l1", 0)
        self.assertEqual(a, b)


class TestGoldenViaWorker(unittest.TestCase):
    """Golden через FakeBackend (відомі тривалості): span-map raw→слова наскрізь,
    14:30 / абревіатура / апостроф / дефіс / emoji / кілька речень / source_start_cp."""

    def _synth(self, text, *, source_start_cp=0):
        import os
        import tempfile
        from whisper_core.tts import worker as W
        from whisper_core.tts.engines.fake import FakeTtsEngine
        eng = FakeTtsEngine()
        eng.load("")
        events = []
        d = tempfile.mkdtemp(prefix="karaoke-")
        W.synthesize_stream(
            eng, {"id": "g", "text": text, "wav_dir": d, "want_timings": True,
                  "source_start_cp": source_start_cp},
            events.append, lambda: False)
        chunks = [e for e in events if e["type"] == "chunk_ready"]
        return chunks

    def test_time_expands_all_words_one_raw(self):
        chunks = self._synth("14:30")
        wt = chunks[0]["timings"]
        self.assertTrue(wt)
        # усі слова «чотирнадцята година тридцять хвилин» → один сирий діапазон 14:30
        for w in wt:
            self.assertEqual(w["raw_start"], 0)
            self.assertEqual(w["raw_end"], 5)

    def test_worker_returns_fragment_relative(self):
        # §3.2 (ревізія): worker НЕ бейкає editor-anchor (source_start_cp) — вертає
        # FRAGMENT-relative координати; editor-anchor додає БАТЬКО. Тож попри
        # source_start_cp=100 перше слово фрагмента має raw_start=0.
        chunks = self._synth("привіт", source_start_cp=100)
        wt = chunks[0]["timings"]
        self.assertEqual(wt[0]["raw_start"], 0)
        self.assertEqual(wt[0]["raw_end"], 6)

    def test_multi_sentence_offsets(self):
        chunks = self._synth("Перше велике речення тут. Друге велике речення там.")
        self.assertGreaterEqual(len(chunks), 2)
        # друге речення: raw_start другого чанку > довжини першого речення
        wt2 = chunks[1]["timings"]
        self.assertTrue(wt2)
        self.assertGreater(wt2[0]["raw_start"], 20)

    def test_abbreviation_span_map(self):
        # §11.2: «78 ТОВ» через span-map (не лише normalize). «78»→2 слова raw(0,2);
        # «ТОВ»→побуквено raw(3,6). Усі таймінги мапляться в правильні сирі діапазони.
        chunks = self._synth("78 ТОВ")
        wt = chunks[0]["timings"]
        self.assertTrue(wt)
        num_words = [w for w in wt if w["raw_start"] == 0]
        abbr_words = [w for w in wt if w["raw_start"] == 3]
        self.assertTrue(num_words)               # «78» → слова з raw(0,2)
        self.assertTrue(abbr_words)              # «ТОВ» → слова з raw(3,6)
        for w in num_words:
            self.assertEqual(w["raw_end"], 2)
        for w in abbr_words:
            self.assertEqual(w["raw_end"], 6)

    def test_first_chunk_capped_on_long_text_want_timings(self):
        # БЛОКЕР суду: TTFS<0.5c для playback (want_timings=True). Довгий текст без
        # крапок (перелік/адреса, 40 слів) → ПЕРШИЙ чанк ≤ cap слів, не один шматок.
        from whisper_core.tts import FIRST_CHUNK_MAX_WORDS
        long = " ".join(f"пункт{i}" for i in range(40))
        chunks = self._synth(long)
        self.assertGreaterEqual(len(chunks), 2)  # порізано, не один чанк тиші
        # перший чанк озвучив ≤ cap слів (перший звук швидкий)
        first_words = len(chunks[0]["normalized_text"].replace("[fake-tts]", "").split())
        self.assertLessEqual(first_words, FIRST_CHUNK_MAX_WORDS + 1)

    def test_hyphen_and_apostrophe_present(self):
        chunks = self._synth("девʼятий військово-технічний")
        wt = chunks[0]["timings"]
        self.assertTrue(wt)                        # без краху на апострофі/дефісі

    def test_emoji_raw_codepoints(self):
        # emoji — 1 code point; слово після нього має code-point raw_start=2
        chunks = self._synth("👍 слово")
        wt = chunks[0]["timings"]
        word = [w for w in wt if w["raw_start"] >= 2]
        self.assertTrue(word)


class TestRealEngineGolden(unittest.TestCase):
    """Golden з РЕАЛЬНОГО рушія (§11 Хвиля 2) — скіп без torch/моделі. Коли стек
    має torch + завантажений StyleTTS2-голос, перевіряє наскрізний span-map живцем.
    Без torch у реліз-venv тест скіпається (як frozen-exe) — чесно, не фальшива зелень."""

    def setUp(self):
        import importlib.util
        if importlib.util.find_spec("torch") is None:
            self.skipTest("torch відсутній — real-engine golden лише зі стеком воркера")
        import os
        from whisper_core import paths
        vd = paths.tts_voices_dir() / "styletts2_ua"
        if not vd.exists():
            self.skipTest("StyleTTS2-голос не завантажено — golden лише з моделлю")
        self._voice_dir = str(vd)

    def test_span_map_sums_to_wav(self):
        from whisper_core.tts.engines.styletts2 import StyleTTS2Engine
        from whisper_core.tts import timings as T
        eng = StyleTTS2Engine()
        eng.load(self._voice_dir)
        norm = normalize("14:30")
        res = eng.synthesize(norm.text, speed=1.0, want_timings=True)
        self.assertTrue(res.token_durations)
        wrs = T.normalized_word_raw_spans(norm)
        wt = T.build_word_timings(res.token_durations, res.phoneme_to_word,
                                  res.frame_hop_ms, wrs)
        self.assertTrue(wt)
        # сума тривалостей × hop ≈ довжина WAV (допуск на hifigan-артефакт)
        import numpy as np
        wav_ms = len(np.asarray(res.wav).reshape(-1)) / res.sample_rate * 1000.0
        end_ms = max(w["end_ms"] for w in wt)
        self.assertLess(abs(end_ms - wav_ms), 400)


if __name__ == "__main__":
    unittest.main()
