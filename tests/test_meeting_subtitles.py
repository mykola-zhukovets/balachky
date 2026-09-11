"""Експорт субтитрів наради (SRT і WebVTT) з іменами мовців — issue #18.

Три шари: ядро (``postprocess.subtitle_segments`` → ``export.to_srt/to_vtt``),
контролер (``DesktopApp.meeting_subtitles`` читає артефакти через
``read_artifact``, помилки не ковтає) і сторінка (``MeetingPage._save_subtitles``
пише файл або показує “не вдалося”, без порожнього файлу).
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fronts.desktop.app import DesktopApp
from fronts.desktop.i18n import STRINGS, tr
from fronts.desktop.pages import meeting as meeting_page
from whisper_core import export
from whisper_core.meeting import postprocess as mpost
from whisper_core.meeting.session import MeetingMeta
from whisper_core.meeting.storage_crypto import VaultPasswordRequired

ME, OTHERS = "Я", "Інший учасник"


def _segments(utterances, names=None):
    return mpost.subtitle_segments(utterances, names, me_label=ME, others_label=OTHERS)


class SubtitleSegmentsTests(unittest.TestCase):
    def test_names_and_source_labels(self):
        utterances = [
            {"start": 1.0, "end": 4.5, "speaker": "speaker_01", "speaker_id": "speaker_01",
             "text": "Добрий день."},
            {"start": 5.0, "end": 7.2, "speaker": "me", "source": "me", "text": "Вітаю всіх."},
            {"start": 8.0, "end": 9.0, "speaker": "others", "source": "others", "text": "Дякую."},
        ]
        segs = _segments(utterances, {"speaker_01": "Олексій"})
        self.assertEqual(segs, [
            (1.0, 4.5, "Олексій: Добрий день."),
            (5.0, 7.2, "Я: Вітаю всіх."),
            (8.0, 9.0, "Інший учасник: Дякую."),
        ])
        self.assertIsInstance(segs[0][0], float)

    def test_unnamed_diarized_speaker_gets_others_label_without_number(self):
        utterances = [
            {"start": 1.0, "end": 3.0, "speaker": "speaker_02", "speaker_id": "speaker_02",
             "text": "Перша репліка"},
        ]
        segs = _segments(utterances, {})
        self.assertEqual(segs[0][2], "Інший учасник: Перша репліка")
        self.assertNotIn("2", segs[0][2])

    def test_single_track_has_no_label_like_text_export(self):
        # Одна доріжка (speaker=single): .txt пише текст без мітки — субтитри теж,
        # незалежно від джерела (мікрофон чи системний звук).
        utterances = [
            {"start": 0.5, "end": 2.0, "speaker": "single", "source": "me", "text": "Лекція"},
            {"start": 3.0, "end": 4.0, "speaker": "single", "source": "sys", "text": "Далі"},
        ]
        self.assertEqual([s[2] for s in _segments(utterances, {})], ["Лекція", "Далі"])

    def test_empty_texts_and_missing_times_skipped(self):
        utterances = [
            {"start": 0.0, "end": 1.0, "speaker": "me", "text": "   "},
            {"start": 1.0, "end": 2.0, "speaker": "me", "text": ""},
            {"start": None, "end": 3.0, "speaker": "me", "text": "Без старту"},
            {"start": 3.0, "end": None, "speaker": "me", "text": "Без кінця"},
            {"start": 4, "end": 5, "speaker": "me", "text": "Дійсний текст"},
        ]
        self.assertEqual(_segments(utterances), [(4.0, 5.0, "Я: Дійсний текст")])

    def test_show_source_false_mutes_source_labels_only(self):
        # Чекбокс “Хто говорити” знято: “Я”/“Співрозмовники” зникають, імена лишаються,
        # безіменний діаризований мовець теж без мітки — як у .txt.
        utterances = [
            {"start": 1.0, "end": 2.0, "speaker": "me", "text": "Моє"},
            {"start": 3.0, "end": 4.0, "speaker": "speaker_01", "text": "Названий"},
            {"start": 5.0, "end": 6.0, "speaker": "speaker_02", "text": "Безіменний"},
        ]
        segs = mpost.subtitle_segments(utterances, {"speaker_01": "Олексій"},
                                       me_label=ME, others_label=OTHERS, show_source=False)
        self.assertEqual([s[2] for s in segs], ["Моє", "Олексій: Названий", "Безіменний"])

    def test_sorted_by_start_and_bounds_untouched(self):
        utterances = [
            {"start": 10.0, "end": 12.0, "speaker": "me", "text": "Пізніше"},
            {"start": 2.0, "end": 4.0, "speaker": "others", "text": "Раніше"},
            {"start": 5.0, "end": 7.0, "speaker": "others", "text": "Посередині"},
        ]
        self.assertEqual([(s[0], s[1]) for s in _segments(utterances)],
                         [(2.0, 4.0), (5.0, 7.0), (10.0, 12.0)])


class SubtitleFormatTests(unittest.TestCase):
    SEGS = [(1.234, 4.567, "Олексій: Перша репліка"), (6.0, 8.5, "Я: Друга репліка")]

    def test_srt_numbers_and_comma(self):
        srt = export.to_srt(self.SEGS)
        self.assertTrue(srt.startswith("1\n00:00:01,234 --> "))
        self.assertIn("\n\n2\n00:00:06,000 --> ", srt)
        self.assertIn("Олексій: Перша репліка", srt)

    def test_vtt_header_and_period(self):
        vtt = export.to_vtt(self.SEGS)
        self.assertTrue(vtt.startswith("WEBVTT\n"))
        self.assertIn("00:00:01.234 --> ", vtt)
        self.assertIn("Я: Друга репліка", vtt)

    def test_no_bom(self):
        for text in (export.to_srt(self.SEGS), export.to_vtt(self.SEGS)):
            self.assertFalse(text.startswith("﻿"))
            self.assertFalse(text.encode("utf-8").startswith(b"\xef\xbb\xbf"))


class ControllerTests(unittest.TestCase):
    TRANSCRIPT = [
        {"start": 1.0, "end": 3.0, "speaker": "speaker_01", "speaker_id": "speaker_01",
         "text": "Доброго дня"},
        {"start": 4.0, "end": 6.0, "speaker": "me", "source": "me", "text": "Вітаю"},
    ]

    def _fake_app(self, sdir):
        emitted = []
        app = SimpleNamespace(
            _meeting_session_dir=lambda sid: sdir,
            meeting_vault_needed=SimpleNamespace(emit=lambda: emitted.append(True)))
        return app, emitted

    def test_reads_transcript_and_names_via_read_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdir = Path(tmp)
            meta = MeetingMeta(schema=2, id="s1", created=1700000000, status="done",
                               preset="both", sources=["mic", "sys"],
                               speaker_names={"speaker_01": "Марія"})
            artifacts = {"transcript.json": json.dumps(self.TRANSCRIPT).encode("utf-8"),
                         "meeting.json": meta.to_json().encode("utf-8")}
            app, _ = self._fake_app(sdir)
            with patch("whisper_core.meeting.session.read_artifact",
                       side_effect=lambda d, name: artifacts[name]):
                srt = DesktopApp.meeting_subtitles(app, "s1", "srt")
                vtt = DesktopApp.meeting_subtitles(app, "s1", "vtt")
        self.assertIn("Марія: Доброго дня", srt)
        self.assertIn(f"{tr('meeting_speaker_me')}: Вітаю", srt)
        self.assertIn("00:00:01,000 --> ", srt)
        self.assertTrue(vtt.startswith("WEBVTT\n"))
        self.assertIn("00:00:01.000 --> ", vtt)

    def test_show_source_forwarded(self):
        artifacts = {"transcript.json": json.dumps(self.TRANSCRIPT).encode("utf-8"),
                     "meeting.json": MeetingMeta(
                         schema=2, id="s1", created=1700000000, status="done", preset="both",
                         sources=["mic", "sys"], speaker_names={"speaker_01": "Марія"}
                     ).to_json().encode("utf-8")}
        app, _ = self._fake_app(Path("does-not-matter"))
        with patch("whisper_core.meeting.session.read_artifact",
                   side_effect=lambda d, name: artifacts[name]):
            srt = DesktopApp.meeting_subtitles(app, "s1", "srt", show_source=False)
        self.assertIn("Марія: Доброго дня", srt)
        self.assertIn("\nВітаю\n", srt)
        self.assertNotIn(f"{tr('meeting_speaker_me')}:", srt)

    def test_read_failure_is_raised_not_swallowed(self):
        app, emitted = self._fake_app(Path("does-not-matter"))
        with patch("whisper_core.meeting.session.read_artifact",
                   side_effect=OSError("диск недоступний")):
            with self.assertRaises(OSError):
                DesktopApp.meeting_subtitles(app, "s1", "srt")
        self.assertEqual(emitted, [])

    def test_locked_vault_emits_signal_and_raises(self):
        app, emitted = self._fake_app(Path("does-not-matter"))
        with patch("whisper_core.meeting.session.read_artifact",
                   side_effect=VaultPasswordRequired()):
            with self.assertRaises(VaultPasswordRequired):
                DesktopApp.meeting_subtitles(app, "s1", "vtt")
        self.assertEqual(emitted, [True])


class _Label:
    def __init__(self):
        self.text, self.shown = None, False

    def setText(self, text):
        self.text = text

    def show(self):
        self.shown = True


class PageSaveTests(unittest.TestCase):
    def _run(self, controller, out_path, **kwargs):
        page = SimpleNamespace(controller=controller)
        label = _Label()
        with patch.object(meeting_page.QFileDialog, "getSaveFileName",
                          return_value=(str(out_path), "")):
            meeting_page.MeetingPage._save_subtitles(page, "s1", "vtt", label, **kwargs)
        return label

    def test_show_source_forwarded_to_controller(self):
        seen = []
        controller = SimpleNamespace(
            meeting_subtitles=lambda sid, fmt, **kw: (seen.append(kw), "WEBVTT\n")[1],
            log_meeting_export=lambda *a: None)
        with tempfile.TemporaryDirectory() as tmp:
            self._run(controller, Path(tmp) / "a.vtt", show_source=False)
            self._run(controller, Path(tmp) / "b.vtt")
        self.assertEqual(seen, [{"show_source": False, "stem": "transcript"},
                                {"show_source": True, "stem": "transcript"}])

    def test_locked_vault_exits_quietly(self):
        # Контролер уже подав сигнал на пароль: ні напису “не вдалося”, ні файлу, ні стеку в лозі.
        def locked(sid, fmt, **kw):
            raise VaultPasswordRequired()
        controller = SimpleNamespace(meeting_subtitles=locked,
                                     log_meeting_export=lambda *a: self.fail("не мало логуватись"))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "s1.vtt"
            with self.assertNoLogs(level="ERROR"):
                label = self._run(controller, out)
            self.assertFalse(out.exists())
        self.assertIsNone(label.text)
        self.assertFalse(label.shown)

    def test_writes_utf8_without_bom_and_logs_export(self):
        calls = []
        controller = SimpleNamespace(
            meeting_subtitles=lambda sid, fmt, **kw: "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nЯ: Привіт\n",
            log_meeting_export=lambda *a: calls.append(a))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "s1.vtt"
            label = self._run(controller, out)
            data = out.read_bytes()
        self.assertTrue(data.startswith(b"WEBVTT\n"))
        self.assertNotIn(b"\r\n", data)
        self.assertEqual(calls, [("s1", "vtt", str(out))])
        # Порівнювати з tr(...) не можна (вартовий test_i18n_tautology_lint):
        # зламаний ключ ламає обидві сторони однаково. Перевіряємо частину,
        # яку дає сам продукт, — ім'я збереженого файла.
        self.assertIn("s1.vtt", label.text)
        self.assertTrue(label.shown)

    def test_controller_failure_shows_message_and_writes_nothing(self):
        def boom(sid, fmt, **kw):
            raise OSError("немає доступу")
        controller = SimpleNamespace(meeting_subtitles=boom,
                                     log_meeting_export=lambda *a: self.fail("не мало логуватись"))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "s1.vtt"
            with self.assertLogs(level="ERROR"):
                label = self._run(controller, out)
            self.assertFalse(out.exists())
        # Напис про помилку є і це НЕ напис про успіх: він не несе імені файла.
        self.assertTrue(label.text)
        self.assertNotIn("s1.vtt", label.text)
        self.assertTrue(label.shown)


class I18nTests(unittest.TestCase):
    def test_keys_in_both_languages_without_guillemets(self):
        for key in ("meeting_exp_srt", "meeting_exp_vtt"):
            for lang in ("uk", "en"):
                value = STRINGS[lang][key]
                self.assertTrue(value.strip())
                self.assertNotIn("«", value)
                self.assertNotIn("»", value)
        self.assertEqual(STRINGS["uk"]["meeting_exp_srt"], "Субтитри для відеоплеєра (.srt)")
        self.assertEqual(STRINGS["uk"]["meeting_exp_vtt"], "Субтитри для сайтів (.vtt)")


if __name__ == "__main__":
    unittest.main()
