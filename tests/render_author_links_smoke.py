"""Smoke-рендер значків автора (GitHub + «Підтримати») — ОКРЕМИЙ процес
(конвенція render_*_smoke; offscreen ДОПУСТИМИЙ: перевіряємо СТРУКТУРУ, не вигляд).

Фідбек Миколи 22.07 + 30.07: підпис автора доповнено значками-посиланнями і в
майстрі першого запуску (крок «Вітання»), і в Налаштуваннях. 30.07 картку
«Про автора» перенесено з вкладки «Система» на окрему вкладку «Про програму»
(власник: «навіщо на цій сторінці двічі дублювати про автора»). Тест стереже:
  • обидва значки існують, мають accessibleName і клікабельні (clicked під'єднано);
  • у властивості linkUrl лежать саме URL із єдиного джерела (fronts/desktop/links);
  • секція «Про автора» — картка вкладки «Про програму», решта секцій на місці.

    python -m unittest tests.render_author_links_smoke
    python tests/render_author_links_smoke.py
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtWidgets import QToolButton, QFrame, QLabel

from whisper_core import profiles
from fronts.desktop import links
from fronts.desktop.i18n import tr
from tests.render_nav_smoke import _NavController, _make_sandbox


def _sig_connected(obj, *sigs) -> bool:
    meta = obj.metaObject()
    for sig in sigs:
        idx = meta.indexOfSignal(sig)
        if idx >= 0 and obj.isSignalConnected(meta.method(idx)):
            return True
    return False


class AuthorLinksSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))
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
        from PySide6.QtCore import QTimer
        win = self._win
        if win is not None:
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

    def _assert_link(self, btn, url):
        self.assertIsNotNone(btn, "значка-посилання не знайдено")
        self.assertTrue((btn.accessibleName() or "").strip(),
                        "значок без accessibleName (§18)")
        self.assertTrue((btn.toolTip() or "").strip(),
                        "значок без tooltip-пояснення")
        # Owner decision d6b00eb: external-link tooltips stay arrow-free.
        self.assertNotIn("↗", btn.toolTip())
        self.assertEqual(btn.property("linkUrl"), url,
                         "у значку не той URL, що в єдиному джерелі links.py")
        self.assertTrue(_sig_connected(btn, "clicked(bool)", "clicked()"),
                        "значок-посилання не реагує на клік (мертвий)")

    def test_onboarding_welcome_has_author_links(self):
        """Крок 1 «Вітання» майстра: два клікабельні значки з правильними URL."""
        from fronts.desktop.onboarding import FirstRunWizard
        with patch.object(FirstRunWizard, "_gpu_step_possible",
                          return_value=False):
            wiz = FirstRunWizard()
        self.addCleanup(wiz.deleteLater)
        welcome = wiz._stack.widget(0)          # сторінка «Вітання»

        gh = welcome.findChild(QToolButton, "authorGithubLink")
        support = welcome.findChild(QToolButton, "authorSupportLink")
        self._assert_link(gh, links.GITHUB_URL)
        self._assert_link(support, links.SUPPORT_URL)

    def _window(self):
        from fronts.desktop.main_window import MainWindow
        win = MainWindow(_NavController(self._sandbox))
        self._win = win
        return win

    def test_about_tab_author_section_present(self):
        """Вкладка «Про програму»: секція «Про автора» на місці, з контактом
        GitHub (значок «Підтримати» тепер живе у рядку валютної підтримки —
        реквізити відразу видно, а не лише за одним значком, фідбек власника
        25.07); наявні секції вкладки не загублено."""
        win = self._window()
        tabs = win.settings._tabs
        about_idx = next(i for i in range(tabs.count())
                         if tabs.tabText(i) == tr("set_tab_about"))
        content = tabs.widget(about_idx).widget()        # QScrollArea → вміст
        layout = content.layout()

        author_card = None
        for i in range(layout.count()):
            w = layout.itemAt(i).widget()
            if w is not None and w.objectName() == "authorCard":
                author_card = w
                break
        self.assertIsNotNone(author_card,
                             "секція «Про автора» відсутня на вкладці «Про програму»")

        gh = author_card.findChild(QToolButton, "authorGithubLink")
        self._assert_link(gh, links.GITHUB_URL)

        # підпис автора на місці
        texts = [l.text() for l in author_card.findChildren(QLabel)]
        self.assertIn(tr("set_author"), texts, "нема підпису автора")

        # наявні секції не загублено: hero/whatsnew/author/help/license = 5 карток
        cards = [w for w in content.findChildren(QFrame) if w.property("card")]
        self.assertGreaterEqual(
            len(cards), 5, "на вкладці «Про програму» бракує секцій")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(AuthorLinksSmokeTests))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
