"""Звірка №4 (18.07): нестрокові значення *_dir у config.toml не мають
проходити в Config — інакше Path(int) кидає TypeError повз except
(OSError, ValueError) у споживачах (obsidian/watch/auto-export)."""
import tempfile
import unittest
from pathlib import Path

from whisper_core.config import Config
from whisper_core import obsidian


class DirFieldTypeGuardTests(unittest.TestCase):
    def _load_with(self, toml_text: str) -> Config:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.toml"
            p.write_text(toml_text, encoding="utf-8")
            return Config.load(p)

    def test_non_string_obsidian_dir_ignored(self):
        cfg = self._load_with("obsidian_dir = 123\n")
        self.assertIsNone(cfg.obsidian_dir)     # лишився дефолт, не int

    def test_non_string_watch_dir_ignored(self):
        cfg = self._load_with("watch_dir = 42\n")
        self.assertNotIsInstance(cfg.watch_dir, int)

    def test_valid_string_dir_kept(self):
        cfg = self._load_with('obsidian_dir = "D:/vault"\n')
        self.assertEqual(cfg.obsidian_dir, "D:/vault")


class WriteMarkdownGuardTests(unittest.TestCase):
    def test_int_vault_dir_raises_value_error(self):
        # ValueError (не TypeError!) — щоб ловили наявні except-блоки викликачів
        with self.assertRaises(ValueError):
            obsidian.write_markdown(123, "нота.md", "текст")

    def test_empty_vault_dir_raises_value_error(self):
        with self.assertRaises(ValueError):
            obsidian.write_markdown("   ", "нота.md", "текст")


class DiarizationNumSpeakersCoercionTests(unittest.TestCase):
    def _load_with(self, toml_text: str) -> Config:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.toml"
            p.write_text(toml_text, encoding="utf-8")
            return Config.load(p)

    def test_string_number_coerced_to_int(self):
        cfg = self._load_with('diarization_num_speakers = "3"\n')
        self.assertEqual(cfg.diarization_num_speakers, 3)
        self.assertIsInstance(cfg.diarization_num_speakers, int)

    def test_garbage_string_falls_back_to_none(self):
        cfg = self._load_with('diarization_num_speakers = "сміття"\n')
        self.assertIsNone(cfg.diarization_num_speakers)

    def test_out_of_range_number_falls_back_to_none(self):
        cfg = self._load_with('diarization_num_speakers = 15\n')
        self.assertIsNone(cfg.diarization_num_speakers)


if __name__ == "__main__":
    unittest.main()

