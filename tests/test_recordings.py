"""Юніти диктофона (feature/player-recordings): збереження/перелік/видалення
записів — ядро whisper_core.recordings. Без Qt, без реального аудіо: аудіо —
згенерований numpy-масив, диск — tempfile."""
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from whisper_core import recordings


class SaveTests(unittest.TestCase):
    def test_saves_valid_wav_with_expected_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = np.zeros(16000, dtype=np.float32)   # рівно 1 с при 16 кГц
            out = recordings.save_recording(tmp, audio, 16000)
            self.assertIsNotNone(out)
            self.assertTrue(out.exists())
            self.assertTrue(out.name.endswith(".wav"))
            self.assertTrue(recordings.is_safe_recording_name(out.name))
            with wave.open(str(out), "rb") as w:
                self.assertEqual(w.getnchannels(), 1)
                self.assertEqual(w.getsampwidth(), 2)
                self.assertEqual(w.getframerate(), 16000)
                self.assertEqual(w.getnframes(), 16000)

    def test_empty_audio_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(recordings.save_recording(tmp, None, 16000))
            self.assertIsNone(
                recordings.save_recording(tmp, np.zeros(0, dtype=np.float32), 16000))

    def test_clip_protection_on_out_of_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = np.array([2.0, -2.0, 0.5], dtype=np.float32)  # поза [-1,1]
            out = recordings.save_recording(tmp, audio, 16000)
            with wave.open(str(out), "rb") as w:
                data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
            self.assertEqual(data[0], 32767)     # +2.0 обрізано до +повна шкала
            self.assertEqual(data[1], -32767)    # -2.0 обрізано до -повна шкала

    def test_collision_gets_numeric_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = np.zeros(1000, dtype=np.float32)
            with patch.object(recordings.time, "strftime",
                              return_value="2026-07-16_14-30-05"):
                a = recordings.save_recording(tmp, audio, 16000)
                b = recordings.save_recording(tmp, audio, 16000)
            self.assertEqual(a.name, "2026-07-16_14-30-05.wav")
            self.assertEqual(b.name, "2026-07-16_14-30-05-1.wav")


class WriterTests(unittest.TestCase):
    """RecordingWriter — стрімінг диктофона на диск (без буферизації в RAM)."""

    def test_streams_chunks_to_valid_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = recordings.RecordingWriter(tmp, 16000)
            for _ in range(4):
                w.write(np.zeros(8000, dtype=np.float32))   # 4×0.5 с
            out = w.close()
            self.assertIsNotNone(out)
            with wave.open(str(out), "rb") as wf:
                self.assertEqual(wf.getnframes(), 32000)    # заголовок виправлено
                self.assertEqual(wf.getframerate(), 16000)

    def test_too_short_recording_deleted_with_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = recordings.RecordingWriter(tmp, 16000)
            w.write(np.zeros(1000, dtype=np.float32))       # 0.06 с < MIN_SECONDS
            path = w.path
            self.assertIsNone(w.close())
            self.assertFalse(path.exists())

    def test_abort_deletes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = recordings.RecordingWriter(tmp, 16000)
            w.write(np.zeros(16000, dtype=np.float32))
            path = w.path
            w.abort()
            self.assertFalse(path.exists())

    def test_write_after_close_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = recordings.RecordingWriter(tmp, 16000)
            w.write(np.zeros(16000, dtype=np.float32))
            out = w.close()
            w.write(np.zeros(16000, dtype=np.float32))      # спізнілий callback
            with wave.open(str(out), "rb") as wf:
                self.assertEqual(wf.getnframes(), 16000)    # не доросло


class ListTests(unittest.TestCase):
    def test_missing_dir_returns_empty(self):
        self.assertEqual(recordings.list_recordings(Path("/no/such/dir")), [])

    def test_lists_newest_first_with_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = np.zeros(8000, dtype=np.float32)     # 0.5 с
            with patch.object(recordings.time, "strftime",
                              return_value="2026-07-16_10-00-00"):
                old = recordings.save_recording(tmp, audio, 16000)
            with patch.object(recordings.time, "strftime",
                              return_value="2026-07-16_11-00-00"):
                new = recordings.save_recording(tmp, audio, 16000)
            import os
            os.utime(old, (1000, 1000))
            os.utime(new, (2000, 2000))
            recs = recordings.list_recordings(tmp)
            self.assertEqual([r.name for r in recs], [new.name, old.name])
            self.assertAlmostEqual(recs[0].duration, 0.5, places=2)
            self.assertGreater(recs[0].size, 0)


class DeleteTests(unittest.TestCase):
    def test_deletes_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = recordings.save_recording(
                tmp, np.zeros(1000, dtype=np.float32), 16000)
            self.assertTrue(recordings.delete_recording(tmp, out.name))
            self.assertFalse(out.exists())
            # повторний виклик безпечний (файлу вже нема)
            self.assertFalse(recordings.delete_recording(tmp, out.name))

    def test_rejects_unsafe_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp).parent / "victim.wav"
            outside.write_bytes(b"x")
            try:
                self.assertFalse(
                    recordings.delete_recording(tmp, "..\\victim.wav"))
                self.assertFalse(recordings.delete_recording(tmp, "evil.txt"))
                self.assertTrue(outside.exists())   # traversal не спрацював
            finally:
                outside.unlink()

    def test_is_safe_name(self):
        self.assertTrue(
            recordings.is_safe_recording_name("2026-07-16_14-30-05.wav"))
        self.assertTrue(
            recordings.is_safe_recording_name("2026-07-16_14-30-05-2.wav"))
        self.assertFalse(recordings.is_safe_recording_name("2026-07-16.wav"))
        self.assertFalse(recordings.is_safe_recording_name("..\\x.wav"))
        self.assertFalse(recordings.is_safe_recording_name("x.mp3"))


class ConfigTests(unittest.TestCase):
    def test_recordings_dir_roundtrip(self):
        from whisper_core.config import Config
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            cfg = Config()
            cfg.recordings_dir = str(Path(d) / "recs")
            cfg.save(p)
            self.assertEqual(Config.load(p).recordings_dir, str(Path(d) / "recs"))

    def test_recordings_dir_not_written_when_none(self):
        from whisper_core.config import Config
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            Config().save(p)
            self.assertNotIn("recordings_dir", p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
