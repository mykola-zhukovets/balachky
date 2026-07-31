"""feature/copy-on-card: у картці стрічки диктування має бути кнопка
«Копіювати» — раніше свіжий надиктований текст доводилось виділяти мишею.

Перевіряємо не наявність кнопки, а результат: клік кладе в буфер САМЕ
оброблений текст картки (не сирий!) і САМЕ той, що в картці на момент кліку
(після «Переформатувати…» — уже новий). Плюс поділ «Копіювати» / «Копіювати
дослівно»: одна умова для кнопки, пункту ПКМ-меню й підпису «виправлено
написання» — на одній картці вони не мають розходитись.

Плюс сторож ВЕРСТКИ ряду дій на найвужчому вікні (DictationCardNarrowRowTests):
три підписані кнопки в мета-рядку не вміщались на 1000 точок і Qt стискав їх
нижче minimumSizeHint — різало не лише нову «Копіювати дослівно», а й здорову
«Переформатувати…» (регрес, знахідка рецензії 25.07).
"""
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import patch

from PySide6.QtCore import QPoint, QTimer
from PySide6.QtWidgets import QApplication, QAbstractButton, QLabel

from fronts.desktop import main_window as mw
from fronts.desktop import motion
from fronts.desktop.i18n import current_language, set_language, tr
from fronts.desktop.main_window import MainWindow
from fronts.desktop.theme import QSS, load_fonts
from tests.render_nav_smoke import _NavController

_APP = QApplication.instance() or QApplication([])


class _FakeAction:
    def __init__(self, text=""):
        self.text = text
        self.triggered = type("_S", (), {"connect": lambda *a, **k: None})()

    def setEnabled(self, _):
        pass


class _FakeMenu:
    """Дублер QMenu (як у test_processing_recovery): збирає підписи доданих дій,
    exec — no-op. Справжній QMenu.exec() блокує прогін на показі меню."""
    last = None

    def __init__(self, *a, **k):
        self.texts = []
        _FakeMenu.last = self

    def addAction(self, text=""):
        self.texts.append(text)
        return _FakeAction(text)

    def addSeparator(self):
        pass

    def exec(self, *a, **k):
        pass


class DictationCardCopyTests(unittest.TestCase):
    def setUp(self):
        self._language = current_language()
        self.addCleanup(set_language, self._language)
        set_language("uk")
        self.tmp_dir = tempfile.mkdtemp()
        self.win = MainWindow(_NavController(self.tmp_dir))
        self.page = self.win.dictation
        _APP.clipboard().clear()

    def tearDown(self):
        for t in self.win.findChildren(QTimer):   # тости/анімації не переживають тест
            t.stop()
        self.win.close()
        _APP.processEvents()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # ─── доступ до нутрощів першої картки стрічки ───
    def _card(self):
        return self.page._feedbox.itemAt(0).widget()

    def _card_buttons(self, caption):
        """ВИДИМІ кнопки з підписом caption у ПЕРШІЙ картці стрічки. Вікно в
        тесті не показане, тож isVisible() завжди False — питаємо isHidden(),
        який відбиває саме явне приховання кнопки кодом картки."""
        return [b for b in self._card().findChildren(QAbstractButton)
                if b.text() == caption and not b.isHidden()]

    def _card_label(self):
        """QLabel із текстом розшифровки (та, що несе _final_text)."""
        for lbl in self._card().findChildren(QLabel):
            if hasattr(lbl, "_final_text"):
                return lbl
        self.fail("у картці немає мітки з _final_text")

    def _menu_captions(self, label):
        """Підписи пунктів ПКМ-меню ЦІЄЇ картки (через дублер QMenu)."""
        with patch.object(mw, "QMenu", _FakeMenu):
            self.page._term_fix_menu(label, QPoint(1, 1))
        return _FakeMenu.last.texts

    def _meta_text(self):
        """Мета-рядок картки (час + можливий підпис «виправлено написання»)."""
        for lbl in self._card().findChildren(QLabel):
            if lbl.property("muted") and ":" in lbl.text():
                return lbl.text()
        self.fail("у картці немає мета-рядка")

    # ─── копіюється ОБРОБЛЕНИЙ текст, а не сирий ───
    def test_copy_button_puts_processed_text_not_raw(self):
        """Сирий і оброблений текст РІЗНІ: «Копіювати» мусить класти оброблений
        (з пунктуацією й чисткою). Раніше тест подавав однакові raw/final, тож
        підміна _final_text → _raw_text проходила гейт зеленою."""
        raw = "прошу підготувати зведення до 15,5 години"
        final = "Прошу підготувати зведення до 15,5 години."
        self.page.add_entry(raw, final)
        _APP.processEvents()

        btns = self._card_buttons(tr("common_copy"))
        self.assertEqual(len(btns), 1,
                         "у картці стрічки немає кнопки «Копіювати»")
        btns[0].click()
        _APP.processEvents()
        self.assertEqual(_APP.clipboard().text(), final)
        self.assertNotEqual(_APP.clipboard().text(), raw,
                            "кнопка поклала НЕОБРОБЛЕНИЙ текст")

    # ─── текст читається в момент кліку, не на побудові ───
    def test_copy_button_reads_text_at_click_time(self):
        """Так текст картки міняє «Переформатувати…» (_apply). Кнопка мусить
        покласти НОВИЙ текст — інакше вона тримає значення з побудови."""
        self.page.add_entry("сирий текст", "Оброблений текст.")
        _APP.processEvents()

        label = self._card_label()
        label._final_text = "Переформатований текст."   # рівно те, що робить _apply

        self._card_buttons(tr("common_copy"))[0].click()
        _APP.processEvents()
        self.assertEqual(_APP.clipboard().text(), "Переформатований текст.")

    def test_copy_button_follows_rewrite_through_shared_setter(self):
        """Той самий шлях, але через справжню точку зміни тексту картки
        (_set_card_text — її кличуть і «Переформатувати…», і виправлення слова)."""
        self.page.add_entry("сирий текст", "Оброблений текст.")
        _APP.processEvents()

        label = self._card_label()
        self.page._set_card_text(label, "Текст після переформатування.", words=[])
        _APP.processEvents()

        self._card_buttons(tr("common_copy"))[0].click()
        _APP.processEvents()
        self.assertEqual(_APP.clipboard().text(), "Текст після переформатування.")

    # ─── «Копіювати дослівно»: кнопка, меню й підпис — одна умова ───
    def test_verbatim_button_copies_raw_only_when_it_differs(self):
        raw = "прошу підготувати зведення"
        final = "Прошу підготувати зведення."
        self.page.add_entry(raw, final)
        _APP.processEvents()

        btns = self._card_buttons(tr("common_copy_verbatim"))
        self.assertEqual(len(btns), 1,
                         "немає кнопки «Копіювати дослівно» при raw != final")
        btns[0].click()
        _APP.processEvents()
        self.assertEqual(_APP.clipboard().text(), raw)

    def test_no_verbatim_button_when_raw_equals_final(self):
        same = "Текст без обробки"
        self.page.add_entry(same, same)
        _APP.processEvents()
        self.assertEqual(self._card_buttons(tr("common_copy_verbatim")), [],
                         "дубль кнопки: raw збігається з обробленим текстом")

    def test_verbatim_button_and_context_menu_agree_after_rewrite(self):
        """Кнопка вирішувала видимість раз на побудові, а ПКМ-меню — у момент
        показу: після «Переформатувати…» на ОДНІЙ картці виходили різні
        відповіді. Тепер обидві сторони дивляться в одну умову."""
        raw = "прошу підготувати зведення"
        self.page.add_entry(raw, "Прошу підготувати зведення.")
        _APP.processEvents()
        label = self._card_label()

        # переформатували так, що оброблений текст ЗБІГСЯ з сирим → дослівне
        # копіювання втратило сенс: ні кнопки, ні пункту меню
        self.page._set_card_text(label, raw, words=[])
        _APP.processEvents()
        self.assertEqual(self._card_buttons(tr("common_copy_verbatim")), [],
                         "кнопка «дослівно» лишилась, хоч текст уже дорівнює сирому")
        self.assertNotIn(tr("common_copy_verbatim"), self._menu_captions(label))

        # і назад: текст знову інший → з'явились обидві
        self.page._set_card_text(label, "Прошу підготувати зведення!", words=[])
        _APP.processEvents()
        self.assertEqual(len(self._card_buttons(tr("common_copy_verbatim"))), 1,
                         "кнопка «дослівно» не повернулась, хоч текст знову інший")
        self.assertIn("Копіювати дослівно", self._menu_captions(label))

    def test_whitespace_only_difference_gives_neither_note_nor_verbatim(self):
        """Підпис «виправлено написання» ставився за final != raw БЕЗ strip, а
        кнопка — з strip: на різниці в пробілах підпис був, а кнопки не було."""
        self.page.add_entry("  Текст без обробки  ", "Текст без обробки")
        _APP.processEvents()

        self.assertNotIn(tr("dict_meta_fixed").strip(), self._meta_text(),
                         "підпис «виправлено написання» на різниці лише в пробілах")
        self.assertEqual(self._card_buttons(tr("common_copy_verbatim")), [],
                         "кнопка «дослівно» на різниці лише в пробілах")

    def test_processing_note_and_verbatim_button_appear_together(self):
        """Зворотний бік тієї ж умови: справжня обробка → і підпис, і кнопка."""
        self.page.add_entry("прошу підготувати зведення",
                            "Прошу підготувати зведення.")
        _APP.processEvents()

        self.assertIn(tr("dict_meta_fixed").strip(), self._meta_text())
        self.assertEqual(len(self._card_buttons(tr("common_copy_verbatim"))), 1)

    # ─── діри покриття, названі рецензією 25.07 ───
    def test_set_card_text_renders_with_NEW_words_not_stale(self):
        """У _set_card_text порядок несучий: label._words МУСИТЬ бути присвоєне
        ДО setText(_render_fix_html(...)), бо рендер читає підсвітку саме з
        label._words. Переставлення рядків малювало б картку зі СТАРИМИ словами —
        підсвітка непевних слів відставала б на одну правку, і жоден тест цього
        не бачив."""
        self.page.add_entry("сирий текст", "Оброблений текст.")   # words=None → []
        _APP.processEvents()
        label = self._card_label()

        self.page._set_card_text(label, "непевне слово",
                                 words=[("непевне", 0.1), ("слово", 0.9)])
        _APP.processEvents()

        htm = label.text()
        self.assertEqual(htm.count("<span"), 1,
                         "рендер узяв СТАРІ label._words (присвоєння після setText?)")
        self.assertIn("непевне</span>", htm,
                      "підсвічено не те слово, що подали з новими words")

    def test_files_card_menu_has_no_verbatim_item(self):
        """Відсів порожнього сирого тексту у _verbatim_differs (bool(raw) and …)
        несучий: картки Файлів (і Історії) _raw_text не мають узагалі, тож без
        відсіву їхнє ПКМ-меню отримало б фальшивий пункт «Копіювати дослівно»,
        який поклав би в буфер порожньо."""
        from fronts.desktop.main_window import FileStatus
        files = self.win.files
        files.controller.enqueue_file = lambda p, model=None: 7
        files.add_files(["зразок.wav"])
        files._on_done(7, "Оброблений текст файлу.", f"{FileStatus.DONE}:1", [], [])
        _APP.processEvents()

        label = next(l for l in files.findChildren(QLabel)
                     if getattr(l, "_final_text", None) == "Оброблений текст файлу.")
        self.assertFalse(hasattr(label, "_raw_text"),
                         "картка файлу раптом несе _raw_text — тест міряє не те")
        self.assertFalse(files.verbatim_available(label))
        with patch.object(mw, "QMenu", _FakeMenu):
            files._term_fix_menu(label, QPoint(1, 1))
        self.assertNotIn(tr("common_copy_verbatim"), _FakeMenu.last.texts,
                         "фальшиве «Копіювати дослівно» на картці без сирого тексту")


class DictationCardNarrowRowTests(unittest.TestCase):
    """Сторож регресу: на НАЙВУЖЧОМУ досяжному вікні (MainWindow.minimumWidth() —
    1000 точок, вужче Qt не дасть) картка стрічки з raw ≠ final не має жодного
    стиснутого підпису. Міряємо не «схоже на око», а Qt-інваріант: якщо ряд не
    вміщається, Qt тисне контроли НИЖЧЕ minimumSizeHint (для GlassButton це
    ширина тексту + запас 40) — саме так різало «Переформатувати…».

    Вікно тут ПОКАЗАНЕ і має розмір: без show()+resize геометрія лишається
    дефолтною, і сторож пропустив би регрес. Анімації вимкнені (той самий канон
    детермінованості, що у візуальному гейті): з увімкненими wrap_appear кладе
    картку в анімовану обгортку, і без живого цикла подій геометрія лишається
    незавершеною — виміри були б випадкові."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._motion_cfg = motion._cfg
        ctrl = _NavController(self.tmp_dir)
        # анімації гасимо в КОНФІГУ контролера: MainWindow сам кличе
        # motion.init_config(controller.cfg) і перебив би пряме вимкнення
        ctrl.cfg.animations = False
        self.win = MainWindow(ctrl)
        # Стилі — НЕСУЧІ для цього виміру, не косметика: без QSS картка не має
        # реальних відступів/розмірів кнопок, QScrollArea віддає стрічці ширину
        # по її запиту (з горизонтальною прокруткою), нічого не стискається — і
        # сторож не побачив би регресу. Продукт завжди йде з theme.QSS.
        load_fonts()
        self.win.setStyleSheet(QSS)
        self.page = self.win.dictation
        self.width = self.win.minimumWidth()
        self.win.resize(self.width, self.win.minimumHeight())
        self.win.show()
        self.win.set_page(self.win.pages.indexOf(self.page))
        _APP.processEvents()

    def tearDown(self):
        for t in self.win.findChildren(QTimer):
            t.stop()
        self.win.close()
        _APP.processEvents()
        motion.init_config(self._motion_cfg)   # інші тести бачать свій конфіг
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_narrow_window_does_not_squeeze_card_controls(self):
        self.assertLessEqual(self.width, 1000,
                             "мінімум вікна виріс — сторож міряє вже не найвужчий випадок")
        # raw ≠ final: видима і «Копіювати дослівно», і найдовший мета-підпис
        # «· виправлено написання деяких слів» — найтісніший реальний стан картки
        self.page.add_entry(
            "прошу підготувати зведення особового складу до 15,5 години",
            "Прошу підготувати зведення особового складу до 15,5 години.")
        for _ in range(8):
            _APP.processEvents()

        card = self.page._feedbox.itemAt(0).widget()
        squeezed = []
        seen = []
        for w in card.findChildren(QAbstractButton) + card.findChildren(QLabel):
            if w.isHidden() or not w.text():
                continue
            seen.append(w.text())
            need = w.minimumSizeHint().width()
            if need > 0 and w.width() < need:
                squeezed.append(f"«{w.text()}» {w.width()}<{need}")
        # без цієї перевірки сторож був би порожній: не знайшовши контролів
        # (перейменували картку, картка в анімованій обгортці) він «проходив» би
        # з будь-якою версткою
        for caption in (tr("common_copy"), tr("common_copy_verbatim"),
                        tr("rewrite_card_btn")):
            self.assertIn(caption, seen,
                          f"сторож не побачив кнопку «{caption}» — міряти нічого")
        self.assertEqual(squeezed, [],
                         f"на ширині {self.width} контроли картки стиснуті нижче "
                         f"власного мінімуму (обрізані підписи): {squeezed}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
