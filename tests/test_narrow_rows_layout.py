"""Сторожі верстки на НАЙВУЖЧОМУ вікні: ряди дій, які до 25.07.2026 різались.

Візуальний гейт до тієї дати міряв рівно одну ширину (1856) і чесно писав «0
порушень» там, де на мінімумі вікна (1000) підписи були обрізані. Перший прогін
на трьох ширинах відкрив борг у трьох рядах:
  • картка Історії — «Розпізнано погано…» (140/98) і «Видалити з історії» (121/86);
  • Налаштування/Нарада — чотири кнопки шифрування («Захистити файлом-ключем…»
    просила 197 при 130 доступних);
  • Налаштування/Система — «Відкрити папку логів» (142/101), «Скопіювати
    діагностику» (164/122), «Повідомити про проблему» (185/143).
Лікування — не скорочені підписи, а виніс частини дій в окремий рядок. Ці
сторожі тримають лікування: міряємо Qt-інваріант «контроль вужчий за власний
minimumSizeHint» — саме так Qt показує, що ряд не вмістився і підпис ріжеться.

Канон виміру (як у DictationCardNarrowRowTests): вікно ПОКАЗАНЕ, з розміром, зі
справжнім theme.QSS і вимкненими анімаціями. Це не косметика: без QSS кнопки не
мають продуктових відступів, а QScrollArea віддає вмісту ширину по запиту —
нічого не стискається, і сторож пропустив би регрес.
"""
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QAbstractButton

from fronts.desktop import i18n, motion
from fronts.desktop.i18n import tr
from fronts.desktop.main_window import MainWindow
from fronts.desktop.theme import QSS, load_fonts
from tests.render_nav_smoke import _NavController
from whisper_core.history import log_history

_APP = QApplication.instance() or QApplication([])


def _squeezed(root):
    """Підписані контроли, вужчі за власний minimumSizeHint (= підпис ріжеться)."""
    out = []
    for w in root.findChildren(QAbstractButton):
        if w.isHidden() or not w.text():
            continue
        need = w.minimumSizeHint().width()
        if need > 0 and w.width() < need:
            out.append(f"«{w.text()}» {w.width()}<{need}")
    return out


def _captions(root):
    return [w.text() for w in root.findChildren(QAbstractButton)
            if not w.isHidden() and w.text()]


class NarrowRowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._motion_cfg = motion._cfg
        self._lang = i18n.current_language()
        self.win = None
        self._build("uk")

    def _build(self, lang):
        """Показане вікно найвужчого розміру заданою мовою (англійські підписи
        інші й місцями довші — колонка Наради вужча саме англійською)."""
        self._close()
        i18n.set_language(lang)
        ctrl = _NavController(self.tmp_dir)
        # анімації гасимо в КОНФІГУ контролера: MainWindow сам кличе
        # motion.init_config(controller.cfg) і перебив би пряме вимкнення
        ctrl.cfg.animations = False
        ctrl.cfg.ui_language = lang
        self.win = MainWindow(ctrl)
        load_fonts()
        self.win.setStyleSheet(QSS)
        self.width = self.win.minimumWidth()
        self.win.resize(self.width, self.win.minimumHeight())
        self.win.show()
        _APP.processEvents()
        self.assertLessEqual(self.width, 1000,
                             "мінімум вікна виріс — сторож міряє вже не найвужчий випадок")

    def _close(self):
        if self.win is None:
            return
        for t in self.win.findChildren(QTimer):
            t.stop()
        self.win.close()
        _APP.processEvents()
        self.win = None

    def tearDown(self):
        self._close()
        i18n.set_language(self._lang)
        motion.init_config(self._motion_cfg)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _open(self, page):
        self.win.set_page(self.win.pages.indexOf(page))
        for _ in range(5):
            _APP.processEvents()

    def _open_tab(self, title):
        page = self.win.settings
        self._open(page)
        idx = next((i for i in range(page._tabs.count())
                    if page._tabs.tabText(i) == title), None)
        self.assertIsNotNone(idx, f"нема вкладки «{title}»")
        page._tabs.setCurrentIndex(idx)
        for _ in range(5):
            _APP.processEvents()
        return page._tabs.widget(idx)

    def test_history_card_row_fits(self):
        page = self.win.history
        # Картку будуємо ПРОДУКТОВИМ шляхом (запис у пам'ять → refresh), а не
        # прямим _add_card: без записів сторінка показує порожній стан, картка
        # лишається невидимою — і жодна геометрія не рахується.
        # raw ≠ final: у ряду видима і «Копіювати дослівно» — найтісніший стан.
        log_history(self.win.controller.profile.history_path,
                    "прошу підготувати зведення особового складу",
                    "Прошу підготувати зведення особового складу.")
        self._open(page)
        page.refresh()
        for _ in range(8):
            _APP.processEvents()
        self.assertTrue(page._cards, "картка не збудувалась — міряти нічого")
        card = page._cards[-1][0]
        self.assertTrue(card.isVisible(),
                        "картка невидима — геометрія не рахується, вимір фальшивий")
        seen = _captions(card)
        for caption in (tr("common_copy"), tr("corpus_menu_bad"), tr("hist_delete")):
            self.assertIn(caption, seen,
                          f"сторож не побачив кнопку «{caption}» — міряти нічого")
        self.assertEqual(_squeezed(card), [],
                         f"на ширині {self.width} кнопки картки Історії стиснуті: "
                         f"{_squeezed(card)}")

    def test_meeting_vault_buttons_fit(self):
        tab = self._open_tab(tr("set_tab_meeting"))
        seen = _captions(tab)
        for caption in (tr("set_vault_pw_set"), tr("set_vault_keyfile_set"),
                        tr("set_vault_twofactor_set"), tr("set_vault_keyfile_create")):
            self.assertIn(caption, seen,
                          f"сторож не побачив кнопку «{caption}» — міряти нічого")
        self.assertEqual(_squeezed(tab), [],
                         f"на ширині {self.width} контролі вкладки «Нарада» стиснуті: "
                         f"{_squeezed(tab)}")

    def test_meeting_vault_buttons_fit_in_every_state(self):
        """Видимий набір кнопок захисту залежить від стану сховища, і поділ на два
        ряди зроблено так, щоб у КОЖНОМУ стані в ряду було не більше двох. Стан
        підмінюємо на контролері (у стенді методу нема — сторінка бачить «none»)."""
        for lang in ("uk", "en"):
            self._build(lang)
            tab = self._open_tab(tr("set_tab_meeting"))
            page = self.win.settings
            for state in ("none", "dpapi", "password", "locked", "keyfile",
                          "twofactor", "lost"):
                with self.subTest(lang=lang, state=state):
                    self.win.controller.meeting_vault_state = lambda s=state: s
                    page._refresh_vault_ui()
                    for _ in range(5):
                        _APP.processEvents()
                    self.assertEqual(_squeezed(tab), [],
                                     f"{lang}, стан «{state}» на ширині "
                                     f"{self.width}: {_squeezed(tab)}")

    def test_system_diagnostics_buttons_fit(self):
        tab = self._open_tab(tr("set_tab_system"))
        seen = _captions(tab)
        for caption in (tr("set_open_logs"), tr("set_copy_diagnostics"),
                        tr("set_report_problem")):
            self.assertIn(caption, seen,
                          f"сторож не побачив кнопку «{caption}» — міряти нічого")
        self.assertEqual(_squeezed(tab), [],
                         f"на ширині {self.width} контролі вкладки «Система» стиснуті: "
                         f"{_squeezed(tab)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
