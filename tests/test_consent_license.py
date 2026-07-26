"""Consent завантаження моделей показує ліцензію з клікабельним посиланням:
пресети Gemma несуть звірену ліцензію й сторінку моделі, а i18n-рядок ліцензії
є в обох мовах і коректно підставляє назву й URL."""
import unittest

from fronts.desktop.i18n import STRINGS, tr, set_language
from whisper_core.protocol import model_manager as mm


class PresetLicenseTests(unittest.TestCase):
    # Ліцензія кожного пресета звірена з метаданими GGUF (general.license):
    #  fast (E4B) = apache-2.0; quality (12B QAT) = gemma. Показуємо чесно в consent.
    _EXPECTED_LICENSE = {"fast": "Apache 2.0", "quality": "Gemma"}

    def test_gemma_presets_carry_verified_license_and_page(self):
        for pid, preset in mm.PRESETS.items():
            self.assertEqual(preset.license_name, self._EXPECTED_LICENSE[pid], pid)
            # посилання — на СТОРІНКУ моделі, не на resolve-URL файлу
            self.assertTrue(preset.page_url.startswith("https://huggingface.co/"),
                            pid)
            self.assertNotIn("/resolve/", preset.page_url, pid)


class ConsentLicenseStringTests(unittest.TestCase):
    def tearDown(self):
        set_language("uk")

    def test_license_string_present_both_languages(self):
        for lang in ("uk", "en"):
            self.assertIn("dl_consent_license", STRINGS[lang])
            self.assertIn("{url}", STRINGS[lang]["dl_consent_license"])
            self.assertIn("{license}", STRINGS[lang]["dl_consent_license"])

    def test_license_renders_name_and_link(self):
        set_language("uk")
        out = tr("dl_consent_license", license="MIT",
                 url="https://huggingface.co/example/model")
        self.assertIn("MIT", out)
        self.assertIn('href="https://huggingface.co/example/model"', out)


if __name__ == "__main__":
    unittest.main()
