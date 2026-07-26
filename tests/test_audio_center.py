"""feature/audio-center: пристрій виводу, розширений VAD, шумовий гейт, AGC.

Чотири групи:
  * серіалізація нових полів конфігу (round-trip save/load, умовний запис);
  * новий VAD-параметр min_speech доходить до faster_whisper.transcribe;
  * шумовий гейт (RMS + гістерезис) — чиста DSP-функція;
  * AGC (лінійний нормалізатор) — чиста DSP-функція.

DSP тестуємо синтетичними сигналами: гейт/AGC не залежать від заліза.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from whisper_core.config import (
    Config, VAD_MIN_SPEECH_MS_DEFAULT,
    NOISE_GATE_THRESHOLD_DB_DEFAULT, AGC_TARGET_DB_DEFAULT,
)
from whisper_core.engine import Engine
from whisper_core import audiodsp


def _sine(amp, seconds=1.0, sr=16000, freq=220.0):
    t = np.arange(int(sr * seconds)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _rms_db(x):
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    return -np.inf if rms <= 1e-10 else 20.0 * np.log10(rms)


class AudioCenterConfigTests(unittest.TestCase):
    def test_new_defaults(self):
        cfg = Config()
        self.assertEqual(cfg.vad_min_speech_ms, VAD_MIN_SPEECH_MS_DEFAULT)
        self.assertFalse(cfg.noise_gate_enabled)
        self.assertEqual(cfg.noise_gate_threshold_db, NOISE_GATE_THRESHOLD_DB_DEFAULT)
        self.assertFalse(cfg.agc_enabled)
        self.assertEqual(cfg.agc_target_db, AGC_TARGET_DB_DEFAULT)
        self.assertIsNone(cfg.output_device)

    def test_fields_survive_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            cfg = Config()
            cfg.vad_min_speech_ms = 400
            cfg.noise_gate_enabled = True
            cfg.noise_gate_threshold_db = -52.0
            cfg.agc_enabled = True
            cfg.agc_target_db = -16.0
            cfg.output_device = "Динаміки (Realtek)"
            cfg.save(path)
            loaded = Config.load(path)
            self.assertEqual(loaded.vad_min_speech_ms, 400)
            self.assertTrue(loaded.noise_gate_enabled)
            self.assertEqual(loaded.noise_gate_threshold_db, -52.0)
            self.assertTrue(loaded.agc_enabled)
            self.assertEqual(loaded.agc_target_db, -16.0)
            self.assertEqual(loaded.output_device, "Динаміки (Realtek)")

    def test_output_device_written_only_when_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            Config().save(path)                       # output_device=None
            self.assertNotIn("output_device", path.read_text(encoding="utf-8"))


class VadMinSpeechTranscribeTests(unittest.TestCase):
    def test_min_speech_reaches_transcribe(self):
        cfg = Config()
        cfg.vad_min_speech_ms = 333
        with patch("whisper_core.engine.WhisperModel") as model:
            model.return_value.transcribe.return_value = (
                [], SimpleNamespace(duration=1.0))
            Engine(cfg).transcribe("audio.wav")
        params = model.return_value.transcribe.call_args.kwargs["vad_parameters"]
        self.assertEqual(params["min_speech_duration_ms"], 333)

    def test_default_min_speech_reaches_transcribe(self):
        cfg = Config()
        with patch("whisper_core.engine.WhisperModel") as model:
            model.return_value.transcribe.return_value = (
                [], SimpleNamespace(duration=1.0))
            Engine(cfg).transcribe("audio.wav")
        params = model.return_value.transcribe.call_args.kwargs["vad_parameters"]
        self.assertEqual(params["min_speech_duration_ms"], VAD_MIN_SPEECH_MS_DEFAULT)


class NoiseGateTests(unittest.TestCase):
    SR = 16000

    def test_empty_and_none_pass_through(self):
        self.assertIsNone(audiodsp.noise_gate(None, self.SR))
        out = audiodsp.noise_gate(np.zeros(0, dtype=np.float32), self.SR)
        self.assertEqual(len(out), 0)

    def test_shape_dtype_preserved_and_input_not_mutated(self):
        a = _sine(0.3)
        original = a.copy()
        out = audiodsp.noise_gate(a, self.SR, -45.0)
        self.assertEqual(out.shape, a.shape)
        self.assertEqual(out.dtype, np.float32)
        np.testing.assert_array_equal(a, original)   # вхід не мутовано

    def test_loud_speech_passes_silence_is_gated(self):
        # гучна мова [0.3с..0.6с], решта — цифрова тиша
        sr = self.SR
        a = np.zeros(sr, dtype=np.float32)
        speech = _sine(0.3, seconds=0.3, sr=sr)
        a[int(0.3 * sr):int(0.3 * sr) + len(speech)] = speech
        out = audiodsp.noise_gate(a, sr, -45.0)
        # середина мови — збережена (майже без втрат)
        mid = slice(int(0.4 * sr), int(0.5 * sr))
        self.assertGreater(_rms_db(out[mid]), -20.0)
        # тиша далеко від меж — приглушена в нуль
        far_silence = slice(int(0.85 * sr), int(0.95 * sr))
        self.assertTrue(np.allclose(out[far_silence], 0.0))

    def test_quiet_noise_below_threshold_is_gated(self):
        # рівномірний тихий шум під порогом (-60 dBFS) → майже все в нуль
        rng = np.random.default_rng(0)
        noise = (rng.standard_normal(self.SR) * 0.001).astype(np.float32)  # ~-60 dB
        out = audiodsp.noise_gate(noise, self.SR, -45.0)
        self.assertGreater(_rms_db(noise), _rms_db(out) + 20.0)  # приглушено щонайменше на 20 дБ


class AgcTests(unittest.TestCase):
    def test_empty_and_none_pass_through(self):
        self.assertIsNone(audiodsp.agc(None))
        out = audiodsp.agc(np.zeros(0, dtype=np.float32))
        self.assertEqual(len(out), 0)

    def test_silence_unchanged(self):
        z = np.zeros(16000, dtype=np.float32)
        np.testing.assert_array_equal(audiodsp.agc(z), z)

    def test_input_not_mutated(self):
        a = _sine(0.05)
        original = a.copy()
        audiodsp.agc(a, -20.0)
        np.testing.assert_array_equal(a, original)

    def test_quiet_signal_boosted_toward_target(self):
        # синус -29 dBFS RMS → у межах стелі підсилення підтягнути до -20
        a = _sine(0.05)
        self.assertLess(_rms_db(a), -25.0)
        out = audiodsp.agc(a, -20.0)
        self.assertAlmostEqual(_rms_db(out), -20.0, delta=1.5)
        self.assertEqual(out.dtype, np.float32)

    def test_gain_is_capped(self):
        # дуже тихий сигнал (-63 dBFS) не має роздуватись понад стелю +20 дБ
        a = _sine(0.001)
        before = _rms_db(a)
        out = audiodsp.agc(a, -20.0)
        self.assertLessEqual(_rms_db(out) - before, 20.0 + 0.5)

    def test_no_clipping(self):
        # гучний сигнал з високою ціллю — піки мають лишитись під лімітом
        a = _sine(0.5)
        out = audiodsp.agc(a, -3.0)
        self.assertLessEqual(float(np.max(np.abs(out))), 0.99 + 1e-6)


if __name__ == "__main__":
    unittest.main()
