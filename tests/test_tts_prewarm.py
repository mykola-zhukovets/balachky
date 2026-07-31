"""Тест прогріву рушія озвучення при відкритті панелі та вивантаження при закритті."""
from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock

from PySide6.QtWidgets import QApplication
from fronts.desktop.tts_controller import TtsController
from fronts.desktop.tts_panel import ListenPanel


class TestTtsPrewarm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            cls.qapp = QApplication([])
        else:
            cls.qapp = QApplication.instance()

    def test_prewarm_integrity_check_runs_only_in_worker_thread(self):
        caller_thread = threading.get_ident()
        integrity_threads = []
        integrity_checked = threading.Event()

        def check_integrity():
            integrity_threads.append(threading.get_ident())
            integrity_checked.set()
            return True

        cfg = MagicMock(tts_enabled=True)
        coordinator = MagicMock()
        coordinator.acquire_tts.return_value = MagicMock()
        sidecar = MagicMock()
        voice = MagicMock(
            available=lambda: True, integrity_available=check_integrity,
            id="styletts2_ua", engine_kind="styletts2",
            manifest_path="manifest.json")
        ctrl = TtsController(
            cfg=cfg, coordinator=coordinator,
            resolve_voice=lambda voice_id, lang: voice,
            sidecar_factory=lambda: sidecar)

        ctrl.prewarm("uk")

        self.assertNotIn(caller_thread, integrity_threads)
        self.assertTrue(integrity_checked.wait(2), "prewarm worker не перевірив голос")
        self.assertNotIn(caller_thread, integrity_threads)

    def test_prewarm_starts_sidecar(self):
        cfg = MagicMock(tts_enabled=True)
        coordinator = MagicMock()
        sidecar = MagicMock()
        mock_resolve = MagicMock()
        mock_voice = MagicMock(
            available=lambda: True, integrity_available=lambda: True,
            id="styletts2_ua", engine_kind="styletts2",
            manifest_path="manifest.json")
        mock_resolve.return_value = mock_voice

        sidecar_started = threading.Event()
        sidecar.start.side_effect = sidecar_started.set
        ctrl = TtsController(
            cfg=cfg, coordinator=coordinator, resolve_voice=mock_resolve,
            sidecar_factory=lambda: sidecar
        )

        coordinator.acquire_tts.return_value = MagicMock()

        ctrl.prewarm("uk")

        self.assertTrue(sidecar_started.wait(2), "prewarm worker не стартував sidecar")
        self.assertIsNotNone(ctrl._sidecar)

    def test_panel_close_triggers_grace_shutdown(self):
        on_close = MagicMock()
        panel = ListenPanel(None, has_voice=True, text="Тестовий текст", on_close=on_close)

        panel.close()

        on_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
