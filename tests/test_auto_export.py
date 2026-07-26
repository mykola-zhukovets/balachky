# feature/auto-export
"""Тести чистої функції export.append_transcript (без Qt): створення файлу-дня,
дозапис, формати md/txt, окремий файл на кожен день, порожній текст і неіснуюча
тека. Стиль — як tests/test_backend_regressions.py (tempfile, час параметром)."""
import datetime
import os
import tempfile
import unittest
from pathlib import Path

from whisper_core import export


def _dt(y=2026, mo=7, d=16, h=14, mi=30):
    return datetime.datetime(y, mo, d, h, mi)


class AppendTranscriptTests(unittest.TestCase):
    def test_creates_day_file_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = export.append_transcript(tmp, "Привіт світ", _dt(), "md")
            self.assertEqual(Path(path).name, "balachky-2026-07-16.md")
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(Path(path).read_text(encoding="utf-8"),
                             "## 14:30\nПривіт світ\n\n")

    def test_txt_format_uses_bracket_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = export.append_transcript(tmp, "Друга нотатка", _dt(h=9, mi=5),
                                            "txt")
            self.assertEqual(Path(path).name, "balachky-2026-07-16.txt")
            self.assertEqual(Path(path).read_text(encoding="utf-8"),
                             "[09:05] Друга нотатка\n\n")

    def test_appends_second_entry_same_day_one_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = export.append_transcript(tmp, "перша", _dt(h=8, mi=0), "md")
            p2 = export.append_transcript(tmp, "друга", _dt(h=8, mi=1), "md")
            self.assertEqual(p1, p2)                       # той самий файл-день
            self.assertEqual(len(os.listdir(tmp)), 1)      # рівно один файл
            self.assertEqual(Path(p1).read_text(encoding="utf-8"),
                             "## 08:00\nперша\n\n## 08:01\nдруга\n\n")

    def test_separate_file_per_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            export.append_transcript(tmp, "вчора", _dt(d=15), "md")
            export.append_transcript(tmp, "сьогодні", _dt(d=16), "md")
            self.assertEqual(sorted(os.listdir(tmp)),
                             ["balachky-2026-07-15.md", "balachky-2026-07-16.md"])

    def test_empty_text_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(export.append_transcript(tmp, "   ", _dt(), "md"))
            self.assertIsNone(export.append_transcript(tmp, "", _dt(), "md"))
            self.assertEqual(os.listdir(tmp), [])          # жодного файлу не створено

    def test_unknown_format_falls_back_to_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = export.append_transcript(tmp, "текст", _dt(), "pdf")
            self.assertTrue(str(path).endswith(".md"))
            self.assertEqual(Path(path).read_text(encoding="utf-8"),
                             "## 14:30\nтекст\n\n")

    def test_text_is_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = export.append_transcript(tmp, "  зайві пробіли  \n", _dt(), "md")
            self.assertEqual(Path(path).read_text(encoding="utf-8"),
                             "## 14:30\nзайві пробіли\n\n")

    def test_nonexistent_dir_raises_oserror(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "gone")            # теку не створюємо
            with self.assertRaises(OSError):
                export.append_transcript(missing, "текст", _dt(), "md")

    def test_writes_bare_lf_not_crlf(self):
        # read_text() нормалізує CRLF→LF і сховав би баг — тому перевіряємо
        # реальні байти: у файлі має бути чистий LF (0x0a), жодного CR (0x0d).
        with tempfile.TemporaryDirectory() as tmp:
            path = export.append_transcript(tmp, "Привіт, світе!", _dt(), "md")
            raw = Path(path).read_bytes()
            self.assertEqual(raw, "## 14:30\nПривіт, світе!\n\n".encode("utf-8"))
            self.assertNotIn(b"\r", raw)
            path_txt = export.append_transcript(tmp, "рядок", _dt(h=9, mi=5), "txt")
            self.assertNotIn(b"\r", Path(path_txt).read_bytes())


if __name__ == "__main__":
    unittest.main()
