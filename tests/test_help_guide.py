"""Довідка: локальний README пріоритетно, інакше — сторінка репо; і що пункт
трею «Довідка» справді зʼявляється, коли передано колбек on_help."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fronts.desktop import help as help_mod
from fronts.desktop.tray import Tray
from fronts.desktop.i18n import tr


class OpenUserGuideTests(unittest.TestCase):
    def test_prefers_local_readme_when_bundled(self):
        opened = {}
        with patch.object(help_mod.paths, "bundled_doc",
                          return_value="C:/app/README.md"), \
             patch.object(help_mod, "current_language", return_value="uk"), \
             patch.object(help_mod.QDesktopServices, "openUrl",
                          side_effect=lambda url: opened.update(url=url)):
            help_mod.open_user_guide()
        # відкрито локальний файл, а не мережеву сторінку
        self.assertTrue(opened["url"].isLocalFile())
        self.assertTrue(opened["url"].toLocalFile().endswith("README.md"))

    def test_each_language_asks_for_its_own_readme(self):
        """Довідка мусить брати файл СВОЄЇ мови — і той файл має існувати.

        Мапа мов пережила перейменування README: канонічним став український
        README.md, англійський переїхав у README.en.md, а мапа лишилась
        старою — «en» відкривало користувачеві українську довідку, «uk»
        просило README.uk.md, якого в репозиторії вже немає. Старий тест
        цього не бачив, бо перевіряв лише «локальний важливіший за мережу».
        Тут вимагаємо і правильне ім'я, і наявність файлу в репозиторії."""
        from pathlib import Path
        repo = Path(__file__).resolve().parents[1]
        expected = {"uk": "README.md", "en": "README.en.md"}
        for lang, name in expected.items():
            asked = {}
            # openUrl → False: імітуємо, що браузер не відкрився, і довідка
            # доходить до локального файла. Порядок змінено 25.07: спершу
            # сторінка репозиторію, бо Windows зазвичай не має програми для
            # файлів .md і локальний файл просто не відкривався.
            with patch.object(help_mod.paths, "bundled_doc",
                              side_effect=lambda n: asked.update(name=n)), \
                 patch.object(help_mod, "current_language", return_value=lang), \
                 patch.object(help_mod.QDesktopServices, "openUrl", return_value=False):
                help_mod.open_user_guide()
            self.assertEqual(
                asked.get("name"), name,
                f"для мови «{lang}» довідка має брати {name}")
            self.assertTrue(
                (repo / name).is_file(),
                f"{name} немає в репозиторії — довідка відкриє порожнечу")
            self.assertIn(name, help_mod._REMOTE[lang],
                          f"запасне посилання для «{lang}» веде не на {name}")

    def test_falls_back_to_repo_when_no_local(self):
        opened = {}
        with patch.object(help_mod.paths, "bundled_doc", return_value=None), \
             patch.object(help_mod, "current_language", return_value="en"), \
             patch.object(help_mod.QDesktopServices, "openUrl",
                          side_effect=lambda url: opened.update(url=url)):
            help_mod.open_user_guide()
        self.assertFalse(opened["url"].isLocalFile())
        self.assertIn("github.com", opened["url"].toString())


class TrayHelpItemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _tray(self, **kw):
        base = dict(profile_names=["Загальний"], active="Загальний",
                    memory_on=True, on_switch_profile=lambda *_: None,
                    on_toggle_memory=lambda *_: None, on_reset_memory=lambda: None,
                    on_reload_terms=lambda: None, on_quit=lambda: None)
        base.update(kw)
        t = Tray(**base)
        self.addCleanup(t.icon.hide)
        return t

    def test_help_action_present_and_wired(self):
        clicked = {"n": 0}
        t = self._tray(on_help=lambda: clicked.__setitem__("n", clicked["n"] + 1))
        acts = [a for a in t._menu.actions() if a.text() == tr("tray_help")]
        self.assertEqual(len(acts), 1)
        acts[0].trigger()
        self.assertEqual(clicked["n"], 1)

    def test_no_help_action_without_callback(self):
        t = self._tray()
        acts = [a for a in t._menu.actions() if a.text() == tr("tray_help")]
        self.assertEqual(acts, [])


if __name__ == "__main__":
    unittest.main()
