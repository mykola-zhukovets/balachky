"""Регресія зауваження №1: кнопка «пройти майстер налаштування ще раз».

Критичний баг (знайдений рецензентом): попередній фікс видаляв ключ «onboarded»
ДО показу майстра і відновлював його ЛИШЕ на шляху успіху. Скасування майстра
лишало ключ видаленим → наступний старт показував FirstRunWizard, а повторне
скасування → sys.exit(0). Один клік «Скасувати» ламав запуск.

Тут перевіряємо:
  • скасування майстра → «onboarded» лишається=1 (не None), cfg не змінено;
  • прийняття → вибір майстра застосовано до cfg;
  • майстер передзаповнюється поточними значеннями cfg, а не хардкод-дефолтами.
"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings

from fronts.desktop import onboarding
from fronts.desktop.pages.settings import SettingsPage


def _fake_self():
    """Легкий двійник SettingsPage: метод _rerun_onboarding чіпає лише
    controller.cfg, controller.save_config і _mark_restart_pending."""
    cfg = SimpleNamespace(
        model_name="large-v3",           # НЕ дефолт (турбо) — перевіримо передзаповнення
        model_dir="D:/custom/models",    # НЕ дефолт — не має перезаписатись на дефолт
        language="en",                   # НЕ дефолт (uk)
        ui_language="en",
        ptt_key="ctrl+alt+r",            # НЕ дефолт
        device="cpu",
        compute_type="int8",
    )
    saved = {"count": 0}
    controller = SimpleNamespace(
        cfg=cfg,
        save_config=lambda: saved.__setitem__("count", saved["count"] + 1),
    )
    fake = SimpleNamespace(
        controller=controller,
        _mark_restart_pending=lambda: None,
        _saved=saved,
    )
    return fake


class RerunOnboardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_cancel_keeps_onboarded_and_leaves_cfg_untouched(self):
        settings = QSettings("Balachky", "Balachky")
        settings.setValue("onboarded", 1)
        fake = _fake_self()

        # майстер скасовано (exec() → 0)
        wiz = SimpleNamespace(exec=lambda: 0)
        with patch.object(onboarding, "FirstRunWizard", return_value=wiz):
            SettingsPage._rerun_onboarding(fake)

        # ключ НЕ зачеплено — застосунок стартуватиме нормально
        self.assertEqual(settings.value("onboarded"), 1)
        self.assertIsNotNone(settings.value("onboarded"))
        # cfg без змін, конфіг не збережено
        self.assertEqual(fake.controller.cfg.model_name, "large-v3")
        self.assertEqual(fake.controller.cfg.model_dir, "D:/custom/models")
        self.assertEqual(fake._saved["count"], 0)

    def test_accept_applies_wizard_choice_to_cfg(self):
        fake = _fake_self()
        wiz = SimpleNamespace(
            exec=lambda: 1,
            model_name="large-v3-turbo",
            model_dir="E:/new/models",
            language="uk",
            ptt_key="ctrl+shift+space",
            use_gpu=False,
        )
        with patch.object(onboarding, "FirstRunWizard", return_value=wiz):
            SettingsPage._rerun_onboarding(fake)

        cfg = fake.controller.cfg
        self.assertEqual(cfg.model_name, "large-v3-turbo")
        self.assertEqual(cfg.model_dir, "E:/new/models")
        self.assertEqual(cfg.language, "uk")
        self.assertEqual(cfg.ui_language, "uk")
        self.assertEqual(cfg.ptt_key, "ctrl+shift+space")
        self.assertEqual(fake._saved["count"], 1)

    def test_wizard_prefilled_from_current_cfg(self):
        fake = _fake_self()
        captured = {}

        def _capture(parent, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(exec=lambda: 0)

        with patch.object(onboarding, "FirstRunWizard", side_effect=_capture):
            SettingsPage._rerun_onboarding(fake)

        # майстер отримав поточні значення cfg, а не хардкод-дефолти
        self.assertEqual(captured["model_name"], "large-v3")
        self.assertEqual(captured["model_dir"], "D:/custom/models")
        self.assertEqual(captured["language"], "en")
        self.assertEqual(captured["ptt_key"], "ctrl+alt+r")


class WizardPrefillTests(unittest.TestCase):
    """Передзаповнення радіокнопок майстра з переданих значень."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _wizard(self, **kwargs):
        wiz = onboarding.FirstRunWizard(**kwargs)
        self.addCleanup(wiz.deleteLater)
        return wiz

    def test_defaults_when_no_params(self):
        wiz = self._wizard()
        self.assertTrue(wiz._rb_fast.isChecked())      # турбо за замовч.
        self.assertTrue(wiz._rb_uk.isChecked())        # українська за замовч.
        self.assertEqual(wiz.ptt_key, "ctrl+shift+space")

    def test_prefill_precise_model_and_english(self):
        wiz = self._wizard(model_name="large-v3", model_dir="D:/m",
                           language="en", ptt_key="ctrl+alt+r")
        self.assertTrue(wiz._rb_precise.isChecked())
        self.assertFalse(wiz._rb_fast.isChecked())
        self.assertTrue(wiz._rb_en.isChecked())
        self.assertFalse(wiz._rb_uk.isChecked())
        self.assertEqual(wiz.model_dir, "D:/m")
        self.assertEqual(wiz.ptt_key, "ctrl+alt+r")

    def test_prefill_turbo_and_ukrainian(self):
        wiz = self._wizard(model_name="large-v3-turbo", language="uk")
        self.assertTrue(wiz._rb_fast.isChecked())
        self.assertTrue(wiz._rb_uk.isChecked())


if __name__ == "__main__":
    unittest.main()
