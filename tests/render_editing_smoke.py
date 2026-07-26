"""Offscreen-рендер панелі редагування розшифровки (feature/transcript-editing).

Окремо від `unittest discover` (патерн test*.py) — як render_meeting_smoke:
живі QWidget з show()/grab() тримаємо поза основним набором, щоб той лишався
детермінований. Панель без QTimer, тож teardown простий (close → deleteLater →
флаш DeferredDelete, поки QApplication живий).

    python -m unittest tests.render_editing_smoke
    python -m unittest discover -s tests -p "render_*.py"
    python tests/render_editing_smoke.py
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
         / "balachky-diag" / "editing")


class EditPanelRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))
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
        for w in self._live:
            try:
                w.close()
            except Exception:
                pass
            w.deleteLater()
        self._live = []
        self._flush_deferred(self._app)

    def _pump(self, n=4):
        for _ in range(n):
            self._app.processEvents()

    def _panel(self, text):
        """Хост: QLabel body + TranscriptEditPanel над спільним сховищем-списком."""
        from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
        from fronts.desktop.pages.edit_search import TranscriptEditPanel
        store = {"text": text}
        host = QWidget()
        lay = QVBoxLayout(host)
        body = QLabel(store["text"])
        body.setWordWrap(True)
        lay.addWidget(body)

        def _apply(new):
            store["text"] = new
            body.setText(new)

        panel = TranscriptEditPanel(body, lambda: store["text"], _apply)
        lay.addWidget(panel.edit_button)
        lay.addWidget(panel)
        host._panel = panel
        host._body = body
        host._store = store
        self._live.append(host)
        host.resize(720, 420)
        host.show()
        self._pump()
        return host

    def test_view_then_edit_mode(self):
        host = self._panel("Доброго дня, дякую, що зібралися сьогодні.")
        panel = host._panel
        self.assertTrue(host._body.isVisible())
        self.assertFalse(panel._editor.isVisible())
        panel.begin_edit()
        self._pump()
        self.assertTrue(panel._editor.isVisible())
        self.assertFalse(host._body.isVisible())
        pix = host.grab()
        self.assertFalse(pix.isNull())
        pix.save(str(_DIAG / "01-edit-mode.png"))

    def test_save_persists_new_text(self):
        host = self._panel("старий текст")
        panel = host._panel
        panel.begin_edit()
        panel._editor.setPlainText("новий текст")
        panel._save()
        self._pump()
        self.assertEqual(host._store["text"], "новий текст")
        self.assertEqual(host._body.text(), "новий текст")
        self.assertFalse(panel._editor.isVisible())

    def test_cancel_discards(self):
        host = self._panel("оригінал")
        panel = host._panel
        panel.begin_edit()
        panel._editor.setPlainText("зіпсовано")
        panel._cancel()
        self._pump()
        self.assertEqual(host._store["text"], "оригінал")
        self.assertEqual(host._body.text(), "оригінал")

    def test_search_counts_and_highlights(self):
        host = self._panel("ба ба ба ще ба")
        panel = host._panel
        panel.begin_edit()
        panel._toggle_search()               # Ctrl+F еквівалент
        self.assertTrue(panel._search_row.isVisible())
        panel._search.setText("ба")
        self._pump()
        self.assertEqual(len(panel._matches), 4)
        self.assertEqual(panel._count.text(), "1/4")
        self.assertEqual(len(panel._editor.extraSelections()), 4)
        panel._step(True)                    # наступний збіг
        self.assertEqual(panel._count.text(), "2/4")
        pix = host.grab()
        self.assertFalse(pix.isNull())
        pix.save(str(_DIAG / "02-search.png"))

    def test_search_toggle_off_clears(self):
        host = self._panel("пошук збіг збіг")
        panel = host._panel
        panel.begin_edit()
        panel._toggle_search()
        panel._search.setText("збіг")
        self._pump()
        self.assertEqual(len(panel._editor.extraSelections()), 2)
        panel._toggle_search()               # закрити пошук
        self.assertFalse(panel._search_row.isVisible())
        self.assertEqual(len(panel._editor.extraSelections()), 0)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(EditPanelRenderTests))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
