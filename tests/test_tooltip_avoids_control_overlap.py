"""Регрес діагнозу 2026-07-30 №3: підказка кнопки «Звільнити місце» ховала
сусідню кнопку «Обробити нараду» під собою (Правило збереження контролів:
«Підказка ніколи не повинна перекривати сусідні елементи керування»).

Фікс — один спільний хелпер fronts/desktop/glass.py::show_tooltip_avoiding_siblings,
яким тепер користуються і GlassButton (event() перехоплює QEvent.ToolTip), і
TipToolButton (enterEvent) — не сім різних латок по кнопках.

    python -m unittest tests.test_tooltip_avoids_control_overlap
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QHBoxLayout, QToolTip, QWidget


_LONG_TIP = ("Звільнити 1234 МБ дискового простору — дуже довгий текст підказки, "
             "явно ширший за кнопку, щоб гарантовано перекрити сусіда знизу-зліва")


class TooltipAvoidsControlOverlapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _two_button_row(self):
        from fronts.desktop.glass import GlassButton
        host = QWidget()
        lay = QHBoxLayout(host)
        lay.setSpacing(8)
        process = GlassButton("Обробити нараду")
        free = GlassButton("Звільнити місце")
        lay.addWidget(process)
        lay.addWidget(free)
        host.setAttribute(Qt.WA_DontShowOnScreen, True)
        host.resize(260, 60)
        host.show()
        for _ in range(3):
            self._app.processEvents()
        return host, process, free

    def test_flips_position_when_default_would_cover_sibling(self):
        """Дефолтна позиція (під лівим краєм кнопки) для довгої підказки
        перекриває сусідню кнопку зліва в тому самому layout — хелпер МУСИТЬ
        змістити підказку (не лишати її на перекривній позиції)."""
        from fronts.desktop.glass import (
            _tooltip_overlap_band, show_tooltip_avoiding_siblings,
        )
        host, process, free = self._two_button_row()
        try:
            naive_below = free.mapToGlobal(free.rect().bottomLeft())
            naive_band = _tooltip_overlap_band(naive_below, _LONG_TIP)
            process_rect_before = process.mapToGlobal(process.rect().topLeft())
            # припущення тесту: наївна позиція ДІЙСНО ризикує перекрити сусіда —
            # інакше перевірка нічого не доводить (короткий текст не дав би дефекту)
            from PySide6.QtCore import QRect
            process_rect = QRect(process_rect_before, process.size())
            self.assertTrue(
                naive_band.intersects(process_rect),
                "тестовий текст мусить бути достатньо довгим/близьким, щоб "
                "наївна позиція ризикувала перекрити сусіда — інакше тест vacuous")

            captured = []
            orig = QToolTip.showText
            QToolTip.showText = staticmethod(
                lambda pos, text, widget=None, *a: captured.append(pos))
            try:
                show_tooltip_avoiding_siblings(free, _LONG_TIP)
            finally:
                QToolTip.showText = orig

            self.assertTrue(captured, "show_tooltip_avoiding_siblings не показав підказку")
            chosen = captured[0]
            self.assertNotEqual(
                chosen, naive_below,
                "виявивши перекриття сусіда, хелпер мусить змістити підказку "
                "(напр. над кнопкою), а не лишати її на перекривній позиції")
            chosen_band = _tooltip_overlap_band(chosen, _LONG_TIP)
            self.assertFalse(
                chosen_band.intersects(process_rect),
                "нова позиція підказки й досі перекриває сусідню кнопку "
                "«Обробити нараду» (Правило збереження контролів)")
        finally:
            host.close()
            host.deleteLater()

    def test_short_tip_without_siblings_keeps_default_position(self):
        """Без ризику перекриття (нема сусідів у layout) хелпер НЕ має
        зайвий раз зсувати підказку — інакше кожна підказка стрибала б вгору."""
        from fronts.desktop.glass import GlassButton, show_tooltip_avoiding_siblings
        lone = GlassButton("Кнопка")
        lone.setAttribute(Qt.WA_DontShowOnScreen, True)
        lone.resize(120, 30)
        lone.show()
        for _ in range(3):
            self._app.processEvents()
        try:
            captured = []
            orig = QToolTip.showText
            QToolTip.showText = staticmethod(
                lambda pos, text, widget=None, *a: captured.append(pos))
            try:
                show_tooltip_avoiding_siblings(lone, "Коротко")
            finally:
                QToolTip.showText = orig
            self.assertTrue(captured)
            self.assertEqual(captured[0], lone.mapToGlobal(lone.rect().bottomLeft()))
        finally:
            lone.close()
            lone.deleteLater()


if __name__ == "__main__":
    unittest.main()
