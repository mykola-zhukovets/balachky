"""Регрес діагнозу 2026-07-30 №2: подвійне обведення активної кнопки в
сегмент-контролі (перемикач джерела «Монітор/Вікно» на ScreenPage).

Причина була структурна: контейнер-підкладка (QFrame[glasspanel="true"])
малює власну рамку 1px (app-level QSS), а активна GlassButton усередині
малює СВОЮ золоту рамку з відступом 2px (padding контейнера) — дві лінії
поспіль. Фікс — theme.style_segment_frame(): підкладка лишає фон і радіус,
але явно вимикає власну рамку (border: none), тож єдина рамка, яку бачить
користувач, — золота рамка активної кнопки.

    python -m unittest tests.test_segment_controls_no_double_border
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.render_nav_smoke import _NavController, _make_sandbox


class SegmentControlsNoDoubleBorderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))
        cls._sandbox = _make_sandbox()

    def setUp(self):
        self._pages = []

    def tearDown(self):
        from PySide6.QtCore import QTimer
        for page in self._pages:
            for t in page.findChildren(QTimer):
                try:
                    t.stop()
                except RuntimeError:
                    pass
            try:
                page.close()
            except Exception:
                pass
            page.deleteLater()
        self._pages = []
        for _ in range(3):
            self._app.processEvents()

    def _screen_page(self):
        from fronts.desktop.pages.screen import ScreenPage
        page = ScreenPage(_NavController(self._sandbox))
        self._pages.append(page)
        return page

    def test_segment_container_has_no_own_border(self):
        """Контейнер сегмент-перемикача «Монітор/Вікно»: [glasspanel="true"]
        (звідки й береться app-level border 1px), АЛЕ інстанс-стиль явно
        вимикає власну рамку — інакше вона накладається на золоту рамку
        активної checked-кнопки (подвійне обведення)."""
        page = self._screen_page()
        buttons = page._kind.buttons()
        self.assertTrue(buttons, "перемикач джерела не збудувався (_kind порожній)")
        frame = buttons[0].parentWidget()
        self.assertIsNotNone(frame, "у сегмент-кнопки немає контейнера-підкладки")
        self.assertTrue(
            bool(frame.property("glasspanel")),
            "тест припускає glasspanel=true — інакше app QSS не дає рамки й "
            "перевірка нічого не доводить")
        style = frame.styleSheet()
        self.assertIn(
            "border: none", style,
            "підкладка сегмент-контролу МАЄ явно вимикати власну рамку "
            "(border: none), інакше вона малює 1px поверх золотої рамки "
            "активної кнопки — подвійне обведення (діагноз 2026-07-30 №2)")

    def test_only_one_button_checked_has_active_border(self):
        """Активний стан лишається на кнопці (checked=True за замовч. на
        «Монітор») — фікс контейнера не мав зняти саму рамку активної кнопки,
        лише зняти ЗАЙВУ зовнішню."""
        page = self._screen_page()
        checked = [b for b in page._kind.buttons() if b.isChecked()]
        self.assertEqual(len(checked), 1,
                          "рівно одна кнопка джерела має бути активною")


if __name__ == "__main__":
    unittest.main()
