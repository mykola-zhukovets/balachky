"""Т47/Т48 — чіп+попап рівня обробки і кадрів/сек: структура, сигнали, a11y.

Offscreen ДОПУСТИМИЙ: перевіряємо контракт/сигнали/доступність, не піксельний
вигляд. Живий вигляд чіпа/попапа — окремим скрін-скриптом (день і ніч).
Виняток — перстень фокуса на ручці: там саме піксель є доказом видимості,
тож рендеримо ручку в QImage і рахуємо контраст (див. ``FocusRingTests``).
"""
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                          # noqa: E402
from PySide6.QtGui import QColor, QImage               # noqa: E402
from PySide6.QtWidgets import (                        # noqa: E402
    QApplication, QSlider, QStyle, QStyleOptionSlider,
)

from fronts.desktop import motion, theme               # noqa: E402
from fronts.desktop.i18n import (                     # noqa: E402
    STRINGS, current_language, set_language, tr,
)
from fronts.desktop.chip_popover import (              # noqa: E402
    Popover, ValueSliderChip, _focus_ring_color, make_slider,
)
from fronts.desktop.processing_slider import ProcessingChip, ProcessingSlider  # noqa: E402
from whisper_core import processing                   # noqa: E402


def _luminance(color: QColor) -> float:
    """Відносна яскравість (WCAG 2.x)."""
    def _ch(v: float) -> float:
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return (0.2126 * _ch(color.red()) + 0.7152 * _ch(color.green())
            + 0.0722 * _ch(color.blue()))


def _contrast(a, b) -> float:
    """Коефіцієнт контрасту WCAG між двома кольорами (1.0 — однакові)."""
    la, lb = _luminance(QColor(a)), _luminance(QColor(b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


class _Rec:
    def __init__(self):
        self.values = []

    def __call__(self, v):
        self.values.append(v)


class ProcessingChipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        motion.init_config(SimpleNamespace(animations=False))
        cls._language = current_language()
        cls.addClassCleanup(set_language, cls._language)
        set_language("uk")

    def test_chip_label_shows_current_level(self):
        c = ProcessingChip(processing.DICTATION)
        self.assertEqual(c._chip.text(), "Обробка: Дослівно")
        c.setMode("fillers")
        self.assertEqual(c._chip.text(), "Обробка: Без слів-паразитів")

    def test_chip_has_accessible_name(self):
        c = ProcessingChip(processing.DICTATION)
        self.assertEqual(c._chip.accessibleName(), "Рівень обробки тексту")

    def test_mode_api_forwarded(self):
        c = ProcessingChip(processing.DICTATION)
        self.assertEqual(c.mode(), "verbatim")
        c.setMode("fillers")
        self.assertEqual(c.mode(), "fillers")

    def test_modechanged_reemitted_and_label_updates(self):
        c = ProcessingChip(processing.DICTATION)
        rec = _Rec()
        c.modeChanged.connect(rec)
        c._slider._labels[1].clicked.emit(1)     # вибір у попапі-слайдері
        self.assertEqual(rec.values, ["fillers"])
        self.assertIn("Без слів-паразитів", c._chip.text())

    def test_document_unavailable_forwarded(self):
        c = ProcessingChip(processing.DICTATION)
        c.setMode("document")
        rec = _Rec()
        c.modeChanged.connect(rec)
        c.setDocumentAvailable(False)            # відкат на безпечну позицію
        self.assertEqual(c.mode(), "fillers")

    def test_popover_holds_the_slider(self):
        c = ProcessingChip(processing.DICTATION)
        c._open()
        self.assertIsInstance(c._popover, Popover)
        self.assertIn(c._slider, c._popover.findChildren(ProcessingSlider))

    def test_tooltip_lands_on_chip(self):
        c = ProcessingChip(processing.DICTATION)
        c.setToolTip(tr("proc_slider_dict_hint"))
        self.assertEqual(
            c._chip.toolTip(),
            "Дослівно — вставляється як почули. Без слів-паразитів — прибирає "
            "“емм”, “ну”. З пунктуацією — розставляє розділові знаки й "
            "причісує чернетку.")

    def test_sync_animations_safe(self):
        self.assertIsNone(ProcessingChip(processing.DICTATION).sync_animations())


class ValueSliderChipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        motion.init_config(SimpleNamespace(animations=False))
        cls._language = current_language()
        cls.addClassCleanup(set_language, cls._language)
        set_language("uk")

    def _chip(self, value=30):
        return ValueSliderChip(5, 60, value, label_key="screen_fps_chip",
                               name_key="screen_fps")

    def test_chip_label_shows_value(self):
        c = self._chip(30)
        self.assertEqual(c._chip.text(), "Кадри: 30")

    def test_value_and_range(self):
        c = self._chip(24)
        self.assertEqual(c.value(), 24)
        self.assertEqual(c._slider.minimum(), 5)
        self.assertEqual(c._slider.maximum(), 60)

    def test_slider_fixed_width_not_full(self):
        # Т48: слайдер компактний (фіксована ширина), не тягнеться на всю сторінку.
        c = self._chip()
        self.assertGreater(c._slider.maximumWidth(), 0)
        self.assertLess(c._slider.maximumWidth(), 16777215)   # QWIDGETSIZE_MAX

    def test_value_change_updates_label_and_emits(self):
        c = self._chip(30)
        rec = _Rec()
        c.valueChanged.connect(rec)
        c._slider.setValue(45)
        self.assertEqual(rec.values, [45])
        self.assertEqual(c.value(), 45)
        self.assertIn("45", c._chip.text())

    def test_chip_and_slider_have_accessible_name(self):
        c = self._chip()
        self.assertEqual(c._chip.accessibleName(), "Кадрів на секунду")
        self.assertEqual(c._slider.accessibleName(), "Кадрів на секунду")

    def test_popover_holds_slider(self):
        c = self._chip()
        c._open()
        self.assertIsInstance(c._popover, Popover)
        self.assertIn(c._slider, c._popover.findChildren(QSlider))


class FocusRingTests(unittest.TestCase):
    """Клавіатурний фокус на ручці слайдера в попапі мусить бути ВИДИМИМ.

    Регрес, який ці тести ловлять назавжди: перстень малювався кольором
    ``theme.FOCUS``, а в денній палітрі FOCUS == GOLD == #F39200 — тобто
    рівно колір заливки ручки. Перстень зливався із ручкою і клавіатурний
    користувач не бачив, який контрол активний.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        motion.init_config(SimpleNamespace(animations=False))

    def _render(self, *, focused: bool):
        """Слайдер попапа у QImage + прямокутник ручки. Повертає (img, rect)."""
        s = make_slider(stops=0)
        s.setRange(5, 60)
        s.setValue(30)
        s.setFocusPolicy(Qt.StrongFocus if focused else Qt.NoFocus)
        s.resize(220, 36)
        s.show()
        if focused:
            s.activateWindow()
            s.setFocus()
        self.app.processEvents()
        self.assertEqual(s.hasFocus(), focused)
        img = QImage(s.size(), QImage.Format_ARGB32)
        img.fill(0)
        s.render(img)
        opt = QStyleOptionSlider()
        s.initStyleOption(opt)
        rect = s.style().subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, s)
        return img, rect

    @staticmethod
    def _contrasty_pixels(img, rect, fill) -> int:
        """Скільки непрозорих пікселів у зоні ручки контрастують із заливкою
        (>= 3:1 — поріг WCAG 1.4.11 для нетекстових індикаторів)."""
        n = 0
        for y in range(rect.top(), rect.bottom() + 1):
            for x in range(rect.left(), rect.right() + 1):
                px = img.pixelColor(x, y)
                if px.alpha() == 255 and _contrast(px, fill) >= 3.0:
                    n += 1
        return n

    def test_ring_color_contrasts_with_handle_fill_in_both_palettes(self):
        # І день, і (схований поки) нічний режим: перстень мусить бути
        # відрізнити від золотої/червоної заливки ручки.
        for name, palette in (("day", theme._DAY), ("night", theme._NIGHT)):
            with self.subTest(palette=name):
                ring = _focus_ring_color(palette)
                self.assertGreaterEqual(
                    _contrast(ring, palette["GOLD"]), 3.0,
                    f"перстень {ring} зливається із ручкою {palette['GOLD']}")

    def test_focused_handle_shows_contrasting_ring(self):
        img, rect = self._render(focused=True)
        self.assertGreater(
            self._contrasty_pixels(img, rect, theme.GOLD), 20,
            "у зоні ручки немає видимого персня фокуса")

    def test_unfocused_handle_has_no_ring(self):
        img, rect = self._render(focused=False)
        self.assertLessEqual(
            self._contrasty_pixels(img, rect, theme.GOLD), 4,
            "без фокуса ручка мусить лишатись рівно залитою")


class NewStringsParityTests(unittest.TestCase):
    def test_new_keys_present_both_languages(self):
        for key in ("proc_chip_label", "screen_fps_chip", "hint_paste_here"):
            self.assertIn(key, STRINGS["uk"], key)
            self.assertIn(key, STRINGS["en"], key)


if __name__ == "__main__":
    unittest.main()
