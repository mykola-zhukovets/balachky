"""feature/status-tags-soul — анімація плаваючого індикатора диктування.

Перевіряє «душу» пілюлі pill.FloatingPill і, головне, ДИСЦИПЛІНУ таймера:
- стани перемикаються (recording/busy видима, idle схована);
- таймер руху крутиться ЛИШЕ поки пілюля видима й у кольоровому стані;
- у idle таймер стоїть (0% CPU у спокої) — ключова вимога перфомансу;
- Reduce Motion (анімації вимкнені) → таймер не стартує, кадр статичний;
- зміна стану морфить колір крапки; offscreen-рендер не падає.

Кожен тест завершується у стані idle (таймер зупинено) + teardown знищує
віджет — інакше недобитий Qt-таймер дає флакі-краш на виході (0xC000041D,
як задокументовано в test_meeting_ui).
"""
import os
import unittest
from types import SimpleNamespace

# Віджети без екрана: рендер-тесту потрібен QApplication, не реальний екран.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fronts.desktop import motion
from fronts.desktop.pill import FloatingPill


def _app():
    return QApplication.instance() or QApplication([])


class PillAnimationTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        # детермінізм: не залежати від системного перемикача анімацій машини
        motion.init_config(SimpleNamespace(animations=True))
        motion._system_ok = True
        self.pill = FloatingPill(on_moved=lambda x, y: None,
                                 on_reset=lambda: None)

    def tearDown(self):
        # у idle — гарантовано зупиняє таймер; далі знищуємо віджет
        self.pill.set_state("idle")
        self.assertFalse(self.pill._timer.isActive())
        self.pill.deleteLater()
        self.app.processEvents()
        motion.init_config(None)

    # --- стани ---
    def test_states_switch_visibility(self):
        self.pill.set_state("recording")
        self.assertEqual(self.pill._state, "recording")
        self.assertTrue(self.pill.isVisible())

        self.pill.set_state("busy")
        self.assertEqual(self.pill._state, "busy")
        self.assertTrue(self.pill.isVisible())

        self.pill.set_state("loading")
        self.assertEqual(self.pill._state, "loading")
        self.assertTrue(self.pill.isVisible())

        self.pill.set_state("idle")
        self.assertEqual(self.pill._state, "idle")
        self.assertFalse(self.pill.isVisible())

    def test_accent_matches_state(self):
        from fronts.desktop.theme import ALERT, GOLD
        self.pill.set_state("recording")
        self.assertEqual(self.pill._accent.name().lower(), ALERT.lower())
        # перехід recording→busy морфить колір (анімація стартувала)
        self.pill.set_state("busy")
        self.assertIsNotNone(self.pill._morph_anim)
        # вимкнення анімацій доганяє морф миттєво до цільового кольору
        motion.init_config(SimpleNamespace(animations=False))
        self.pill.sync_animations()
        self.assertIsNone(self.pill._morph_anim)
        self.assertEqual(self.pill._accent.name().lower(), GOLD.lower())

    # --- дисципліна таймера (ядро перфомансу) ---
    def test_timer_runs_only_when_active_and_visible(self):
        self.assertFalse(self.pill._timer.isActive())    # створена — тиша
        self.pill.set_state("recording")
        self.assertTrue(self.pill._timer.isActive())     # запис — рух
        self.pill.set_state("busy")
        self.assertTrue(self.pill._timer.isActive())     # розпізнавання — рух
        self.pill.set_state("idle")
        self.assertFalse(self.pill._timer.isActive())    # idle — СТОП (0% CPU)

    def test_hidden_stops_timer(self):
        self.pill.set_state("busy")
        self.assertTrue(self.pill._timer.isActive())
        self.pill.hide()                                 # вікно сховане — таймер стоп
        self.assertFalse(self.pill._timer.isActive())

    def test_reduce_motion_no_timer(self):
        motion.init_config(SimpleNamespace(animations=False))
        self.pill.set_state("recording")
        self.assertTrue(self.pill.isVisible())           # пілюля показана
        self.assertFalse(self.pill._timer.isActive())    # але БЕЗ таймера (статика)

    def test_sync_animations_starts_and_stops(self):
        self.pill.set_state("busy")
        self.assertTrue(self.pill._timer.isActive())
        motion.init_config(SimpleNamespace(animations=False))
        self.pill.sync_animations()
        self.assertFalse(self.pill._timer.isActive())    # вимкнули — таймер спинився
        motion.init_config(SimpleNamespace(animations=True))
        motion._system_ok = True
        self.pill.sync_animations()
        self.assertTrue(self.pill._timer.isActive())     # увімкнули — знову крутиться

    # --- offscreen-рендер не падає в обох активних станах ---
    def test_offscreen_render(self):
        for state in ("recording", "busy", "loading"):
            self.pill.set_state(state)
            pm = self.pill.grab()                        # форсує paintEvent
            self.assertFalse(pm.isNull())


if __name__ == "__main__":
    unittest.main()
