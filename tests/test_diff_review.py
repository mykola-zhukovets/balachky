"""feature/player-pack — «Огляд перед дією»: diff перед застосуванням
автоматичних змін розшифровки. Чиста логіка + конструкція діалогу (offscreen).
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fronts.desktop.diff_review import word_diff, _render_html, chosen_text
from fronts.desktop import theme


class WordDiffTests(unittest.TestCase):
    def test_no_change(self):
        toks = word_diff("привіт світ", "привіт світ")
        self.assertTrue(all(op == "equal" for op, _ in toks))

    def test_filler_removed(self):
        # чистка філерів прибрала «ееє»
        toks = word_diff("я ееє думаю так", "я думаю так")
        self.assertIn(("removed", "ееє"), toks)
        self.assertNotIn(("added", "ееє"), toks)

    def test_typo_replaced(self):
        # автокорекція виправила одрук → одне removed + одне added
        toks = word_diff("це помидка тексту", "це помилка тексту")
        self.assertIn(("removed", "помидка"), toks)
        self.assertIn(("added", "помилка"), toks)

    def test_word_added(self):
        toks = word_diff("привіт", "привіт друже")
        self.assertIn(("added", "друже"), toks)


class RenderHtmlTests(unittest.TestCase):
    def test_colors_and_escaping(self):
        html = _render_html([("equal", "a"), ("removed", "b"), ("added", "<c>")])
        self.assertIn(theme.ALERT, html)          # видалене — ALERT
        self.assertIn("line-through", html)
        self.assertIn(theme.SUCCESS, html)        # додане — SUCCESS
        self.assertIn("&lt;c&gt;", html)          # екранування HTML

    def test_empty(self):
        self.assertEqual(_render_html([]), "")


class ChosenTextTests(unittest.TestCase):
    def test_apply_returns_after(self):
        self.assertEqual(chosen_text("було", "стало", True), "стало")

    def test_keep_returns_before(self):
        self.assertEqual(chosen_text("було", "стало", False), "було")


class CleanTranscriptGateTests(unittest.TestCase):
    """clean_transcript_text: обидва кроки вимкнено → текст незмінний (жоден
    важкий компонент не викликається)."""

    def test_both_off_unchanged(self):
        from types import SimpleNamespace
        from fronts.desktop.app import DesktopApp
        stub = SimpleNamespace(
            cfg=SimpleNamespace(filler_cleanup=False, autocorrect_enabled=False),
            terms=[])
        out = DesktopApp.clean_transcript_text(stub, "текст без змін")
        self.assertEqual(out, "текст без змін")

    def test_empty_unchanged(self):
        from types import SimpleNamespace
        from fronts.desktop.app import DesktopApp
        stub = SimpleNamespace(
            cfg=SimpleNamespace(filler_cleanup=True, autocorrect_enabled=True),
            terms=[])
        self.assertEqual(DesktopApp.clean_transcript_text(stub, ""), "")


class DialogConstructTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
        except Exception:                                  # pragma: no cover
            raise unittest.SkipTest("PySide6 недоступний")
        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_builds_without_crash(self):
        from fronts.desktop.diff_review import DiffReviewDialog
        dlg = DiffReviewDialog("я ееє тут", "я тут")
        self.assertTrue(dlg.isModal())
        dlg.deleteLater()


if __name__ == "__main__":
    unittest.main()
