"""Персоналізація кольору у треї (аудит мономи theme.py, 25.07): idle/busy/
loading перефарбовуються під активний колір інтерфейсу; "recording" — ЄДИНИЙ
фіксований колір (активний мікрофон = семантично критичний стан ОС-треї,
впізнаваність важливіша за персоналізацію) і НЕ має рухатись разом із темою.
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class _QtBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])


class TrayColorTests(_QtBase):
    def tearDown(self):
        from fronts.desktop import theme
        theme.set_ui_color("classic")     # не протікати активний колір між тестами

    def test_recording_is_fixed_across_all_colors(self):
        from fronts.desktop import theme, tray
        for color in ("classic", "red", "teal"):
            theme.set_ui_color(color)
            self.assertEqual(tray._tray_color("recording"), "#E52421")

    def test_idle_and_busy_follow_active_theme(self):
        from fronts.desktop import theme, tray
        theme.set_ui_color("classic")
        idle_classic = tray._tray_color("idle")
        busy_classic = tray._tray_color("busy")
        self.assertEqual(idle_classic, theme.IDLE)
        self.assertEqual(busy_classic, theme.GOLD)

        theme.set_ui_color("red")
        self.assertEqual(tray._tray_color("idle"), theme.IDLE)
        self.assertEqual(tray._tray_color("busy"), theme.GOLD)
        self.assertNotEqual(tray._tray_color("idle"), idle_classic)
        self.assertNotEqual(tray._tray_color("busy"), busy_classic)

    def test_loading_shares_busy_color(self):
        from fronts.desktop import theme, tray
        theme.set_ui_color("teal")
        self.assertEqual(tray._tray_color("loading"), tray._tray_color("busy"))

    def test_unknown_state_falls_back_to_idle(self):
        from fronts.desktop import theme, tray
        theme.set_ui_color("classic")
        self.assertEqual(tray._tray_color("bogus"), theme.IDLE)


if __name__ == "__main__":
    unittest.main()
