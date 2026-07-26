"""Режим тестування (детальний дія-журнал) + номер збірки (git-коміт).

Перевіряємо: test_log — no-op поки режим вимкнено; довжини текстів логуються, а
самі тексти лише при include_text; вимкнення режиму повертає рівень за log_level
(DEBUG не персистить); build_version дає «версія (коміт)»; звіт містить компоненти.
"""
import logging
import tempfile
import unittest
import zipfile
from pathlib import Path

from whisper_core.config import Config
from whisper_core import _buildinfo, __version__
from fronts.desktop import crash
from fronts.desktop.report import build_report_zip


class TestModeLogging(unittest.TestCase):
    def setUp(self):
        # ізольований захоплювач подій balachky.event
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "balachky.log"
        self.handler = crash._file_handler(self.path)
        self.logger = logging.getLogger("balachky.event")
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        self._prop = self.logger.propagate
        self.logger.propagate = False

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.handler.close()
        self.logger.propagate = self._prop
        crash.set_test_mode(False)
        self._tmp.cleanup()

    def _content(self):
        return self.path.read_text(encoding="utf-8")

    def test_test_log_is_noop_when_mode_off(self):
        crash.set_test_mode(False)
        crash.test_log("pipe_transcribe", ms=12.3, text_final="секрет")
        self.assertEqual(self._content(), "")

    def test_text_logged_as_length_by_default(self):
        crash.set_test_mode(True, include_text=False)
        crash.test_log("pipe_transcribe", ms=5, text_final="дуже приватний текст")
        content = self._content()
        self.assertIn("test event=pipe_transcribe", content)
        self.assertIn("text_final_len=20", content)
        self.assertNotIn("дуже приватний текст", content)

    def test_text_included_only_when_enabled(self):
        crash.set_test_mode(True, include_text=True)
        crash.test_log("pipe_transcribe", text_final="показати мене")
        content = self._content()
        self.assertIn("text_final_len=13", content)
        self.assertIn("показати мене", content)

    def test_bool_and_number_fields(self):
        crash.set_test_mode(True)
        crash.test_log("pipe_macros", hit=True, ms=1.5)
        content = self._content()
        self.assertIn("hit=true", content)
        self.assertIn("ms=1.5", content)


class ApplyTestMode(unittest.TestCase):
    def tearDown(self):
        crash.set_test_mode(False)
        logging.getLogger().setLevel(logging.INFO)

    def test_enabling_forces_debug(self):
        cfg = Config()
        cfg.test_mode = True
        cfg.log_level = "INFO"
        crash.apply_test_mode(cfg)
        self.assertTrue(crash.test_mode_active())
        self.assertEqual(logging.getLogger().level, logging.DEBUG)

    def test_disabling_restores_configured_level(self):
        cfg = Config()
        cfg.test_mode = False
        cfg.log_level = "WARNING"
        crash.apply_test_mode(cfg)
        self.assertFalse(crash.test_mode_active())
        self.assertEqual(logging.getLogger().level, logging.WARNING)

    def test_round_trip_persists_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            cfg = Config()
            cfg.test_mode = True
            cfg.test_mode_include_text = True
            cfg.save(path)
            loaded = Config.load(path)
            self.assertTrue(loaded.test_mode)
            self.assertTrue(loaded.test_mode_include_text)


class BuildNumber(unittest.TestCase):
    def test_build_version_wraps_commit_in_parens(self):
        self.assertEqual(_buildinfo.build_version("1.0.0"),
                         f"1.0.0 ({_buildinfo.build_commit()})")

    def test_build_commit_is_nonempty_string(self):
        commit = _buildinfo.build_commit()
        self.assertIsInstance(commit, str)
        self.assertTrue(commit)

    def test_report_includes_components_and_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = build_report_zip(
                Path(tmp) / "out", app_version="1.0.0", cfg=None, log_dir=None)
            with zipfile.ZipFile(zip_path) as zf:
                self.assertIn("components.txt", zf.namelist())
                info = zf.read("info.txt").decode("utf-8")
                # версія + коміт у шапці info.txt
                self.assertIn(f"Balachky {__version__} ({_buildinfo.build_commit()})", info)


if __name__ == "__main__":
    unittest.main()
