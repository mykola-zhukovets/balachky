"""Offscreen-рендер плаваючої нотатки (feature/scratchpad-note) — ОКРЕМИЙ процес.

Як render_meeting_smoke/render_splash_smoke: єдиний тест, що реально show()/grab()
живий QWidget із RecButton (у якого QVariantAnimation-пульс під час запису). У
спільному процесі з рештою тестів offscreen-Qt дає флакі-краш на виході, тож файл
НЕ підхоплюється `unittest discover -p test*.py`.

Прогін:
    python -m unittest tests.render_note_smoke
    python -m unittest discover -s tests -p "render_*.py"
    python tests/render_note_smoke.py

Скріншоти станів (idle/recording/busy/із текстом) — у
C:\\Users\\nikol\\Desktop\\balachky-diag\\note\\.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Тека діагностичних знімків рендера. Шлях НЕ зашиваємо: домашня тека
# розробника не має місця в публічному коді. Перевизначається змінною
# BALACHKY_DIAG_DIR, інакше — тимчасова тека системи.
_DIAG = (Path(os.environ.get("BALACHKY_DIAG_DIR", tempfile.gettempdir()))
         / "balachky-diag" / "note")


class _RenderController:
    """Легкий контролер із note_state-сигналом і буфером для рендеру вікна."""

    def __init__(self, text=""):
        from PySide6.QtCore import QObject, Signal

        class _Ctl(QObject):
            note_state = Signal(str)
            note_key_captured = Signal(str)

            def __init__(self, text):
                super().__init__()
                self._buf = text
                self._state = "idle"
                self.toggles = []
                self.tray = type("T", (), {"notify": lambda self, m: None})()

            def note_text(self):
                return self._buf

            def note_set_buffer(self, t):
                self._buf = t

            def note_clear(self):
                self._buf = ""

            def note_state_value(self):
                return self._state

            def note_record_toggle(self):
                self.toggles.append(1)

        self._ctl = _Ctl(text)

    def __getattr__(self, name):
        return getattr(self._ctl, name)


class NoteRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        from types import SimpleNamespace
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))  # без пульсу — детермінізм
        _DIAG.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls._flush()

    @classmethod
    def _flush(cls):
        from PySide6.QtCore import QCoreApplication, QEvent
        for _ in range(3):
            cls._app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        cls._app.processEvents()

    def setUp(self):
        self._live = []

    def tearDown(self):
        for w in self._live:
            try:
                w.close()
            except Exception:
                pass
            w.deleteLater()
        self._live = []
        self._flush()

    def _window(self, text=""):
        from fronts.desktop.note import NoteWindow
        win = NoteWindow(_RenderController(text))
        self._live.append(win)
        return win

    def _save(self, win, name):
        pix = win.grab()
        self.assertFalse(pix.isNull())
        pix.save(str(_DIAG / name))

    def test_render_empty(self):
        win = self._window("")
        self._save(win, "01-empty.png")

    def test_render_with_text(self):
        win = self._window("Перша репліка нотатки.\nДруга репліка, довша, "
                           "щоб перевірити перенос рядків у полі нотатки.")
        self._save(win, "02-with-text.png")

    def test_render_recording_state(self):
        win = self._window("Запис триває…")
        win._on_state("recording")
        self._save(win, "03-recording.png")
        self.assertTrue(win._rec_btn.isEnabled())

    def test_render_busy_state(self):
        win = self._window("Розшифровую…")
        win._on_state("busy")
        self._save(win, "04-busy.png")
        self.assertFalse(win._rec_btn.isEnabled())   # кнопку заглушено на час розшифровки

    def test_append_reflects_in_editor(self):
        """set_text із буфера справді потрапляє у редактор (шлях append)."""
        win = self._window("рядок один")
        win.set_text("рядок один\nрядок два")
        self.assertIn("рядок два", win._editor.toPlainText())

    def test_mic_click_calls_toggle(self):
        win = self._window("")
        win._rec_btn.click()
        self.assertEqual(win.controller.toggles, [1])


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(NoteRenderTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
