# feature/transcript-editing
"""Тести чистої логіки редагування розшифровки (без Qt):
  - fronts.desktop.textsearch: пошук збігів і циклічна навігація;
  - whisper_core.history.update_final: правка final у history.jsonl (raw цілий);
  - whisper_core.meeting.postprocess.write_transcript_text: перезапис лише .txt.

UI (панель, підсвітка) — у tests/render_editing_smoke.py, поза unittest discover.
"""
import json
import tempfile
import unittest
from pathlib import Path

from fronts.desktop.textsearch import find_matches, step_index
from whisper_core import history
from whisper_core.meeting import postprocess as mpost


class FindMatchesTests(unittest.TestCase):
    def test_empty_query_no_matches(self):
        self.assertEqual(find_matches("будь-який текст", ""), [])

    def test_no_match(self):
        self.assertEqual(find_matches("привіт світ", "яблуко"), [])

    def test_single_match_positions(self):
        self.assertEqual(find_matches("привіт світ", "світ"), [(7, 11)])

    def test_multiple_matches_non_overlapping(self):
        # три «ба» у слові — збіги не перетинаються
        self.assertEqual(find_matches("ба ба ба", "ба"), [(0, 2), (3, 5), (6, 8)])

    def test_case_insensitive(self):
        self.assertEqual(find_matches("Балачки БАЛАЧКИ", "балачки"),
                         [(0, 7), (8, 15)])

    def test_overlapping_pattern_skips_ahead(self):
        # "аа" у "аааа": збіги від кінця попереднього → позиції 0 і 2, не 0/1/2
        self.assertEqual(find_matches("аааа", "аа"), [(0, 2), (2, 4)])


class StepIndexTests(unittest.TestCase):
    def test_no_matches(self):
        self.assertEqual(step_index(-1, 0, True), -1)

    def test_forward_wraps(self):
        self.assertEqual(step_index(2, 3, True), 0)

    def test_backward_wraps(self):
        self.assertEqual(step_index(0, 3, False), 2)

    def test_forward_middle(self):
        self.assertEqual(step_index(0, 3, True), 1)

    def test_out_of_range_resets_to_first(self):
        self.assertEqual(step_index(9, 3, True), 0)


class UpdateFinalTests(unittest.TestCase):
    def _write(self, tmp, records):
        path = Path(tmp) / "history.jsonl"
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8")
        return path

    def test_updates_final_keeps_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"ts": 1, "raw": "сирий А", "final": "фінал А", "source": "file"},
            ])
            ok = history.update_final(path, "фінал А", "виправлено А",
                                      source="file")
            self.assertTrue(ok)
            rec = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(rec["final"], "виправлено А")
            self.assertEqual(rec["raw"], "сирий А")       # raw НЕ чіпаємо
            self.assertEqual(rec["source"], "file")

    def test_matches_newest_when_duplicate_finals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"ts": 1, "raw": "r1", "final": "той самий", "source": "file"},
                {"ts": 2, "raw": "r2", "final": "той самий", "source": "file"},
            ])
            history.update_final(path, "той самий", "новий", source="file")
            lines = path.read_text(encoding="utf-8").splitlines()
            first, second = json.loads(lines[0]), json.loads(lines[1])
            self.assertEqual(first["final"], "той самий")   # старий цілий
            self.assertEqual(second["final"], "новий")      # переписано найновіший

    def test_source_filter_skips_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"ts": 1, "raw": "r", "final": "текст", "source": "desktop"},
            ])
            ok = history.update_final(path, "текст", "нове", source="file")
            self.assertFalse(ok)
            rec = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(rec["final"], "текст")         # без source-збігу — no-op

    def test_no_match_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"ts": 1, "raw": "r", "final": "текст", "source": "file"},
            ])
            self.assertFalse(history.update_final(path, "інше", "нове"))

    def test_missing_file_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "нема.jsonl"
            self.assertFalse(history.update_final(path, "a", "b"))

    def test_skips_broken_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            path.write_text(
                "не-json\n"
                + json.dumps({"ts": 1, "raw": "r", "final": "ок",
                              "source": "file"}, ensure_ascii=False) + "\n",
                encoding="utf-8")
            self.assertTrue(history.update_final(path, "ок", "готово",
                                                 source="file"))
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "не-json")           # битий рядок лишається
            self.assertEqual(json.loads(lines[1])["final"], "готово")


class WriteTranscriptTextTests(unittest.TestCase):
    def test_overwrites_txt_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "transcript.txt").write_text("старий текст",
                                                    encoding="utf-8")
            (session / "transcript.json").write_text('{"raw": "джерело"}',
                                                     encoding="utf-8")
            out = mpost.write_transcript_text(session, "новий текст")
            self.assertEqual(Path(out).name, "transcript.txt")
            self.assertEqual((session / "transcript.txt").read_text(
                encoding="utf-8"), "новий текст")
            # структурне джерело (raw) не чіпали
            self.assertEqual((session / "transcript.json").read_text(
                encoding="utf-8"), '{"raw": "джерело"}')

    def test_creates_txt_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = mpost.write_transcript_text(tmp, "текст")
            self.assertTrue(Path(out).is_file())
            self.assertEqual(Path(out).read_text(encoding="utf-8"), "текст")

    def test_bad_dir_returns_none(self):
        # шлях-файл замість теки → OSError усередині → None (некритично)
        with tempfile.TemporaryDirectory() as tmp:
            not_a_dir = Path(tmp) / "file.bin"
            not_a_dir.write_text("x", encoding="utf-8")
            self.assertIsNone(mpost.write_transcript_text(not_a_dir, "текст"))


if __name__ == "__main__":
    unittest.main()
