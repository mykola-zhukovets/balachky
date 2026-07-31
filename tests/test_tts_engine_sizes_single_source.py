"""Розміри пакета озвучення беруться з одного джерела, а не з літералів у екранах.

Знахідка рецензії 25.07: вага завантаження рушія була вписана числами прямо в
settings.py у трьох місцях. Числа випадково збігалися з правдою, але при
перевипуску пакета інтерфейс мовчки збрехав би — саме той дефект, який до цього
показував користувачеві "150 МБ" замість 321 МБ.

Тут доводимо не наявність константи, а те, що ПІДПИС НА ЕКРАНІ справді її
читає: підміняємо джерело — текст картки мусить змінитися. Якщо хтось повернe
літерал у settings.py, тест почервоніє.
"""
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel        # noqa: E402

from fronts.desktop import motion                          # noqa: E402
from fronts.desktop.i18n import human_size                 # noqa: E402
from fronts.desktop.pages.settings import SettingsPage     # noqa: E402
from whisper_core.tts import engine_manager                # noqa: E402
from tests.render_nav_smoke import _NavController, _make_sandbox  # noqa: E402


def _engine_subtext(page) -> str:
    """Текст підпису під кнопкою рушія озвучення в центрі моделей."""
    for lbl in page.findChildren(QLabel):
        if lbl.objectName() == "tts_engine_subtext":
            return lbl.text()
    raise AssertionError("підпису tts_engine_subtext немає — тест нічого не перевіряє")


class EngineSizesSingleSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        motion.init_config(SimpleNamespace(animations=False))
        cls.sandbox = _make_sandbox()

    def test_card_reads_package_constant(self):
        """Без встановленого рушія підпис показує розмір опублікованого пакета."""
        page = SettingsPage(_NavController(self.sandbox))
        text = _engine_subtext(page)
        self.assertIn(human_size(engine_manager.PACKAGE_ARCHIVE_SIZE_BYTES), text)
        self.assertIn(human_size(engine_manager.PACKAGE_EXTRACTED_SIZE_BYTES), text)

    def test_card_follows_changed_source(self):
        """Змінилося джерело — змінився підпис. Літерал у settings.py тут падає."""
        fake_archive, fake_disk = 111_000_000, 222_000_000
        with mock.patch.object(engine_manager, "PACKAGE_ARCHIVE_SIZE_BYTES", fake_archive), \
             mock.patch.object(engine_manager, "PACKAGE_EXTRACTED_SIZE_BYTES", fake_disk):
            page = SettingsPage(_NavController(self.sandbox))
            text = _engine_subtext(page)
        self.assertIn(human_size(fake_archive), text)
        self.assertNotIn(human_size(engine_manager.PACKAGE_ARCHIVE_SIZE_BYTES), text)

    def test_installed_engine_manifest_wins(self):
        """Є маніфест встановленого рушія — числа беруться з нього, не з констант."""
        with mock.patch("whisper_core.paths.tts_engine_dir") as dir_mock:
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as tmp:
                dir_mock.return_value = Path(tmp)
                (Path(tmp) / "engine_manifest.json").write_text(
                    json.dumps({"archive_size_bytes": 7_000_000,
                                "extracted_size_bytes": 9_000_000}), encoding="utf-8")
                archive, extracted = engine_manager.expected_engine_sizes()
        self.assertEqual((archive, extracted), (7_000_000, 9_000_000))

    def test_broken_manifest_falls_back_to_constants(self):
        """Побитий маніфест не валить екран — беремо розміри пакета."""
        with mock.patch("whisper_core.paths.tts_engine_dir") as dir_mock:
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as tmp:
                dir_mock.return_value = Path(tmp)
                (Path(tmp) / "engine_manifest.json").write_text("{зламано", encoding="utf-8")
                sizes = engine_manager.expected_engine_sizes()
        self.assertEqual(sizes, (engine_manager.PACKAGE_ARCHIVE_SIZE_BYTES,
                                 engine_manager.PACKAGE_EXTRACTED_SIZE_BYTES))

    def test_no_size_literals_left_in_settings(self):
        """Замок: у settings.py не лишилось байтових літералів розміру рушія."""
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / "fronts" / "desktop" / "pages" / "settings.py"
        text = src.read_text(encoding="utf-8")
        for literal in ("336909393", "895247902"):
            self.assertNotIn(literal, text,
                             f"розмір {literal} знову вписаний числом у settings.py")


if __name__ == "__main__":
    unittest.main()
