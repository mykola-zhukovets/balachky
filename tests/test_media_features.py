"""Контракти пакета медіа-зручностей; без Qt і без аудіопристроїв."""
import tempfile
import unittest
from pathlib import Path

import numpy as np

from whisper_core.meeting.media import (available_formats, export_audio, mix_tracks,
                                        soft_limit, timestamp_range, write_wav)
from whisper_core.meeting.session import MeetingMeta, add_bookmark, create_session, load_meta


class BookmarkRoundTripTests(unittest.TestCase):
    def test_bookmarks_round_trip_in_meeting_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = create_session(Path(tmp), ["mic"])
            add_bookmark(sess.dir, 12.3456, "Домовились про реліз")
            fresh = load_meta(sess.dir)
            self.assertEqual(fresh.bookmarks, [{"timestamp": 12.346, "title": "Домовились про реліз"}])
            self.assertEqual(MeetingMeta.from_json(fresh.to_json()).bookmarks, fresh.bookmarks)


class MixTests(unittest.TestCase):
    def test_mix_preserves_longest_track_and_never_clips(self):
        mixed = mix_tracks([np.full(5, 0.8, dtype=np.float32), np.full(3, 0.8, dtype=np.float32)])
        self.assertEqual(len(mixed), 5)
        self.assertLessEqual(float(np.max(np.abs(mixed))), 1.0)
        self.assertAlmostEqual(float(mixed[-1]), 0.8, places=5)

    def test_two_loud_tracks_stay_below_ceiling(self):
        # Дві гучні доріжки одночасно (сума 1.8) → лімітер утримує піки < 1.0.
        loud = np.full(64, 0.9, dtype=np.float32)
        mixed = mix_tracks([loud, loud])
        self.assertLess(float(np.max(np.abs(mixed))), 1.0)

    def test_quiet_pair_is_unchanged_linear_sum(self):
        # Тиха пара (сума 0.5 < поріг) не чіпається — чиста лінійна сума.
        a = np.full(16, 0.2, dtype=np.float32)
        b = np.full(16, 0.3, dtype=np.float32)
        mixed = mix_tracks([a, b])
        np.testing.assert_allclose(mixed, np.full(16, 0.5, dtype=np.float32), atol=1e-6)

    def test_soft_limit_preserves_sign_and_below_threshold(self):
        x = np.array([-0.9, -0.1, 0.0, 0.1, 0.9], dtype=np.float32)
        out = soft_limit(x)
        # Підпорогові значення не змінені; надпорогові стиснуті, знак збережено.
        self.assertAlmostEqual(float(out[1]), -0.1, places=6)
        self.assertAlmostEqual(float(out[3]), 0.1, places=6)
        self.assertLess(out[0], 0.0)
        self.assertGreater(out[4], 0.0)
        self.assertLessEqual(float(np.max(np.abs(out))), 0.999)

    def test_timestamp_to_frame_range_is_clamped(self):
        self.assertEqual(timestamp_range(-1, 9, 10, 35), (0, 35))
        self.assertEqual(timestamp_range(1.2, 2.7, 10, 100), (12, 27))


class ExportWrapperTests(unittest.TestCase):
    def test_exports_small_synthetic_audio_when_codec_exists(self):
        formats = available_formats()
        if not formats:
            self.skipTest("PyAV export codec unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = write_wav(root / "tiny.wav", np.sin(np.linspace(0, 20, 3200)).astype(np.float32) * 0.1, 16000)
            ext = "mp3" if "mp3" in formats else next(iter(formats))
            out = export_audio([wav], root / f"tiny.{ext}", ext, 96)
            self.assertTrue(out.is_file())
            self.assertGreater(out.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
