"""Регрес живого тесту 21.07: кнопки глобального діалогу краху («Балачки
перестали працювати») ОБРІЗАНІ — 5 кнопок у один ряд не вміщались, читалось
лише «Закрити вікно», решта — «оказати детал», «рвати технічні», «и папку з
жур», «ити звіт на Gi».

Інваріант: кожна кнопка діалогу читається ПОВНІСТЮ (fontMetrics тексту + падинг
<= фактична ширина кнопки) в обох мовах. Міряємо тими самими метриками, що й
продакшн, тож стійко до підміни шрифту offscreen.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fronts.desktop import crash, i18n


def _app():
    return QApplication.instance() or QApplication([])


class CrashDialogButtonsFitTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        self._lang = i18n.current_language()

    def tearDown(self):
        i18n.set_language(self._lang)
        self.app.processEvents()

    def _assert_buttons_fit(self, lang):
        i18n.set_language(lang)
        dlg, buttons, _ = crash._build_crash_dialog("трасування помилки\n" * 6)
        try:
            dlg.show()
            for _ in range(5):
                self.app.processEvents()
            # всі 4 дії присутні (копіювати / журнали / звіт / закрити)
            self.assertGreaterEqual(len(buttons), 4, "діалог має 4 кнопки дій")
            for b in buttons:
                fm = b.fontMetrics()
                need = fm.horizontalAdvance(b.text().replace("&", ""))
                self.assertGreaterEqual(
                    b.width(), need + 16,
                    f"[{lang}] кнопка ріже {b.text()!r}: ширина {b.width()}px "
                    f"< текст {need}px + падинг")
        finally:
            dlg.deleteLater()
            self.app.processEvents()

    def test_buttons_fit_ukrainian(self):
        self._assert_buttons_fit("uk")

    def test_buttons_fit_english(self):
        self._assert_buttons_fit("en")


if __name__ == "__main__":
    unittest.main()
