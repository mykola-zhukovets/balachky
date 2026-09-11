"""Тести whisper_core.paths.asset_root() (feature/frozen-paths).

asset_root() — єдиний резолвер asset-кореня для fronts/* (заміна крихкого
Path(__file__).parents[N] у theme.py/main_window.py). На відміну від
APP_ROOT/_DATA_DIR (застигають при першому імпорті модуля), рахує
sys.frozen/sys._MEIPASS живцем при кожному виклику — тому тут мокаємо саме
sys, а не whisper_core.paths.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from whisper_core import paths


class AssetRootTests(unittest.TestCase):
    def test_dev_mode_returns_repo_root(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(paths.asset_root(), paths.APP_ROOT)

    def test_frozen_mode_returns_meipass(self):
        fake_meipass = r"C:\Balachky\_internal"
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "_MEIPASS", fake_meipass, create=True):
            self.assertEqual(paths.asset_root(), Path(fake_meipass))

    def test_frozen_without_meipass_falls_back_to_executable_dir(self):
        # onedir-збірка без _MEIPASS (напр. однофайловий екзот) — падає
        # назад на теку exe, як і APP_ROOT у paths.py.
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable",
                             r"C:\Balachky\Balachky.exe"):
            if hasattr(sys, "_MEIPASS"):
                delattr(sys, "_MEIPASS")
            try:
                self.assertEqual(paths.asset_root(), Path(r"C:\Balachky"))
            finally:
                pass


class TelegramSecretPathTests(unittest.TestCase):
    def test_token_path_is_local_app_data_in_dev_and_frozen(self):
        local_app_data = Path(r"C:\Users\tester\AppData\Local")
        expected = local_app_data / "Balachky" / "telegram" / "bot-token.json"
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}):
            for frozen in (False, True):
                with self.subTest(frozen=frozen), \
                        patch.object(paths, "FROZEN", frozen):
                    self.assertEqual(paths.telegram_token_path(), expected)
                    self.assertFalse(
                        paths.telegram_token_path().is_relative_to(paths.APP_ROOT))


class SafeUnderTests(unittest.TestCase):
    """paths.safe_under() — захист від path-traversal (спільний для CLI/MCP).

    Ключова гарантія за порадою рецензента: symlink УСЕРЕДИНІ root, що вказує
    НАЗОВНІ, після resolve() спливає за межі → safe_under має відхилити.
    """

    def test_target_inside_root_is_safe(self):
        with tempfile.TemporaryDirectory() as root:
            inner = Path(root) / "sub" / "file.txt"
            self.assertTrue(paths.safe_under(root, inner))

    def test_root_itself_is_safe(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertTrue(paths.safe_under(root, root))

    def test_dotdot_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            outside = Path(root) / ".." / "evil.txt"
            self.assertFalse(paths.safe_under(root, outside))

    def test_symlink_inside_root_pointing_outside_is_rejected(self):
        # symlink у межах root → ціль назовні: resolve() йде за лінком і
        # спливає за межі → safe_under=False. На Windows створення symlink
        # може вимагати прав/dev-режиму: graceful-skip, якщо ОС відмовила.
        with tempfile.TemporaryDirectory() as root, \
                tempfile.TemporaryDirectory() as outside:
            link = Path(root) / "escape"
            try:
                os.symlink(outside, link, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink недоступний у цьому середовищі: {exc}")
            # сам лінк фізично в root, але його ціль — назовні
            self.assertFalse(paths.safe_under(root, link))
            # і будь-що «під» лінком теж вислизає назовні
            self.assertFalse(paths.safe_under(root, link / "loot.txt"))


class AllocTimestampedPathTests(unittest.TestCase):
    """paths.alloc_timestamped_path() — генерація шляхів з таймстемпом та захистом від колізій."""

    def test_creates_timestamped_path_and_handles_collisions(self):
        with tempfile.TemporaryDirectory() as root:
            import time
            with patch.object(time, "strftime", return_value="2026-08-30_12-00-00"):
                p1 = paths.alloc_timestamped_path(root, ".wav")
                self.assertEqual(p1.name, "2026-08-30_12-00-00.wav")
                p1.touch()

                p2 = paths.alloc_timestamped_path(root, ".wav")
                self.assertEqual(p2.name, "2026-08-30_12-00-00-1.wav")
                p2.touch()

                p3 = paths.alloc_timestamped_path(root, ".wav")
                self.assertEqual(p3.name, "2026-08-30_12-00-00-2.wav")


if __name__ == "__main__":
    unittest.main()
