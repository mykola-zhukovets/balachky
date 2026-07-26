"""Юніти пост-обробки наради (Б3). Чиста математика — без Qt, без реального
аудіо: вхідні сегменти будуємо самі (сирий float32 у tempfile).

Покриття (спека §5.1 + список Б3): склейка сегментів, ресемпл (довжина/
частота), вирівнювання по кадру, кліп-захист, зшивка міток за часом
(перекриття / порожня доріжка / одна доріжка), відновлення після битого
сегмента, round-trip transcript.json.
"""
import json
import tempfile
import tracemalloc
import unittest
import wave
from unittest.mock import patch
from pathlib import Path

import numpy as np

from whisper_core.meeting import postprocess as pp


def _make_session(tmp: str, *, rate: int = 48000, channels: int = 2) -> Path:
    session = Path(tmp) / "2026-07-15_14-30-05"
    session.mkdir(parents=True, exist_ok=True)
    (session / "meeting.json").write_text(
        json.dumps({"rate": rate, "channels": channels}), encoding="utf-8"
    )
    return session


def _write_segments(session: Path, track: str, segments) -> Path:
    """segments: список float32-масивів (interleaved) → 0000.f32, 0001.f32, …"""
    d = session / track
    d.mkdir(parents=True, exist_ok=True)
    for i, seg in enumerate(segments):
        (d / f"{i:04d}.f32").write_bytes(np.asarray(seg, dtype=np.float32).tobytes())
    return d


def _interleave(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    out = np.empty(left.size + right.size, dtype=np.float32)
    out[0::2] = left
    out[1::2] = right
    return out


# ── склейка сегментів ────────────────────────────────────────────────────────

class ReadTrackTests(unittest.TestCase):
    def test_concatenates_segments_in_index_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp)
            _write_segments(session, "mic", [
                np.array([1.0, 2.0], dtype=np.float32),
                np.array([3.0, 4.0], dtype=np.float32),
                np.array([5.0, 6.0], dtype=np.float32),
            ])
            data = pp._read_track_f32(session / "mic")
            np.testing.assert_array_equal(data, [1, 2, 3, 4, 5, 6])

    def test_missing_track_dir_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp)
            self.assertEqual(pp._read_track_f32(session / "sys").size, 0)

    def test_corrupt_tail_segment_is_truncated_not_crashing(self):
        # Битий останній сегмент (краш під час запису): довжина не кратна 4 байтам.
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp)
            d = session / "mic"
            d.mkdir(parents=True)
            (d / "0000.f32").write_bytes(np.array([1.0, 2.0], dtype=np.float32).tobytes())
            # 10 байтів = 2 повні float32 (8 б) + 2 сміттєві → мають лишитись 2
            (d / "0001.f32").write_bytes(
                np.array([3.0, 4.0], dtype=np.float32).tobytes() + b"\x00\x02"
            )
            # 3 байти — жодного повного float32 → сегмент відкидається цілком
            (d / "0002.f32").write_bytes(b"\x01\x02\x03")
            data = pp._read_track_f32(d)
            np.testing.assert_array_equal(data, [1, 2, 3, 4])


# ── вирівнювання по кадру + стерео→моно мікс ─────────────────────────────────

class ToMonoTests(unittest.TestCase):
    def test_stereo_average_is_half_sum(self):
        left = np.ones(4, dtype=np.float32)          # L = 1.0
        right = np.zeros(4, dtype=np.float32)        # R = 0.0
        mono = pp._to_mono(_interleave(left, right), 2)
        np.testing.assert_allclose(mono, 0.5, rtol=0, atol=1e-6)
        self.assertEqual(mono.size, 4)

    def test_partial_trailing_frame_truncated(self):
        # 5 семплів на 2 канали = 2 повні кадри + 1 зайвий → вирівнюємо по коротшій
        interleaved = np.array([1, 1, 2, 2, 9], dtype=np.float32)
        mono = pp._to_mono(interleaved, 2)
        np.testing.assert_array_equal(mono, [1, 2])   # хвостовий «9» відкинуто

    def test_mono_passthrough(self):
        interleaved = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        mono = pp._to_mono(interleaved, 1)
        np.testing.assert_allclose(mono, [0.1, 0.2, 0.3], atol=1e-6)

    def test_empty_input(self):
        self.assertEqual(pp._to_mono(np.empty(0, dtype=np.float32), 2).size, 0)


# ── ресемпл (довжина / частота) ──────────────────────────────────────────────

class ResampleTests(unittest.TestCase):
    def test_integer_decimation_length_and_averaging(self):
        # 300 семплів @48к, factor 3 → 100 семплів @16к; блок [0,1,2] → середнє 1
        mono = np.tile(np.array([0.0, 1.0, 2.0], dtype=np.float32), 100)
        out = pp._resample(mono, 48000, 16000)
        self.assertEqual(out.size, 100)
        np.testing.assert_allclose(out, 1.0, atol=1e-6)

    def test_same_rate_is_noop(self):
        mono = np.linspace(-1, 1, 50, dtype=np.float32)
        out = pp._resample(mono, 16000, 16000)
        np.testing.assert_array_equal(out, mono)

    def test_non_integer_ratio_linear_fallback_length(self):
        # 44100 не кратне 16000 → лінійний ресемпл, перевіряємо довжину
        mono = np.linspace(-1, 1, 441, dtype=np.float32)
        out = pp._resample(mono, 44100, 16000)
        self.assertEqual(out.size, 160)              # round(441 * 16000/44100)

    def test_empty_stays_empty(self):
        self.assertEqual(pp._resample(np.empty(0, dtype=np.float32), 48000, 16000).size, 0)


# ── кліп-захист (float32 → int16) ────────────────────────────────────────────

class ClipTests(unittest.TestCase):
    def test_out_of_range_is_clamped_not_wrapped(self):
        # Без кліпу 2.0*32767=65534 → int16 обгорнеться у -2 (сміття). Кліп рятує.
        mono = np.array([2.0, -2.0, 1.0, -1.0, 0.0], dtype=np.float32)
        pcm = np.frombuffer(pp._float_to_pcm16(mono), dtype="<i2")
        self.assertEqual(pcm[0], 32767)              # +2.0 обрізано до макс, не -2
        self.assertEqual(pcm[1], -32767)             # -2.0 обрізано до -макс
        self.assertEqual(pcm[2], 32767)
        self.assertEqual(pcm[3], -32767)
        self.assertEqual(pcm[4], 0)
        self.assertEqual(pcm.max(), 32767)
        self.assertGreaterEqual(pcm.min(), -32768)

    def test_half_scale_roundtrips(self):
        pcm = np.frombuffer(pp._float_to_pcm16(np.array([0.5], dtype=np.float32)), dtype="<i2")
        self.assertEqual(pcm[0], round(0.5 * 32767))


# ── build_wav / build_session_wavs (повний конвеєр доріжки) ──────────────────

class BuildWavTests(unittest.TestCase):
    def _read_wav(self, path: Path):
        with wave.open(str(path), "rb") as w:
            return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()

    def test_stereo_48k_to_mono_16k_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp, rate=48000, channels=2)
            n = 48000 * 2                            # 2 секунди @48к
            t = np.arange(n) / 48000.0
            tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
            _write_segments(session, "mic", [_interleave(tone, tone)])
            out = pp.build_wav(session, "mic")
            self.assertIsNotNone(out)
            self.assertTrue(out.exists())
            nch, width, rate, frames = self._read_wav(out)
            self.assertEqual(nch, 1)                  # моно
            self.assertEqual(width, 2)                # 16-біт
            self.assertEqual(rate, 16000)            # ресемпльовано
            self.assertEqual(frames, 16000 * 2)      # 2 с @16к = 32000 кадрів

    def test_reads_rate_channels_from_meta_not_hardcoded(self):
        # Мік записаний моно @44100 — build_wav має покластися на meeting.json
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp, rate=44100, channels=1)
            tone = (0.3 * np.sin(np.linspace(0, 200, 44100))).astype(np.float32)
            _write_segments(session, "mic", [tone])
            out = pp.build_wav(session, "mic")
            self.assertIsNotNone(out)
            _, _, rate, frames = self._read_wav(out)
            self.assertEqual(rate, 16000)
            self.assertEqual(frames, 16000)          # 1 с @16к

    def test_streaming_wav_matches_legacy_small_track_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp, rate=48000, channels=2)
            left = np.array([0.0, 0.3, -0.2, 0.7, -0.9, 0.1] * 9, dtype=np.float32)
            right = np.array([0.1, -0.1, 0.4, 0.2, -0.7, 0.0] * 9, dtype=np.float32)
            interleaved = _interleave(left, right)
            _write_segments(session, "mic", [interleaved[:37], interleaved[37:]])
            # Еталон старого full-RAM конвеєра.
            legacy = pp._read_track_f32(session / "mic")
            expected = pp._float_to_pcm16(pp._resample(
                pp._to_mono(legacy, 2), 48000, 16000))
            out = pp.build_wav(session, "mic")
            with wave.open(str(out), "rb") as w:
                self.assertEqual(w.readframes(w.getnframes()), expected)

    def test_streaming_multiblock_44100_matches_legacy_with_bounded_allocation(self):
        # >1 MiB сирого float32 у ЄДИНОМУ сегменті: read() мусить пройти щонайменше
        # два _STREAM_RAW_BYTES-блоки; 44.1 кГц іде через non-integer resample.
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp, rate=44100, channels=1)
            samples = pp._STREAM_RAW_BYTES // 4 + 4097
            signal = (0.6 * np.sin(np.linspace(0, 400, samples))).astype(np.float32)
            _write_segments(session, "mic", [signal])
            expected = pp._float_to_pcm16(pp._resample(signal, 44100, 16000))

            # Не дозволяємо випадково повернутися до legacy full-track reader;
            # tracemalloc дає верхню межу Python-алокацій саме цього проходу.
            tracemalloc.start()
            try:
                with patch.object(pp, "_read_track_f32",
                                  side_effect=AssertionError("not streaming")):
                    out = pp.build_wav(session, "mic")
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

            with wave.open(str(out), "rb") as w:
                self.assertEqual(w.readframes(w.getnframes()), expected)
            self.assertLess(peak, pp._STREAM_RAW_BYTES * 8)

    def test_pure_silence_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp, channels=2)
            zeros = np.zeros(48000 * 2, dtype=np.float32)   # 1 с тиші стерео
            _write_segments(session, "mic", [zeros])
            self.assertIsNone(pp.build_wav(session, "mic"))

    def test_empty_track_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp)
            self.assertIsNone(pp.build_wav(session, "sys"))

    def test_survives_corrupt_tail_segment(self):
        # Пост-обробка не падає на битому останньому сегменті (краш під час запису)
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp, channels=2)
            n = 48000
            tone = (0.4 * np.sin(np.linspace(0, 300, n))).astype(np.float32)
            good = _interleave(tone, tone)
            _write_segments(session, "mic", [good])
            # дописуємо битий сегмент вручну (довжина не кратна кадру)
            (session / "mic" / "0001.f32").write_bytes(b"\x00\x01\x02")
            out = pp.build_wav(session, "mic")
            self.assertIsNotNone(out)
            self.assertTrue(out.exists())

    def test_build_session_wavs_only_nonempty(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp, channels=2)
            tone = (0.5 * np.sin(np.linspace(0, 300, 48000))).astype(np.float32)
            _write_segments(session, "mic", [_interleave(tone, tone)])
            # sys — сама тиша → у результат не потрапляє
            _write_segments(session, "sys", [np.zeros(48000 * 2, dtype=np.float32)])
            result = pp.build_session_wavs(session)
            self.assertIn("mic", result)
            self.assertNotIn("sys", result)
            self.assertTrue(result["mic"].exists())


# ── зшивка транскриптів (діаризація без моделі) ──────────────────────────────

class StitchTests(unittest.TestCase):
    def test_two_tracks_overlap_chronological_with_labels(self):
        mic = [(0.0, 2.0, "мік перший"), (5.0, 6.0, "мік другий")]
        sys = [(1.0, 3.0, "сис перший"), (4.0, 4.5, "сис другий")]
        result = pp.stitch(mic, sys)
        self.assertEqual([u.start for u in result], [0.0, 1.0, 4.0, 5.0])
        self.assertEqual(
            [u.speaker for u in result],
            [pp.SPK_ME, pp.SPK_OTHERS, pp.SPK_OTHERS, pp.SPK_ME],
        )
        self.assertEqual(result[0].text, "мік перший")

    def test_single_track_sys_none_all_single_no_labels(self):
        mic = [(2.0, 3.0, "друга"), (0.0, 1.0, "перша")]
        result = pp.stitch(mic, None)
        self.assertEqual([u.speaker for u in result], [pp.SPK_SINGLE, pp.SPK_SINGLE])
        self.assertEqual([u.text for u in result], ["перша", "друга"])   # відсортовано

    def test_empty_sys_track_treated_as_single(self):
        mic = [(0.0, 1.0, "сам")]
        result = pp.stitch(mic, [])
        self.assertEqual(result[0].speaker, pp.SPK_SINGLE)

    def test_mic_empty_sys_present_is_single(self):
        sys = [(0.0, 1.0, "лише система")]
        result = pp.stitch([], sys)
        self.assertEqual([u.speaker for u in result], [pp.SPK_SINGLE])

    def test_both_empty_returns_empty(self):
        self.assertEqual(pp.stitch([], None), [])

    def test_blank_text_segments_dropped(self):
        mic = [(0.0, 1.0, "  "), (1.0, 2.0, "є")]
        result = pp.stitch(mic, None)
        self.assertEqual([u.text for u in result], ["є"])

    def test_adjacent_same_speaker_not_merged(self):
        mic = [(0.0, 1.0, "а"), (1.0, 2.0, "б")]
        result = pp.stitch(mic, None)
        self.assertEqual(len(result), 2)             # не зливаємо (MVP)


# ── формат тексту / json + write_transcript ──────────────────────────────────

class TranscriptTextTests(unittest.TestCase):
    def test_fmt_ts_minutes_seconds_and_hours(self):
        self.assertEqual(pp._fmt_ts(0), "00:00")
        self.assertEqual(pp._fmt_ts(65), "01:05")
        self.assertEqual(pp._fmt_ts(3661), "1:01:01")

    def test_labeled_lines_two_tracks(self):
        utts = pp.stitch([(0.0, 1.0, "привіт")], [(2.0, 3.0, "вітаю")])
        text = pp.to_transcript_text(utts, me_label="Я", others_label="Співрозмовники")
        self.assertEqual(
            text,
            "[00:00] Я: привіт\n[00:02] Співрозмовники: вітаю",
        )

    def test_single_track_no_labels(self):
        utts = pp.stitch([(0.0, 1.0, "суцільно")], None)
        text = pp.to_transcript_text(utts, me_label="Я", others_label="Співрозмовники")
        self.assertEqual(text, "[00:00] суцільно")
        self.assertNotIn("Я:", text)

    def test_json_roundtrip(self):
        utts = pp.stitch([(0.0, 1.5, "a")], [(0.5, 2.0, "b")])
        data = pp.to_transcript_json(utts)
        restored = json.loads(json.dumps(data))
        self.assertEqual(restored, [
            {"start": 0.0, "end": 1.5, "speaker": "me", "source": "me", "text": "a"},
            {"start": 0.5, "end": 2.0, "speaker": "others", "source": "others", "text": "b"},
        ])


class SourceTagTests(unittest.TestCase):
    """Мітка джерела «я/співрозмовник» — «діаризація для бідних»."""

    def test_stitch_two_tracks_tag_source_by_origin(self):
        utts = pp.stitch([(0.0, 1.0, "мій")], [(2.0, 3.0, "їхній")])
        self.assertEqual([u.source for u in utts], [pp.SPK_ME, pp.SPK_OTHERS])

    def test_stitch_single_mic_keeps_source_but_no_speaker_label(self):
        # Одна доріжка: speaker=single (без мітки мовця), але джерело збережене.
        utts = pp.stitch([(0.0, 1.0, "сам")], None)
        self.assertEqual(utts[0].speaker, pp.SPK_SINGLE)
        self.assertEqual(utts[0].source, pp.SPK_ME)

    def test_stitch_single_sys_source_is_others(self):
        utts = pp.stitch([], [(0.0, 1.0, "лише система")])
        self.assertEqual(utts[0].source, pp.SPK_OTHERS)

    def test_stitch_tracks_multimic_source_is_track_key(self):
        utts = pp.stitch_tracks({"mic1": [(0.0, 1.0, "а")], "mic2": [(1.0, 2.0, "б")]})
        by_text = {u.text: u.source for u in utts}
        self.assertEqual(by_text, {"а": "mic1", "б": "mic2"})

    def test_json_omits_empty_source_for_legacy_utterance(self):
        # Utterance без джерела (старий шлях) → у JSON поля source немає.
        item = pp.to_transcript_json([pp.Utterance(0.0, 1.0, pp.SPK_SINGLE, "x")])[0]
        self.assertNotIn("source", item)

    def test_migration_old_json_without_source_reads_with_default(self):
        # Стара transcript.json-репліка без поля source читається як раніше.
        old = {"start": 0.0, "end": 1.0, "speaker": "me", "text": "старий запис"}
        u = pp.Utterance(**old)
        self.assertEqual(u.source, "")
        self.assertEqual(u.text, "старий запис")

    def test_show_source_false_drops_me_others_labels(self):
        utts = pp.stitch([(0.0, 1.0, "мій")], [(2.0, 3.0, "їхній")])
        text = pp.to_transcript_text(
            utts, me_label="Я", others_label="Співрозмовники", show_source=False)
        self.assertNotIn("Я:", text)
        self.assertNotIn("Співрозмовники:", text)
        self.assertIn("мій", text)
        self.assertIn("їхній", text)

    def test_show_source_false_keeps_diarization_speaker_names(self):
        # Діаризація має ПРІОРИТЕТ: її імена лишаються навіть при знятому чекбоксі.
        utts = [pp.Utterance(0.0, 1.0, "speaker_1", "репліка", source=pp.SPK_OTHERS)]
        names = {"speaker_1": "Олег"}
        text = pp.to_transcript_text(
            utts, me_label="Я", others_label="Співрозмовники",
            speaker_names=names, show_source=False)
        self.assertIn("Олег: репліка", text)

    def test_markdown_show_source_false_no_none_header(self):
        # Мік (джерело) + діаризований sys: знятий чекбокс не має дати «## None».
        utts = [
            pp.Utterance(0.0, 1.0, pp.SPK_ME, "моя", source=pp.SPK_ME),
            pp.Utterance(1.0, 2.0, "speaker_1", "його", source=pp.SPK_OTHERS),
        ]
        md = pp.to_transcript_markdown(
            utts, me_label="Я", others_label="Співрозмовники",
            speaker_names={"speaker_1": "Олег"}, show_source=False)
        self.assertNotIn("## None", md)
        self.assertNotIn("## Я", md)
        self.assertIn("## Олег", md)

    def test_markdown_chronological_interleaves_speakers(self):
        # С6: MD хронологічний — заголовок міняється при зміні мовця, репліки НЕ
        # групуються, тож порядок збігається з TXT/JSON.
        utts = pp.stitch(
            [(0.0, 1.0, "моє перше"), (4.0, 5.0, "моє друге")],
            [(2.0, 3.0, "його між")])
        md = pp.to_transcript_markdown(
            utts, me_label="Я", others_label="Співрозмовники")
        self.assertLess(md.index("моє перше"), md.index("його між"))
        self.assertLess(md.index("його між"), md.index("моє друге"))
        self.assertEqual(md.count("## Я"), 2)
        self.assertEqual(md.count("## Співрозмовники"), 1)


class WriteTranscriptTests(unittest.TestCase):
    def test_writes_both_files_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp)
            utts = pp.stitch([(0.0, 1.0, "мій")], [(1.0, 2.0, "їхній")])
            txt_path, json_path = pp.write_transcript(
                session, utts, me_label="Я", others_label="Співрозмовники"
            )
            self.assertTrue(txt_path.exists() and json_path.exists())
            text = txt_path.read_text(encoding="utf-8")
            self.assertIn("Я: мій", text)
            self.assertIn("Співрозмовники: їхній", text)
            # порядок за часом: «Я» перед «Співрозмовники»
            self.assertLess(text.index("Я: мій"), text.index("Співрозмовники: їхній"))
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual([u["speaker"] for u in loaded], ["me", "others"])

    def test_single_track_txt_has_no_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp)
            utts = pp.stitch([(0.0, 1.0, "монолог")], None)
            txt_path, _ = pp.write_transcript(
                session, utts, me_label="Я", others_label="Співрозмовники"
            )
            text = txt_path.read_text(encoding="utf-8")
            self.assertIn("монолог", text)
            self.assertNotIn("Я:", text)
            self.assertNotIn("Співрозмовники:", text)


class RedactionTests(unittest.TestCase):
    def test_redact_marks_only_overlapping_and_keeps_original(self):
        utts = [
            pp.Utterance(0.0, 2.0, pp.SPK_SINGLE, "до діапазону"),
            pp.Utterance(3.0, 6.0, pp.SPK_SINGLE, "чутливе"),      # перетинає [4,5)
            pp.Utterance(7.0, 9.0, pp.SPK_SINGLE, "після діапазону"),
        ]
        out = pp.redact_utterances(utts, 4.0, 5.0, marker="[вилучено]")
        self.assertEqual([u.text for u in out],
                         ["до діапазону", "[вилучено]", "після діапазону"])
        self.assertEqual(utts[1].text, "чутливе")   # оригінал недоторканий
        self.assertEqual(out[1].start, 3.0)         # таймкоди/мовця збережено
        self.assertEqual(out[1].speaker, pp.SPK_SINGLE)

    def test_redact_boundary_touch_is_not_overlap(self):
        # Дотик краєм ([2,4) і [4,6)) — НЕ перетин, репліки не редагуються.
        utts = [pp.Utterance(2.0, 4.0, pp.SPK_SINGLE, "ліворуч"),
                pp.Utterance(4.0, 6.0, pp.SPK_SINGLE, "праворуч")]
        out = pp.redact_utterances(utts, 4.0, 4.0, marker="[x]")   # порожній діапазон
        self.assertEqual([u.text for u in out], ["ліворуч", "праворуч"])

    def test_append_transcript_note_adds_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp)
            (Path(session) / "transcript.txt").write_text("рядок один\n", encoding="utf-8")
            pp.append_transcript_note(session, "фрагмент 0:04–0:05 вилучено")
            text = (Path(session) / "transcript.txt").read_text(encoding="utf-8")
            self.assertIn("рядок один", text)
            self.assertTrue(text.rstrip().endswith("фрагмент 0:04–0:05 вилучено"))


if __name__ == "__main__":
    unittest.main()
