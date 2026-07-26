"""Юніти неруйнівного WAV-редактора: без Qt, моделі й аудіопристроїв."""
import tempfile
import unittest

import numpy as np

from whisper_core import audioedit


class RangeEditTests(unittest.TestCase):
    def test_trim_and_cut_keep_exact_sample_ranges(self):
        audio = np.arange(100, dtype=np.float32)[:, None] / 100.0
        trimmed = audioedit.trim_to_range(audio, 10, 2.0, 5.0)
        cut = audioedit.cut_range(audio, 10, 2.0, 5.0)
        np.testing.assert_array_equal(trimmed, audio[20:50])
        np.testing.assert_array_equal(cut, np.concatenate((audio[:20], audio[50:])))
        self.assertEqual(len(trimmed) / 10, 3.0)

    def test_range_is_queued_as_separate_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = audioedit.write_wav(
                f"{tmp}/source.wav", np.arange(80, dtype=np.float32)[:, None] / 100.0, 20)
            queued = []
            out = audioedit.queue_range(source, f"{tmp}/selection.wav", 1.0, 2.5, queued.append)
            audio, rate = audioedit.read_wav(out)
            self.assertEqual(queued, [str(out)])
            self.assertEqual(rate, 20)
            self.assertEqual(len(audio), 30)
            np.testing.assert_allclose(audio[:, 0], np.arange(20, 50) / 100.0, atol=1 / 32767)


class RedactionTests(unittest.TestCase):
    def test_silence_zeros_range_keeps_length_and_leaves_original(self):
        audio = np.arange(1, 101, dtype=np.float32)[:, None] / 100.0
        before = audio.copy()
        out = audioedit.redact_range(audio, 10, 2.0, 5.0, mode="silence")
        self.assertEqual(len(out), len(audio))               # та сама тривалість
        np.testing.assert_array_equal(out[20:50], 0.0)       # виділення — тиша
        np.testing.assert_array_equal(out[:20], audio[:20])  # поза виділенням — недоторкано
        np.testing.assert_array_equal(out[50:], audio[50:])
        np.testing.assert_array_equal(audio, before)         # оригінал не змінено

    def test_beep_fills_range_with_1khz_tone(self):
        audio = np.zeros((16000, 1), dtype=np.float32)
        before = audio.copy()
        out = audioedit.redact_range(audio, 16000, 0.25, 0.75, mode="beep", freq=1000.0)
        self.assertEqual(len(out), len(audio))
        seg = out[4000:12000]
        self.assertGreater(float(np.max(np.abs(seg))), 0.15)  # тон присутній
        np.testing.assert_array_equal(out[:4000], 0.0)        # поза виділенням — тиша
        np.testing.assert_array_equal(out[12000:], 0.0)
        # 1 кГц за 16 кГц: рівно 1000 повних періодів у 1 с → перетини нуля дають частоту
        crossings = np.count_nonzero(np.diff(np.signbit(seg[:, 0])))
        self.assertAlmostEqual(crossings / 2 / 0.5, 1000.0, delta=5.0)
        np.testing.assert_array_equal(audio, before)          # оригінал не змінено

    def test_range_edge_cases(self):
        audio = np.arange(40, dtype=np.float32)[:, None]
        # діапазон за межами кінця — обрізається безпечно, без краху
        clamped = audioedit.redact_range(audio, 10, 3.0, 99.0, mode="silence")
        np.testing.assert_array_equal(clamped[:30], audio[:30])
        np.testing.assert_array_equal(clamped[30:], 0.0)
        # порожній діапазон (start == end) нічого не заглушує
        noop = audioedit.redact_range(audio, 10, 2.0, 2.0, mode="silence")
        np.testing.assert_array_equal(noop, audio)
        # 1D-аудіо теж підтримується
        mono = np.ones(40, dtype=np.float32)
        out1d = audioedit.redact_range(mono, 10, 1.0, 2.0, mode="silence")
        np.testing.assert_array_equal(out1d[10:20], 0.0)
        self.assertEqual(len(out1d), 40)


class ArchiveProcessingTests(unittest.TestCase):
    def test_remove_silence_removes_quiet_frames_with_no_padding(self):
        # 5 × 20 мс рамок за 1 кГц: тиша, мовлення, тиша, мовлення, тиша.
        audio = np.concatenate((np.zeros(20), np.full(20, .2), np.zeros(20),
                                np.full(20, .3), np.zeros(20))).astype(np.float32)
        out = audioedit.remove_silence(audio, 1000, threshold_db=-30,
                                       frame_ms=20, padding_ms=0)
        self.assertEqual(len(out), 40)
        np.testing.assert_allclose(out, np.concatenate((np.full(20, .2), np.full(20, .3))))

    def test_normalize_never_clips(self):
        audio = np.array([.6, -.8, .25, -.1], dtype=np.float32)
        out = audioedit.normalize_archive(audio, target_db=-3, limit=.99)
        self.assertLessEqual(float(np.max(np.abs(out))), .990001)
        self.assertGreater(float(np.max(np.abs(out))), .8)


if __name__ == "__main__":
    unittest.main()
