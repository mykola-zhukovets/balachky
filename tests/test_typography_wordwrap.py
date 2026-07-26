"""Юніт-перевірка переносу тексту (канон DESIGN-TYPOGRAPHY §3) для лейблів, які
не досяжні через навігацію головного вікна: in-window toast (motion.toast) і
шапка сторінки (pages.page_header). Сторінкові лейбли перевіряє
render_a11y_smoke; тут — компоненти-віджети напряму.

    python -m unittest tests.test_typography_wordwrap
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtWidgets import QApplication, QWidget, QLabel


class WordWrapCanonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_toast_label_word_wraps(self):
        """§3: довгий toast переноситься в межах вікна (setWordWrap)."""
        from fronts.desktop import motion
        parent = QWidget()
        parent.resize(600, 400)
        motion.toast(parent, "Дуже довге повідомлення, яке має переноситися по "
                             "словах, а не вилазити за межі вікна або обрізатися.")
        lbl = getattr(parent, "_toast", None)
        self.assertIsNotNone(lbl, "toast не створив лейбл")
        self.assertTrue(lbl.wordWrap(), "toast-лейбл без wordWrap (§3)")
        self.assertLessEqual(lbl.width(), parent.width(),
                             "toast ширший за вікно — вилазить (§3)")
        parent.deleteLater()

    def test_page_header_h1_word_wraps(self):
        """§3: H1 шапки сторінки переноситься, а не обрізається на вузькому вікні."""
        from fronts.desktop.pages import page_header
        box = page_header("Дуже довгий заголовок сторінки для перевірки переносу",
                          "Підзаголовок сторінки")
        h1 = box.itemAt(0).widget()
        self.assertIsInstance(h1, QLabel)
        self.assertTrue(h1.wordWrap(), "H1 шапки без wordWrap (§3)")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(WordWrapCanonTests))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
