"""Юніти збирача корпусу (feature/accuracy-corpus): ядро whisper_core.corpus
(збереження пари аудіо+текст, перелік, лічильник) і метрики dev.ab_test
(нормалізація, WER, CER). Без Qt, без реального рушія: аудіо — numpy-масив,
диск — tempfile."""
import json
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from whisper_core import corpus
from dev.ab_test import normalize, wer, cer


class SaveFromAudioTests(unittest.TestCase):
    def test_saves_wav_and_manifest_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = np.zeros(16000, dtype=np.float32)   # 1 с при 16 кГц
            rec = corpus.save_sample(
                "привіт свцерква", "привіт, Свято-Церква",
                audio=audio, sample_rate=16000,
                model="large-v3", source="desktop", root=tmp)
            self.assertIsNotNone(rec)
            self.assertEqual(rec["model"], "large-v3")
            self.assertEqual(rec["source"], "desktop")
            self.assertEqual(rec["corrected"], "привіт, Свято-Церква")
            self.assertIsNotNone(rec["wav"])
            wav = Path(tmp) / rec["wav"]
            self.assertTrue(wav.exists())
            with wave.open(str(wav), "rb") as w:
                self.assertEqual(w.getframerate(), 16000)
                self.assertEqual(w.getnframes(), 16000)
            # manifest.jsonl має рівно один валідний рядок із тим самим ts
            man = (Path(tmp) / corpus.MANIFEST_NAME).read_text(encoding="utf-8")
            rows = [json.loads(l) for l in man.splitlines() if l.strip()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["ts"], rec["ts"])
            self.assertAlmostEqual(rows[0]["duration"], 1.0, places=2)

    def test_empty_corrected_saves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = np.zeros(16000, dtype=np.float32)
            rec = corpus.save_sample("щось", "   ", audio=audio,
                                     sample_rate=16000, root=tmp)
            self.assertIsNone(rec)
            self.assertEqual(corpus.count(tmp), 0)


class SaveFromWavTests(unittest.TestCase):
    def _make_wav(self, path: Path, seconds=0.5, rate=16000):
        frames = int(seconds * rate)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"\x00\x00" * frames)

    def test_copies_source_wav_into_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "meeting.wav"
            self._make_wav(src)
            root = Path(tmp) / "corpus"
            rec = corpus.save_sample("розпізнано", "виправлено",
                                     src_wav=src, source="file", root=root)
            self.assertIsNotNone(rec)
            self.assertIsNotNone(rec["wav"])
            self.assertTrue((root / rec["wav"]).exists())
            self.assertEqual(rec["sample_rate"], 16000)

    def test_missing_src_wav_saves_text_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = corpus.save_sample("розпізнано", "виправлено",
                                     src_wav=Path(tmp) / "nope.wav", root=tmp)
            self.assertIsNotNone(rec)
            self.assertIsNone(rec["wav"])


class LoadCountTests(unittest.TestCase):
    def test_count_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = np.zeros(8000, dtype=np.float32)
            corpus.save_sample("a", "A", audio=audio, sample_rate=16000, root=tmp)
            corpus.save_sample("b", "B", audio=audio, sample_rate=16000, root=tmp)
            self.assertEqual(corpus.count(tmp), 2)
            samples = corpus.load_samples(tmp)
            self.assertEqual([s["corrected"] for s in samples], ["A", "B"])
            self.assertTrue(all(s["wav_path"] is not None for s in samples))

    def test_missing_manifest_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(corpus.count(Path(tmp) / "absent"), 0)
            self.assertEqual(corpus.load_samples(Path(tmp) / "absent"), [])

    def test_broken_lines_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            man = Path(tmp) / corpus.MANIFEST_NAME
            man.write_text('{"corrected": "ok", "wav": null}\nне-json\n\n',
                           encoding="utf-8")
            self.assertEqual(corpus.count(tmp), 1)


class ProfileScopeTests(unittest.TestCase):
    """feature/selflearn-dict: зразок несе ім'я словника, а load_samples уміє
    фільтрувати за ним (щоб щоденник/підказки не змішували профілі)."""

    def test_save_records_profile_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = corpus.save_sample("а", "А", profile="дім", root=tmp)
            self.assertEqual(rec["profile"], "дім")

    def test_default_profile_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = corpus.save_sample("а", "А", root=tmp)
            self.assertEqual(rec["profile"], "")

    def test_load_samples_filters_by_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus.save_sample("а", "А", profile="дім", root=tmp)
            corpus.save_sample("б", "Б", profile="робота", root=tmp)
            corpus.save_sample("в", "В", root=tmp)          # legacy, без прив'язки
            home = corpus.load_samples(tmp, profile="дім")
            self.assertEqual([s["corrected"] for s in home], ["А"])
            # legacy-зразок (без прив'язки) не потрапляє в жоден іменований профіль
            work = corpus.load_samples(tmp, profile="робота")
            self.assertEqual([s["corrected"] for s in work], ["Б"])
            # без фільтра — усі три
            self.assertEqual(len(corpus.load_samples(tmp)), 3)


class MetricTests(unittest.TestCase):
    def test_normalize_strips_punct_case_apostrophe(self):
        self.assertEqual(normalize("Привіт, М'яч!"), "привіт мяч")
        self.assertEqual(normalize("  кілька   пробілів "), "кілька пробілів")

    def test_wer_identical_is_zero(self):
        self.assertEqual(wer("привіт світ", "Привіт, світ!"), 0.0)

    def test_wer_one_wrong_word_of_two(self):
        self.assertAlmostEqual(wer("привіт світ", "привіт край"), 0.5)

    def test_cer_counts_char_edits(self):
        # "кіт" → "кит": одна заміна з трьох символів
        self.assertAlmostEqual(cer("кіт", "кит"), 1 / 3)

    def test_empty_reference(self):
        self.assertEqual(wer("", ""), 0.0)
        self.assertEqual(wer("", "щось"), 1.0)


if __name__ == "__main__":
    unittest.main()
