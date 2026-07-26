"""Тест прогріву рушія озвучення при відкритті панелі та вивантаження при закритті."""
from __future__ import annotations

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
    def test_prewarm_starts_sidecar(self):
        cfg = MagicMock(tts_enabled=True)
        coordinator = MagicMock()
        sidecar = MagicMock()
        mock_resolve = MagicMock()
        mock_voice = MagicMock(available=lambda: True, id="styletts2_ua", engine_kind="styletts2", manifest_path="manifest.json")
        mock_resolve.return_value = mock_voice

        ctrl = TtsController(
            cfg=cfg, coordinator=coordinator, resolve_voice=mock_resolve,
            sidecar_factory=lambda: sidecar
        )

        coordinator.acquire_tts.return_value = MagicMock()

        ctrl.prewarm("uk")

        # Дочікуємося виконання фонового потоку prewarm
        if ctrl._worker and ctrl._worker.is_alive():
            ctrl._worker.join(timeout=2.0)

        # Перевіряємо, що sidecar було створено та заведено
        self.assertIsNotNone(ctrl._sidecar)

    def test_panel_close_triggers_grace_shutdown(self):
        on_close = MagicMock()
        panel = ListenPanel(None, has_voice=True, text="Тестовий текст", on_close=on_close)

        panel.close()

        on_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
