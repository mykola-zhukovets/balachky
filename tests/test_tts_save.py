"""Хвиля 1: «Зберегти озвучення» — санітизація/формат/склейка (§8.7, §11.2)."""
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.tts import save


class TestSanitize(unittest.TestCase):
    def test_removes_windows_bad_chars(self):
        out = save.sanitize_filename('на/ра:да*"?<>|.wav')
        for ch in '\\/:*?"<>|':
            self.assertNotIn(ch, out)

    def test_empty_becomes_default(self):
        self.assertEqual(save.sanitize_filename(""), "озвучення")
        self.assertEqual(save.sanitize_filename("..."), "озвучення")

    def test_length_capped(self):
        self.assertLessEqual(len(save.sanitize_filename("х" * 500)), 120)

    def test_keeps_cyrillic(self):
        self.assertEqual(save.sanitize_filename("Нарада 23 липня"), "Нарада 23 липня")


class TestFormat(unittest.TestCase):
    def test_mp3_available_is_bool(self):
        self.assertIsInstance(save.mp3_encoder_available(), bool)


class TestCombine(unittest.TestCase):
    def _wav(self, path, frames=100):
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\x00\x00" * frames)

    def test_combine_wavs(self):
        d = tempfile.mkdtemp(prefix="save-")
        p1 = str(Path(d) / "s0.wav")
        p2 = str(Path(d) / "s1.wav")
        self._wav(p1, 100)
        self._wav(p2, 150)
        out = str(Path(d) / "out.wav")
        save.combine_wavs([p1, p2], out)
        with wave.open(out, "rb") as w:
            self.assertEqual(w.getnframes(), 250)     # склеєно
            self.assertEqual(w.getframerate(), 24000)

    def test_free_space_check(self):
        d = tempfile.mkdtemp(prefix="save-")
        self.assertTrue(save.enough_free_space(d, 10))
        self.assertFalse(save.enough_free_space(d, 10 ** 18))   # завідомо забагато


if __name__ == "__main__":
    unittest.main()
