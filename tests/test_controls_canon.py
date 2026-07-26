"""Тести канону контролів: повзунки (QSlider) та шапки таблиць (QHeaderView).

Перевіряють, що:
1. Усі QSlider у застосунку мають канонічну роль `canon_role == "slider"`.
2. Усі QHeaderView у застосунку мають канонічну роль `canon_role == "table_header"`.
"""
import os
import shutil
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QSlider, QTableWidget  # noqa: E402

from fronts.desktop.chip_popover import make_slider  # noqa: E402
from fronts.desktop.pages.settings import SettingsPage  # noqa: E402
from fronts.desktop.pages.vocab import VocabPage  # noqa: E402
from tests.render_nav_smoke import _NavController, _make_sandbox  # noqa: E402


class ControlsCanonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_make_slider_has_canon_role(self):
        s = make_slider()
        self.assertEqual(s.property("canon_role"), "slider")

    def test_settings_page_sliders_have_canon_role(self):
        sandbox = _make_sandbox()
        try:
            ctrl = _NavController(sandbox)
            page = SettingsPage(ctrl)
            sliders = page.findChildren(QSlider)
            self.assertGreater(len(sliders), 0, "SettingsPage must contain sliders")
            for slider in sliders:
                role = slider.property("canon_role")
                self.assertEqual(
                    role, "slider",
                    f"Slider {slider} on SettingsPage lacks canon_role='slider'"
                )
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)

    def test_vocab_page_table_headers_have_canon_role(self):
        sandbox = _make_sandbox()
        try:
            ctrl = _NavController(sandbox)
            page = VocabPage(ctrl)
            tables = page.findChildren(QTableWidget)
            self.assertGreater(len(tables), 0, "VocabPage must contain tables")
            for table in tables:
                header = table.horizontalHeader()
                role = header.property("canon_role")
                self.assertEqual(
                    role, "table_header",
                    f"Header of table {table} on VocabPage lacks canon_role='table_header'"
                )
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)

    def test_settings_page_table_headers_have_canon_role(self):
        sandbox = _make_sandbox()
        try:
            ctrl = _NavController(sandbox)
            page = SettingsPage(ctrl)
            tables = page.findChildren(QTableWidget)
            self.assertGreater(len(tables), 0, "SettingsPage must contain tables")
            for table in tables:
                header = table.horizontalHeader()
                role = header.property("canon_role")
                self.assertEqual(
                    role, "table_header",
                    f"Header of table {table} on SettingsPage lacks canon_role='table_header'"
                )
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
