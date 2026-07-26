"""Ліцензія в інсталяторі не має розійтися з ліцензією проєкту.

Inno показує installer\\LICENSE.txt (копія з BOM — без BOM кирилиця стає
кашею). Копія легко застаріє при зміні ліцензії, а користувач побачить перед
встановленням не той текст, який діє. Тому звіряємо їх автоматично.
"""
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_LICENSE = _ROOT / "LICENSE"
_INSTALLER_COPY = _ROOT / "installer" / "LICENSE.txt"
_ISS = _ROOT / "installer" / "balachky.iss"


class InstallerLicenseTests(unittest.TestCase):
    def test_copy_matches_project_license(self):
        self.assertTrue(_INSTALLER_COPY.exists(), "немає installer/LICENSE.txt")
        # utf-8-sig прибирає BOM у копії; порівнюємо самий текст
        original = _LICENSE.read_text(encoding="utf-8")
        copy = _INSTALLER_COPY.read_text(encoding="utf-8-sig")
        self.assertEqual(
            original, copy,
            "installer/LICENSE.txt розійшовся з LICENSE — перезніми копію "
            "(UTF-8 З BOM), інакше інсталятор показує стару ліцензію",
        )

    def test_copy_has_bom(self):
        head = _INSTALLER_COPY.read_bytes()[:3]
        self.assertEqual(head, b"\xef\xbb\xbf",
                         "без BOM Inno читає файл як ANSI — кирилиця стане кашею")

    def test_iss_shows_the_license(self):
        iss = _ISS.read_text(encoding="utf-8")
        self.assertIn("LicenseFile=LICENSE.txt", iss,
                      "інсталятор мусить показувати ліцензію перед встановленням")

    def test_stale_files_are_removed_on_upgrade(self):
        """Оновлення поверх старої версії має зносити її залишки.

        Без [InstallDelete] воркер озвучення зі збірки 1.2.3 (4,2 ГБ з
        torch/CUDA) лишався б на диску назавжди, а engine_available()
        приймав би той чужий exe за наявний рушій (аудит Kimi 24.07)."""
        iss = _ISS.read_text(encoding="utf-8")
        self.assertIn("[InstallDelete]", iss)
        self.assertIn(r'Name: "{app}\_internal"', iss)
        self.assertIn(r'Name: "{app}\balachky-tts-worker.exe"', iss)

    def test_license_is_polyform_not_gpl(self):
        text = _LICENSE.read_text(encoding="utf-8")
        self.assertIn("PolyForm Noncommercial", text)
        self.assertNotIn("GNU GENERAL PUBLIC LICENSE", text.upper())


if __name__ == "__main__":
    unittest.main()
