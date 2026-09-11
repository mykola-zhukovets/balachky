"""feature/stt-sherpa-parakeet: рушій SherpaEngine з контрактом Engine.

Рушій тестується з фейковим recognizer і фейковим VAD — без моделі на диску й
без sherpa-onnx у процесі. Перевіряємо саме контракт, який споживає решта
програми: (raw, final, duration_s, words, segments[, timed_words]), глосарій,
нарізку на чанки, зсув часових позначок, скасування, порожнє аудіо, фабрику.
"""
import inspect
import os
import re
import types
import unittest
from unittest.mock import patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from whisper_core import stt_sherpa
from whisper_core.engine import (ModelRevisionUnavailable, TranscriptionCancelled,
                                 make_engine, Engine)
from whisper_core.terms import Terms

SR = 16000


class _FakeStream:
    def __init__(self):
        self.samples = None
        self.result = None

    def accept_waveform(self, sample_rate, samples):
        assert sample_rate == SR
        self.samples = np.asarray(samples)


class _FakeRecognizer:
    """Скриптований recognizer: черга відповідей (text, tokens, timestamps)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create_stream(self):
        return _FakeStream()

    def decode_stream(self, stream):
        self.calls.append(len(stream.samples))
        text, tokens, stamps = self.responses.pop(0)
        stream.result = types.SimpleNamespace(text=text, tokens=tokens, timestamps=stamps)


def _cfg(**over):
    base = dict(model_name="parakeet-tdt-0.6b-v3", model_dir="", device="cpu",
                compute_type="int8", language="uk", beam_size=5,
                vad_threshold=0.5, vad_min_speech_ms=250, vad_min_silence_ms=500,
                no_repeat_ngram_size=3)
    base.update(over)
    return types.SimpleNamespace(**base)


def _engine(responses, vad):
    rec = _FakeRecognizer(responses)
    eng = stt_sherpa.SherpaEngine(_cfg(), recognizer_factory=lambda paths, cfg: rec,
                                  vad_fn=vad)
    return eng, rec


class ContractTests(unittest.TestCase):
    def test_single_chunk_words_segments_and_glossary(self):
        audio = np.zeros(SR * 2, dtype=np.float32)
        eng, rec = _engine(
            [("привіт свит", ["▁при", "віт", "▁свит"], [0.10, 0.30, 0.50])],
            vad=lambda a, cfg: [(0, len(a))])
        terms = Terms(pattern=re.compile(r"свит"), variant_map={"свит": "світ"})
        raw, final, dur, words, segs = eng.transcribe(audio, terms)
        self.assertEqual(raw, "привіт свит")
        self.assertAlmostEqual(dur, 2.0)
        self.assertEqual([w for w, _p in words], ["привіт", "свит"])
        self.assertTrue(all(p == 1.0 for _w, p in words))     # ймовірностей модель не дає
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0][0], 0.0)
        self.assertAlmostEqual(segs[0][1], 2.0)
        self.assertEqual(rec.calls, [SR * 2])
        self.assertTrue(eng.is_available)
        self.assertEqual(final, "привіт світ")           # глосарій застосовано до final
        self.assertEqual(segs[0][2], "привіт світ")      # і до тексту сегмента

    def test_word_timestamps_offset_by_chunk_start(self):
        audio = np.zeros(SR * 12, dtype=np.float32)
        eng, rec = _engine(
            [("раз два", ["▁раз", "▁два"], [0.0, 0.5]),
             ("три", ["▁три"], [0.2])],
            vad=lambda a, cfg: [(0, SR), (10 * SR, 11 * SR)])   # пауза 9 с — окремі чанки
        result = eng.transcribe(audio, include_word_timestamps=True)
        self.assertEqual(len(result), 6)
        raw, final, dur, words, segs, timed = result
        self.assertEqual(raw, "раз два три")
        self.assertEqual([s[:2] for s in segs], [(0.0, 1.0), (10.0, 11.0)])
        self.assertEqual([t["word"] for t in timed], ["раз", "два", "три"])
        self.assertAlmostEqual(timed[0]["start"], 0.0)
        self.assertAlmostEqual(timed[0]["end"], 0.5)        # до початку наступного слова
        self.assertAlmostEqual(timed[1]["end"], 1.0)        # останнє слово чанка — до кінця чанка
        self.assertAlmostEqual(timed[2]["start"], 10.2)     # зсув на початок другого чанка
        self.assertAlmostEqual(timed[2]["end"], 11.0)
        self.assertEqual(rec.calls, [SR, SR])

    def test_short_gaps_merged_into_one_chunk_long_split(self):
        audio = np.zeros(SR * 50, dtype=np.float32)
        eng, rec = _engine(
            [("а", ["▁а"], [0.0]), ("б", ["▁б"], [0.0])],
            vad=lambda a, cfg: [(0, 10 * SR), (12 * SR, 20 * SR), (35 * SR, 40 * SR)])
        raw, *_ = eng.transcribe(audio)
        # 0-10 і 12-20 разом займають 20 с (<= 30) — один чанк; 35-40 — окремий
        self.assertEqual(rec.calls, [20 * SR, 5 * SR])
        self.assertEqual(raw, "а б")

    def test_oversized_speech_split_into_windows(self):
        audio = np.zeros(SR * 70, dtype=np.float32)
        eng, rec = _engine(
            [("а", ["▁а"], [0.0])] * 3,
            vad=lambda a, cfg: [(0, 70 * SR)])
        eng.transcribe(audio)
        self.assertEqual(rec.calls, [30 * SR, 30 * SR, 10 * SR])

    def test_cancel_between_chunks(self):
        audio = np.zeros(SR * 12, dtype=np.float32)
        eng, rec = _engine([("а", ["▁а"], [0.0]), ("б", ["▁б"], [0.0])],
                           vad=lambda a, cfg: [(0, SR), (10 * SR, 11 * SR)])
        flags = iter([False, True])
        with self.assertRaises(TranscriptionCancelled):
            eng.transcribe(audio, should_cancel=lambda: next(flags))
        self.assertEqual(rec.calls, [SR])          # другий чанк не декодувався

    def test_no_speech_returns_empty_result(self):
        audio = np.zeros(SR * 3, dtype=np.float32)
        eng, rec = _engine([], vad=lambda a, cfg: [])
        raw, final, dur, words, segs = eng.transcribe(audio)
        self.assertEqual((raw, final, words, segs), ("", "", [], []))
        self.assertAlmostEqual(dur, 3.0)
        self.assertEqual(rec.calls, [])

    def test_real_sherpa_token_format_space_prefixed(self):
        """Реальний sherpa-onnx (перевірено живим дампом 07.09) віддає межу слова
        ЛІТЕРНИМ пробілом, не ▁: " Якщо", " інвестує", " велика", ..., пунктуація
        окремим токеном без пробілу. Слова мають розклеїтись саме за цим."""
        audio = np.zeros(SR * 2, dtype=np.float32)
        eng, _ = _engine(
            [("Якщо інвестує велика корпорація.",
              [" Якщо", " інвест", "ує", " велика", " корпорація", "."],
              [0.1, 0.4, 0.5, 0.8, 1.1, 1.5])],
            vad=lambda a, cfg: [(0, len(a))])
        raw, final, dur, words, segs, timed = eng.transcribe(audio, include_word_timestamps=True)
        self.assertEqual([w for w, _ in words], ["Якщо", "інвестує", "велика", "корпорація."])
        self.assertEqual([t["word"] for t in timed], ["Якщо", "інвестує", "велика", "корпорація."])
        self.assertAlmostEqual(timed[1]["start"], 0.4)
        self.assertAlmostEqual(timed[1]["end"], 0.8)

    def test_special_tokens_ignored_and_punctuation_attached(self):
        audio = np.zeros(SR, dtype=np.float32)
        eng, _ = _engine(
            [("Так, добре.", ["<|uk|>", "▁Так", ",", "▁добре", "."], [0.0, 0.1, 0.2, 0.3, 0.4])],
            vad=lambda a, cfg: [(0, SR)])
        raw, final, dur, words, segs = eng.transcribe(audio)
        self.assertEqual([w for w, _ in words], ["Так,", "добре."])
        self.assertEqual(raw, "Так, добре.")

    def test_path_input_is_decoded_to_16k(self):
        eng, rec = _engine([("а", ["▁а"], [0.0])], vad=lambda a, cfg: [(0, len(a))])
        with patch("whisper_core.stt_sherpa._decode_audio",
                   return_value=np.zeros(SR, dtype=np.float32)) as dec:
            raw, *_ = eng.transcribe("C:/tmp/some.wav")
        dec.assert_called_once()
        self.assertEqual(dec.call_args.args[0], "C:/tmp/some.wav")
        self.assertEqual(raw, "а")

    def test_close_is_idempotent(self):
        eng, _ = _engine([], vad=lambda a, cfg: [])
        eng.close()
        eng.close()
        self.assertIsNone(eng._recognizer)

    def test_missing_package_raises_typed_error(self):
        with patch("whisper_core.stt_sherpa_models.models_available", return_value=False):
            with self.assertRaises(ModelRevisionUnavailable) as ctx:
                stt_sherpa.SherpaEngine(_cfg())
        self.assertEqual(ctx.exception.model_name, "parakeet-tdt-0.6b-v3")
        self.assertFalse(ctx.exception.has_other_revision)


class FactoryTests(unittest.TestCase):
    def test_factory_dispatches_by_preset_kind(self):
        with patch("whisper_core.stt_sherpa.SherpaEngine") as sherpa_cls, \
                patch("whisper_core.engine.Engine") as whisper_cls:
            make_engine(_cfg(model_name="parakeet-tdt-0.6b-v3"))
            sherpa_cls.assert_called_once()
            whisper_cls.assert_not_called()
        with patch("whisper_core.stt_sherpa.SherpaEngine") as sherpa_cls, \
                patch("whisper_core.engine.Engine") as whisper_cls:
            make_engine(_cfg(model_name="large-v3-turbo"), revision_override="abc")
            whisper_cls.assert_called_once()
            self.assertEqual(whisper_cls.call_args.kwargs.get("revision_override"), "abc")
            sherpa_cls.assert_not_called()

    def test_factory_treats_custom_model_as_whisper(self):
        with patch("whisper_core.engine.Engine") as whisper_cls:
            make_engine(_cfg(model_name="owner/custom-ct2-model"))
            whisper_cls.assert_called_once()

    def test_engine_class_still_importable_for_legacy_callers(self):
        # "callable(Engine)" — тавтологія: будь-який клас є callable незалежно
        # від того, чи має він очікуваний контракт. Справжній контракт —
        # обидва рушії мають ОДНАКОВУ сигнатуру transcribe (імена, kind,
        # значення за замовчуванням), бо make_engine підставляє їх один
        # замість одного.
        # Анотації в одному модулі рядкові (from __future__ import annotations),
        # в іншому обчислені, тож звіряємо саме контракт: імена, вид, дефолти.
        def shape(func):
            return [(p.name, p.kind, p.default)
                    for p in inspect.signature(func).parameters.values()]
        self.assertEqual(shape(stt_sherpa.SherpaEngine.transcribe), shape(Engine.transcribe))


if __name__ == "__main__":
    unittest.main()
