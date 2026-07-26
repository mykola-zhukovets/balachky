"""Smoke-рендер статусу завантаження компонентів постобробки — ловить
ГОРИЗОНТАЛЬНЕ й ВЕРТИКАЛЬНЕ обрізання довгого повідомлення про помилку
(_autocorrect_status / _punctuator_status на вкладці «Запис» налаштувань).

Чому окремо: visual_gate сканує лише ДЕФОЛТНИЙ стан сторінки й ніколи не бачить
пост-фейл setText() з довгим рядком винятку. Тут ми відтворюємо саме той стан:
за реального (мінімального) розміру вікна кладемо в статус довгий мережевий
виняток через РЕАЛЬНИЙ шлях коду (SettingsPage._set_component_error) і
перевіряємо, що текст переноситься й показується ПОВНІСТЮ — без обрізання.

Клас багів: QLabel[wordWrap] переносить лише по пробілах, тож довгий неперервний
токен (URL/клас винятку) ширший за колонку ріжеться по горизонталі, а рядок
QGridLayout не завжди сам перераховує висоту після setText.

    python -m unittest tests.render_component_status_smoke
    python tests/render_component_status_smoke.py
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

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics

from whisper_core import profiles
from tests.render_nav_smoke import _NavController, _make_sandbox

# Довгий синтетичний рядок помилки: типовий мережевий виняток із неперервним
# URL-токеном (55+ символів без пробілу) — саме він раніше різався по горизонталі.
_LONG_ERR = ("HTTPSConnectionPool(host='huggingface.co', port=443): Max retries "
             "exceeded with url: /api/models/"
             "sberbank-ai-ruRoberta-large-punctuation-model/resolve/main/"
             "pytorch_model.bin (Caused by NewConnectionError: Failed to "
             "establish a new connection: [Errno 11001] getaddrinfo failed)")
_ZWSP = "​"


def _process(app, n=10):
    for _ in range(n):
        app.processEvents()


def _max_run_px(fm: QFontMetrics, text: str) -> int:
    """Ширина найширшого неперервного прогону (розрив = пробіл АБО м'який перенос)."""
    runs = text.replace(_ZWSP, " ").split()
    return max((fm.horizontalAdvance(r) for r in runs), default=0)


class ComponentStatusSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion, i18n
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))
        i18n.set_language("uk")
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
        cls._flush(cls._app)

    @staticmethod
    def _flush(app):
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
        self._flush(self._app)

    def _settings_recording_tab(self):
        """Збудувати вікно, відкрити вкладку «Запис» налаштувань за МІНІМАЛЬНОГО
        розміру вікна (найвужча реальна колонка → найгірший випадок переносу)."""
        from fronts.desktop.main_window import MainWindow
        from fronts.desktop.pages.settings import SettingsPage
        win = MainWindow(_NavController(self._sandbox))
        self._win = win
        win.setAttribute(Qt.WA_DontShowOnScreen, True)
        win.resize(win.minimumWidth(), win.minimumHeight())  # найвужча колонка
        win.show()
        idx = next(i for i in range(win.pages.count())
                   if isinstance(win.pages.widget(i), SettingsPage))
        page = win.pages.widget(idx)
        win.set_page(idx)
        _process(self._app, 5)
        from fronts.desktop.i18n import tr
        rec_title = tr("set_tab_recording")
        rec_idx = next(i for i in range(page._tabs.count()) if page._tabs.tabText(i) == rec_title)
        page._tabs.setCurrentIndex(rec_idx)   # вкладка «Запис і звук» містить статуси компонентів
        _process(self._app, 8)
        return page

    def test_long_download_error_wraps_without_clipping(self):
        page = self._settings_recording_tab()
        for name in ("_autocorrect_status", "_punctuator_status"):
            with self.subTest(label=name):
                lbl = getattr(page, name)
                fm = QFontMetrics(lbl.font())

                # (передумова) сирий текст БИ обрізався: неперервний токен ширший
                # за колонку — інакше тест нічого не ловить (лейбл завеликий).
                self.assertGreater(
                    _max_run_px(fm, _LONG_ERR), lbl.width(),
                    f"{name}: лейбл завеликий — стан обрізання не відтворюється, "
                    "тест втратив сенс (звузь вікно)")

                # РЕАЛЬНИЙ шлях коду, яким failed-колбек кладе помилку в статус
                page._set_component_error(lbl, _LONG_ERR)
                _process(self._app, 12)

                w = max(1, lbl.width())
                shown = lbl.text()
                need = fm.boundingRect(0, 0, w, 99999,
                                       Qt.TextWordWrap, shown).height()

                # переносу СПРАВДІ багато рядків (не однорядковий випадок)
                self.assertGreaterEqual(
                    lbl.height(), 2 * fm.lineSpacing(),
                    f"{name}: очікувався багаторядковий перенос")
                # ВЕРТИКАЛЬ: висота рядка виросла під перенесений текст
                self.assertGreaterEqual(
                    lbl.height(), need - 4,
                    f"{name}: висота {lbl.height()} < потрібних {need} — "
                    "рядок сітки не перерахувався (вертикальне обрізання)")
                # ГОРИЗОНТАЛЬ: жоден неперервний прогін не ширший за лейбл
                self.assertLessEqual(
                    _max_run_px(fm, shown), w + 2,
                    f"{name}: неперервний токен ширший за лейбл "
                    "(горизонтальне обрізання)")
                # повний оригінал доступний у tooltip (без службових символів)
                self.assertIn("getaddrinfo", lbl.toolTip())
                self.assertNotIn(_ZWSP, lbl.toolTip())

    def test_soft_break_long_contract(self):
        """Чистий контракт хелпера: довгі токени отримують точки розриву (кожен
        прогін між пробілом/м'яким переносом ≤ chunk), короткі — недоторкані."""
        from fronts.desktop.pages.settings import _soft_break_long
        chunk = 12
        out = _soft_break_long(_LONG_ERR, chunk=chunk)
        # текст без службового символу не змінився
        self.assertEqual(out.replace(_ZWSP, ""), _LONG_ERR)
        # жоден неперервний прогін (по символах) не довший за chunk
        longest = max((len(r) for r in out.replace(_ZWSP, " ").split()), default=0)
        self.assertLessEqual(longest, chunk)
        # короткі статуси не чіпаємо (жодного м'якого переносу)
        self.assertNotIn(_ZWSP, _soft_break_long("Спочатку завантажте компонент"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
