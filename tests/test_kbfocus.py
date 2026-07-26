"""Рамка фокуса — лише при клавіатурній навігації.

Микола на живому тесті 24.07: після кліку мишею кнопка лишалась із рамкою і
виглядала «залиплою». Рішення: властивість kbfocus, яку ставить застосунковий
фільтр подій за QFocusEvent.reason(); QSS малює рамку тільки для неї.

Стережемо обидві сторони: миша НЕ малює рамку, Tab — малює (інакше «фікс»
перетворився б на втрату доступності для клавіатури).
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                    # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from fronts.desktop import theme                 # noqa: E402


class KeyboardFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.apply_theme(cls.app, night=False)   # тут вмикається фільтр

    def _btn(self):
        # вікно має бути АКТИВНИМ, інакше Qt не доставляє QFocusEvent —
        # setFocus лише запам'ятовує майбутнього власника фокуса
        b = QPushButton("Кнопка", None)
        b.show()
        b.activateWindow()
        self.app.processEvents()
        # активація вікна сама віддає фокус кнопці (ActiveWindowFocusReason) —
        # скидаємо, щоб далі перевіряти саме ту причину, яку ставить тест
        b.clearFocus()
        return b

    def test_mouse_focus_does_not_mark_button(self):
        b = self._btn()
        b.setFocus(Qt.FocusReason.MouseFocusReason)
        self.assertIsNone(b.property("kbfocus"),
                          "після кліку мишею рамка фокуса не має малюватись")

    def test_tab_focus_marks_button(self):
        b = self._btn()
        b.setFocus(Qt.FocusReason.TabFocusReason)
        self.assertEqual(b.property("kbfocus"), "true",
                         "при навігації Tab рамка фокуса ОБОВ'ЯЗКОВА (доступність)")

    def test_mark_cleared_on_focus_out(self):
        first, second = self._btn(), self._btn()
        first.setFocus(Qt.FocusReason.TabFocusReason)
        second.setFocus(Qt.FocusReason.TabFocusReason)
        self.assertIsNone(first.property("kbfocus"),
                          "мітка має зніматись, коли фокус пішов далі")

    def test_qss_draws_border_only_for_keyboard_focus(self):
        self.assertIn('QPushButton[kbfocus="true"]', theme.QSS)
        self.assertNotIn("QPushButton:focus {", theme.QSS)


if __name__ == "__main__":
    unittest.main()
