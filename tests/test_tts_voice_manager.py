"""Хвиля 3: UI менеджера голосів (§7.3) — картки за мовою, стани, OpenRAIL-M нота."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from fronts.desktop.i18n import tr
from fronts.desktop.tts_voices import VoiceCard, VoiceManagerDialog
from whisper_core.tts import voices as V

_APP = QApplication.instance() or QApplication([])


def _labels(widget):
    return [w.text() for w in widget.findChildren(QLabel)]


class TestVoiceCard(unittest.TestCase):
    def test_card_shows_download_when_not_available(self):
        card = VoiceCard(V.VOICE_PRESETS["styletts2_ua"], available=False, lang="uk")
        from fronts.desktop.glass import GlassButton
        texts = [b.text() for b in card.findChildren(GlassButton)]
        self.assertIn(tr("tts_voice_download"), texts)

    def test_openrail_voice_has_ai_note(self):
        # Supertonic = OpenRAIL-M → обовʼязкова позначка «згенеровано ШІ» (§7.6)
        card = VoiceCard(V.VOICE_PRESETS["supertonic"], available=False, lang="uk")
        self.assertIn(tr("tts_voice_ai_note"), _labels(card))

    def test_non_openrail_no_ai_note(self):
        card = VoiceCard(V.VOICE_PRESETS["styletts2_ua"], available=False, lang="uk")
        self.assertNotIn(tr("tts_voice_ai_note"), _labels(card))

    def test_sample_button_disabled_without_wav(self):
        # демо-WAV ще нема → кнопка «Почути приклад» неактивна (чесна заглушка §7.6)
        from fronts.desktop.glass import GlassButton
        card = VoiceCard(V.VOICE_PRESETS["styletts2_ua"], available=False, lang="uk")
        sample = [b for b in card.findChildren(GlassButton)
                  if b.text() == tr("tts_voice_sample")]
        self.assertTrue(sample)
        self.assertFalse(sample[0].isEnabled())


class TestVoiceManagerDialog(unittest.TestCase):
    def test_builds_grouped_by_language(self):
        dlg = VoiceManagerDialog(None, root=None)
        labels = _labels(dlg)
        # заголовки мов присутні (uk, en)
        self.assertIn(tr("tts_voice_lang", langs="uk"), labels)
        self.assertIn(tr("tts_voice_lang", langs="en"), labels)
        # укр-голоси у списку
        self.assertIn(tr("tts_voice_styletts2"), labels)
        self.assertIn(tr("tts_voice_kokoro"), labels)

    def test_activate_callback(self):
        picked = []
        dlg = VoiceManagerDialog(
            None, root=None, on_activate=lambda vid, lang: picked.append((vid, lang)))
        # не завантажено → радіо нема, але діалог будується без помилок
        self.assertIsNotNone(dlg)

    def test_add_custom_callback_wired(self):
        called = []
        dlg = VoiceManagerDialog(None, root=None, on_add_custom=lambda: called.append(1))
        from fronts.desktop.glass import GlassButton
        custom = [b for b in dlg.findChildren(GlassButton)
                  if b.text() == tr("tts_voice_custom")]
        self.assertTrue(custom)
        custom[0].click()
        self.assertEqual(called, [1])


if __name__ == "__main__":
    unittest.main()
