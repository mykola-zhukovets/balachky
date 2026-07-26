"""Майстер після оновлення програми (скарга власника 25.07: "де ті 6 кроків").

Реєстровий прапорець ``onboarded`` і наявність config.toml (тест_registry_
clean_onboarding.py) не міняються — тут третій, ДОДАТКОВИЙ сигнал: версія, на
якій майстер востаннє пройдено (``onboarded_version``). Показуємо майстер,
коли вона відрізняється від ``whisper_core.__version__`` (порожнє/відсутнє
значення = «давня версія»).

ГОЛОВНА ПАСТКА, яку тут ловимо: повторний прохід НЕ МАЄ стирати вже
налаштоване (мову/хоткей/модель) хардкод-дефолтами майстра — майстер мусить
бути передзаповнений з наявного cfg, а ``_apply_onboarding_result`` — писати
в cfg лише те, що людина справді змінила.
"""
import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings

from fronts.desktop.app import (
    _should_show_onboarding, _apply_onboarding_result,
    _is_onboarding_repeat, _handle_onboarding_dismissed,
    _build_onboarding_wizard,
)
from fronts.desktop.onboarding import FirstRunWizard
from whisper_core.config import Config

CURRENT_VERSION = "1.2.3"
OLDER_VERSION = "1.2.2"


class TempSettings:
    """QSettings у власній temp-теці (IniFormat) — ізоляція від реального
    реєстру, як у test_registry_clean_onboarding.py."""

    def __init__(self):
        self._dir = tempfile.mkdtemp(prefix="balachky-onbver-settings-")
        path = str(Path(self._dir) / "settings.ini")
        self.settings = QSettings(path, QSettings.IniFormat)

    def set_onboarded(self, version=None):
        self.settings.setValue("onboarded", 1)
        if version is not None:
            self.settings.setValue("onboarded_version", version)
        self.settings.sync()


class ShouldShowOnboardingVersionTests(unittest.TestCase):
    """Пункт 1 і 5 (перші три тести-критерії) завдання."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_fresh_profile_shows_wizard(self):
        # свіжий профіль (немає прапорця, немає конфіга) → майстер показується
        s = TempSettings()
        self.assertTrue(
            _should_show_onboarding(s.settings, False, None,
                                    current_version=CURRENT_VERSION))

    def test_same_profile_same_version_no_wizard(self):
        # той самий профіль, ТА САМА версія → майстра НЕМА
        s = TempSettings()
        s.set_onboarded(version=CURRENT_VERSION)
        self.assertFalse(
            _should_show_onboarding(s.settings, True, None,
                                    current_version=CURRENT_VERSION))

    def test_same_profile_newer_version_shows_wizard(self):
        # той самий профіль, НОВІША версія → майстер показується
        s = TempSettings()
        s.set_onboarded(version=OLDER_VERSION)
        self.assertTrue(
            _should_show_onboarding(s.settings, True, None,
                                    current_version=CURRENT_VERSION))

    def test_missing_saved_version_treated_as_old(self):
        # onboarded=1 давно, «onboarded_version» ще жодного разу не писали
        # (апгрейд з версії, що version-сигналу не мала) → показуємо
        s = TempSettings()
        s.set_onboarded(version=None)
        self.assertTrue(
            _should_show_onboarding(s.settings, True, None,
                                    current_version=CURRENT_VERSION))

    def test_empty_saved_version_treated_as_old(self):
        s = TempSettings()
        s.settings.setValue("onboarded", 1)
        s.settings.setValue("onboarded_version", "")
        s.settings.sync()
        self.assertTrue(
            _should_show_onboarding(s.settings, True, None,
                                    current_version=CURRENT_VERSION))

    def test_no_current_version_keeps_old_behaviour(self):
        # виклик без 4-го аргументу (старі тести/старі виклики) — версійний
        # сигнал вимкнено, поведінка та сама, що й до цього завдання
        s = TempSettings()
        s.set_onboarded(version=OLDER_VERSION)
        self.assertFalse(_should_show_onboarding(s.settings, True, None))

    def test_dev_models_env_still_wins_over_version(self):
        s = TempSettings()
        s.set_onboarded(version=OLDER_VERSION)
        self.assertFalse(
            _should_show_onboarding(s.settings, True, "1",
                                    current_version=CURRENT_VERSION))


class DontShowAgainTests(unittest.TestCase):
    """Пункт 4 завдання: позначка «більше не показувати» запам'ятовує версію
    без проходу кроків, і наступний запуск майстра не дає."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_dont_show_again_remembers_version(self):
        s = TempSettings()
        s.set_onboarded(version=OLDER_VERSION)
        wizard = SimpleNamespace(dont_show_again=True)

        _handle_onboarding_dismissed(wizard, s.settings, CURRENT_VERSION)

        self.assertEqual(s.settings.value("onboarded_version"), CURRENT_VERSION)
        # наступний запуск тієї самої версії майстра вже не дає
        self.assertFalse(
            _should_show_onboarding(s.settings, True, None,
                                    current_version=CURRENT_VERSION))

    def test_plain_dismiss_without_checkbox_keeps_showing(self):
        # закрив/скасував БЕЗ позначки «більше не показувати» — версію не
        # чіпаємо, майстер повернеться на наступному запуску тієї самої версії
        s = TempSettings()
        s.set_onboarded(version=OLDER_VERSION)
        wizard = SimpleNamespace(dont_show_again=False)

        _handle_onboarding_dismissed(wizard, s.settings, CURRENT_VERSION)

        self.assertEqual(s.settings.value("onboarded_version"), OLDER_VERSION)
        self.assertTrue(
            _should_show_onboarding(s.settings, True, None,
                                    current_version=CURRENT_VERSION))

    def test_wizard_close_button_sets_dont_show_again_and_rejects(self):
        wiz = FirstRunWizard(model_name="large-v3", model_dir="D:/custom/models",
                             language="en", ptt_key="ctrl+alt+r", repeat=True)
        self.addCleanup(lambda: (wiz.done(0), wiz.deleteLater()))

        self.assertFalse(wiz.dont_show_again)
        wiz._repeat_dont_show_chk.setChecked(True)
        with patch.object(FirstRunWizard, "reject") as reject:
            wiz._close_repeat_wizard()
        self.assertTrue(wiz.dont_show_again)
        reject.assert_called_once()

    def test_wizard_close_button_hidden_on_true_first_run(self):
        with patch.object(FirstRunWizard, "_gpu_step_possible", return_value=False):
            wiz = FirstRunWizard()
        self.addCleanup(lambda: (wiz.done(0), wiz.deleteLater()))
        self.assertFalse(hasattr(wiz, "_repeat_close_btn"),
                         "кнопка «Закрити» на вітальному кроці лише в repeat=True")


class IsOnboardingRepeatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_true_first_run_is_not_repeat(self):
        s = TempSettings()
        self.assertFalse(_is_onboarding_repeat(s.settings, False))

    def test_onboarded_with_config_is_repeat(self):
        s = TempSettings()
        s.set_onboarded(version=OLDER_VERSION)
        self.assertTrue(_is_onboarding_repeat(s.settings, True))

    def test_onboarded_flag_survived_uninstall_is_not_repeat(self):
        # прапорець є, а конфіга нема (баг «чистої деінсталяції») — це
        # фактично перший запуск наново, не «що змінилося»
        s = TempSettings()
        s.set_onboarded(version=OLDER_VERSION)
        self.assertFalse(_is_onboarding_repeat(s.settings, False))


class RepeatWizardTextsTests(unittest.TestCase):
    """Пункт 3 завдання: тексти повторного режиму інші за перший запуск."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_repeat_welcome_texts_differ_from_first_run(self):
        with patch.object(FirstRunWizard, "_gpu_step_possible", return_value=False):
            first = FirstRunWizard()
        repeat = FirstRunWizard(repeat=True)
        self.addCleanup(lambda: (first.done(0), first.deleteLater()))
        self.addCleanup(lambda: (repeat.done(0), repeat.deleteLater()))

        from fronts.desktop.i18n import tr
        self.assertNotEqual(tr("onb_welcome_title"), tr("onb_welcome_title_repeat"))
        self.assertNotEqual(tr("onb_welcome_body"), tr("onb_welcome_body_repeat"))
        self.assertTrue(repeat.repeat)
        self.assertFalse(first.repeat)


class BuildOnboardingWizardTests(unittest.TestCase):
    """Функція, якою main() будує майстер — саме вона захищає від ГОЛОВНОЇ
    ПАСТКИ (непередзаповнений майстер у повторному режимі)."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_first_run_ignores_prior_cfg(self):
        wiz = _build_onboarding_wizard(False, None)
        self.addCleanup(lambda: (wiz.done(0), wiz.deleteLater()))
        self.assertFalse(wiz.repeat)
        self.assertEqual(wiz.model_name, "large-v3-turbo")   # хардкод-дефолт майстра

    def test_repeat_prefills_from_prior_cfg(self):
        prior = SimpleNamespace(model_name="large-v3", model_dir="D:/custom/models",
                                language="en", ui_language="en",
                                ptt_key="ctrl+alt+r")
        wiz = _build_onboarding_wizard(True, prior)
        self.addCleanup(lambda: (wiz.done(0), wiz.deleteLater()))
        self.assertTrue(wiz.repeat)
        self.assertEqual(wiz.model_name, "large-v3")
        self.assertEqual(wiz.model_dir, "D:/custom/models")
        self.assertEqual(wiz.language, "en")
        self.assertEqual(wiz.ptt_key, "ctrl+alt+r")


class ApplyOnboardingResultVersionTests(unittest.TestCase):
    """Версію зберігаємо разом з «onboarded» (для _should_show_onboarding)."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _cfg(self):
        return SimpleNamespace(
            model_name="", model_dir="", language="", ui_language="",
            ptt_key="", device="cpu", compute_type="int8",
            save=lambda: None,
        )

    def test_current_version_is_persisted(self):
        s = TempSettings()
        cfg = self._cfg()
        wizard = SimpleNamespace(model_name="large-v3-turbo", model_dir="X:/m",
                                 language="uk", ptt_key="ctrl+shift+space",
                                 use_gpu=False)
        _apply_onboarding_result(cfg, wizard, s.settings, current_version=CURRENT_VERSION)
        self.assertEqual(s.settings.value("onboarded_version"), CURRENT_VERSION)

    def test_no_version_arg_does_not_write_key(self):
        # старі виклики (без 4-го аргументу) не мають чіпати новий ключ
        s = TempSettings()
        cfg = self._cfg()
        wizard = SimpleNamespace(model_name="large-v3-turbo", model_dir="X:/m",
                                 language="uk", ptt_key="ctrl+shift+space",
                                 use_gpu=False)
        _apply_onboarding_result(cfg, wizard, s.settings)
        self.assertIsNone(s.settings.value("onboarded_version"))


class RepeatPassPreservesConfigTests(unittest.TestCase):
    """ГОЛОВНИЙ тест: пройти майстер повторно, НЕ змінивши нічого → у
    config.toml не втрачено ЖОДНОГО налаштування (порівняно поле за полем на
    справжньому dataclass Config, не на двійнику)."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _make_configured_cfg(self, config_path):
        """Cfg, «раніше налаштований» користувачем (не дефолти майстра)."""
        cfg = Config()
        cfg.model_name = "large-v3"                 # НЕ дефолт майстра (турбо)
        cfg.model_dir = str(Path(tempfile.mkdtemp(prefix="balachky-models-")))
        cfg.language = "en"                          # НЕ дефолт (uk)
        cfg.ui_language = "en"
        cfg.ptt_key = "ctrl+alt+r"                    # НЕ дефолт
        cfg.device = "cpu"
        cfg.compute_type = "int8"
        cfg.log_level = "DEBUG"                       # поле, якого майстер НЕ торкається
        cfg.animations = False                        # так само
        cfg.save(config_path)
        return cfg

    def test_unmodified_repeat_pass_keeps_every_field(self):
        with tempfile.TemporaryDirectory(prefix="balachky-onbver-cfg-") as d:
            config_path = Path(d) / "config.toml"
            settings = TempSettings().settings

            before_cfg = self._make_configured_cfg(config_path)
            before = Config.load(config_path)
            self.assertEqual(dataclasses.asdict(before), dataclasses.asdict(before_cfg))

            # Майстер через ту саму функцію, що й main() у повторному режимі
            # (_build_onboarding_wizard) — передзаповнений із наявного cfg.
            # Користувач нічого не змінив і одразу прогорнув до кінця (accept
            # без правок радіо/полів).
            wizard = _build_onboarding_wizard(True, before)
            self.addCleanup(lambda: (wizard.done(0), wizard.deleteLater()))
            wizard._collect_choices()   # те, що робить _go_next() з кроку моделі/мови

            reloaded = Config.load(config_path)
            # cfg.save() без аргументів пише в whisper_core.paths.config_path() —
            # підміняємо саме її, щоб не чіпати реальний файл користувача.
            with patch("whisper_core.config.paths.config_path", return_value=config_path):
                _apply_onboarding_result(reloaded, wizard, settings,
                                         current_version=CURRENT_VERSION)

            after = Config.load(config_path)
            self.assertEqual(
                dataclasses.asdict(before), dataclasses.asdict(after),
                "повторний прохід без правок не має міняти ЖОДНОГО поля конфігу")


if __name__ == "__main__":
    unittest.main()
