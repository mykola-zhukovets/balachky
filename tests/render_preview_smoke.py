"""Offscreen-рендер картки перегляду (feature/paste-preview) — ОКРЕМИЙ процес.

Поза `unittest discover -s tests` (патерн `test*.py`), як render_meeting_smoke.py:
картка — живий top-level QWidget із QTimer автосховання (30с), тож тримаємо його
подалі від основного набору, щоб недобитий таймер не давав флакі-краху на виході.

    python -m unittest tests.render_preview_smoke
    python -m unittest discover -s tests -p "render_*.py"
    python tests/render_preview_smoke.py

Скріншот — у C:\\Users\\nikol\\Desktop\\balachky-diag\\paste-preview\\.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Тека діагностичних знімків рендера. Шлях НЕ зашиваємо: домашня тека
# розробника не має місця в публічному коді. Перевизначається змінною
# BALACHKY_DIAG_DIR, інакше — тимчасова тека системи.
_DIAG = (Path(os.environ.get("BALACHKY_DIAG_DIR", tempfile.gettempdir()))
         / "balachky-diag" / "paste-preview")


class PreviewRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        _DIAG.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls._flush_deferred(cls._app)

    @staticmethod
    def _flush_deferred(app):
        from PySide6.QtCore import QCoreApplication, QEvent
        for _ in range(3):
            app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()

    def setUp(self):
        self._live = []

    def tearDown(self):
        from PySide6.QtCore import QTimer
        for card in self._live:
            for t in card.findChildren(QTimer):
                try:
                    t.stop()
                except RuntimeError:
                    pass
            try:
                card._timer.stop()
            except (RuntimeError, AttributeError):
                pass
            try:
                card.close()
            except Exception:
                pass
            card.deleteLater()
        self._live = []
        self._flush_deferred(self._app)

    def _pump(self, n=4):
        for _ in range(n):
            self._app.processEvents()

    def _card(self, text):
        from fronts.desktop.preview import PreviewCard
        card = PreviewCard(text)
        self._live.append(card)
        card.resize(380, 200)
        card.show()
        self._pump()
        return card

    def test_render_card(self):
        card = self._card("Доброго дня, дякую, що зібралися сьогодні.")
        self._pump()
        pix = card.grab()
        self.assertFalse(pix.isNull())
        pix.save(str(_DIAG / "01-preview-card.png"))

    def test_accept_emits_edited_text(self):
        card = self._card("привіт")
        got = []
        card.accepted.connect(got.append)
        card._edit.setPlainText("привіт, світе")
        card._on_accept()
        self.assertEqual(got, ["привіт, світе"])

    def test_empty_accept_cancels(self):
        card = self._card("текст")
        events = []
        card.accepted.connect(lambda t: events.append(("accept", t)))
        card.cancelled.connect(lambda: events.append(("cancel",)))
        card._edit.setPlainText("   ")
        card._on_accept()
        self.assertEqual(events, [("cancel",)])

    def test_single_result_only(self):
        card = self._card("текст")
        events = []
        card.accepted.connect(lambda t: events.append("accept"))
        card.cancelled.connect(lambda: events.append("cancel"))
        card._on_accept()
        card._on_cancel()          # уже завершено — має бути no-op
        self.assertEqual(events, ["accept"])

    def test_copy_emits(self):
        card = self._card("копію")
        got = []
        card.copied.connect(got.append)
        card._on_copy()
        self.assertEqual(got, ["копію"])


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(PreviewRenderTests))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
