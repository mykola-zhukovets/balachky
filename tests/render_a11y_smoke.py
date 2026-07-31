"""Smoke-рендер доступності головного вікна — ОКРЕМИЙ процес (конвенція
render_*_smoke; offscreen ДОПУСТИМИЙ: перевіряємо СТРУКТУРУ a11y-дерева, не
вигляд). Рівень 3.5 QA-гейта (Рів.6 рубрики: функціональність/a11y).

Ловить три класи багів на ВСІХ сторінках MainWindow (фейк-контролер спільний із
render_nav_smoke):
  (а) §18 accessible name: кожен видимий QPushButton/GlassButton/QCheckBox/
      QComboBox має непорожній accessibleName АБО text — інакше рецензент/скрінрідер
      не назве контрол. Fail зі списком порушників.
  (б) §17 «мертві кнопки»: кожна видима кнопка має ≥1 приймач на clicked
      (isSignalConnected) — БЕЗ реального кліку (клік у smoke-процесі ризикований).
      Fail зі списком.
  (в) §19 Tab-порядок: для «Диктування» й «Налаштування» ланцюг nextInFocusChain
      видимих фокусованих контролів іде згори-вниз — м'яка перевірка, fail лише
      на ГРУБІ стрибки фокуса назад-угору (більш ніж на кілька рядів).

    python -m unittest tests.render_a11y_smoke              # звичайний прогін
    python tests/render_a11y_smoke.py                       # standalone-раннер
"""
import os
import sys
import unittest
from pathlib import Path

# Тут offscreen допустимий: перевіряємо a11y-дерево (імена/сигнали/фокус), не вигляд.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import (QCheckBox, QComboBox, QPushButton, QLabel,
                               QToolButton)

from types import SimpleNamespace

from whisper_core import profiles
from tests.render_nav_smoke import _NavController, _make_sandbox

# скільки пунктів навігації (== сторінок стека)
_PAGE_COUNT = 8
_ROW_H = 60          # орієнтовна висота ряду контролів (для м'якого Tab-порогу)
_TAB_GROSS = 180     # грубий стрибок фокуса назад-угору (≈3 ряди) — тільки такий fail


def _sig_connected(obj, *sigs) -> bool:
    meta = obj.metaObject()
    for sig in sigs:
        idx = meta.indexOfSignal(sig)
        if idx >= 0 and obj.isSignalConnected(meta.method(idx)):
            return True
    return False


def _clicked_connected(btn) -> bool:
    """≥1 приймач на clicked. Кнопка в QButtonGroup «жива», коли під'єднано
    груповий сигнал (idClicked/buttonClicked/…): Qt веде цей зв'язок через
    групу, а не через публічний clicked самої кнопки (isSignalConnected його
    не бачить)."""
    if _sig_connected(btn, "clicked(bool)", "clicked()"):
        return True
    if hasattr(btn, "menu") and btn.menu() is not None:
        return True                       # кнопка-меню: клік розкриває QMenu
    grp = btn.group() if hasattr(btn, "group") else None
    if grp is not None and _sig_connected(
            grp, "idClicked(int)", "buttonClicked(QAbstractButton*)",
            "buttonToggled(QAbstractButton*,bool)", "idToggled(int,bool)",
            "buttonPressed(QAbstractButton*)"):
        return True
    return False


def _has_name(w) -> bool:
    """Непорожній accessibleName або (де є) text."""
    if (w.accessibleName() or "").strip():
        return True
    txt = w.text() if hasattr(w, "text") else ""
    return bool((txt or "").strip())


def _label(w) -> str:
    """Читабельний ідентифікатор порушника для fail-повідомлення."""
    txt = ""
    if hasattr(w, "text"):
        txt = (w.text() or "").strip()
    hint = txt or (w.accessibleName() or "").strip() or (w.toolTip() or "").strip()
    page = ""
    p = w.parent()
    while p is not None:
        cn = type(p).__name__
        if cn.endswith("Page"):
            page = cn
            break
        p = p.parent()
    return f"{type(w).__name__}({hint!r}) на {page or '?'}"


def _is_descendant(w, ancestor) -> bool:
    p = w
    while p is not None:
        if p is ancestor:
            return True
        p = p.parent()
    return False


class A11ySmokeTests(unittest.TestCase):
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
        from PySide6.QtCore import QTimer as _QTimer
        win = self._win
        if win is not None:
            for t in win.findChildren(_QTimer):
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
        win.resize(1856, 1044)
        win.show()
        self._app.processEvents()
        return win

    def _visible_controls(self, win):
        """Усі видимі QPushButton/QCheckBox/QComboBox по ВСІХ сторінках (кожна
        стає поточною через set_page), дедуп за id."""
        found = {}
        from PySide6.QtWidgets import QToolButton
        for i in range(_PAGE_COUNT):
            win.set_page(i)
            self._app.processEvents()
            # Розкривачки (згорнуті панелі, напр. налаштування Наради) ховають
            # свій вміст від isVisible — розкриваємо, інакше скан сліпий.
            page = win.pages.widget(i)
            for toggle in page.findChildren(QToolButton):
                if toggle.isCheckable() and not toggle.isChecked():
                    toggle.setChecked(True)
            self._app.processEvents()
            for cls in (QPushButton, QCheckBox, QComboBox):
                for w in win.findChildren(cls):
                    if w.isVisible():
                        found[id(w)] = w
        return list(found.values())

    def test_every_control_has_accessible_name(self):
        """§18: жоден видимий контрол не безіменний."""
        win = self._window()
        bad = [_label(w) for w in self._visible_controls(win) if not _has_name(w)]
        self.assertFalse(
            bad, "Безіменні контролі (нема accessibleName/text) — §18:\n  "
                 + "\n  ".join(sorted(bad)))

    def test_no_dead_buttons(self):
        """§17: у кожної видимої кнопки clicked під'єднано (не «мертва»)."""
        win = self._window()
        bad = [_label(w) for w in self._visible_controls(win)
               if isinstance(w, QPushButton) and not _clicked_connected(w)]
        self.assertFalse(
            bad, "«Мертві» кнопки (clicked ні до чого не під'єднано) — §17:\n  "
                 + "\n  ".join(sorted(bad)))

    def _focus_chain_y(self, win, page):
        """(контрол, глобальний y) уздовж nextInFocusChain — лише видимі,
        фокусовані, увімкнені нащадки `page`."""
        out = []
        seen = set()
        w = win.nextInFocusChain()
        guard = 0
        while w is not None and guard < 20000:
            guard += 1
            if id(w) in seen:
                break
            seen.add(id(w))
            if (_is_descendant(w, page) and w.isVisible()
                    and w.focusPolicy() != Qt.NoFocus and w.isEnabled()):
                out.append((w, w.mapToGlobal(QPoint(0, 0)).y()))
            w = w.nextInFocusChain()
        return out

    def _assert_tab_top_down(self, win, index, page):
        win.set_page(index)
        self._app.processEvents()
        chain = self._focus_chain_y(win, page)
        gross = []
        for (a, ya), (b, yb) in zip(chain, chain[1:]):
            if yb < ya - _TAB_GROSS:            # стрибок назад-угору
                gross.append(f"{_label(a)} (y={ya}) → {_label(b)} (y={yb})")
        self.assertFalse(
            gross, f"Tab-порядок стрибає назад-угору на {type(page).__name__} "
                   f"(§19, поріг {_TAB_GROSS}px):\n  " + "\n  ".join(gross))

    # Ключові довгі лейбли, що МУСЯТЬ переноситися (канон DESIGN-TYPOGRAPHY §3):
    # заголовок-шапка кожної сторінки, багатослівний підпис поля «Кадрів за
    # секунду» на «Записі екрана» і пояснення блоку помічених слів у Словнику.
    _MUST_WRAP = {"pageHeaderH1", "screenFpsCaption", "vocabSpottedExplain"}

    def test_key_long_labels_word_wrap(self):
        """§3: ключові довгі лейбли переносяться (setWordWrap), а не обрізаються."""
        win = self._window()
        found = {}
        for i in range(_PAGE_COUNT):
            win.set_page(i)
            self._app.processEvents()
            # Частина налаштувань живе за розкривачем і згорнута за
            # замовчуванням (канон сторінок: головне видно, другорядне
            # сховане). Для перевірки переносу розгортаємо їх — інакше
            # підпис просто невидимий і тест «не знаходить» його,
            # хоча дефекту немає.
            for tb in win.findChildren(QToolButton):
                if tb.property("disclosure") and not tb.isChecked():
                    tb.click()
            self._app.processEvents()
            for w in win.findChildren(QLabel):
                if w.objectName() in self._MUST_WRAP and w.isVisible():
                    found.setdefault(w.objectName(), []).append(w)
        missing_names = self._MUST_WRAP - set(found)
        self.assertFalse(
            missing_names, "Не знайдено ключові лейбли на жодній сторінці "
                           f"(рендер §3): {sorted(missing_names)}")
        bad = [name for name, labels in found.items()
               if not all(w.wordWrap() for w in labels)]
        self.assertFalse(
            bad, "Ключові довгі лейбли без wordWrap (обрізаються, §3):\n  "
                 + "\n  ".join(sorted(bad)))

    def test_tab_order_top_down_key_pages(self):
        """§19 (м'яко): фокус не стрибає грубо назад-угору на ключових сторінках."""
        win = self._window()
        self._assert_tab_top_down(win, 0, win.dictation)     # Диктування
        self._assert_tab_top_down(win, 6, win.settings)      # Налаштування


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(A11ySmokeTests))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
