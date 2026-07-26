"""Регресії routine diagnostic log: ротація, приватність і безпечний report."""
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from whisper_core.config import Config
from fronts.desktop import crash


class RoutineLogTests(unittest.TestCase):
    def _handler(self, directory):
        path = Path(directory) / "balachky.log"
        return path, crash._file_handler(path)

    def test_rotation_creates_second_file_after_five_megabytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, handler = self._handler(tmp)
            logger = logging.getLogger("test.routine.rotation")
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
            try:
                # Понад 5 МБ фейкових технічних подій, не користувацький текст.
                for _ in range(1200):
                    logger.info("event=fake size=5120 " + "x" * 5100)
            finally:
                logger.removeHandler(handler)
                handler.close()
            self.assertTrue(path.exists())
            self.assertTrue((Path(tmp) / "balachky.log.1").exists())

    def test_diagnostic_helper_never_writes_transcript(self):
        secret_text = "НАДЗВИЧАЙНО_ПРИВАТНА_РОЗШИФРОВКА"
        with tempfile.TemporaryDirectory() as tmp:
            path, handler = self._handler(tmp)
            logger = logging.getLogger("balachky.event")
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
            try:
                crash.diagnostic_event("dictation_finished", transcript=secret_text,
                                       text=secret_text, chars=len(secret_text))
            finally:
                logger.removeHandler(handler)
                handler.close()
            content = path.read_text(encoding="utf-8")
            self.assertIn("event=dictation_finished", content)
            self.assertIn("chars=", content)
            self.assertNotIn(secret_text, content)

    def test_log_level_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            cfg = Config()
            cfg.log_level = "DEBUG"
            cfg.save(path)
            self.assertEqual(Config.load(path).log_level, "DEBUG")

    def test_copied_diagnostics_excludes_secrets_and_vault_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "balachky.log"
            log.write_text("token=abc\nC:/private/.vaultkey\nordinary event\n", encoding="utf-8")
            cfg = Config()
            cfg.password = "do-not-copy"
            cfg.api_token = "also-do-not-copy"
            cfg.model_dir = "C:/private/.vaultkey"
            with patch.object(crash, "LOG_FILE", log):
                report = crash.copy_diagnostics(cfg)
            self.assertNotIn("do-not-copy", report)
            self.assertNotIn("also-do-not-copy", report)
            self.assertNotIn("abc", report)
            self.assertNotIn(".vaultkey", report)
            self.assertIn("ordinary event", report)
