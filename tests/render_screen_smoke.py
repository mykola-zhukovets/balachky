"""Offscreen-рендер сторінки «Запис екрана» — ОКРЕМИЙ процес (як
render_meeting_smoke): сторінка має живий QTimer (_tick 1s), тож тримаємо її
поза спільним `unittest discover`, а таймери жорстко спиняємо у teardown, щоб
не було 0xC000041D на нативній деструкції offscreen-Qt.

Візуальний аудит 1.2.1 №3/№4 та хвиля «Запис-полірування»:
- єдина текстова кнопка з відео-іконкою (fa6s.video);
- кнопка disabled до вибору джерела при «Не знайдено екранів» з тултіпом;
- перемикач джерела у сегмент-контролі;
- WebM у дропдауні форматів.

Запуск:
    python -m unittest tests.render_screen_smoke
    python tests/render_screen_smoke.py
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

from fronts.desktop.i18n import STRINGS, current_language, set_language


class _ScreenController:
    """Легкий контролер із Qt-сигналами запису екрана для рендеру сторінки."""

    def __init__(self, has_sources=True, start_result=True, start_error=None,
                 recordings=None):
        from PySide6.QtCore import QObject, Signal

        class _Ctl(QObject):
            screen_record_state = Signal(str)
            screen_record_error = Signal(str)
            screen_record_finished = Signal(str, bool)

            def list_screen_monitors(self):
                if not self.has_sources:
                    return []
                return [SimpleNamespace(label="Екран 1", index=1,
                                        left=0, top=0, width=1920, height=1080)]

            def list_screen_windows(self):
                return []

            def __init__(self, has_sources, start_result=True, start_error=None,
                         recordings=None):
                super().__init__()
                self.has_sources = has_sources
                self.start_result = start_result
                self.start_error = start_error
                self.cfg = SimpleNamespace(
                    screen_record_fps=30, screen_record_resolution="native",
                    screen_record_quality="medium", screen_record_format="webm",
                    screen_record_system_audio=False)
                self.started = []
                self._recordings = list(recordings or [])
                self.renamed = []
                self.deleted = []
                self.shown_in_folder = []

            def list_screen_recordings(self):
                return list(self._recordings)

            def rename_screen_recording(self, path, new_name):
                self.renamed.append((path, new_name))
                new_path = path.with_name(new_name + path.suffix)
                self._recordings = [new_path if p == path else p for p in self._recordings]
                return new_path

            def delete_screen_recording(self, path):
                self.deleted.append(path)
                self._recordings = [p for p in self._recordings if p != path]
                return True

            def show_screen_recording_in_folder(self, path):
                self.shown_in_folder.append(path)

            def open_screen_recordings_folder(self):
                pass

            def screen_record_start(self, source, options):
                self.started.append((source, options))
                # реальний DesktopApp.screen_record_start завжди шле
                # screen_record_error ПЕРЕД поверненням False — мокуємо ту саму
                # поведінку, щоб тест сторінки перевіряв реальний контракт.
                if not self.start_result and self.start_error:
                    self.screen_record_error.emit(self.start_error)
                return self.start_result

            def screen_record_stop(self):
                pass

            def save_config(self):
                pass

        self._impl = _Ctl(has_sources, start_result, start_error, recordings)

    def __getattr__(self, name):
        return getattr(self._impl, name)


class ScreenRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))

    @classmethod
    def tearDownClass(cls):
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
        self._language = current_language()
        self.addCleanup(set_language, self._language)
        set_language("uk")
        self._live = []

    def tearDown(self):
        from PySide6.QtCore import QTimer
        for page, ctl in self._live:
            for t in page.findChildren(QTimer):
                try:
                    t.stop()
                except RuntimeError:
                    pass
            try:
                page.close()
            except Exception:
                pass
            page.deleteLater()
            ctl._impl.deleteLater()
        self._live = []
        self._flush_deferred(self._app)

    def _page(self, has_sources=True, start_result=True, start_error=None,
              recordings=None):
        from fronts.desktop.pages.screen import ScreenPage
        ctl = _ScreenController(has_sources=has_sources, start_result=start_result,
                                start_error=start_error, recordings=recordings)
        page = ScreenPage(ctl)
        self._live.append((page, ctl))
        page.resize(1000, 640)
        page.show()
        for _ in range(4):
            self._app.processEvents()
        return page, ctl

    def test_recording_settings_collapsed_behind_a_visible_disclosure(self):
        """Канон побудови сторінок 30.07 п.4: шість рядків налаштувань запису
        екрана (джерело, кількість к/с, роздільність, якість, формат, звук)
        стояли розгорнутими ЗАВЖДИ, відсуваючи стрічку записів. Тепер вони —
        в одному згорнутому блоці, як на Нараді (п.3): розкривач — та сама
        помітна кнопка з property("disclosure"), не дрібний рядок."""
        page, _ = self._page()
        self.assertFalse(
            page._settings_panel.isVisible(),
            "налаштування запису екрана мають бути згорнуті за замовчуванням")
        self.assertTrue(page._settings_toggle.isVisible(),
                        "розкривачка налаштувань видима")
        self.assertTrue(
            bool(page._settings_toggle.property("disclosure")),
            "toggle без property(disclosure) — QSS-межа/padding (теми.py) "
            "не застосуються, і розкривач знову стане тонким рядком")

        page._settings_toggle.setChecked(True)
        for _ in range(2):
            self._app.processEvents()
        self.assertTrue(page._settings_panel.isVisible(),
                        "клік по розкривачу мусить показати панель")
        self.assertTrue(page._source.isVisible())
        self.assertTrue(page._fps.isVisible())

        page._settings_toggle.setChecked(False)
        for _ in range(2):
            self._app.processEvents()
        self.assertFalse(page._settings_panel.isVisible(),
                         "повторний клік мусить згорнути панель назад")

    def test_single_start_button_uses_video_icon(self):
        """Хвиля 'Запис-полірування': одна текстова кнопка з відео-іконкою в шапці."""
        page, _ = self._page()
        self.assertFalse(page._rec_action.icon().isNull())
        self.assertFalse(hasattr(page, "_rec"), "Окрема кругла кнопка вилучена")

    def test_start_button_disabled_when_no_source(self):
        """Кнопка 'Почати запис' disabled при 'Не знайдено екранів' з тултіпом."""
        page, _ = self._page(has_sources=False)
        self.assertFalse(page._rec_action.isEnabled())
        self.assertEqual(page._rec_action.toolTip(),
                         "Оберіть джерело для запису екрана")

    def test_explicit_start_stop_button_tracks_state(self):
        """Явна текстова кнопка «Почати запис» ↔ «Зупинити запис», синхронна зі станом."""
        page, _ = self._page(has_sources=True)
        self.assertTrue(page._rec_action.isEnabled())
        self.assertEqual(page._rec_action.text(), "Почати запис")
        page._state("recording")
        for _ in range(2):
            self._app.processEvents()
        self.assertEqual(page._rec_action.text(), "Зупинити запис")
        self.assertEqual(page._rec_action.accessibleName(), "Зупинити запис")
        page._state("idle")
        self.assertEqual(page._rec_action.text(), "Почати запис")

    def test_format_dropdown_shows_webm(self):
        """Формат 'webm' показується як 'WebM'."""
        page, _ = self._page()
        texts = [page._format.itemText(i) for i in range(page._format.count())]
        self.assertIn("WebM", texts)
        self.assertNotIn("WEBM", texts)

    def test_unimplemented_area_source_is_not_offered(self):
        """Не показуємо “Ділянку”, доки інтерфейс справжнього вибору не реалізовано."""
        page, _ = self._page()
        kinds = {button.property("kind") for button in page._kind.buttons()}
        self.assertNotIn("rect", kinds)

    def test_unimplemented_area_is_not_promised_in_subtitle(self):
        self.assertNotIn("частини екрана", STRINGS["uk"]["screen_subtitle"].lower())
        self.assertNotIn("area", STRINGS["en"]["screen_subtitle"].lower())

    def test_recording_card_has_always_visible_action_bar(self):
        """Дефект живого тесту 30.07: над записом екрана НЕ БУЛО жодної дії,
        крім «Дивитися відео» — людина мусила йти в провідник, щоб перейменувати
        чи видалити невдалий дубль. RecordActionBar додає рядок
        дій, кнопки якого не приховані за hover (канон п.4: сенсорний екран/
        клавіатура/чужий ноутбук)."""
        from fronts.desktop.record_action_bar import RecordActionBar
        page, ctl = self._page(recordings=[Path("C:/rec/screen-20260730-1200.webm")])
        bars = page.findChildren(RecordActionBar)
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        for btn in (bar.rename_btn, bar.folder_btn, bar.share_btn, bar.delete_btn):
            self.assertTrue(btn.isVisibleTo(page), "кнопка дії мусить бути видима без наведення")

    def test_recording_card_rename_updates_label_and_bar_state(self):
        from fronts.desktop.record_action_bar import RecordActionBar
        path = Path("C:/rec/screen-20260730-1200.webm")
        page, ctl = self._page(recordings=[path])
        bar = page.findChildren(RecordActionBar)[0]
        bar.rename_requested.emit("нарада-липень")
        self.assertEqual(ctl.renamed, [(path, "нарада-липень")])
        card = bar.parentWidget()
        label = card.findChildren(QLabel)[0]
        self.assertEqual(label.text(), "нарада-липень.webm")

    def test_recording_card_delete_refreshes_list(self):
        from fronts.desktop.record_action_bar import RecordActionBar
        path = Path("C:/rec/screen-20260730-1200.webm")
        page, ctl = self._page(recordings=[path])
        bar = page.findChildren(RecordActionBar)[0]
        bar.delete_requested.emit()
        self._flush_deferred(self._app)
        self.assertEqual(ctl.deleted, [path])
        self.assertEqual(page.findChildren(RecordActionBar), [])

    def test_recording_card_show_in_folder_targets_this_file(self):
        from fronts.desktop.record_action_bar import RecordActionBar
        path = Path("C:/rec/screen-20260730-1200.webm")
        page, ctl = self._page(recordings=[path])
        bar = page.findChildren(RecordActionBar)[0]
        bar.show_in_folder_requested.emit()
        self.assertEqual(ctl.shown_in_folder, [path])

    def test_render_screen_page(self):
        page, _ = self._page()
        pix = page.grab()
        self.assertFalse(pix.isNull())

    def test_empty_state_visible_with_no_recordings(self):
        """Аудит 31.07 (критичний дефект): без жодного запису сторінка
        раніше лишала голе поле без іконки й тексту. Тепер — спільний
        EmptyState видно на index 0 стеку."""
        page, _ = self._page(recordings=[])
        self.assertEqual(page._stack.currentIndex(), 0)
        self.assertFalse(page._empty.isHidden())
        self.assertTrue(page._empty.title_label.text())
        self.assertTrue(page._empty.hint_label.text())

    def test_empty_state_hidden_once_first_recording_exists(self):
        page, _ = self._page(recordings=[Path("C:/rec/screen-20260731.webm")])
        self.assertEqual(page._stack.currentIndex(), 1,
                         "перший запис мав прибрати порожній стан і показати список")

    def test_empty_state_button_starts_recording_same_as_header_button(self):
        """Кнопка першого кроку в порожньому стані веде на ту саму дію, що
        велика кнопка запису в шапці (не в нікуди)."""
        page, ctl = self._page(has_sources=True, recordings=[])
        self.assertTrue(page._empty.button.isEnabled())
        page._empty.button.click()
        for _ in range(2):
            self._app.processEvents()
        self.assertEqual(len(ctl.started), 1)
        self.assertTrue(page._recording)

    def test_empty_state_button_disabled_when_no_source(self):
        page, _ = self._page(has_sources=False, recordings=[])
        self.assertFalse(page._empty.button.isEnabled())

    def test_toggle_without_source_shows_visible_error_not_silent(self):
        """Аудит 'тихі відмови' №2: клік без джерела мусить дати видимий бейдж
        помилки й НЕ звертатись до контролера, а не мовчки нічого не робити."""
        page, ctl = self._page(has_sources=False)
        page._toggle()
        for _ in range(2):
            self._app.processEvents()
        self.assertEqual(page._badge._kind, "error")
        self.assertEqual(ctl.started, [])
        self.assertFalse(page._recording)

    def test_failed_start_reflects_as_error_badge_not_silence(self):
        """Аудит №2: коли controller.screen_record_start повертає False (рушій
        відхилив старт), кнопка НЕ повинна лишати бейдж у 'очікуванні' —
        сторінка отримує screen_record_error і показує чесний стан помилки."""
        page, ctl = self._page(has_sources=True, start_result=False,
                                start_error="Помилка: рушій відхилив старт")
        page._toggle()
        for _ in range(2):
            self._app.processEvents()
        self.assertEqual(len(ctl.started), 1)
        self.assertFalse(page._recording)
        self.assertEqual(page._badge._kind, "error")

    def test_successful_start_still_shows_recording_state(self):
        """Контроль: успішний старт (мутація НЕ мала б зламати штатний шлях)."""
        page, ctl = self._page(has_sources=True, start_result=True)
        page._toggle()
        for _ in range(2):
            self._app.processEvents()
        self.assertEqual(len(ctl.started), 1)
        self.assertTrue(page._recording)

    def test_detailed_error_before_finished_signal_is_not_overwritten(self):
        """Аудит чесності (31.07, знахідка 4): якщо конкретна причина збою
        (screen_record_error) прийшла ПЕРЕД сигналом завершення
        (screen_record_finished(ok=False)), _finished не мав права затерти
        її загальним "screen_failed" — людина мусить бачити СПРАВЖНЮ причину."""
        from fronts.desktop.i18n import tr
        page, ctl = self._page(has_sources=True, start_result=True)
        page._toggle()                     # старт: скидає _error_shown
        for _ in range(2):
            self._app.processEvents()

        detailed = "Диск переповнено під час запису"
        ctl.screen_record_error.emit(detailed)
        for _ in range(2):
            self._app.processEvents()
        self.assertEqual(page._badge._text, "Помилка: " + detailed)

        # Помилка ПЕРЕД завершенням — той самий порядок сигналів з аудиту.
        ctl.screen_record_finished.emit("C:/rec/a.webm", False)
        for _ in range(2):
            self._app.processEvents()

        self.assertEqual(page._badge._kind, "error")
        self.assertEqual(page._badge._text, "Помилка: " + detailed)
        self.assertNotEqual(page._badge._text, "Не вдалося записати")

    def test_finished_failure_without_prior_error_still_shows_generic(self):
        """Регресія: якщо НЕ було screen_record_error взагалі, старий
        загальний текст лишається чесним фолбеком (не ламаємо цей шлях)."""
        from fronts.desktop.i18n import tr
        page, ctl = self._page(has_sources=True, start_result=True)
        page._toggle()
        for _ in range(2):
            self._app.processEvents()

        ctl.screen_record_finished.emit("C:/rec/a.webm", False)
        for _ in range(2):
            self._app.processEvents()

        self.assertEqual(page._badge._kind, "error")
        self.assertEqual(page._badge._text, "Не вдалося записати")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(ScreenRenderTests))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
