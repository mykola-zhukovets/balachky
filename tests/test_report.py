"""Юніт-тест збирача zip-звіту про проблему (чиста функція, без Qt)."""
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

from whisper_core import __version__
from fronts.desktop.report import build_report_zip, safe_config_dump


class _Cfg:
    model_name = "large-v3"
    device = "cpu"
    compute_type = "int8"
    log_level = "INFO"
    # приватне поле — НЕ має потрапити у звіт
    meeting_dir = r"X:\Робочі-наради"
    watch_dir = r"X:\Тека-спостереження"


class BuildReportZip(unittest.TestCase):
    def _make_logs(self, log_dir: Path):
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "balachky.log").write_text(
            "2026-07-17T10:00:00 INFO test line\n", encoding="utf-8")
        (log_dir / "balachky.log.1").write_text("rotated\n", encoding="utf-8")

    def test_zip_created_with_log_and_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            log_dir = tmp / "logs_src"
            self._make_logs(log_dir)
            dest = tmp / "desktop"
            now = datetime(2026, 7, 17, 12, 34, 56)

            zip_path = build_report_zip(
                dest, app_version="1.0.0", cfg=_Cfg(), log_dir=log_dir,
                extra_info={"DPI": 96}, now=now)

            self.assertTrue(zip_path.exists())
            self.assertEqual(zip_path.name, "balachky-звіт-2026-07-17_12-34-56.zip")
            with zipfile.ZipFile(zip_path) as zf:
                names = set(zf.namelist())
                self.assertIn("info.txt", names)
                self.assertIn("config-safe.txt", names)
                self.assertIn("logs/balachky.log", names)
                info = zf.read("info.txt").decode("utf-8")
                self.assertIn(f"Balachky {__version__}", info)
                self.assertIn("DPI: 96", info)
                log_text = zf.read("logs/balachky.log").decode("utf-8")
                self.assertIn("test line", log_text)

    def test_config_dump_excludes_private_paths(self):
        dump = safe_config_dump(_Cfg())
        self.assertIn("model_name", dump)
        self.assertNotIn("Робочі-наради", dump)
        self.assertNotIn("meeting_dir", dump)
        self.assertNotIn("watch_dir", dump)

    def test_works_without_log_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = build_report_zip(
                Path(tmp) / "out", app_version="1.0.0", cfg=None, log_dir=None)
            self.assertTrue(zip_path.exists())
            with zipfile.ZipFile(zip_path) as zf:
                self.assertIn("info.txt", zf.namelist())


if __name__ == "__main__":
    unittest.main()
