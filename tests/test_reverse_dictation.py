# feature/reverse-dictation
"""Тести чистої логіки зворотного диктування (без Qt):
  - whisper_core.history.update_record: правка final за ts (raw цілий) + позначка
    «edited»; точність матчингу за ts, не за текстом;
  - whisper_core.history.log_history: поле audio у записі (ім'я збереженого WAV);
  - whisper_core.phrasebook.phrase_like: чи виправлення схоже на коротку пару-фразу;
  - recordings.save_recording ↔ is_safe_recording_name: ім'я збереженого аудіо
    проходить перевірку, за якою картка резолвить «Переслухати».

UI (діалог, кнопки картки) — у tests/render_reverse_dictation_smoke.py.
"""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from whisper_core import history, phrasebook, recordings


def _write(tmp, records):
    path = Path(tmp) / "history.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8")
    return path


class UpdateRecordTests(unittest.TestCase):
    def test_updates_final_keeps_raw_and_marks_edited(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, [
                {"ts": 5, "raw": "сирий ворктрі", "final": "сирий ворктрі",
                 "source": "desktop"},
            ])
            ok = history.update_record(path, 5, final="сирий worktree",
                                       mark_edited=True)
            self.assertTrue(ok)
            rec = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(rec["final"], "сирий worktree")   # переписано
            self.assertEqual(rec["raw"], "сирий ворктрі")      # raw цілий (verbatim)
            self.assertTrue(rec["edited"])                     # позначка виправлення

    def test_matches_exact_ts_not_text(self):
        # два записи з ОДНАКОВИМ final, різні ts — правимо саме ts=2
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, [
                {"ts": 1, "raw": "r1", "final": "той самий", "source": "desktop"},
                {"ts": 2, "raw": "r2", "final": "той самий", "source": "desktop"},
            ])
            ok = history.update_record(path, 2, final="новий", mark_edited=True)
            self.assertTrue(ok)
            lines = path.read_text(encoding="utf-8").splitlines()
            first, second = json.loads(lines[0]), json.loads(lines[1])
            self.assertEqual(first["final"], "той самий")   # ts=1 недоторканий
            self.assertNotIn("edited", first)
            self.assertEqual(second["final"], "новий")      # ts=2 виправлено
            self.assertTrue(second["edited"])

    def test_mark_edited_only_keeps_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, [
                {"ts": 7, "raw": "r", "final": "текст", "source": "desktop"},
            ])
            ok = history.update_record(path, 7, mark_edited=True)
            self.assertTrue(ok)
            rec = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(rec["final"], "текст")         # final не чіпали
            self.assertTrue(rec["edited"])

    def test_unknown_ts_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, [
                {"ts": 1, "raw": "r", "final": "текст", "source": "desktop"},
            ])
            self.assertFalse(history.update_record(path, 999, final="нове"))
            rec = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(rec["final"], "текст")         # без збігу — no-op

    def test_missing_file_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "нема.jsonl"
            self.assertFalse(history.update_record(path, 1, final="b"))

    def test_skips_broken_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            path.write_text(
                "не-json\n"
                + json.dumps({"ts": 3, "raw": "r", "final": "ок",
                              "source": "desktop"}, ensure_ascii=False) + "\n",
                encoding="utf-8")
            self.assertTrue(history.update_record(path, 3, final="готово"))
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "не-json")           # битий рядок лишається
            self.assertEqual(json.loads(lines[1])["final"], "готово")


class LogHistoryAudioTests(unittest.TestCase):
    def test_audio_name_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            rec = history.log_history(path, "сир", "фінал", source="desktop",
                                      audio="2026-07-23_10-00-00.wav")
            self.assertEqual(rec["audio"], "2026-07-23_10-00-00.wav")
            stored = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(stored["audio"], "2026-07-23_10-00-00.wav")

    def test_no_audio_field_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            rec = history.log_history(path, "сир", "фінал", source="desktop")
            self.assertNotIn("audio", rec)
            stored = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertNotIn("audio", stored)

    def test_none_audio_not_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            rec = history.log_history(path, "сир", "фінал", audio=None)
            self.assertNotIn("audio", rec)


class PhraseLikeTests(unittest.TestCase):
    def test_short_bilingual_pair_is_phrase_like(self):
        self.assertTrue(phrasebook.phrase_like("ворктрі", "worktree"))
        self.assertTrue(phrasebook.phrase_like("пул реквест", "pull request"))

    def test_empty_sides_not_phrase_like(self):
        self.assertFalse(phrasebook.phrase_like("", "worktree"))
        self.assertFalse(phrasebook.phrase_like("ворктрі", ""))

    def test_identical_not_phrase_like(self):
        self.assertFalse(phrasebook.phrase_like("Слово", "слово"))  # без урах. регістру

    def test_long_sentence_not_phrase_like(self):
        long_heard = "це доволі довге речення яке точно не є терміном"
        self.assertFalse(phrasebook.phrase_like(long_heard, "інше"))

    def test_too_many_chars_not_phrase_like(self):
        self.assertFalse(phrasebook.phrase_like("а" * 50, "б"))


class SavedAudioNameRoundtripTests(unittest.TestCase):
    """Ім'я, яке дає save_recording, мусить проходити is_safe_recording_name —
    інакше картка не змогла б безпечно резолвити/видалити аудіо диктування."""

    def test_saved_name_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = np.zeros(16000, dtype=np.float32)   # 1 с тиші
            audio[100:200] = 0.1                          # трохи сигналу
            out = recordings.save_recording(tmp, audio, 16000)
            self.assertIsNotNone(out)
            self.assertTrue(recordings.is_safe_recording_name(out.name))

    def test_empty_audio_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                recordings.save_recording(tmp, np.zeros(0, dtype=np.float32), 16000))


if __name__ == "__main__":
    unittest.main()
