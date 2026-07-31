"""Хвиля 1: глобальний хоткей «Прослухати виділене» (§8.8, §11.1).

Thread-affinity: native callback ЛИШЕ емітить Qt-сигнал (не викликає плеєр напряму);
порожнє виділення → тост; конфлікт RegisterHotKey → чесна обробка без краху."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

APP_SRC = (Path(__file__).resolve().parents[1] / "fronts" / "desktop"
           / "app.py").read_text(encoding="utf-8")


class TestHotkeyThreadAffinity(unittest.TestCase):
    """Контракт §8.8: native callback → .emit сигналу, НЕ прямий виклик плеєра."""

    def test_hotkey_callback_is_signal_emit(self):
        # хоткей вішається на self.tts_listen_requested.emit (маршал у GUI), НЕ на
        # прямий виклик плеєра/капчури з hook-потоку
        self.assertIn("self.tts_listen_hotkey = hotkeys_native.make_action_hotkeys(",
                      APP_SRC)
        self.assertIn("self.tts_listen_requested.emit", APP_SRC)
        # сигнал з'єднано зі слотом у GUI-потоці
        self.assertIn("self.tts_listen_requested.connect(self.listen_selection_from_hotkey)",
                      APP_SRC)

    def test_signal_declared(self):
        self.assertIn("tts_listen_requested = Signal()", APP_SRC)


class TestListenSlot(unittest.TestCase):
    """listen_selection_from_hotkey — unbound на SimpleNamespace (як інші app-тести)."""

    def _slot(self):
        from fronts.desktop.app import DesktopApp
        return DesktopApp.listen_selection_from_hotkey

    def test_empty_selection_toasts_no_synth(self):
        notified = []
        ctl = SimpleNamespace(
            tray=SimpleNamespace(notify=notified.append),
            open_listen_panel=lambda t: notified.append(("panel", t)))
        with patch("fronts.desktop.app.capture_selection", return_value="   "):
            self._slot()(ctl)
        # порожнє виділення → тост, панель НЕ відкрито
        self.assertTrue(notified)
        self.assertFalse(any(isinstance(n, tuple) for n in notified))

    def test_text_selection_opens_panel(self):
        opened = []
        ctl = SimpleNamespace(
            tray=SimpleNamespace(notify=lambda *_: None),
            open_listen_panel=lambda t: opened.append(t))
        with patch("fronts.desktop.app.capture_selection",
                   return_value="Прослухай це виділене"):
            self._slot()(ctl)
        self.assertEqual(opened, ["Прослухай це виділене"])

    def test_capture_failure_is_safe(self):
        # збій зчитування виділення не має валити слот
        ctl = SimpleNamespace(
            tray=SimpleNamespace(notify=lambda *_: None),
            open_listen_panel=lambda t: None)
        with patch("fronts.desktop.app.capture_selection",
                   side_effect=RuntimeError("elevated window")):
            self._slot()(ctl)                    # без винятку

    def test_capture_failure_reports_honest_reason_not_cancelled(self):
        """Аудит 'тихі відмови' №1: раніше збій capture_selection() маскувався
        під tts_cancelled ('Озвучення скасовано') — неправдива причина. Тепер
        людина бачить чесне 'не вдалося прочитати виділений текст', а не
        вигадану відміну, і подія лишається в журналі."""
        from fronts.desktop.i18n import tr
        notified = []
        ctl = SimpleNamespace(
            tray=SimpleNamespace(notify=notified.append),
            open_listen_panel=lambda t: notified.append(("panel", t)))
        with patch("fronts.desktop.app.capture_selection",
                   side_effect=RuntimeError("elevated window")), \
             self.assertLogs(level="ERROR") as logs:
            self._slot()(ctl)
        self.assertEqual(notified, [tr("tts_selection_capture_failed")])
        self.assertNotIn(tr("tts_cancelled"), notified)
        self.assertTrue(any("виділ" in rec.lower() for rec in logs.output))


class TestHotkeyConflict(unittest.TestCase):
    """Конфлікт RegisterHotKey (зайнята комбінація) → чесна обробка без краху."""

    def test_apply_bad_key_does_not_raise(self):
        from fronts.desktop import hotkeys_native
        cfg = SimpleNamespace()
        hk = hotkeys_native.make_action_hotkeys(cfg, lambda: None, lambda: None)
        # застосування завідомо непридатної/зайнятої комбінації не має кидати виняток
        try:
            hk.apply("ctrl+alt+shift+f24+bogus", "")
        except Exception as exc:                 # noqa: BLE001
            self.fail(f"apply кинув виняток на конфлікті/битій комбінації: {exc}")


if __name__ == "__main__":
    unittest.main()
