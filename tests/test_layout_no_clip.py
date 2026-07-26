"""Регрес кластера «обрізані написи»: ширина текстових контролів має рахуватись
від fontMetrics, а не бути сталою константою, інакше довший підпис (укр АБО
англ) ріжеться. Тести перевіряють ІНВАРІАНТ «напис уміщується» тими самими
метриками, що ними рахує продакшн-код, тож стійкі до підміни шрифту offscreen
(де Segoe UI підмінюється вужчим бандлом — абсолютні пікселі offscreen «брешуть»,
а відношення width>=fontMetrics зберігається).

Ловить два конкретні дефекти живого тесту й той самий клас загалом:
- плаваюча пілюля з фікс. шириною 132px різала «Розшифровую…»/«Transcribing…»;
- підпис рівня наради з фікс. 120px різав «Системний звук»/«Microphone 88».
"""
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from fronts.desktop import i18n
from fronts.desktop.i18n import tr


def _app():
    return QApplication.instance() or QApplication([])


class PillWidthTests(unittest.TestCase):
    """Плаваюча пілюля: підпис стану вміщується в обох мовах (fix instance #1)."""

    def setUp(self):
        self.app = _app()
        self._lang = i18n.current_language()

    def tearDown(self):
        i18n.set_language(self._lang)

    def _assert_pill_fits(self, lang):
        from fronts.desktop.pill import (
            FloatingPill, _STATE_KEY, _TEXT_LEFT, _TEXT_RIGHT,
        )
        i18n.set_language(lang)
        pill = FloatingPill(on_moved=lambda *a: None, on_reset=lambda: None)
        try:
            f = QFont(pill.font())
            f.setPixelSize(13)          # той самий кегль, що й у paintEvent
            fm = QFontMetrics(f)
            avail = pill.width() - _TEXT_LEFT - _TEXT_RIGHT
            for state in ("recording", "busy"):
                pill.set_state(state)   # реальний state → i18n key → paint-шлях
                self.app.processEvents()
                key = _STATE_KEY[state]
                self.assertGreaterEqual(
                    avail, fm.horizontalAdvance(tr(key)),
                    f"[{lang}/{state}] пілюля ріже {tr(key)!r}: "
                    f"доступно {avail}px")
                self.assertEqual(pill._state, state)
                self.assertFalse(pill.grab().isNull(),
                                 f"[{lang}/{state}] стан не рендериться")
        finally:
            pill._timer.stop()
            pill.hide()
            pill.deleteLater()
            self.app.processEvents()

    def test_pill_fits_ukrainian(self):
        self._assert_pill_fits("uk")

    def test_pill_fits_english(self):
        self._assert_pill_fits("en")


class LevelCaptionTests(unittest.TestCase):
    """Підпис колонки рівнів наради вміщує найдовший підпис (fix «Системний звук»)."""

    def setUp(self):
        self.app = _app()
        self._lang = i18n.current_language()

    def tearDown(self):
        i18n.set_language(self._lang)

    def _assert_level_fits(self, lang):
        from fronts.desktop.pages.meeting import MeetingPage
        i18n.set_language(lang)
        row = MeetingPage._level_row(tr("meeting_level_sys"), QWidget())
        try:
            lbl = row.findChildren(QLabel)[0]
            f = QFont(lbl.font())
            f.setPixelSize(14)          # QSS[formlabel] = 14px
            fm = QFontMetrics(f)
            col = lbl.minimumWidth()    # setFixedWidth → min==max
            for cap in (tr("meeting_level_mic"), tr("meeting_level_sys"),
                        tr("meeting_microphone_number", number="88")):
                self.assertGreaterEqual(
                    col - fm.horizontalAdvance(cap), 6,
                    f"[{lang}] підпис рівня ріже {cap!r}: колонка {col}px")
        finally:
            row.deleteLater()
            self.app.processEvents()

    def test_level_caption_fits_ukrainian(self):
        self._assert_level_fits("uk")

    def test_level_caption_fits_english(self):
        self._assert_level_fits("en")


class ModelCardHeightTests(unittest.TestCase):
    """Картки вибору моделі ШІ (Нарада): підписи мають МІНІМУМ за fontMetrics.

    Регрес візуального гейта (12 порушень text_clipped_v, uk+en): стоковий
    QLabel(wordWrap) віддає мінімумом один рядок, і в тісній колонці підпис
    ріжеться знизу. WrapLabel рахує мінімум тією самою формулою, що ними QLabel
    малює текст, тож перевірка стійка до підміни шрифту offscreen.
    """

    def setUp(self):
        self.app = _app()
        self._lang = i18n.current_language()

    def tearDown(self):
        i18n.set_language(self._lang)

    def _assert_card_labels_fit(self, lang):
        import tempfile
        from pathlib import Path
        from PySide6.QtCore import QRect, Qt
        from PySide6.QtWidgets import QVBoxLayout
        from fronts.desktop.pages.meeting import MeetingPage, WrapLabel
        from whisper_core.protocol import model_manager as mm
        i18n.set_language(lang)
        with tempfile.TemporaryDirectory() as tmp:
            resolved = mm.resolve("fast", Path(tmp), [])
            # unbound-виклик на легкому self (канон test_meeting_ui): картка
            # чіпає self лише в лямбдах кнопок, які тест не натискає.
            card = MeetingPage._build_model_card(
                SimpleNamespace(), resolved, None, "quality")
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(card)
        # ВУЗЬКА картка: назва пресета не влазить в один рядок — саме той випадок,
        # коли «мінімум = один рядок» ріже текст.
        host.setFixedWidth(240)
        host.show()
        try:
            self.app.processEvents()
            labels = [w for w in card.findChildren(WrapLabel) if w.text()]
            self.assertGreaterEqual(len(labels), 3,
                                    "підписів картки не знайдено — тест виродився")
            multiline = 0
            for lbl in labels:
                avail = lbl.contentsRect().width()
                self.assertGreater(avail, 0, f"[{lang}] нульова ширина підпису")
                fm = QFontMetrics(lbl.font())
                flags = int(Qt.TextWordWrap) | int(lbl.alignment())
                need = fm.boundingRect(
                    QRect(0, 0, avail, 1 << 20), flags, lbl.text()).height()
                if need > fm.height() + 1:
                    multiline += 1
                self.assertGreaterEqual(
                    lbl.minimumSizeHint().height(), need,
                    f"[{lang}] мінімум підпису нижчий за текст: "
                    f"{lbl.text()!r} — треба {need}px")
            self.assertTrue(
                multiline,
                f"[{lang}] жоден підпис не перенісся на 240px — тест виродився")
        finally:
            host.hide()
            host.deleteLater()
            self.app.processEvents()

    def test_model_card_labels_fit_ukrainian(self):
        self._assert_card_labels_fit("uk")

    def test_model_card_labels_fit_english(self):
        self._assert_card_labels_fit("en")


if __name__ == "__main__":
    unittest.main()
