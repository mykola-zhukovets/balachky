"""Smoke-рендер підсвітки непевних слів — ОКРЕМИЙ процес (як render_nav_smoke).

feature/model-bottlenecks (під-хвиля 2). Дві склейки, які НІЩО не тестувало до
цього (рецензія довела мутацією _render_html(final, []) — вона пережила 1839 тестів,
рівно вихідний клас бага хвилі: word-дані є, а до QLabel не долітають):

  * DictationPage.add_entry(raw, final, words) → text.setText(render HTML) —
    стрічка диктування (PTT). Мутація words→[] у add_entry → без підсвітки → тест
    червоніє.
  * FilesPage._on_done(..., words) → body.setText(render HTML) — картка файлу.
    Та сама золота позначка, що й у стрічці (спростовує «нема word-UI у файлах»).

Живе MainWindow → окремий процес (конвенція render_*_smoke): у спільному
unittest-наборі недобитий Qt-таймер під час static-деструкції offscreen-Qt дає
флакі-краш. Teardown жорсткий: спиняємо QTimer/QAbstractAnimation, close →
deleteLater → флаш (як у render_nav_smoke / render_dictation_feed_smoke).

    python -m unittest tests.render_uncertainty_smoke   # звичайний прогін
    python tests/render_uncertainty_smoke.py            # standalone-раннер
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtWidgets import QLabel

from whisper_core import profiles
from tests.render_nav_smoke import _NavController, _make_sandbox

# «світ» непевне (0.3<0.5) → золотий span; «привіт» певне (0.9) → без span.
_FINAL = "привіт світ"
_WORDS = [("привіт", 0.9), ("світ", 0.3)]


class UncertaintyHighlightSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))  # без живих таймерів
        cls._sandbox = _make_sandbox()
        cls._orig_list = profiles.list_profiles
        profiles.list_profiles = lambda root=None: cls._orig_list(cls._sandbox)

    @classmethod
    def tearDownClass(cls):
        profiles.list_profiles = cls._orig_list
        try:
            from fronts.desktop import glass
            glass._TAG_DRIVER._timer.stop()
            glass._TAG_DRIVER._pills.clear()
        except Exception:
            pass
        cls._flush_deferred(cls._app)

    @staticmethod
    def _flush_deferred(app):
        from PySide6.QtCore import QCoreApplication, QEvent
        for _ in range(3):
            app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()

    def setUp(self):
        self._win = None

    def tearDown(self):
        from PySide6.QtCore import QTimer, QAbstractAnimation
        win = self._win
        if win is not None:
            for a in win.findChildren(QAbstractAnimation):
                try:
                    a.stop()
                except RuntimeError:
                    pass
            for t in win.findChildren(QTimer):
                try:
                    t.stop()
                except RuntimeError:
                    pass
            try:
                win.close()
            except Exception:
                pass
            win.deleteLater()
        self._win = None
        self._flush_deferred(self._app)

    def _window(self):
        from fronts.desktop.main_window import MainWindow
        win = MainWindow(_NavController(self._sandbox))
        self._win = win
        return win

    @staticmethod
    def _card_label(page, final):
        """QLabel-картка з розшифровкою final (несе ._final_text)."""
        return [l for l in page.findChildren(QLabel)
                if getattr(l, "_final_text", None) == final]

    def test_dictation_add_entry_highlights_uncertain_word(self):
        """Склейка add_entry → _render_html: непевне слово підсвічене в стрічці.
        Мутація words→[] в add_entry прибрала б span → тест червоніє."""
        win = self._window()
        page = win.dictation
        page.add_entry("raw", _FINAL, _WORDS)
        self._app.processEvents()

        labels = self._card_label(page, _FINAL)
        self.assertTrue(labels, "картка диктування не знайдена у стрічці")
        htm = labels[0].text()
        self.assertIn("<span", htm, "непевне слово не підсвічене (words не долетіли)")
        # саме «світ» (0.3) обгорнуте, «привіт» (0.9) — ні
        self.assertEqual(htm.count("<span"), 1)
        self.assertIn("світ", htm)

    def test_file_card_highlights_uncertain_word(self):
        """Склейка file_done → _on_done → render: картка файлу підсвічує непевне
        слово тим самим кодом, що й стрічка (спростовує «нема word-UI у файлах»)."""
        from fronts.desktop.main_window import FileStatus
        win = self._window()
        files = win.files
        # add_files кличе controller.enqueue_file(p, model=): _NavController-заглушка
        # приймає лише path — підміняємо на варіант із model, щоб побудувати рядок.
        win.controller.enqueue_file = lambda p, model=None: 7
        files.add_files([Path("зразок.wav")])
        files._on_done(7, _FINAL, f"{FileStatus.DONE}:1", [], _WORDS)
        self._app.processEvents()

        labels = self._card_label(files, _FINAL)
        self.assertTrue(labels, "картка файлу не знайдена")
        htm = labels[0].text()
        self.assertIn("<span", htm, "непевне слово у картці файлу не підсвічене")
        self.assertEqual(htm.count("<span"), 1)
        self.assertIn("світ", htm)


if __name__ == "__main__":
    import unittest as _ut
    result = _ut.TextTestRunner(verbosity=2).run(
        _ut.TestLoader().loadTestsFromTestCase(UncertaintyHighlightSmoke))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
