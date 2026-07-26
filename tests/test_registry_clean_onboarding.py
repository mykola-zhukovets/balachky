"""Регресія хвилі №1: «чиста деінсталяція» лишала прапорець ``onboarded``
у реєстрі (HKCU\\Software\\Balachky\\Balachky), тож після перевстановлення
майстер першого запуску не показувався (баг підтверджений живим тестом).

Фікс — ДВА незалежні сигнали. Майстер показуємо, коли:
  • реєстровий прапорець ``onboarded`` відсутній,  АБО
  • конфіг-файла (config.toml) немає — навіть якщо реєстр каже «onboarded».
Dev-виняток: ``WHISPER_TYPER_MODELS`` (моделі вже у дев-кеші) → майстра нема.

Тут перевіряємо чисту функцію рішення ``_should_show_onboarding`` на
тимчасовому QSettings-scope (IniFormat у temp-теці — реального реєстру не
чіпаємо) і звіряємо, що ім'я org/app у тесті збігається з реальним у app.py.
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings

from fronts.desktop.app import _should_show_onboarding

# Реальні org/app QSettings у застосунку. Тест звіряє їх із джерелом app.py
# (нижче), тож розбіжність між тестом і кодом впаде як помилка.
ORG = "Balachky"
APP = "Balachky"


class TempSettings:
    """QSettings у власній temp-теці (IniFormat) — ізоляція від реального
    реєстру. Кожен екземпляр отримує свій файл, тож тести не течуть один в
    одного."""

    def __init__(self):
        self._dir = tempfile.mkdtemp(prefix="balachky-settings-")
        path = str(Path(self._dir) / "settings.ini")
        self.settings = QSettings(path, QSettings.IniFormat)

    def set_onboarded(self):
        self.settings.setValue("onboarded", 1)
        self.settings.sync()


class ShouldShowOnboardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_fresh_install_no_flag_no_config(self):
        # чистий перший запуск: прапорця нема, конфіга нема → майстер показуємо
        s = TempSettings()
        self.assertTrue(_should_show_onboarding(s.settings, False, None))

    def test_normal_start_flag_and_config(self):
        # звичайний старт: прапорець є, конфіг є → майстра НЕ показуємо
        s = TempSettings()
        s.set_onboarded()
        self.assertFalse(_should_show_onboarding(s.settings, True, None))

    def test_dirty_uninstall_flag_survived_but_config_gone(self):
        # ЯДРО БАГУ: реєстровий прапорець пережив деінсталяцію, а конфіга нема
        # → другий сигнал вмикає майстер попри «onboarded».
        s = TempSettings()
        s.set_onboarded()
        self.assertTrue(_should_show_onboarding(s.settings, False, None))

    def test_no_flag_but_config_present(self):
        # прапорця нема (напр. реєстр вичищено), конфіг лишився → майстер
        s = TempSettings()
        self.assertTrue(_should_show_onboarding(s.settings, True, None))

    def test_dev_models_env_suppresses_wizard(self):
        # dev-кейс: моделі у дев-кеші → майстра нема навіть без прапорця/конфіга
        s = TempSettings()
        self.assertFalse(_should_show_onboarding(s.settings, False, "1"))

    def test_dev_models_env_wins_over_everything(self):
        s = TempSettings()
        s.set_onboarded()
        self.assertFalse(_should_show_onboarding(s.settings, True, "/dev/models"))

    def test_registry_and_real_config_file_truth_table(self):
        """Повна матриця двох незалежних startup-сигналів.

        На відміну від точкових тестів вище, тут другий сигнал приходить від
        реального ``Path.exists()``, як у ``main()``, а не з вручну заданого bool.
        """
        cases = (
            # onboarded, config.toml, show wizard
            (False, False, True),
            (False, True, True),
            (True, False, True),
            (True, True, False),
        )
        with tempfile.TemporaryDirectory(prefix="balachky-onboarding-") as d:
            config_path = Path(d) / "config.toml"
            for onboarded, config_exists, expected in cases:
                with self.subTest(onboarded=onboarded,
                                  config_exists=config_exists):
                    settings_path = Path(d) / "settings.ini"
                    settings = QSettings(str(settings_path), QSettings.IniFormat)
                    settings.clear()
                    if onboarded:
                        settings.setValue("onboarded", 1)
                    settings.sync()
                    if config_exists:
                        config_path.write_text("[app]\n", encoding="utf-8")
                    else:
                        config_path.unlink(missing_ok=True)

                    self.assertEqual(
                        _should_show_onboarding(
                            settings, config_path.exists(), models_env=None),
                        expected,
                    )

    def test_org_app_names_match_source(self):
        # Захист від дрейфу: ім'я org/app у тесті мусить збігатися з реальним
        # викликом QSettings у app.py (інакше деінсталятор чистив би не той ключ).
        src = Path(__file__).resolve().parents[1] / "fronts" / "desktop" / "app.py"
        text = src.read_text(encoding="utf-8")
        self.assertIn(f'QSettings("{ORG}", "{APP}")', text)
        # рішення онбордингу теж читає саме цей scope
        self.assertRegex(
            text,
            r'_should_show_onboarding\(\s*settings\b',
        )


if __name__ == "__main__":
    unittest.main()
