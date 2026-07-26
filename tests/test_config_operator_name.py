"""feature/evidence-plus: «хто зафіксував» (operator_name) мусить переживати
save→load. Раніше поле було в дефолтах Config, але не в переліку keys методу
save(), тож не писалось у config.toml — і recorded_by кожної наради лишався
порожнім. Цей тест фіксує round-trip, щоб регрес не повернувся."""
import tempfile
import unittest
from pathlib import Path

from whisper_core.config import Config


class OperatorNamePersistenceTests(unittest.TestCase):
    def _round_trip(self, value: str) -> Config:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.toml"
            c = Config()
            c.operator_name = value
            c.save(p)
            return Config.load(p)

    def test_operator_name_survives_save_load(self):
        cfg = self._round_trip("Олена Ковальчук")
        self.assertEqual(cfg.operator_name, "Олена Ковальчук")

    def test_operator_name_written_to_toml(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.toml"
            c = Config()
            c.operator_name = "Андрій Мельник"
            c.save(p)
            self.assertIn("operator_name", p.read_text(encoding="utf-8"))

    def test_default_operator_name_is_empty(self):
        cfg = self._round_trip("")
        self.assertEqual(cfg.operator_name, "")


if __name__ == "__main__":
    unittest.main()
