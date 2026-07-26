"""feature/hover-lift — наведення «підйом + тінь» на кнопки й картки-плитки.

Смоук-перевірка, що механізм наведення ПІДКЛЮЧЕНО і поводиться за каноном:
- action-GlassButton отримує HoverLift, сайдбар-навігація (nav=True) — НІ
  (у неї власний активний стан: золота рамка checked-кнопки);
- у спокої на елементі НЕМА graphics-ефекту — стрічка з десятками карток не
  тримає десятки offscreen-тіней (ключова вимога перфомансу);
- Enter → лінива тінь створюється + елемент підіймається; Leave → тінь знята,
  ефекту знову нема;
- Reduce Motion (анімації вимкнені) → ані підйому, ані тіні (жодного руху);
- lift_on_hover ідемпотентний;
- a11y: підйом НЕ чіпає accessibleName / focusPolicy / enabled елемента.

Teardown зупиняє анімації й знищує віджети — недобитий Qt-об'єкт дає флакі-краш
на виході (як задокументовано в test_pill_animation / test_meeting_ui).
"""
import os
import unittest
from types import SimpleNamespace

# Віджети без екрана: рендер-тесту потрібен QApplication, не реальний екран.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsDropShadowEffect, QVBoxLayout, QWidget,
)

from fronts.desktop import motion
from fronts.desktop.glass import GlassButton


def _app():
    return QApplication.instance() or QApplication([])


def _enter(w):
    QApplication.sendEvent(w, QEvent(QEvent.Type.Enter))


def _leave(w):
    QApplication.sendEvent(w, QEvent(QEvent.Type.Leave))


def _settle(w):
    """Догнати анімацію наведення до кінця детерміновано (без event-loop)."""
    anim = w._hover_lift._anim
    anim.setCurrentTime(anim.duration())


class HoverLiftTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        # детермінізм: не залежати від системного перемикача анімацій машини
        motion.init_config(SimpleNamespace(animations=True))
        motion._system_ok = True
        self._trash = []

    def tearDown(self):
        for w in self._trash:
            lift = getattr(w, "_hover_lift", None)
            if lift is not None:
                lift._anim.stop()
            w.deleteLater()
        self.app.processEvents()
        motion.init_config(None)
        motion._system_ok = None

    def _track(self, w):
        self._trash.append(w)
        return w

    # --- підключення ---
    def test_action_glassbutton_gets_lift(self):
        b = self._track(GlassButton("Зберегти"))
        self.assertIsInstance(getattr(b, "_hover_lift", None), motion.HoverLift)

    def test_nav_glassbutton_has_no_lift(self):
        # сайдбар-навігація має власний активний стан — підйом до неї не чіпляємо
        b = self._track(GlassButton("Історія", nav=True))
        self.assertIsNone(getattr(b, "_hover_lift", None))

    def test_button_inside_lift_card_drops_own_lift(self):
        # Sol-ревізія №2: картка-плитка вже має lift; її дочірня GlassButton
        # НЕ повинна мати власного HoverLift, інакше наведення на кнопку в
        # наведеній картці дає ПОДВІЙНИЙ підйом (6px сумарного руху).
        card = self._track(QFrame())
        card.setProperty("card", True)
        lay = QVBoxLayout(card)
        motion.lift_on_hover(card)
        btn = GlassButton("Переформатувати")       # parentless → отримує lift
        self.assertIsInstance(getattr(btn, "_hover_lift", None), motion.HoverLift)
        lay.addWidget(btn)                          # у картку з lift → власний lift знімається
        self.assertIsNone(getattr(btn, "_hover_lift", None))

    def test_button_in_plain_panel_keeps_lift(self):
        # кнопка в звичайній панелі (контейнер БЕЗ lift) — свій підйом зберігає
        panel = self._track(QWidget())
        pl = QVBoxLayout(panel)
        btn = GlassButton("Грати")
        pl.addWidget(btn)
        self.assertIsInstance(getattr(btn, "_hover_lift", None), motion.HoverLift)

    def test_lift_on_hover_idempotent(self):
        w = self._track(QFrame())
        motion.lift_on_hover(w)
        first = w._hover_lift
        motion.lift_on_hover(w)
        self.assertIs(w._hover_lift, first)

    # --- перфоманс: 0 ефектів у спокої ---
    def test_no_effect_at_rest(self):
        w = self._track(QFrame())
        motion.lift_on_hover(w)
        self.assertIsNone(w.graphicsEffect())   # стрічка не тримає тіней у спокої

    def test_list_holds_no_effects_at_rest(self):
        """Стрічка 25 карток: у спокої НУЛЬ offscreen-тіней (прокрутка без лагу),
        під курсором — РІВНО одна. Ключова вимога перфомансу сторінки «Історія»."""
        cards = [self._track(QFrame()) for _ in range(25)]
        for c in cards:
            motion.lift_on_hover(c)
        self.assertEqual(sum(c.graphicsEffect() is not None for c in cards), 0)
        _enter(cards[7])
        _settle(cards[7])
        self.assertEqual(sum(c.graphicsEffect() is not None for c in cards), 1)

    # --- підйом + тінь ---
    def test_enter_creates_shadow_and_raises(self):
        w = self._track(QFrame())
        w.resize(240, 90)
        w.move(0, 100)
        motion.lift_on_hover(w)
        _enter(w)
        _settle(w)
        self.assertIsInstance(w.graphicsEffect(), QGraphicsDropShadowEffect)
        self.assertGreater(w.graphicsEffect().blurRadius(), 1.0)
        self.assertEqual(w.y(), 100 - int(round(motion.HoverLift.RISE)))

    def test_leave_removes_shadow(self):
        host = self._track(QWidget())
        lay = QVBoxLayout(host)
        w = QFrame()
        lay.addWidget(w)
        motion.lift_on_hover(w)
        host.resize(260, 120)
        host.show()
        self.app.processEvents()
        _enter(w)
        _settle(w)
        self.assertIsInstance(w.graphicsEffect(), QGraphicsDropShadowEffect)
        _leave(w)
        _settle(w)
        self.assertIsNone(w.graphicsEffect())   # тінь знята → знову 0 ефектів
        host.hide()

    # --- Reduce Motion ---
    def test_reduce_motion_no_lift_no_shadow(self):
        motion.init_config(SimpleNamespace(animations=False))
        w = self._track(QFrame())
        w.move(0, 100)
        motion.lift_on_hover(w)
        _enter(w)
        self.assertIsNone(w.graphicsEffect())   # без тіні
        self.assertEqual(w.y(), 100)            # без підйому

    # --- a11y: підйом нічого не ламає ---
    def test_lift_preserves_accessibility(self):
        b = self._track(GlassButton("Обробити"))
        b.setAccessibleName("Обробити")
        pol = b.focusPolicy()
        _enter(b)
        _settle(b)
        self.assertEqual(b.accessibleName(), "Обробити")
        self.assertEqual(b.focusPolicy(), pol)
        self.assertTrue(b.isEnabled())

    def test_disabled_widget_does_not_lift(self):
        b = self._track(GlassButton("Вимкнено"))
        b.setEnabled(False)
        b.move(0, 100)
        _enter(b)
        self.assertIsNone(b.graphicsEffect())   # вимкнена кнопка не реагує
        self.assertEqual(b.y(), 100)


if __name__ == "__main__":
    unittest.main()
