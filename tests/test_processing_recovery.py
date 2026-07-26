"""feature/processing-slider (спека §7, §11 стадія 2): відновлення дослівного —
дія «Копіювати дослівно» видима користувачу на картці диктування та в історії.

Гарантія цілісності raw перевіряється деінде (raw завжди в history.jsonl); тут
перевіряємо саме ВИДИМІСТЬ дії відновлення — блокер суду №2 («history recovery
action» відсутня). Показуємо її лише коли обробка справді змінила текст (raw ≠ final).
"""
import os
import time
import types
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint                       # noqa: E402
from PySide6.QtWidgets import (                         # noqa: E402
    QApplication, QLabel, QPushButton, QVBoxLayout,
)
from unittest.mock import patch                         # noqa: E402

from fronts.desktop import motion                       # noqa: E402
from fronts.desktop import main_window as mw            # noqa: E402
from fronts.desktop.i18n import tr                      # noqa: E402
from fronts.desktop.pages.history import HistoryPage    # noqa: E402


class _FakeAction:
    def __init__(self, text=""):
        self.text = text
        self.triggered = SimpleNamespace(connect=lambda *a, **k: None)

    def setEnabled(self, _):
        pass


class _FakeMenu:
    """Мінімальний дублер QMenu: збирає тексти доданих дій, exec — no-op."""
    last = None

    def __init__(self, *a, **k):
        self.texts = []
        _FakeMenu.last = self

    def addAction(self, text=""):
        self.texts.append(text)
        return _FakeAction(text)

    def addSeparator(self):
        pass

    def exec(self, *a, **k):
        pass


class _Host(mw.TermFixMenuMixin):
    _fix_menu_allow_ban = False


def _menu_texts(raw, final):
    host = _Host()
    label = QLabel()                      # без виділення → selectedText() == ""
    label._final_text = final
    label._raw_text = raw
    with patch.object(mw, "QMenu", _FakeMenu):
        host._term_fix_menu(label, QPoint(0, 0))
    return _FakeMenu.last.texts


class CardMenuVerbatim(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        motion.init_config(SimpleNamespace(animations=False))

    def test_verbatim_action_shown_when_raw_differs(self):
        texts = _menu_texts("привіт я готовий", "Привіт, я готовий.")
        self.assertIn(tr("common_copy_verbatim"), texts)

    def test_verbatim_action_hidden_when_raw_equals_final(self):
        # Дослівний режим: final == raw → окрема дія непотрібна (не дублюємо «Копіювати»).
        texts = _menu_texts("привіт я готовий", "привіт я готовий")
        self.assertNotIn(tr("common_copy_verbatim"), texts)


def _history_card(raw, final):
    """Побудувати одну картку історії без важкого __init__ сторінки: реальний метод
    _add_card прив'язуємо до легкого носія (як у test_processing_document_gate)."""
    feedbox = QVBoxLayout()
    feedbox.addStretch()
    page = SimpleNamespace(
        _feedbox=feedbox, _cards=[],
        controller=SimpleNamespace(profile=SimpleNamespace()))
    add_card = types.MethodType(HistoryPage._add_card, page)
    add_card("{}", {"raw": raw, "final": final, "ts": int(time.time()),
                    "source": "desktop"})
    return page._cards[-1][0]


def _button_by_text(card, text):
    for b in card.findChildren(QPushButton):
        if b.text() == text:
            return b
    return None


class HistoryVerbatim(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        motion.init_config(SimpleNamespace(animations=False))

    def test_history_shows_copy_verbatim_when_raw_differs(self):
        card = _history_card("привіт я готовий", "Привіт, я готовий.")
        self.assertIsNotNone(_button_by_text(card, tr("common_copy_verbatim")))

    def test_history_hides_copy_verbatim_when_raw_equals_final(self):
        card = _history_card("привіт я готовий", "привіт я готовий")
        self.assertIsNone(_button_by_text(card, tr("common_copy_verbatim")))

    def test_history_copy_verbatim_copies_raw_not_final(self):
        raw, final = "привіт я готовий", "Привіт, я готовий."
        card = _history_card(raw, final)
        btn = _button_by_text(card, tr("common_copy_verbatim"))
        self.assertIsNotNone(btn)
        btn.click()
        self.assertEqual(QApplication.clipboard().text(), raw)


if __name__ == "__main__":
    unittest.main()
