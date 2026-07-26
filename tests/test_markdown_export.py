# feature/markdown-export
"""Тести чистих функцій Markdown-експорту (без Qt): export.to_markdown із YAML-
frontmatter для розшифровок-файлів і postprocess.to_transcript_markdown із
секціями за мітками мовців для нарад. Стиль — як tests/test_auto_export.py."""
import unittest

from whisper_core import export
from whisper_core.meeting import postprocess as mpost
from whisper_core.meeting.postprocess import Utterance, SPK_ME, SPK_OTHERS, SPK_SINGLE


class DurationAndFrontmatterTests(unittest.TestCase):
    def test_duration_str_hms(self):
        self.assertEqual(export.duration_str(0), "00:00:00")
        self.assertEqual(export.duration_str(65), "00:01:05")
        self.assertEqual(export.duration_str(3725), "01:02:05")

    def test_duration_str_none_and_negative(self):
        self.assertIsNone(export.duration_str(None))
        self.assertEqual(export.duration_str(-3), "00:00:00")

    def test_frontmatter_defaults_tags_balachky(self):
        fm = export.build_frontmatter({})
        self.assertEqual(fm.splitlines()[0], "---")
        self.assertEqual(fm.splitlines()[-1], "---")
        self.assertIn("tags: [балачки]", fm)

    def test_frontmatter_orders_fields_and_quotes(self):
        fm = export.build_frontmatter(
            {"date": "2026-07-16", "source": "розмова.wav", "duration": "00:03:12"})
        self.assertEqual(fm.splitlines(), [
            "---",
            "date: 2026-07-16",
            'source: "розмова.wav"',
            'duration: "00:03:12"',      # у лапках: інакше YAML 1.1 прочитав би base-60
            "tags: [балачки]",
            "---",
        ])

    def test_frontmatter_escapes_backslash_and_quote_in_source(self):
        fm = export.build_frontmatter({"source": r'C:\путь\a"b.wav'})
        self.assertIn(r'source: "C:\\путь\\a\"b.wav"', fm)

    def test_frontmatter_skips_empty_fields(self):
        fm = export.build_frontmatter({"date": "", "source": None})
        self.assertNotIn("date:", fm)
        self.assertNotIn("source:", fm)


class ToMarkdownTests(unittest.TestCase):
    def test_frontmatter_plus_body(self):
        md = export.to_markdown("Привіт світ", {"date": "2026-07-16",
                                                "source": "нотатка"})
        self.assertEqual(md, "---\ndate: 2026-07-16\nsource: \"нотатка\"\n"
                              "tags: [балачки]\n---\n\nПривіт світ\n")

    def test_duration_from_segments(self):
        segs = [(0.0, 2.0, "а"), (2.0, 5.5, "бе")]
        md = export.to_markdown("текст", {}, segs)
        self.assertIn('duration: "00:00:06"', md)   # округлення 5.5→6

    def test_no_duration_when_no_segments(self):
        md = export.to_markdown("текст", {})
        self.assertNotIn("duration:", md)

    def test_explicit_duration_not_overwritten_by_segments(self):
        segs = [(0.0, 9.0, "а")]
        md = export.to_markdown("т", {"duration": "01:00:00"}, segs)
        self.assertIn('duration: "01:00:00"', md)
        self.assertNotIn('00:00:09', md)

    def test_body_stripped_and_empty_ok(self):
        md = export.to_markdown("  край  \n", {})
        self.assertTrue(md.endswith("\n\nкрай\n"))
        md_empty = export.to_markdown("", {})
        self.assertTrue(md_empty.endswith("---\n\n\n"))


class MeetingMarkdownTests(unittest.TestCase):
    def test_two_tracks_sections_by_label(self):
        utts = [
            Utterance(0.0, 2.0, SPK_ME, "привіт"),
            Utterance(3.0, 4.0, SPK_OTHERS, "вітаю"),
            Utterance(5.0, 6.0, SPK_ME, "як справи"),
        ]
        md = mpost.to_transcript_markdown(
            utts, me_label="Я", others_label="Співрозмовники",
            meta={"date": "2026-07-16", "source": "Нарада"})
        self.assertIn("## Я", md)
        self.assertIn("## Співрозмовники", md)
        # секція «Я» збирає обидві мої репліки, у хронологічному порядку
        me_section = md.split("## Я", 1)[1]
        self.assertIn("[00:00] привіт", me_section)
        self.assertIn("[00:05] як справи", me_section)
        self.assertIn('duration: "00:00:06"', md)

    def test_section_order_by_first_appearance(self):
        utts = [
            Utterance(0.0, 1.0, SPK_OTHERS, "перший"),
            Utterance(2.0, 3.0, SPK_ME, "другий"),
        ]
        md = mpost.to_transcript_markdown(
            utts, me_label="Я", others_label="Гість")
        self.assertLess(md.index("## Гість"), md.index("## Я"))

    def test_diarized_speakers_use_saved_names(self):
        utts = [
            Utterance(0.0, 1.0, "speaker_1", "вітаю"),
            Utterance(2.0, 3.0, SPK_ME, "привіт"),
        ]
        md = mpost.to_transcript_markdown(
            utts, me_label="Я", others_label="Співрозмовники",
            speaker_names={"speaker_1": "Мовець 1"})
        self.assertIn("## Мовець 1", md)
        self.assertIn("## Я", md)
        self.assertNotIn("## None", md)

    def test_single_track_no_sections(self):
        utts = [Utterance(0.0, 1.0, SPK_SINGLE, "суцільний текст")]
        md = mpost.to_transcript_markdown(
            utts, me_label="Я", others_label="Інші")
        self.assertNotIn("##", md)
        self.assertIn("[00:00] суцільний текст", md)

    def test_empty_utterances_only_frontmatter(self):
        md = mpost.to_transcript_markdown(
            [], me_label="Я", others_label="Інші", meta={"date": "2026-07-16"})
        self.assertIn("tags: [балачки]", md)
        self.assertNotIn("##", md)
        self.assertNotIn("[", md.split("---", 2)[-1])   # тіла немає

    def test_hour_long_meeting_timestamp(self):
        utts = [Utterance(3661.0, 3665.0, SPK_SINGLE, "пізня репліка")]
        md = mpost.to_transcript_markdown(
            utts, me_label="Я", others_label="Інші")
        self.assertIn("[1:01:01] пізня репліка", md)


if __name__ == "__main__":
    unittest.main()
