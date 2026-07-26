"""Хвиля 1: реєстр рушіїв TTS (§4.3, §10 sherpa-stub).

Ключова вимога: реєстр будується БЕЗ ImportError, коли sherpa-адаптера ще нема."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.tts.engines import (ENGINE_REGISTRY, EngineLoadError,
                                       create_engine)
from whisper_core.tts.engines import styletts2, radtts


class TestRegistry(unittest.TestCase):
    def test_registry_builds_without_importerror(self):
        # sherpa-адаптера ще нема (Хвиля 3), але реєстр існує й містить ключ
        self.assertIn("sherpa", ENGINE_REGISTRY)
        self.assertIn("styletts2", ENGINE_REGISTRY)
        self.assertIn("radtts", ENGINE_REGISTRY)

    def test_sherpa_now_real_engine(self):
        # Хвиля 3: sherpa-адаптер реальний (sherpa_onnx у стеку) — фабрика створює
        # екземпляр, не кидає EngineLoadError на реєстрі.
        eng = create_engine("sherpa")
        self.assertEqual(eng.KIND, "sherpa")
        caps = eng.capabilities()
        self.assertFalse(caps.native_word_timings)   # sherpa → фолбек таймінгів (§4.5)

    def test_unknown_kind_no_silent_fallback(self):
        with self.assertRaises(EngineLoadError):
            create_engine("нема-такого")

    def test_fake_engine_instantiates(self):
        eng = create_engine("fake")
        self.assertEqual(eng.KIND, "fake")

    def test_styletts2_module_imports_without_torch(self):
        # модуль адаптера імпортується без torch (torch — лише в методах)
        self.assertEqual(styletts2.SAMPLE_RATE, 24000)
        self.assertEqual(styletts2.FRAME_HOP_MS, 25.0)

    def test_styletts2_duration_hook_canary(self):
        # КАНАРКА: приватний шлях hook зафіксовано; зміна коду має ламати цей тест
        self.assertEqual(styletts2.DURATION_HOOK_PATH,
                         ("predictor", "duration_proj"))

    def test_radtts_synthesis_defaults_has_13_args_shape(self):
        # усі дефолти synthesis() присутні (README-приклад із 3 аргументами застарілий)
        d = radtts.SYNTHESIS_DEFAULTS
        for k in ("n_takes", "use_latest_take", "f0_mean", "f0_std",
                  "energy_mean", "energy_std", "sigma_decoder",
                  "sigma_token_duration", "sigma_f0", "sigma_energy"):
            self.assertIn(k, d)

    def test_fake_synthesize_returns_marker(self):
        from whisper_core.tts import FAKE_ENGINE_MARKER
        eng = create_engine("fake")
        eng.load("")
        res = eng.synthesize("привіт світ", speed=1.0, want_timings=False)
        self.assertIn(FAKE_ENGINE_MARKER, res.normalized_text)
        self.assertEqual(res.sample_rate, 24000)
        self.assertTrue(res.wav.size > 0)


class TestStyleTts2Durations(unittest.TestCase):
    """Чиста обробка виходу duration-предиктора (numpy, без torch). Мутація суду
    (прибраний .reshape(-1)) лишалася зеленою офлайн — torch-гілку synthesize()
    покриває лише real-engine golden, що скіпається без стека воркера."""

    def test_flattens_batch_to_flat_int_list(self):
        import numpy as np
        from whisper_core.tts.engines.styletts2 import durations_from_proj
        # форма (batch=1, seq=3, max_dur=4); sigmoid(0)=0.5 → сума по max_dur=2.0
        out = np.zeros((1, 3, 4), dtype=np.float32)
        dur = durations_from_proj(out, speed=1.0)
        self.assertEqual(dur, [2, 2, 2])              # (1,seq)→РОЗПЛЮЩЕНО у (seq,)
        self.assertEqual(len(dur), 3)                 # без reshape → TypeError/нест-список
        self.assertTrue(all(isinstance(x, int) for x in dur))

    def test_clamp_min_one(self):
        import numpy as np
        from whisper_core.tts.engines.styletts2 import durations_from_proj
        out = np.full((1, 2, 2), -100.0)              # sigmoid→~0, сума→0 → clamp ≥1
        self.assertEqual(durations_from_proj(out, speed=1.0), [1, 1])

    def test_speed_divides_duration(self):
        import numpy as np
        from whisper_core.tts.engines.styletts2 import durations_from_proj
        out = np.zeros((1, 1, 8))                      # сума sigmoid=4.0; /speed 2 → 2
        self.assertEqual(durations_from_proj(out, speed=2.0), [2])


class TestRadttsOfflineHf(unittest.TestCase):
    """`_prepare_offline_hf` готує кеш вокодера офлайн (без torch): refs/main + env."""

    def test_prepares_ref_and_env(self):
        import os
        import tempfile
        model_path = tempfile.mkdtemp(prefix="radtts-voice-")
        commit = "e7d50512f731887429abfa9ba1e82d1a76f2360d"
        snap = os.path.join(
            model_path, "hf",
            "models--patriotyk--vocos-mel-hifigan-compat-44100khz",
            "snapshots", commit)
        os.makedirs(snap)
        with open(os.path.join(snap, "config.yaml"), "w") as fh:
            fh.write("x")
        saved = os.environ.get("HF_HUB_CACHE")
        try:
            ret = radtts._prepare_offline_hf(model_path)
            self.assertEqual(ret, model_path)                # майбутній CWD
            self.assertEqual(os.environ["HF_HUB_CACHE"],
                             os.path.join(model_path, "hf"))
            self.assertEqual(os.environ.get("HF_HUB_OFFLINE"), "1")
            ref_main = os.path.join(
                model_path, "hf",
                "models--patriotyk--vocos-mel-hifigan-compat-44100khz",
                "refs", "main")
            self.assertTrue(os.path.isfile(ref_main))
            with open(ref_main) as fh:
                self.assertEqual(fh.read().strip(), commit)  # revision 'main' → commit
        finally:
            if saved is not None:
                os.environ["HF_HUB_CACHE"] = saved
            else:
                os.environ.pop("HF_HUB_CACHE", None)


if __name__ == "__main__":
    unittest.main()
