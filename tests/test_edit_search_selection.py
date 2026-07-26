"""feature/voice-edit-selection — Scenario B: кнопка «AI-редагувати виділене» в
редакторі розшифровки (TranscriptEditPanel._ai_edit_selection).

Регресія на нормалізацію виділеного тексту. QTextCursor.selectedText() віддає
розриви рядків як U+2029 (PARAGRAPH SEPARATOR), тож перед показом diff і перед
відправкою в LLM їх треба повернути в '\\n' — але ПРОБІЛИ всередині рядків мають
лишитися недоторканими. Якби нормалізація замінювала пробіл (' ') замість U+2029,
будь-яке багатослівне виділення побилося б на «одне слово в рядок», спотворивши і
diff, і вхід моделі. Ці тести ловлять саме таку помилку.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel
from PySide6.QtGui import QTextCursor

from fronts.desktop.pages.edit_search import TranscriptEditPanel

_PS = chr(0x2029)   # PARAGRAPH SEPARATOR — selectedText() дає його замість переносу


def _app():
    return QApplication.instance() or QApplication([])


class AiEditSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def _panel(self, text):
        """Панель з увімкненим ai_edit_fn, що ЛОВИТЬ переданий у неї виділений
        текст. Повертає (panel, captured), де captured["selected"] — те, що піде
        в LLM/diff."""
        store = {"text": text}
        captured = {}

        def ai_edit_fn(selected, replace_with):
            captured["selected"] = selected
            captured["replace_with"] = replace_with

        body = QLabel(text)
        panel = TranscriptEditPanel(
            body, lambda: store["text"], lambda new: store.update(text=new),
            ai_edit_fn=ai_edit_fn)
        panel.begin_edit()
        return panel, captured

    def _select_all_and_fire(self, panel):
        cur = panel._editor.textCursor()
        cur.select(QTextCursor.Document)
        panel._editor.setTextCursor(cur)
        panel._ai_edit_selection()

    def test_spaces_within_line_preserved(self):
        # одинарний рядок з кількома словами: жоден пробіл не має стати переносом.
        panel, captured = self._panel("зроби це речення офіційним")
        self._select_all_and_fire(panel)
        self.assertEqual(captured["selected"], "зроби це речення офіційним")

    def test_paragraph_separator_becomes_newline(self):
        # два рядки: U+2029 між ними → '\n', пробіли всередині рядків лишаються.
        panel, captured = self._panel("перший рядок тут\nдругий рядок теж")
        self._select_all_and_fire(panel)
        self.assertEqual(captured["selected"], "перший рядок тут\nдругий рядок теж")
        # явно: саме '\n', а не U+2029, і не побито по пробілах
        self.assertIn("\n", captured["selected"])
        self.assertNotIn(_PS, captured["selected"])
        self.assertNotIn("рядок\nтут", captured["selected"])

    def test_multiword_selection_not_shredded(self):
        # Головна пастка: якби замість U+2029 замінявся пробіл, вийшло б
        # «одне слово в рядок». Перевіряємо, що переносів немає (один рядок),
        # а не стільки, скільки пробілів.
        panel, captured = self._panel("а б в г д")
        self._select_all_and_fire(panel)
        self.assertEqual(captured["selected"], "а б в г д")
        self.assertEqual(captured["selected"].count("\n"), 0)

    def test_replace_with_applies_only_to_selected_range(self):
        # replace_with, переданий у callback, заміняє САМЕ виділений діапазон.
        panel, captured = self._panel("лишити початок і кінець")
        cur = panel._editor.textCursor()
        cur.setPosition(len("лишити "))
        cur.setPosition(len("лишити початок"), QTextCursor.KeepAnchor)
        panel._editor.setTextCursor(cur)
        panel._ai_edit_selection()
        self.assertEqual(captured["selected"], "початок")
        captured["replace_with"]("ЗАМІНЕНО")
        self.assertEqual(panel._editor.toPlainText(), "лишити ЗАМІНЕНО і кінець")

    def test_no_selection_does_not_call_ai(self):
        panel, captured = self._panel("текст без виділення")
        cur = panel._editor.textCursor()
        cur.clearSelection()
        panel._editor.setTextCursor(cur)
        panel._ai_edit_selection()
        self.assertNotIn("selected", captured)


if __name__ == "__main__":
    unittest.main()
