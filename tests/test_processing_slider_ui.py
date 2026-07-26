"""feature/processing-slider — віджет повзунка: рендер, взаємодія, доступність (§4, §9).

Offscreen ДОПУСТИМИЙ: перевіряємо структуру/сигнали/a11y, не піксельний вигляд.
"""
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from fronts.desktop import motion                    # noqa: E402
from fronts.desktop.i18n import tr                   # noqa: E402
from fronts.desktop.processing_slider import ProcessingSlider  # noqa: E402
from whisper_core import processing                  # noqa: E402


class _Rec:
    def __init__(self):
        self.modes = []

    def __call__(self, mode):
        self.modes.append(mode)


class ProcessingSliderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        motion.init_config(SimpleNamespace(animations=False))   # reduce-motion

    def _slider(self, surface=processing.DICTATION, caption=""):
        w = ProcessingSlider(surface, caption=caption)
        return w

    def test_three_stops_with_exact_labels(self):
        # Диктування: третя позиція — «З пунктуацією» (не «Під документ»): у конвеєрі
        # ще нема генеративного переписування, тож назва чесна (спека §3, блокер суду).
        w = self._slider()
        texts = [lbl.text() for lbl in w.findChildren(QLabel)]
        for key in ("proc_mode_verbatim", "proc_mode_fillers", "proc_mode_punct"):
            self.assertIn(tr(key), texts)
        self.assertNotIn(tr("proc_mode_document"), texts)

    def test_meeting_third_stop_stays_document(self):
        # Нарада: третя позиція лишається «Під документ» — там вона справді генерує
        # протокол (та сама коробка, різна назва за поверхнею, спека §3, §4).
        w = self._slider(processing.MEETING)
        texts = [lbl.text() for lbl in w.findChildren(QLabel)]
        self.assertIn(tr("proc_mode_document"), texts)
        self.assertNotIn(tr("proc_mode_punct"), texts)

    def test_accessible_name_per_surface(self):
        wd = self._slider(processing.DICTATION)
        wm = self._slider(processing.MEETING)
        self.assertEqual(wd._slider.accessibleName(), tr("proc_slider_dict_name"))
        self.assertEqual(wm._slider.accessibleName(), tr("proc_slider_meeting_name"))

    def test_accessible_description_live(self):
        w = self._slider()
        w.setMode("verbatim")
        self.assertIn("1", w._slider.accessibleDescription())
        w.setMode("document")   # диктування показує третю позицію як «З пунктуацією»
        self.assertIn(tr("proc_mode_punct"), w._slider.accessibleDescription())

    def test_default_is_verbatim(self):
        self.assertEqual(self._slider().mode(), "verbatim")

    def test_setmode_no_emit_by_default(self):
        w = self._slider()
        rec = _Rec()
        w.modeChanged.connect(rec)
        w.setMode("fillers")                 # emit=False за замовчуванням
        self.assertEqual(rec.modes, [])
        self.assertEqual(w.mode(), "fillers")

    def test_setmode_emit_true(self):
        w = self._slider()
        rec = _Rec()
        w.modeChanged.connect(rec)
        w.setMode("document", emit=True)
        self.assertEqual(rec.modes, ["document"])

    def test_slider_value_commits_exactly_once(self):
        w = self._slider()
        rec = _Rec()
        w.modeChanged.connect(rec)
        w._slider.setValue(2)                # як кінець/стрілка з клавіатури
        self.assertEqual(rec.modes, ["document"])
        self.assertEqual(w.mode(), "document")

    def test_label_click_commits(self):
        w = self._slider()
        rec = _Rec()
        w.modeChanged.connect(rec)
        w._labels[1].clicked.emit(1)         # клік по мітці позиції
        self.assertEqual(rec.modes, ["fillers"])

    def test_slider_range_and_steps(self):
        w = self._slider()
        self.assertEqual(w._slider.minimum(), 0)
        self.assertEqual(w._slider.maximum(), 2)
        self.assertEqual(w._slider.singleStep(), 1)

    def test_document_unavailable_reverts_without_commit(self):
        w = self._slider()
        w.setMode("fillers")                 # поточна позиція — 1
        w.setDocumentAvailable(False, "нема компонента")
        rec = _Rec()
        w.modeChanged.connect(rec)
        unavail = _Rec()
        w.documentUnavailable.connect(unavail)
        w._slider.setValue(2)                # спроба піти в «Під документ»
        self.assertEqual(w.mode(), "fillers")   # лишилась попередня
        self.assertEqual(rec.modes, [])         # modeChanged НЕ емітився
        self.assertEqual(unavail.modes, ["нема компонента"])

    def test_document_unavailable_while_selected_reverts(self):
        w = self._slider()
        w.setMode("document")
        rec = _Rec()
        w.modeChanged.connect(rec)
        w.setDocumentAvailable(False)
        self.assertEqual(w.mode(), "fillers")   # відкат на попередню безпечну
        self.assertEqual(rec.modes, ["fillers"])

    def test_meeting_caption_present(self):
        w = self._slider(processing.MEETING, caption=tr("proc_meeting_caption"))
        texts = [lbl.text() for lbl in w.findChildren(QLabel)]
        self.assertIn(tr("proc_meeting_caption"), texts)

    def test_sync_animations_is_safe_noop(self):
        w = self._slider()
        self.assertIsNone(w.sync_animations())


if __name__ == "__main__":
    unittest.main()
