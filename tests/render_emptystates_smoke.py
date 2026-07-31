"""Render-smoke порожніх станів (аудит 31.07.2026) — ОКРЕМИЙ процес.

Перевіряє ФАКТИЧНУ видимість спільного компонента EmptyState на сторінках,
що не мали власного render_*_smoke: Аудіофайли (черга), Пошук, Словники
(таблиці термінів/макросів/пам'яті фраз). Диктування/Нарада/Історія/Запис
екрана лишаються у своїх файлах (render_dictation_feed_smoke не чіпаємо,
render_meeting_smoke / render_history_smoke / render_screen_smoke несуть
власні доповнення для цієї ж хвилі).

Для кожної сторінки: 0 записів → порожній стан видно; перший запис → стан
зникає (не «ключ tr() дорівнює ключу tr()», а реальна видимість віджета).

    python -m unittest tests.render_emptystates_smoke
    python tests/render_emptystates_smoke.py
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

from whisper_core import profiles
from tests.render_nav_smoke import _NavController, _make_sandbox


class EmptyStatesSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))
        # staticmethod: без нього доступ через self._orig_list(...) біндить
        # функцію як метод і мовчки підставляє self першим аргументом.
        cls._orig_list = staticmethod(profiles.list_profiles)

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
        # СВІЖА пісочниця на КОЖЕН тест: кілька тестів дописують macros.toml /
        # phrasebook.toml / видаляють термін на диску — спільна пісочниця між
        # тестами класу зробила б їх залежними від порядку виконання (алфавітний
        # порядок unittest уже раз спіймав саме це: "disappears" виконувався
        # РАНІШЕ за "shows_placeholder" і псував стартовий стан).
        self._sandbox = _make_sandbox()
        profiles.list_profiles = lambda root=None: self._orig_list(self._sandbox)

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

    def _window(self):
        from fronts.desktop.main_window import MainWindow
        win = MainWindow(_NavController(self._sandbox))
        self._win = win
        return win

    # --- Аудіофайли: черга ---
    def test_files_queue_empty_state_visible_then_hidden_on_first_file(self):
        win = self._window()
        page = win.files
        self.assertFalse(page._queue_empty.isHidden(),
                         "черга аудіофайлів: порожній стан мусить бути видимий на старті")
        self.assertTrue(page._queue_empty.button.isEnabled(),
                        "кнопка «Вибрати файли…» мусить бути активна (модель є)")

        page.add_files([str(Path(self._sandbox) / "запис.wav")])
        self._app.processEvents()

        self.assertTrue(page._queue_empty.isHidden(),
                        "перший файл у черзі мав прибрати порожній стан")

    def test_files_queue_empty_button_opens_file_picker(self):
        """Кнопка першого кроку веде на СПРАВЖНЮ дію — той самий _pick, що й
        кнопка «Вибрати файли…» у Dropzone (не в нікуди)."""
        from unittest.mock import patch

        win = self._window()
        page = win.files
        self.assertEqual(
            page._queue_empty._on_click, page._pick,
            "кнопка порожнього стану має бути під'єднана до _pick()")

        with patch("fronts.desktop.main_window.QFileDialog.getOpenFileNames",
                   return_value=([], "")) as picker:
            page._queue_empty.button.clicked.emit()
        picker.assert_called_once()

    # --- Пошук: порожня база vs немає збігів ---
    def test_search_shows_nodata_state_when_index_is_completely_empty(self):
        from whisper_core.search_index import SearchIndex

        win = self._window()
        page = win.search
        page._index = SearchIndex([])
        page._search.setText("щось")
        page._run_search()
        self._app.processEvents()

        self.assertEqual(page._stack.currentIndex(), 0)
        self.assertEqual(page._empty_title.text(), "Ще нема що шукати")

    def test_search_shows_none_state_when_index_has_data_but_no_match(self):
        from whisper_core.search_index import SearchIndex, SearchDoc, KIND_DICTATION

        win = self._window()
        page = win.search
        page._index = SearchIndex([SearchDoc(
            kind=KIND_DICTATION, ref="x", title="", date=1.0, text="кавунова нота")])
        page._search.setText("зовсім-інше-слово")
        page._run_search()
        self._app.processEvents()

        self.assertEqual(page._stack.currentIndex(), 0)
        self.assertEqual(page._empty_title.text(), "Нічого не знайдено")

    def test_search_hides_empty_state_once_a_match_is_found(self):
        from whisper_core.search_index import SearchIndex, SearchDoc, KIND_DICTATION

        win = self._window()
        page = win.search
        page._index = SearchIndex([SearchDoc(
            kind=KIND_DICTATION, ref="x", title="", date=1.0, text="кавунова нота")])
        page._search.setText("кавунова")
        page._run_search()
        self._app.processEvents()

        self.assertEqual(page._stack.currentIndex(), 1,
                         "знайдений збіг мав показати стрічку результатів, не порожній стан")

    # --- Словники: таблиці термінів/макросів/пам'яті фраз ---
    def test_vocab_macros_table_shows_placeholder_widget_when_empty(self):
        win = self._window()
        page = win.vocab
        widget = page._macros_table.cellWidget(0, 0)
        self.assertIsNotNone(
            widget, "порожня таблиця макросів мусить нести іконку-пояснення, "
                    "не голий сірий рядок")

    def test_vocab_macros_placeholder_disappears_after_first_macro(self):
        from whisper_core import macros as macros_mod

        win = self._window()
        page = win.vocab
        macros_mod.add_macro(page._macros_path(), "тест", "тестова фраза")
        page._refresh_macros_table()
        self._app.processEvents()

        self.assertIsNone(page._macros_table.cellWidget(0, 0),
                          "перший макрос мав прибрати порожній рядок-заглушку")
        self.assertEqual(page._macros_table.item(0, 0).text(), "тест")

    def test_vocab_phrase_table_shows_placeholder_widget_when_empty(self):
        win = self._window()
        page = win.vocab
        widget = page._phrase_table.cellWidget(0, 0)
        self.assertIsNotNone(
            widget, "порожня таблиця пам'яті фраз мусить нести іконку-пояснення")

    def test_vocab_phrase_placeholder_disappears_after_first_pair(self):
        from whisper_core import phrasebook

        win = self._window()
        page = win.vocab
        phrasebook.add_phrase(page._phrases_path(), "worktree", "ворктрі")
        page._refresh_phrase_table()
        self._app.processEvents()

        self.assertIsNone(page._phrase_table.cellWidget(0, 0),
                          "перша пара мала прибрати порожній рядок-заглушку")

    def test_vocab_terms_table_placeholder_appears_once_dictionary_is_emptied(self):
        """Пісочниця стартує з одним ЛЮДСЬКИМ терміном (GitHub, terms.toml) —
        таблиця НЕ порожня. Людський термін не видаляється з UI (лише машинні
        canonical, editable_canons), тож для «спорожнілого словника» мокуємо
        читання терміна порожнім — так само, як побачить сторінка щойно
        створений профіль без жодного terms.toml."""
        from unittest.mock import patch

        win = self._window()
        page = win.vocab
        self.assertIsNone(page._table.cellWidget(0, 0),
                          "непорожній словник не мусить нести заглушку")

        with patch("fronts.desktop.pages.vocab.read_terms_dict", return_value={}):
            page.refresh()
            self._app.processEvents()

        self.assertIsNotNone(page._table.cellWidget(0, 0),
                             "спорожнілий словник термінів мусить показати заглушку")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(EmptyStatesSmokeTests))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
