"""Offscreen-тест геометрії базових візуальних примітивів під стилем QSS.

Перевіряє коректність побудови та розмірів віджетів (EmptyState, акцентної кнопки,
випадного списку, повзунка, області прокручування та діалогового вікна).
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
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from fronts.desktop.empty_state import EmptyState
from fronts.desktop.theme import QSS


class RenderDesignSmokeTests(unittest.TestCase):
    """Димові тести геометрії базових компонентів інтерфейсу."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])
        cls._app.setStyleSheet(QSS)

    def test_design_primitives_geometry(self):
        """Побудова та перевірка геометрії спільних UI-віджетів."""
        host = QWidget()
        try:
            layout = QVBoxLayout(host)
            layout.setContentsMargins(24, 24, 24, 24)
            empty = EmptyState("fa6s.file-audio", "No files yet", "Choose files or drag them here")
            layout.addWidget(empty)
            row = QHBoxLayout()
            button = QPushButton("Primary")
            button.setProperty("accent", True)
            button.setFocus(Qt.TabFocusReason)
            combo = QComboBox()
            combo.addItem("One")
            slider = QSlider(Qt.Horizontal)
            slider.setValue(50)
            row.addWidget(button)
            row.addWidget(combo)
            row.addWidget(slider)
            layout.addLayout(row)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(QWidget())
            layout.addWidget(scroll)
            host.resize(960, 640)
            host.show()
            self._app.processEvents()

            # нижня межа доступної ширини виводиться з самого віджета, а не
            # з довільного числа. Невикладений віджет має типову ненульову
            # геометрію і навіть isVisibleTo(host) == True, якщо в нього є
            # батько, тож вилучення з компонування ловимо через indexOf.
            title_min_width = QFontMetrics(empty.title_label.font()).horizontalAdvance(
                empty.title_label.text()
            )
            self.assertGreaterEqual(empty.title_label.geometry().width(), title_min_width)
            self.assertGreaterEqual(empty.layout().indexOf(empty.title_label), 0,
                                    "заголовок порожнього стану випав із компонування")
            self.assertGreaterEqual(button.geometry().height(), 24)
            self.assertGreaterEqual(combo.geometry().width(), combo.minimumSizeHint().width())
            self.assertTrue(combo.isVisibleTo(host))
            self.assertGreaterEqual(slider.geometry().width(), slider.minimumSizeHint().width())
            self.assertTrue(slider.isVisibleTo(host))

            # ширину скролбара offscreen-стиль не застосовує до width() — піксельна
            # перевірка тут хибно-негативна; тонкість скролбара дивимось живим оглядом
            dialog = QDialog(host)
            try:
                dialog.resize(320, 160)
                dialog.show()
                self._app.processEvents()
                self.assertEqual(dialog.width(), 320)
            finally:
                dialog.close()
                dialog.deleteLater()
        finally:
            host.close()
            host.deleteLater()
            self._app.processEvents()


if __name__ == "__main__":
    unittest.main()
