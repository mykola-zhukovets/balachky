"""TDD-доказ, що детектор visual_gate НЕ пустушка.

Синтетичне вікно: НАВМИСНО обрізана кнопка (фіксована ширина 30px, довгий підпис)
+ нормальна кнопка (160px, короткий підпис). Детектор МУСИТЬ зловити першу і НЕ
зловити другу. Без цього тесту детектор міг би тихо зламатись (наприклад, рефактор
зробив би check_widget завжди порожнім) і гейт лишався б зеленим на бите вікно.

Чому це працює і offscreen (бандлений шрифт замість Segoe UI): різниця «30px проти
~400px тексту» домінує над будь-якою різницею метрик шрифта — обрізання лишається
обрізанням у будь-якому шрифті, а «ОК» у 160px влазить завжди. Тобто перевіряємо
відношення, а не абсолютні пікселі (той самий принцип, що в test_layout_no_clip).
Реальний windows-прогін детектора (з Segoe UI) дає окремо CLI-режим
`scripts/visual_gate.py --selfcheck`, який qa_gate ганяє на платформі windows.
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT, _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from PySide6.QtWidgets import QApplication, QPushButton, QWidget, QVBoxLayout
from PySide6.QtCore import Qt


def _app():
    return QApplication.instance() or QApplication([])


class VisualGateSelfCheck(unittest.TestCase):
    def test_detector_catches_clipped_button_and_spares_normal(self):
        app = _app()
        # visual_gate на імпорті лише виставляє QT_QPA_PLATFORM (пізно — QApplication
        # уже є) і шляхи; QApplication не створює. Імпорт безпечний тут.
        import visual_gate
        qt = visual_gate._lazy_qt()

        host = QWidget()
        lay = QVBoxLayout(host)
        clipped = QPushButton(
            "Дуже довгий підпис кнопки, що не влізе у тридцять пікселів")
        clipped.setObjectName("clipped_btn")
        clipped.setFixedWidth(30)
        lay.addWidget(clipped)
        normal = QPushButton("ОК")
        normal.setObjectName("normal_btn")
        normal.setFixedWidth(160)
        lay.addWidget(normal)
        host.setAttribute(Qt.WA_DontShowOnScreen, True)
        host.resize(240, 160)
        host.show()
        app.processEvents()

        v_clipped = visual_gate.check_widget(clipped, qt)
        v_normal = visual_gate.check_widget(normal, qt)
        host.close()

        self.assertTrue(
            any(v["type"] == "text_clipped" for v in v_clipped),
            "детектор НЕ зловив навмисно обрізану кнопку — він пустушка")
        self.assertEqual(
            v_normal, [],
            f"детектор хибно позначив нормальну кнопку: {v_normal}")


if __name__ == "__main__":
    unittest.main()
