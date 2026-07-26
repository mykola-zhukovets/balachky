"""feature/edit-pack: імпорт субтитрів назад (SRT/VTT → сегменти).

Замикає цикл «розшифрував → відредагував у субтитрах → повернув». Парсер один
на обидва формати; тут перевіряємо базовий розбір, краї (BOM, багаторядкові,
порожні блоки, номери, WEBVTT-заголовок, налаштування кʼю) і round-trip
export→import (парсер — інверсія серіалізації to_srt/to_vtt).
"""
import unittest

from whisper_core import export


class ParseSrtTests(unittest.TestCase):
    def test_basic_two_cues(self):
        srt = (
            "1\n"
            "00:00:00,000 --> 00:00:02,500\n"
            "Привіт світе\n"
            "\n"
            "2\n"
            "00:00:02,500 --> 00:00:05,000\n"
            "Другий рядок\n"
        )
        cues = export.parse_subtitles(srt)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0]["start"], 0.0)
        self.assertAlmostEqual(cues[0]["end"], 2.5)
        self.assertEqual(cues[0]["text"], "Привіт світе")
        self.assertAlmostEqual(cues[1]["start"], 2.5)
        self.assertEqual(cues[1]["text"], "Другий рядок")

    def test_multiline_cue_joined_with_space(self):
        srt = (
            "1\n"
            "00:00:00,000 --> 00:00:03,000\n"
            "Перший рядок\n"
            "другий рядок\n"
        )
        cues = export.parse_subtitles(srt)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "Перший рядок другий рядок")

    def test_bom_prefix_tolerated(self):
        srt = (
            "﻿1\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "З BOM\n"
        )
        cues = export.parse_subtitles(srt)
        self.assertEqual(len(cues), 1)
        self.assertAlmostEqual(cues[0]["start"], 1.0)
        self.assertEqual(cues[0]["text"], "З BOM")

    def test_empty_blocks_and_blank_text_skipped(self):
        srt = (
            "1\n"
            "00:00:00,000 --> 00:00:01,000\n"
            "\n"                                  # порожній текст → блок відкидаємо
            "\n"
            "2\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "Є текст\n"
        )
        cues = export.parse_subtitles(srt)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "Є текст")

    def test_crlf_line_endings(self):
        srt = "1\r\n00:00:00,000 --> 00:00:02,000\r\nCRLF\r\n"
        cues = export.parse_subtitles(srt)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "CRLF")

    def test_hours_component(self):
        srt = "1\n01:02:03,400 --> 01:02:05,000\nГодини\n"
        cues = export.parse_subtitles(srt)
        self.assertAlmostEqual(cues[0]["start"], 3600 + 120 + 3 + 0.4)

    def test_empty_content(self):
        self.assertEqual(export.parse_subtitles(""), [])
        self.assertEqual(export.parse_subtitles(None), [])


class ParseVttTests(unittest.TestCase):
    def test_webvtt_header_and_dot_separator(self):
        vtt = (
            "WEBVTT\n"
            "\n"
            "00:00:00.000 --> 00:00:02.000\n"
            "Привіт VTT\n"
        )
        cues = export.parse_subtitles(vtt)
        self.assertEqual(len(cues), 1)
        self.assertAlmostEqual(cues[0]["end"], 2.0)
        self.assertEqual(cues[0]["text"], "Привіт VTT")

    def test_mm_ss_without_hours(self):
        vtt = "WEBVTT\n\n00:01.500 --> 00:03.000\nБез годин\n"
        cues = export.parse_subtitles(vtt)
        self.assertAlmostEqual(cues[0]["start"], 1.5)
        self.assertAlmostEqual(cues[0]["end"], 3.0)

    def test_cue_settings_after_timestamp_ignored(self):
        vtt = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:02.000 line:80% align:center\n"
            "З налаштуваннями\n"
        )
        cues = export.parse_subtitles(vtt)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "З налаштуваннями")

    def test_note_block_skipped(self):
        vtt = (
            "WEBVTT\n\n"
            "NOTE це коментар\n\n"
            "00:00:00.000 --> 00:00:01.000\nРеальний\n"
        )
        cues = export.parse_subtitles(vtt)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "Реальний")


class RoundTripTests(unittest.TestCase):
    """Парсер — інверсія серіалізації: export → import повертає ті самі кʼю
    (текст рядків склеєний пробілом, час у межах округлення до мілісекунди)."""

    SEGMENTS = [
        {"start": 0.0, "end": 2.4, "text": "Перше речення тут"},
        {"start": 3.0, "end": 6.2, "text": "Друге, трохи довше речення для перевірки"},
        {"start": 7.0, "end": 9.0, "text": "Третє"},
    ]

    def _expected(self):
        return [{"start": c["start"], "end": c["end"],
                 "text": " ".join(c["lines"])}
                for c in export._build_cues(self.SEGMENTS)]

    def test_srt_roundtrip(self):
        cues = export.parse_subtitles(export.to_srt(self.SEGMENTS))
        exp = self._expected()
        self.assertEqual(len(cues), len(exp))
        for got, want in zip(cues, exp):
            self.assertEqual(got["text"], want["text"])
            self.assertAlmostEqual(got["start"], want["start"], places=2)
            self.assertAlmostEqual(got["end"], want["end"], places=2)

    def test_vtt_roundtrip(self):
        cues = export.parse_subtitles(export.to_vtt(self.SEGMENTS))
        exp = self._expected()
        self.assertEqual(len(cues), len(exp))
        for got, want in zip(cues, exp):
            self.assertEqual(got["text"], want["text"])
            self.assertAlmostEqual(got["start"], want["start"], places=2)
            self.assertAlmostEqual(got["end"], want["end"], places=2)


if __name__ == "__main__":
    unittest.main()
