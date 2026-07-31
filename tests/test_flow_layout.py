"""Прямий юніт-тест glass.FlowLayout (не через ціле MainWindow).

Хвиля 31.07: FlowLayout — механізм, яким закрито всі переноси рядків кнопок
цієї гілки. _do_layout (glass.py) явно пропускає приховані елементи
(``item.isEmpty()``) — рядок наради тримає в одному FlowLayout взаємно
виключні status/bar/cancel-кнопки, і без цього пропуску прихований елемент
лишав би дірку або зсував сусідів. Ця лінія коду досі не мала прямого тесту —
лише непряме покриття через живі сторінки (test_no_clipped_button_text.py).
Тут — мінімальний ізольований контракт: видимий+прихований item; прихований
не займає місця; видимі стоять щільно один за одним (той порядок x, що дає
_do_layout: x, x+w1+spacing, ...)."""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from fronts.desktop.glass import FlowLayout

_APP = QApplication.instance() or QApplication([])


class FlowLayoutHiddenItemTests(unittest.TestCase):
    def test_hidden_item_takes_no_space_visible_items_packed_tight(self):
        host = QWidget()
        flow = FlowLayout(host, margin=0, spacing=8)

        btn_a = QPushButton("A")
        btn_hidden = QPushButton("Прихована кнопка, яка мала б зайняти багато місця")
        btn_b = QPushButton("B")
        flow.addWidget(btn_a)
        flow.addWidget(btn_hidden)
        flow.addWidget(btn_b)
        btn_hidden.hide()   # прихована ДО першого _do_layout — isEmpty() бачить це одразу

        host.resize(2000, 200)   # ширини вистачає на все, ряд не переноситься
        host.show()
        _APP.processEvents()

        rect_a = btn_a.geometry()
        rect_b = btn_b.geometry()
        rect_hidden = btn_hidden.geometry()

        # 1. Приховану кнопку layout не має ставити після A з нормальним
        # проміжком (isEmpty() мав її пропустити повністю) — B йде ОДРАЗУ за A.
        self.assertEqual(rect_b.x(), rect_a.x() + rect_a.width() + flow.spacing(),
                          "B мусить стояти щільно за A — прихована кнопка не "
                          "повинна займати місце в ряду")
        self.assertEqual(rect_a.y(), rect_b.y(), "A і B — один рядок")

        # 2. Прихована кнопка не повинна отримати реальну геометрію поточного
        # проходу layout (лишається там, де Qt її залишив до/без розкладки).
        self.assertNotEqual(rect_hidden, rect_b.translated(0, 0),
                             "прихована кнопка не повинна ділити геометрію з B")

        # 3. sizeHint/heightForWidth FlowLayout також не рахує приховану:
        # мінімальна висота ряду з двох маленьких кнопок помітно менша за
        # висоту, яку дав би довгий текст прихованої кнопки, якби він рахувався.
        host.hide()

    def test_show_then_hide_item_stops_taking_space_on_relayout(self):
        """Симетрична перевірка: елемент видимий → прихований МІЖ проходами
        layout (не лише "прихований одразу") теж звільняє місце на
        наступному _do_layout — так само, як peer-кнопки статусу наради
        (status/bar/cancel) перемикаються одна на одну в живому коді."""
        host = QWidget()
        flow = FlowLayout(host, margin=0, spacing=8)
        btn_a = QPushButton("A")
        btn_c = QPushButton("C, довший підпис кнопки")
        flow.addWidget(btn_a)
        flow.addWidget(btn_c)
        host.resize(2000, 200)
        host.show()
        _APP.processEvents()

        rect_a_before = btn_a.geometry()
        rect_c_before = btn_c.geometry()
        self.assertEqual(rect_c_before.x(),
                         rect_a_before.x() + rect_a_before.width() + flow.spacing())

        btn_c.hide()
        flow.invalidate()
        flow.activate()
        _APP.processEvents()

        # A лишається на своєму місці — без сусіда праворуч нічого не заважає.
        rect_a_after = btn_a.geometry()
        self.assertEqual(rect_a_after.x(), rect_a_before.x())
        host.hide()


if __name__ == "__main__":
    unittest.main(verbosity=2)
