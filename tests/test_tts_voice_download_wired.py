"""Мутація (живий тест власника 30.07): натискання «Завантажити» у менеджері
голосів має АБО справді почати докачку з видимим поступом, АБО показати чесну
причину, чому ні — третього не дано. Стара версія мовчала в ОБОХ випадках:
голос без запінованих файлів (Supertonic/kokoro_en) падав з VoiceDownloadError
у фоновому threading.Thread, який ловив лише цей один виняток і кликав
self.tray.notify() ПРЯМО з фонового потоку (небезпечний крос-тредовий виклик
Qt-віджета) — ні тосту, ні запису в журналі користувач не бачив.

Мережу НЕ чіпаємо: підміняємо whisper_core.tts.voices.download_and_install."""
import logging
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject
from PySide6.QtWidgets import QApplication

from fronts.desktop.app import DesktopApp
from fronts.desktop.i18n import tr
from fronts.desktop.tts_voices import VoiceCard
from whisper_core.tts import voices as V

_APP = QApplication.instance() or QApplication([])


class _FakeController(QObject):
    """Мінімальний QObject-хазяїн реального _tts_download_voice/_on_voice_
    download_failed з app.py — без важкого DesktopApp.__init__ (аудіо/рушії).
    Методи беремо БЕЗ копіювання логіки, щоб тест бив по справжньому коду."""
    _tts_download_voice = DesktopApp._tts_download_voice
    _on_voice_download_failed = DesktopApp._on_voice_download_failed

    def __init__(self):
        super().__init__()
        self.tray = MagicMock()


def _pump(worker, timeout_ms=5000):
    """Дочекатись завершення QThread і прогнати event loop, щоб чергові
    (queued) Qt-сигнали дійшли до слотів у GUI-потоці."""
    worker.wait(timeout_ms)
    for _ in range(20):
        QCoreApplication.processEvents()


class TestVoiceDownloadNotSilent(unittest.TestCase):
    def setUp(self):
        self.ctrl = _FakeController()
        self.addCleanup(self.ctrl.deleteLater)
        self._log = logging.getLogger()
        self._old_level = self._log.level
        self._log.setLevel(logging.INFO)
        self.addCleanup(self._log.setLevel, self._old_level)
        self.records = []
        handler = logging.Handler()
        handler.emit = lambda record: self.records.append(record.getMessage())
        self._log.addHandler(handler)
        self.addCleanup(self._log.removeHandler, handler)

    # --- (а) голос БЕЗ запінованих файлів у цій збірці: чесна відмова -------
    def test_unpinned_voice_button_disabled_with_honest_reason(self):
        """Supertonic/kokoro_en (files=()) — кнопка НЕактивна з поясненням, а
        НЕ клікабельна пастка, що мовчки падає на кожному натисканні."""
        card = VoiceCard(V.VOICE_PRESETS["supertonic"], available=False, lang="uk")
        self.assertFalse(card._dl_btn.isEnabled())
        self.assertEqual(card._dl_btn.toolTip(),
                         tr("hint_tts_voice_download_unavailable"))

    # --- (б) голос ІЗ запінованими файлами: клік реально стартує докачку ----
    def test_pinned_voice_click_starts_download_with_visible_progress(self):
        card = VoiceCard(V.VOICE_PRESETS["styletts2_ua"], available=False, lang="uk")
        self.assertTrue(card._dl_btn.isEnabled())

        def fake_download(voice_id, root=None, progress_cb=None,
                          cancel_check=None, force=False):
            if progress_cb:
                progress_cb(50, 100)

        with patch.object(V, "download_and_install", side_effect=fake_download):
            card._begin_download(self.ctrl._tts_download_voice)
            # відразу після кліку — видимий стан «качаю», НЕ тиша і НЕ як було
            self.assertFalse(card._dl_btn.isEnabled())
            self.assertNotEqual(card._dl_btn.text(), tr("tts_voice_download"))

            worker = self.ctrl._voice_download_workers["styletts2_ua"]
            _pump(worker)

        # завершення дійшло до картки через Qt-сигнал (не тиша)
        self.assertEqual(card._dl_btn.text(), tr("tts_voice_download_done"))
        self.assertTrue(
            any("styletts2_ua" in m for m in self.records),
            "натискання мусить лишити слід у журналі")

    # --- провал: журнал + чесний тост, кнопка знову жива (не мертва назавжди)
    def test_download_click_always_logs_even_on_failure(self):
        card = VoiceCard(V.VOICE_PRESETS["radtts_uk"], available=False, lang="uk")

        def fake_fail(voice_id, root=None, progress_cb=None,
                      cancel_check=None, force=False):
            raise V.VoiceDownloadError("контрольна сума не збіглася (тест)")

        with patch.object(V, "download_and_install", side_effect=fake_fail):
            card._begin_download(self.ctrl._tts_download_voice)
            worker = self.ctrl._voice_download_workers["radtts_uk"]
            _pump(worker)

        # клік лишив слід у журналі НЕЗАЛЕЖНО від результату
        self.assertTrue(any("Завантажити»" in m for m in self.records))
        # і сам провал теж — не тиша
        self.assertTrue(any("radtts_uk" in m for m in self.records))
        # кнопка знову клікабельна — можна спробувати ще раз, а не мертва пастка
        self.assertTrue(card._dl_btn.isEnabled())
        self.assertEqual(card._dl_btn.text(), tr("tts_voice_download"))
        # тост про провал реально показано (bound-метод QObject → безпечно з
        # GUI-потоку, а не self.tray.notify() з фонового потоку, як раніше)
        self.ctrl.tray.notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
