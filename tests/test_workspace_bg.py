import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from whisper_core.config import Config

# For Qt tests offscreen
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QFileDialog

_APP = QApplication.instance() or QApplication([])


class TestWorkspaceBackgroundConfig(unittest.TestCase):
    def test_default_config_fields(self):
        cfg = Config()
        self.assertEqual(cfg.workspace_bg, "mascot")
        self.assertIsNone(cfg.workspace_custom_bg_path)

    def test_config_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = Path(tmpdir) / "config.toml"
            cfg = Config(workspace_bg="solid", workspace_custom_bg_path="workspace_custom_bg.png")
            cfg.save(config_path=cfg_file)

            loaded = Config.load(config_path=cfg_file)
            self.assertEqual(loaded.workspace_bg, "solid")
            self.assertEqual(loaded.workspace_custom_bg_path, "workspace_custom_bg.png")


class TestWorkspaceBackgroundLogic(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.user_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_dummy_image(self, filename="bg.png", width=100, height=100):
        img_path = self.user_path / filename
        img = QImage(width, height, QImage.Format_RGB32)
        img.fill(QColor("blue"))
        img.save(str(img_path))
        return img_path

    def test_tiled_stack_mode_resolution(self):
        from fronts.desktop.main_window import _TiledStack

        with patch("whisper_core.paths.user_dir", return_value=self.user_path):
            stack = _TiledStack()

            # Case 1: mascot (default)
            cfg = Config(workspace_bg="mascot")
            stack.reload_background(cfg)
            self.assertEqual(stack._bg_mode, "mascot")

            # Case 2: solid
            cfg = Config(workspace_bg="solid")
            stack.reload_background(cfg)
            self.assertEqual(stack._bg_mode, "solid")
            self.assertIsNone(stack._custom_pm)

            # Case 3: custom with valid image
            self._create_dummy_image("workspace_custom_bg.png")
            cfg = Config(workspace_bg="custom", workspace_custom_bg_path="workspace_custom_bg.png")
            stack.reload_background(cfg)
            self.assertEqual(stack._bg_mode, "custom")
            self.assertIsNotNone(stack._custom_pm)
            self.assertFalse(stack._custom_pm.isNull())

            # Case 4: custom with missing file -> fallback to mascot
            cfg = Config(workspace_bg="custom", workspace_custom_bg_path="non_existent.png")
            stack.reload_background(cfg)
            self.assertEqual(stack._bg_mode, "mascot")

            # Case 5: custom with broken/corrupt file -> fallback to mascot
            corrupt_path = self.user_path / "corrupt.png"
            corrupt_path.write_bytes(b"NOT_AN_IMAGE_DATA_12345")
            cfg = Config(workspace_bg="custom", workspace_custom_bg_path="corrupt.png")
            stack.reload_background(cfg)
            self.assertEqual(stack._bg_mode, "mascot")

    def test_tiled_stack_actual_paint_rendering(self):
        """Рендеринг (grab) _TiledStack з користувацьким тлом неквадратного розміру."""
        from fronts.desktop.main_window import _TiledStack

        with patch("whisper_core.paths.user_dir", return_value=self.user_path):
            self._create_dummy_image("workspace_custom_bg.png", width=800, height=400)
            cfg = Config(workspace_bg="custom", workspace_custom_bg_path="workspace_custom_bg.png")

            stack = _TiledStack()
            stack.resize(600, 700)
            stack.reload_background(cfg)

            # Виклик grab() примусово викликає paintEvent та малювання QPainter.
            pix = stack.grab()
            self.assertFalse(pix.isNull())
            self.assertEqual(pix.width(), 600)
            self.assertEqual(pix.height(), 700)

    def test_custom_bg_file_validation_and_cleanup(self):
        from fronts.desktop.pages.settings import validate_custom_bg_file, cleanup_custom_bg_file

        # Test size limit (~20 MB)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            large_file = Path(f.name)
        try:
            with patch.object(Path, "is_file", return_value=True), \
                 patch.object(Path, "stat") as mock_stat:
                mock_stat.return_value.st_size = 21 * 1024 * 1024
                valid, err_key = validate_custom_bg_file(large_file)
                self.assertFalse(valid)
                self.assertEqual(err_key, "set_workspace_bg_err_size")
        finally:
            if large_file.exists():
                large_file.unlink()

        # Test corrupt file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            corrupt_file = Path(f.name)
            f.write(b"invalid header data")
        try:
            valid, err_key = validate_custom_bg_file(corrupt_file)
            self.assertFalse(valid)
            self.assertEqual(err_key, "set_workspace_bg_err_corrupt")
        finally:
            if corrupt_file.exists():
                corrupt_file.unlink()

        # Test valid image
        valid_path = self._create_dummy_image("valid.png")
        valid, err_key = validate_custom_bg_file(valid_path)
        self.assertTrue(valid)
        self.assertIsNone(err_key)

        # Test cleanup
        with patch("whisper_core.paths.user_dir", return_value=self.user_path):
            bg_copy = self.user_path / "workspace_custom_bg.png"
            bg_copy.write_bytes(b"dummy")
            self.assertTrue(bg_copy.exists())

            cfg = Config(workspace_bg="custom", workspace_custom_bg_path="workspace_custom_bg.png")
            cleanup_custom_bg_file(cfg)
            self.assertFalse(bg_copy.exists())
            self.assertIsNone(cfg.workspace_custom_bg_path)

    def test_cleanup_safe_under_security(self):
        """Перевірка, що cleanup_custom_bg_file не видаляє файли поза user_dir()."""
        from fronts.desktop.pages.settings import cleanup_custom_bg_file

        outside_dir = tempfile.TemporaryDirectory()
        try:
            outside_file = Path(outside_dir.name) / "secret.txt"
            outside_file.write_text("critical data")

            with patch("whisper_core.paths.user_dir", return_value=self.user_path):
                cfg = Config(workspace_bg="custom", workspace_custom_bg_path=f"../{outside_file.name}")
                cleanup_custom_bg_file(cfg)
                self.assertTrue(outside_file.exists())
                self.assertEqual(outside_file.read_text(), "critical data")
        finally:
            outside_dir.cleanup()

    def test_bg_choose_file_cleans_orphans(self):
        """Перевірка, що вибір нового файлу видаляє застарілі workspace_custom_bg.* з іншими розширеннями."""
        from fronts.desktop.pages.settings import SettingsPage

        with patch("whisper_core.paths.user_dir", return_value=self.user_path):
            old_png = self.user_path / "workspace_custom_bg.png"
            old_png.write_bytes(b"old png data")
            self.assertTrue(old_png.exists())

            new_src = self._create_dummy_image("my_photo.jpg", width=200, height=200)

            mock_controller = MagicMock()
            mock_controller.cfg = Config()
            mock_controller.update_state.return_value = ("1.0.0", None, "", False)
            mock_controller.delivery_state.return_value = ("", "", "")
            page = SettingsPage(mock_controller)

            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(new_src), "")):
                res = page._on_bg_choose_file()
                self.assertTrue(res)

            self.assertFalse(old_png.exists())
            new_jpg = self.user_path / "workspace_custom_bg.jpg"
            self.assertTrue(new_jpg.exists())


if __name__ == "__main__":
    unittest.main()


class ConfigTomlValidityTests(unittest.TestCase):
    """Урок 24.07: голе None у save() ламало ВЕСЬ config.toml → fail-closed скидання."""

    def test_default_config_saves_valid_toml(self):
        import tomllib
        import tempfile
        from pathlib import Path
        from whisper_core.config import Config
        d = Path(tempfile.mkdtemp())
        c = Config()
        c.vad_threshold = 0.3
        c.save(d / "config.toml")
        text = (d / "config.toml").read_text(encoding="utf-8")
        tomllib.loads(text)  # валідний TOML попри workspace_custom_bg_path=None
        self.assertNotIn(" = None", text)
        loaded = Config.load(d / "config.toml")
        self.assertEqual(loaded.vad_threshold, 0.3)

    def test_custom_bg_path_roundtrips_when_set(self):
        import tempfile
        from pathlib import Path
        from whisper_core.config import Config
        d = Path(tempfile.mkdtemp())
        c = Config()
        c.workspace_bg = "custom"
        c.workspace_custom_bg_path = "workspace_custom_bg.png"
        c.save(d / "config.toml")
        loaded = Config.load(d / "config.toml")
        self.assertEqual(loaded.workspace_bg, "custom")
        self.assertEqual(loaded.workspace_custom_bg_path, "workspace_custom_bg.png")
