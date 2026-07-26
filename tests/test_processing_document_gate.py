"""feature/processing-slider (блокер №2): «Під документ» капабіліті-гейтиться
РЕАЛЬНОЮ наявністю пунктуатора у вікні диктування — не тихий no-op (спека §3, §10).

Тестуємо методи DictationPage (_document_ready / _refresh_document_availability)
проти справжнього ProcessingSlider, без побудови цілого вікна (важкий контролер).
"""
import os
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from fronts.desktop import motion                              # noqa: E402
from fronts.desktop.main_window import DictationPage           # noqa: E402
from fronts.desktop.processing_slider import ProcessingSlider  # noqa: E402
from whisper_core import processing, punctuator                # noqa: E402


class _Profile:
    def __init__(self, mode):
        self._mode = mode

    def processing_mode(self, surface):
        return self._mode


def _fake_page(slider, profile, doc_ready):
    """Мінімальний носій стану для виклику методів DictationPage без побудови вікна.
    Реальні методи прив'язуємо до носія, щоб self._document_ready() всередині
    _refresh_document_availability резолвився коректно."""
    page = SimpleNamespace(
        _processing=slider,
        _doc_ready=doc_ready,
        controller=SimpleNamespace(profile=profile),
    )
    page._document_ready = types.MethodType(DictationPage._document_ready, page)
    page._refresh_document_availability = types.MethodType(
        DictationPage._refresh_document_availability, page)
    return page


class DocumentGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        motion.init_config(SimpleNamespace(animations=False))

    def test_document_ready_follows_punctuator_availability(self):
        page = _fake_page(None, None, True)
        with patch.object(punctuator, "available", return_value=False):
            self.assertFalse(page._document_ready())
        with patch.object(punctuator, "available", return_value=True):
            self.assertTrue(page._document_ready())

    def test_refresh_locks_document_when_component_missing(self):
        slider = ProcessingSlider(processing.DICTATION)
        page = _fake_page(slider, _Profile("verbatim"), True)
        with patch.object(punctuator, "available", return_value=False):
            page._refresh_document_availability()
        # «Під документ» стало недоступним → мітка вимкнена, вибір не комітиться
        self.assertFalse(slider._document_available)
        self.assertFalse(slider._labels[2].isEnabled())
        self.assertFalse(page._doc_ready)

    def test_gated_document_selection_does_not_commit(self):
        # Прямий наслідок блокера: без готового компонента спроба «Під документ»
        # відкочується, а modeChanged НЕ емітиться (профіль не пишеться в document).
        slider = ProcessingSlider(processing.DICTATION)
        page = _fake_page(slider, _Profile("verbatim"), True)
        with patch.object(punctuator, "available", return_value=False):
            page._refresh_document_availability()
        seen = []
        unavail = []
        slider.modeChanged.connect(seen.append)
        slider.documentUnavailable.connect(unavail.append)
        slider._slider.setValue(2)               # спроба піти в «Під документ»
        self.assertEqual(slider.mode(), "verbatim")
        self.assertEqual(seen, [])               # коміту не було
        self.assertEqual(len(unavail), 1)        # запущено флоу «недоступно»

    def test_refresh_unlocks_and_restores_saved_document_mode(self):
        slider = ProcessingSlider(processing.DICTATION)
        slider.setDocumentAvailable(False, "нема компонента")
        page = _fake_page(slider, _Profile("document"), False)
        with patch.object(punctuator, "available", return_value=True):
            page._refresh_document_availability()
        self.assertTrue(slider._document_available)
        self.assertTrue(page._doc_ready)
        self.assertEqual(slider.mode(), "document")   # відновлено після розблокування


if __name__ == "__main__":
    unittest.main()
