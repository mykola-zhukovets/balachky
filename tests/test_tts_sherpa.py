"""Хвиля 3: sherpa-onnx TTS-адаптер (§4.2, §4.5) + фолбек таймінгів."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.tts import align
from whisper_core.tts.engines import EngineLoadError, create_engine
from whisper_core.tts.engines.sherpa import SherpaTtsEngine, build_tts_config


class TestSherpaAdapter(unittest.TestCase):
    def test_registry_creates_real_engine(self):
        eng = create_engine("sherpa")
        self.assertIsInstance(eng, SherpaTtsEngine)

    def test_capabilities_no_native_timings(self):
        caps = SherpaTtsEngine().capabilities()
        self.assertFalse(caps.native_word_timings)   # → faster-whisper фолбек (§4.5)

    def test_load_missing_manifest_raises(self):
        eng = SherpaTtsEngine()
        with self.assertRaises(EngineLoadError):
            eng.load(tempfile.mkdtemp(prefix="empty-"))

    def test_combat_load_rejects_malicious_pack(self):
        # CRITICAL §4.4: РЕАЛЬНИЙ load адаптера (не лише helper) відхиляє pack із
        # виконуваним файлом — validate_voice_pack є ЄДИНИМ входом.
        import json as _j
        d = tempfile.mkdtemp(prefix="mal-")
        (Path(d) / "voice.json").write_text(_j.dumps({
            "schema": 1, "kind": "sherpa", "label": "x", "languages": ["en"],
            "model_type": "vits", "files": {"model": "m.onnx", "tokens": "t.txt"},
            "sample_rate": 24000}), encoding="utf-8")
        (Path(d) / "m.onnx").write_bytes(b"\x00" * 16)
        (Path(d) / "t.txt").write_bytes(b"\x00" * 16)
        (Path(d) / "evil.py").write_text("import os")   # заборонений виконуваний файл
        eng = SherpaTtsEngine()
        with self.assertRaises(EngineLoadError) as ctx:
            eng.load(d)                          # бойовий шлях відхиляє
        # відхилено САМЕ безпекою (validate_voice_pack), не пізнішим збоєм OfflineTts
        # на фейк-моделі — інакше bypass security проходив би непомітно (мутація)
        self.assertIn("відхилено", str(ctx.exception))

    def test_build_config_kokoro(self):
        # логіка вибору типу моделі (без реального OfflineTts) через фейкові sherpa-типи
        calls = {}

        class FakeSherpa:
            def OfflineTtsKokoroModelConfig(self, **k):
                calls["kokoro"] = k
                return "kokoro_cfg"
            def OfflineTtsModelConfig(self, **k):
                calls["model"] = k
                return "model_cfg"
            def OfflineTtsConfig(self, **k):
                calls["cfg"] = k
                return "cfg"

        fs = FakeSherpa()
        manifest = {"model_type": "kokoro",
                    "files": {"model": "m.onnx", "voices": "v.bin", "tokens": "t.txt"}}
        cfg = build_tts_config(fs, manifest, "/voice")
        self.assertEqual(cfg, "cfg")
        self.assertIn("kokoro", calls["model"])

    def test_build_config_unknown_type_none(self):
        class FakeSherpa:
            pass
        self.assertIsNone(build_tts_config(FakeSherpa(), {"model_type": "zzz"}, "/x"))


class TestTimingFallback(unittest.TestCase):
    def test_route_native_vs_fallback(self):
        native = SimpleNamespace(native_word_timings=True)
        fb = SimpleNamespace(native_word_timings=False)
        self.assertEqual(align.route_karaoke(native), align.ROUTE_NATIVE)
        self.assertEqual(align.route_karaoke(fb), align.ROUTE_FALLBACK)

    def test_align_asr_to_words(self):
        # ASR дав 2 слова з таймкодами → зіставлено зі словами (raw-діапазони збережено)
        wrs = [(0, 5), (6, 11)]
        asr = [{"start_ms": 0, "end_ms": 300}, {"start_ms": 300, "end_ms": 700}]
        wt = align.align_asr_to_words(wrs, asr, source_start_cp=10)
        self.assertEqual(len(wt), 2)
        self.assertEqual(wt[0]["raw_start"], 10)     # +source_start_cp
        self.assertEqual(wt[1]["start_ms"], 300)
        self.assertEqual(wt[1]["raw_end"], 21)

    def test_whisper_word_timestamps_injected(self):
        # transcribe_fn інжектується (без реальної моделі)
        def fake_transcribe(path):
            return [{"words": [{"word": "привіт", "start": 0.0, "end": 0.5}]}]
        words = align.whisper_word_timestamps("x.wav", transcribe_fn=fake_transcribe)
        self.assertEqual(words[0]["word"], "привіт")
        self.assertEqual(words[0]["end_ms"], 500)


if __name__ == "__main__":
    unittest.main()
