"""HoverLift не жбурляє кнопку в застарілі координати (живий тест 31.07).

Сторінка «Запис екрана» перебудовує картки при кожній зміні стану. Якщо курсор
уже стоїть там, де з'явиться кнопка, Enter прилітає ДО першої розкладки:
«дім» (self._home_y) запам'ятовується з тимчасового y (0), і move() підйому
відносить кнопку в шапку картки — власник бачив обрізану «Видалити» поверх
рамки. Фікс: Move-подія НЕ від самого підйому = layout розставив по-справжньому
→ оновити дім і застосувати підйом від нової позиції.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from fronts.desktop import motion

_APP = QApplication.instance() or QApplication([])


class HoverLiftStaleHomeTests(unittest.TestCase):
    def setUp(self):
        self.parent = QWidget()
        self.parent.resize(400, 200)
        self.btn = QPushButton("Видалити", self.parent)
        self.lift = motion.HoverLift(self.btn)
        # Move-подія прихованого віджета відкладається до показу — а в живому
        # застосунку картки видимі, тож показуємо і тут.
        self.parent.show()
        _APP.processEvents()

    def tearDown(self):
        self.parent.deleteLater()
        _APP.processEvents()

    def _hovered_at_stale_home(self):
        """Стан як у живому випадку: підйом активний, дім запам'ятався з y=0."""
        self.lift._t = 1.0
        self.lift._home_y = self.btn.y()   # 0 — до першої розкладки

    def test_layout_move_during_lift_updates_home_and_position(self):
        self._hovered_at_stale_home()
        self.btn.move(30, 140)             # layout розставив по-справжньому
        _APP.processEvents()
        self.assertEqual(self.lift._home_y, 140,
                         "дім мусить піти за розкладкою, не лишитись 0")
        self.assertEqual(self.btn.y(), 140 - int(motion.HoverLift.RISE),
                         "підйом застосовано від НОВОГО дому")

    def test_own_lift_move_does_not_shift_home(self):
        self.btn.move(30, 140)
        _APP.processEvents()
        self.lift._t = 0.0
        self.lift._home_y = 140
        self.lift._apply(1.0)              # власний рух підйому (y=137)
        _APP.processEvents()
        self.assertEqual(self.lift._home_y, 140,
                         "власний move підйому НЕ має зсувати дім (інакше "
                         "кнопка повзла б угору з кожним кадром анімації)")

    def test_press_animation_move_does_not_drift_home(self):
        """Суд 31.07 (BLOCK): press() анімує geometry тієї ж кнопки; без
        захисту кожен клік під наведенням зсував дім на позицію натиску і
        кнопка назавжди дрейфувала вгору по ~3px за клік."""
        self.btn.move(30, 140)
        _APP.processEvents()
        self.lift._t = 1.0
        self.lift._home_y = 140
        self.btn._press_anim = object()      # триває анімація натиску
        try:
            self.btn.move(30, 138)           # кадр press-анімації (не layout)
            _APP.processEvents()
            self.assertEqual(self.lift._home_y, 140,
                             "дім НЕ має їхати за анімацією натиску")
        finally:
            self.btn._press_anim = None

    def test_move_at_rest_does_not_touch_lift(self):
        self.lift._t = 0.0
        self.btn.move(30, 140)
        _APP.processEvents()
        self.assertEqual(self.btn.y(), 140)   # без підйому — жодних зсувів


if __name__ == "__main__":
    unittest.main()
