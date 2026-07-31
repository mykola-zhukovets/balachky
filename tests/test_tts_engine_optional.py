"""Полегшена збірка (без рушія озвучення) не має обманювати користувача.

Рушій озвучення — це torch+CUDA, 4.7 ГБ на диску проти ~150 МБ усього іншого.
У полегшеному інсталяторі його немає. Небезпека: майстер перших кроків
запропонував би завантажити голос на 714 МБ, який нема чим відтворити.
"""
import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from whisper_core.tts import sidecar  # noqa: E402


class EngineAvailabilityTests(unittest.TestCase):
    def test_dev_run_always_has_engine(self):
        """У режимі розробки воркер запускається модулем — рушій «є»."""
        with patch.object(sys, "frozen", False, create=True):
            self.assertTrue(sidecar.engine_available())

    def test_frozen_without_worker_exe_has_no_engine(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sidecar.os.path, "exists", return_value=False):
            self.assertFalse(sidecar.engine_available())

    def test_frozen_with_worker_exe_has_engine(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sidecar.os.path, "exists", return_value=True):
            self.assertTrue(sidecar.engine_available())

    def test_spec_keeps_legacy_skip_flag_compatibility(self):
        """Старий прапорець лишається лише як сумісний alias нового профілю."""
        from pathlib import Path
        spec = (Path(__file__).resolve().parents[1] / "balachky.spec").read_text(encoding="utf-8")
        self.assertIn('BALACHKY_SKIP_TTS', spec)
        self.assertIn('BALACHKY_BUILD_PROFILE', spec)
        self.assertIn('BALACHKY_SKIP_TTS is deprecated', spec)


class OnboardingSkipsVoiceStepTests(unittest.TestCase):
    def test_voice_step_shown_honestly_when_engine_missing(self):
        """Рішення власника 31.07 (аудит 30.07, Дефект 3): без рушія майстер
        БІЛЬШЕ не пропускає крок «Озвучення» мовчки (старий код перемикав
        сторінку і тієї ж миті кликав _advance_from_voice — вона блимала й
        зникала). Тепер сторінка ЗАВЖДИ показується; чесне пояснення і
        одна кнопка «Далі» — усередині _update_voice_page_state."""
        from PySide6.QtWidgets import QApplication
        from fronts.desktop import onboarding

        app = QApplication.instance() or QApplication([])
        self.addCleanup(lambda: app.processEvents())
        wiz = onboarding.FirstRunWizard()
        wiz._stack.setCurrentIndex(2)
        with patch.object(onboarding, "_tts_engine_available", return_value=False), \
             patch.object(wiz, "_advance_from_voice") as adv, \
             patch.object(wiz, "_update_voice_page_state") as upd:
            wiz._go_next()
        upd.assert_called_once()
        adv.assert_not_called()
        self.assertEqual(wiz._stack.currentIndex(), 3,
                         "крок «Озвучення» мусить лишитись видимим, не проскочити")
        wiz.close()

    def test_voice_step_shown_when_engine_present(self):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop import onboarding

        app = QApplication.instance() or QApplication([])
        self.addCleanup(lambda: app.processEvents())
        wiz = onboarding.FirstRunWizard()
        wiz._stack.setCurrentIndex(2)
        with patch.object(onboarding, "_tts_engine_available", return_value=True), \
             patch.object(wiz, "_advance_from_voice") as adv, \
             patch.object(wiz, "_update_voice_page_state") as upd:
            wiz._go_next()
        upd.assert_called_once()
        adv.assert_not_called()
        wiz.close()


if __name__ == "__main__":
    unittest.main()

