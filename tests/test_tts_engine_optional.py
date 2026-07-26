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

    def test_spec_honours_skip_flag(self):
        """BALACHKY_SKIP_TTS=1 має лишатись у .spec — на ньому тримається полегшена збірка."""
        from pathlib import Path
        spec = (Path(__file__).resolve().parents[1] / "balachky.spec").read_text(encoding="utf-8")
        self.assertIn('BALACHKY_SKIP_TTS', spec)
        self.assertIn('if not _skip_tts and _torch_ilu.find_spec("torch")', spec)
        # os у .spec не імпортований глобально — свій псевдонім обов'язковий,
        # інакше збірка падає на NameError уже після довгого Analysis
        self.assertIn("import os as _os", spec)


class OnboardingSkipsVoiceStepTests(unittest.TestCase):
    def test_voice_step_skipped_when_engine_missing(self):
        """Без рушія майстер не пропонує качати голос, а йде далі."""
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
        adv.assert_called_once()
        upd.assert_not_called()
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

