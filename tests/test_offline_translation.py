"""Тести для Кандидата 2: офлайн-переклад UA -> EN на льоту (POST-95)."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from whisper_core.config import Config
from whisper_core.engine import Engine
from fronts import cli


class ConfigTranslationTests(unittest.TestCase):
    def test_default_is_transcribe(self):
        cfg = Config()
        self.assertEqual(cfg.transcription_task, "transcribe")
        self.assertFalse(cfg.translate_to_en)

    def test_task_translate_syncs_and_survives_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.toml"
            cfg = Config()
            cfg.transcription_task = "translate"
            cfg.save(p)

            loaded = Config.load(p)
            self.assertEqual(loaded.transcription_task, "translate")
            self.assertTrue(loaded.translate_to_en)

    def test_translate_to_en_bool_syncs_and_survives_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.toml"
            cfg = Config()
            cfg.translate_to_en = True
            cfg.save(p)

            loaded = Config.load(p)
            self.assertEqual(loaded.transcription_task, "translate")
            self.assertTrue(loaded.translate_to_en)


class EngineTranslationTests(unittest.TestCase):
    def _call_engine(self, cfg, **kwargs):
        with patch("whisper_core.engine.WhisperModel") as MockModel:
            mock_inst = MockModel.return_value
            mock_inst.transcribe.return_value = (
                [SimpleNamespace(text="Hello world", start=0.0, end=1.0, words=[])],
                SimpleNamespace(duration=1.0),
            )
            engine = Engine(cfg)
            res = engine.transcribe("dummy.wav", **kwargs)
            call_kwargs = mock_inst.transcribe.call_args.kwargs
            return res, call_kwargs

    def test_default_passes_transcribe_task(self):
        cfg = Config()
        _, kwargs = self._call_engine(cfg)
        self.assertEqual(kwargs["task"], "transcribe")

    def test_cfg_translate_to_en_passes_translate_task(self):
        cfg = Config()
        cfg.translate_to_en = True
        _, kwargs = self._call_engine(cfg)
        self.assertEqual(kwargs["task"], "translate")

    def test_explicit_task_argument_overrides_cfg(self):
        cfg = Config()
        cfg.transcription_task = "transcribe"
        _, kwargs = self._call_engine(cfg, task="translate")
        self.assertEqual(kwargs["task"], "translate")


class CliTranslationTests(unittest.TestCase):
    def test_cli_translate_flag_sets_task(self):
        cfg_captured = []

        def fake_transcribe(cfg, terms, path):
            cfg_captured.append(cfg)
            return ("raw", "final", 1.0, [], [(0.0, 1.0, "final")])

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "audio.wav"
            p.write_bytes(b"RIFFdummy")
            args = SimpleNamespace(
                file=str(p), model=None, lang="uk", profile=None,
                translate=True, task=None, json=True,
            )
            prof_mock = SimpleNamespace(
                name="default", terms_path=Path(td) / "terms.toml",
                history_path=Path(td) / "history.jsonl", memory_enabled=False,
            )
            prof_mock.terms_path.write_text("", encoding="utf-8")

            with patch("fronts.cli._resolve_profile", return_value=(prof_mock, None)), \
                 patch("fronts.cli.log_history"):
                rc = cli.cmd_transcribe(args, root=Path(td), transcribe_fn=fake_transcribe)
                self.assertEqual(rc, 0)
                self.assertTrue(len(cfg_captured) > 0)
                self.assertEqual(cfg_captured[0].transcription_task, "translate")
                self.assertTrue(cfg_captured[0].translate_to_en)


if __name__ == "__main__":
    unittest.main()
