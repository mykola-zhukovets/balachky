"""Тости не валять програму, коли підпис помер раніше за таймер.

Живий тест 25.07 дав краш «Балачки перестали працювати»:

    File "fronts/desktop/motion.py", line 580, in _done
    RuntimeError: libshiboken: Internal C++ object
    (PySide6.QtWidgets.QLabel) already deleted.

Корінь: анімація тоста живе, поки живий батьківський віджет, і через 2,5 с
кличе _done, щоб прибрати підпис. Але сам підпис міг зникнути раніше — новий
тост забирає слот `_toast` і видаляє попередній, сторінку могли закрити,
натиснули «Скасувати» в undo-тості. Тоді колбек звертається до мертвого
C++-об'єкта, і Qt кидає RuntimeError уже поза нашим стеком — програма падає.

Тут три сценарії, по одному на кожен колбек motion.py, і кожен ловить свою
мутацію: якщо прибрати перевірку shiboken6.isValid — тест червоніє з тим самим
RuntimeError, що бачив власник.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6
from PySide6.QtWidgets import QApplication, QWidget

from fronts.desktop import motion

_APP = QApplication.instance() or QApplication([])


class ToastLifetimeTests(unittest.TestCase):
    """Кожен тест: показати тост → вбити підпис → змусити колбек спрацювати."""

    def setUp(self):
        self.parent = QWidget()
        self.parent.resize(800, 600)
        self._live = [self.parent]

    def tearDown(self):
        for w in self._live:
            if shiboken6.isValid(w):
                w.close()
                w.deleteLater()
        _APP.processEvents()

    def _kill(self, widget):
        """Знищити C++-об'єкт віджета НАСПРАВДІ.

        deleteLater() тут не годиться: Qt відкладає знищення, а анімація й слот
        _toast тримають посилання, тож після processEvents об'єкт ще живий і тест
        нічого не перевіряє. shiboken6.delete прибирає C++-частину негайно —
        рівно той стан, у якому колбек анімації падав у власника."""
        shiboken6.delete(widget)
        _APP.processEvents()
        self.assertFalse(shiboken6.isValid(widget),
                         "віджет мусив померти — інакше тест нічого не перевіряє")

    def test_animated_toast_survives_dead_label(self):
        """Анімований тост: finished приходить після смерті підпису."""
        motion.toast(self.parent, "Скопійовано")
        lbl = getattr(self.parent, "_toast", None)
        self.assertIsNotNone(lbl, "тост не створив підпису — тест виродився")
        self._kill(lbl)
        # Колбек анімації дістаємо через сам об'єкт анімації: у бою його кличе
        # seq.finished, тут кличемо напряму — це та сама мить, що валила програму.
        for child in self.parent.children():
            done = getattr(child, "_motion_done_for_test", None)
            if done is not None:
                done()
        # Головне: жодного RuntimeError. Якщо захист прибрати — тут падіння.
        motion.toast(self.parent, "Другий тост")

    def test_static_toast_survives_dead_label(self):
        """Тост без анімацій (вимкнені в конфізі) прибирається таймером."""
        motion.init_config(type("Cfg", (), {"animations": False})())
        try:
            motion.toast(self.parent, "Слово додано")
            lbl = getattr(self.parent, "_toast", None)
            self.assertIsNotNone(lbl)
            self._kill(lbl)
            motion.toast(self.parent, "Ще одне слово")
        finally:
            motion.init_config(type("Cfg", (), {"animations": True})())

    def test_second_toast_replaces_first_without_crash(self):
        """Сценарій власника: два тости підряд — другий прибирає перший."""
        motion.toast(self.parent, "Перший")
        first = getattr(self.parent, "_toast", None)
        motion.toast(self.parent, "Другий")
        _APP.processEvents()
        second = getattr(self.parent, "_toast", None)
        self.assertIsNot(first, second, "другий тост мусив замінити перший")
        # Перший або вже мертвий, або помре — у будь-якому разі без падіння.
        _APP.processEvents()
        motion.toast(self.parent, "Третій")


if __name__ == "__main__":
    unittest.main()
