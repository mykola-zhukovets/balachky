"""Smoke-рендер навігації головного вікна — ОКРЕМИЙ процес (як render_meeting_smoke).

Ловить клас багів «кнопка N відкриває чужу сторінку»: стек QStackedWidget має
містити РІВНО стільки сторінок, скільки пунктів у _PAGES, і в тому самому
порядку. Пропустити одну сторінку в __init__ (як було зі ScreenPage) зсуває всі
наступні — «Запис екрана» відкривав «Історію».

Чому окремо від основного `unittest discover` (патерн `test*.py` не матчить
`render_*.py`): будуємо ЖИВЕ MainWindow з усіма сторінками (у них QTimer —
LevelMeter, _tick тощо). У спільному процесі недобитий Qt-таймер під час
static-деструкції offscreen-Qt на виході давав флакі-краш 0xC000041D. Тут
teardown жорсткий: спиняємо всі QTimer, close -> deleteLater -> флаш.

    python -m unittest tests.render_nav_smoke              # звичайний прогін
    python -m unittest discover -s tests -p "render_*.py"  # discover-варіант
    python tests/render_nav_smoke.py                       # standalone-раннер
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace

# Віджети без екрана: рендеру потрібен QApplication, не реальний екран.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# standalone-запуск (python tests/render_nav_smoke.py): корінь репо у sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import QObject, Signal

from whisper_core import profiles
from whisper_core.config import Config


def _make_sandbox() -> Path:
    """Тимчасовий profiles/-корінь: один фейковий словник (без даних користувача)."""
    tmp = Path(tempfile.mkdtemp(prefix="nav-smoke-"))
    proot = tmp / "profiles"
    default = proot / "default"
    default.mkdir(parents=True)
    (default / "terms.toml").write_text('[terms]\nGitHub = ["гітхаб"]\n',
                                        encoding="utf-8")
    now = round(time.time())
    (default / "history.jsonl").write_text(
        json.dumps({"ts": now, "raw": "тест", "final": "тест",
                    "source": "desktop"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (default / "profile.json").write_text('{"memory": true}', encoding="utf-8")
    (proot / "state.json").write_text('{"active": "default"}', encoding="utf-8")
    return tmp


class _FakeRecorder:
    def take_meter(self):
        return 0.06, 0.22


class _FakeTray:
    def sync_animations(self):
        pass

    def notify(self, _text):
        pass


class _NavController(QObject):
    """Мінімальний контракт DesktopApp для MainWindow (адаптовано зі
    scripts/screenshots.py + сигнали/методи вкладок «Нарада» й «Запис екрана»).
    Слоти — no-op: тест лише будує сторінки й перемикає їх, не діє на контролер."""

    # диктування / файли
    transcribed = Signal(str, str, object)
    file_status = Signal(int, str)
    file_done = Signal(int, str, str, object, object)  # +words (під-хвиля 2)
    rec_state = Signal(str)
    # налаштування
    key_captured = Signal(str)
    note_key_captured = Signal(str)
    command_edit_key_captured = Signal(str)
    panic_lock_key_captured = Signal(str)
    mic_test_result = Signal(str)
    update_result = Signal(object)
    download_progress = Signal(int, int)   # feature/auto-update
    update_downloaded = Signal(str)
    download_failed = Signal(str)
    # нарада
    meeting_state = Signal(str)
    meeting_audio_ready = Signal(str)
    meeting_session_done = Signal(str, object)
    meeting_error = Signal(str, str)
    meeting_storage_warning = Signal(str, float)
    meeting_bookmark_key_captured = Signal(str)
    # запис екрана
    screen_record_state = Signal(str)
    screen_record_error = Signal(str)
    screen_record_finished = Signal(str, bool)

    def __init__(self, sandbox: Path):
        super().__init__()
        self.cfg = Config()
        self.cfg.model_name = "large-v3-turbo"
        self.profile = profiles.get_active(sandbox)
        self.output_mode = "paste"
        self.recorder = _FakeRecorder()
        self.tray = _FakeTray()
        self._jobs = 0
        self._capturing = False
        self._mic_testing = False
        self._dictaphone_started = 0
        self._ctx_resolver = None

    # --- диктування / профілі / файли ---
    def switch_profile(self, name): pass
    def toggle_memory(self, on): pass
    def reset_memory(self): pass
    def reload_terms(self): pass
    def save_config(self): pass
    def set_model(self, name): pass
    def set_language(self, code): pass
    def set_device(self, dev): pass
    def set_compute_type(self, ctype): pass
    def set_model_idle_unload(self, seconds): pass
    def set_input_device(self, name): pass
    def set_model_dir(self, path): pass
    def start_key_capture(self): pass
    def record_start(self): pass
    def record_stop(self): pass
    def record_cancel(self): pass
    def record_pause(self, on): pass
    def check_updates_now(self): pass
    def restart_app(self): pass
    def enqueue_file(self, path): self._jobs += 1; return self._jobs
    def update_state(self): return "1.0.0", "1.0.0", "https://example/rel", False
    def delivery_state(self): return None, None, None   # feature/auto-update
    def start_installer_download(self, *a): pass
    def launch_installer_and_quit(self, *a): pass
    def open_recordings_folder(self): pass
    def open_corpus_folder(self): pass
    def corpus_count(self): return 0
    def save_corpus_sample(self, *a, **k): return None
    def dictaphone_level(self): return (0.0, 0.0)
    def dictaphone_start(self): pass
    def dictaphone_stop(self): pass
    def dictaphone_cancel(self): pass
    def list_recordings(self): return []
    def transcribe_recording(self, *a): pass
    def delete_recording(self, *a): pass
    def update_file_transcript(self, *a): pass
    def installed_model_names(self): return []

    # --- нарада ---
    def list_meeting_screen_monitors(self): return []
    def list_meetings(self): return []
    def build_search_index(self):
        from whisper_core.search_index import SearchIndex
        return SearchIndex.build()
    def set_live_transcription(self, on): pass
    def set_meeting_screen_enabled(self, on): pass
    def set_meeting_screen_monitor(self, m): pass

    # --- запис екрана ---
    def open_screen_recordings_folder(self): pass
    def list_screen_monitors(self): return []
    def list_screen_windows(self): return []
    def list_screen_recordings(self): return []

    # --- налаштування (усі під'єднуються до кнопок/полів у __init__) ---
    def reset_ptt_key(self, *a, **k): pass
    def set_ptt_mouse_button(self, *a, **k): pass
    def set_output_device(self, *a, **k): pass
    def set_autostop_silence(self, *a, **k): pass
    def set_max_duration(self, *a, **k): pass
    def set_action_hotkey(self, *a, **k): pass
    def start_note_key_capture(self, *a, **k): pass
    def start_command_edit_key_capture(self, *a, **k): pass
    def clear_command_edit_hotkey(self, *a, **k): pass
    def clear_note_hotkey(self, *a, **k): pass
    def start_panic_key_capture(self, *a, **k): pass
    def clear_panic_lock_hotkey(self, *a, **k): pass
    def set_screen_protection(self, *a, **k): pass
    def start_mic_test(self, *a, **k): pass
    def set_vad(self, *a, **k): pass
    def set_noise_gate(self, *a, **k): pass
    def set_agc(self, *a, **k): pass
    def set_ptt_mode(self, *a, **k): pass
    def set_meeting_dir(self, *a, **k): pass
    def start_meeting_bookmark_key_capture(self, *a, **k): pass
    def clear_meeting_bookmark_hotkey(self, *a, **k): pass
    def set_player_backstep(self, *a, **k): pass
    def reload_context_profiles(self, *a, **k): pass
    def set_auto_export_enabled(self, *a, **k): pass
    def set_auto_export_dir(self, *a, **k): pass
    def set_auto_export_format(self, *a, **k): pass
    def set_obsidian_enabled(self, *a, **k): pass
    def set_obsidian_dir(self, *a, **k): pass
    def set_obsidian_filename_template(self, *a, **k): pass
    def set_watch_enabled(self, *a, **k): pass
    def set_watch_dir(self, *a, **k): pass
    def export_settings_to(self, *a, **k): pass
    def import_settings_from(self, *a, **k): pass
    def set_log_level(self, *a, **k): pass
    def set_ui_language(self, *a, **k): pass
    def set_paste_confirm_sound(self, *a, **k): pass
    def set_quiet_hours(self, *a, **k): pass


class NavSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()                          # як у реальному main(): ДО QSS
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))  # без живих таймерів
        cls._sandbox = _make_sandbox()
        # vocab.refresh() кличе profiles.list_profiles(ROOT) — на пісочницю
        cls._orig_list = profiles.list_profiles
        profiles.list_profiles = lambda root=None: cls._orig_list(cls._sandbox)

    @classmethod
    def tearDownClass(cls):
        profiles.list_profiles = cls._orig_list
        # глобальний годинник пілюль (glass._TAG_DRIVER): явно спиняємо й чистимо,
        # щоб на виході не лишалось ні таймера, ні посилань на знесені C++-пілюлі.
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
            # ПЕРШИМ — спинити ВСІ таймери вікна й сторінок (активний таймер під час
            # деструкції = 0xC000041D), аж ПОТІМ close -> deleteLater -> флаш.
            for t in win.findChildren(QTimer):
                try:
                    t.stop()
                except RuntimeError:
                    pass                       # C++-частина вже знесена
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

    def test_stack_matches_nav_order_and_each_button_opens_its_page(self):
        from fronts.desktop.main_window import _PAGES, DictationPage, FilesPage
        from fronts.desktop.pages.meeting import MeetingPage
        from fronts.desktop.pages.screen import ScreenPage
        from fronts.desktop.pages.history import HistoryPage
        from fronts.desktop.pages.vocab import VocabPage
        from fronts.desktop.pages.settings import SettingsPage
        from fronts.desktop.pages.search import SearchPage

        win = self._window()

        # (а) стільки сторінок, скільки пунктів навігації
        self.assertEqual(win.pages.count(), len(_PAGES))

        # (б) строгий порядок класів у стеку
        expected = [DictationPage, FilesPage, MeetingPage, ScreenPage,
                    HistoryPage, VocabPage, SettingsPage, SearchPage]
        actual = [type(win.pages.widget(i)) for i in range(win.pages.count())]
        self.assertEqual(actual, expected)

        # (в) клік по кожному пункту → відкривається саме його сторінка
        for i in range(len(_PAGES)):
            win.set_page(i)
            self.assertEqual(win.pages.currentIndex(), i)

    def test_search_nav_button_opens_search_page(self):
        """feature/global-search: пункт «Пошук» веде САМЕ на SearchPage (а не на
        чужу сторінку через зсув індексів). Клік по кнопці «Пошук» у _PAGES →
        поточна сторінка стека є SearchPage."""
        from fronts.desktop.main_window import _PAGES
        from fronts.desktop.pages.search import SearchPage

        win = self._window()
        search_index = next(i for i, (_ic, key) in enumerate(_PAGES)
                            if key == "nav_search")
        # клік по кнопці навігації (той самий шлях, що й у користувача)
        win._nav.button(search_index).click()
        self.assertEqual(win.pages.currentIndex(), search_index)
        self.assertIsInstance(win.pages.currentWidget(), SearchPage)

    def test_search_result_click_navigates_to_source(self):
        """feature/global-search: результат клікабельний → відкриває джерело.
        Диктування веде на «Історію» з підставленим запитом. Кнопку «Відкрити»
        знаходимо у стрічці результатів і реально клікаємо."""
        from fronts.desktop.main_window import _PAGES
        from fronts.desktop.pages.history import HistoryPage
        from fronts.desktop.glass import GlassButton
        from whisper_core.search_index import SearchIndex, SearchDoc, KIND_DICTATION
        from fronts.desktop.i18n import tr

        win = self._window()
        ctrl = win.controller
        ctrl.window = win                     # навігація SearchPage потребує вікна
        doc = SearchDoc(kind=KIND_DICTATION, ref='{"final":"кавунова нота"}',
                        title="", date=1000.0, text="кавунова нота диктування")
        ctrl.build_search_index = lambda: SearchIndex([doc])

        search_i = next(i for i, (_ic, k) in enumerate(_PAGES) if k == "nav_search")
        hist_i = next(i for i, (_ic, k) in enumerate(_PAGES) if k == "nav_history")
        win.set_page(search_i)
        page = win.search
        page._rebuild_index()                 # детерміновано, без дебаунсу/showEvent
        page._search.setText("кавунова")
        page._run_search()
        self._app.processEvents()

        # картка-результат із кнопкою «Відкрити»
        opens = [b for b in page.findChildren(GlassButton)
                 if b.text() == tr("search_open")]
        self.assertTrue(opens, "не знайдено кнопку «Відкрити» в результатах")
        opens[0].click()
        self._app.processEvents()

        # відкрилась саме «Історія», і запит підставлено у її поле пошуку
        self.assertEqual(win.pages.currentIndex(), hist_i)
        self.assertIsInstance(win.pages.currentWidget(), HistoryPage)
        self.assertEqual(win.history._search.text(), "кавунова")

    def test_vocab_header_no_clipping_at_1000px(self):
        """Шапка «Словники» на ширині 1000px: жодна кнопка дій не обрізана
        (width ≥ sizeHint), а підзаголовок сторінки — широкий, не стиснутий у
        колонку по слову (канон DESIGN-TYPOGRAPHY §3, п.5). Регрес-запобіжник
        до багу, коли 5 кнопок в одному ряду з шапкою різали підписи посеред слова."""
        from PySide6.QtWidgets import QLabel

        win = self._window()
        win.resize(1000, 720)
        win.show()
        win.set_page(5)                    # індекс «Словники» у _PAGES
        self._app.processEvents()

        # (а) повні підписи кнопок дій — жодного обрізання
        for b in win.vocab._header_buttons:
            self.assertGreaterEqual(
                b.width(), b.sizeHint().width(),
                f"кнопку шапки обрізано: '{b.text()}' "
                f"({b.width()} < {b.sizeHint().width()})")

        # (б) підзаголовок сторінки тягнеться вширину, не стиснутий у колонку
        subs = [w for w in win.vocab.findChildren(QLabel)
                if w.property("pagesub")]
        self.assertTrue(subs, "не знайдено підзаголовок сторінки (pagesub)")
        self.assertGreater(
            subs[0].width(), 300,
            f"підзаголовок стиснуто у колонку: width={subs[0].width()}")

    def test_vocab_scrolls_no_vertical_clipping_at_1080p(self):
        """Сторінка «Словники» вища за клієнтську область на 1080p (вміст ~1153px
        min > ~1044px). Без скролу QStackedWidget тиснув вміст нижче мінімуму й
        РІЗАВ рядки-картки словників (§1 рубрики: літери зрізані рамкою). Запобіжник:
        (а) вміст загорнутий у QScrollArea → переповнення прокручується; (б) жоден
        рядок-картка не стиснутий нижче свого minimumSizeHint (тобто не зрізаний)."""
        from PySide6.QtWidgets import QScrollArea
        from fronts.desktop.pages.vocab import _ProfileRow

        win = self._window()
        win.resize(1280, 1044)            # клієнтська висота як на 1080p
        win.show()
        win.set_page(5)                   # «Словники»
        self._app.processEvents()

        self.assertIsNotNone(
            win.vocab.findChild(QScrollArea),
            "вміст «Словники» не в QScrollArea — переповнення ріже вміст, не прокручує")
        for row in win.vocab.findChildren(_ProfileRow):
            self.assertGreaterEqual(
                row.height(), row.minimumSizeHint().height(),
                f"рядок-картку словника зрізано по висоті: '{row.accessibleName()}' "
                f"({row.height()} < {row.minimumSizeHint().height()})")

    def test_glassbutton_disabled_is_visibly_dimmer(self):
        """Вимкнена скляна кнопка мусить читатись як вимкнена (рубрика §14).
        Раніше disabled-текст брав IDLE (#D6CDB8) ≈ enabled TEXT_BODY (#E6E5D1) —
        різниця на око непомітна. Тепер disabled — тьмяний (низька альфа), як QSS
        `QPushButton:disabled`. Перевіряємо, що ефективна яскравість (luma×alpha)
        помітно нижча за enabled."""
        from fronts.desktop.glass import GlassButton

        b = GlassButton("Тест")
        b.setEnabled(True)
        en = b._content_color()
        b.setEnabled(False)
        dis = b._content_color()

        def eff(c):
            luma = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
            return luma * c.alphaF()

        self.assertLess(
            eff(dis), eff(en) * 0.6,
            f"disabled недостатньо тьмяний: eff(dis)={eff(dis):.0f} "
            f"vs eff(en)={eff(en):.0f} (мусить бути <60%)")

    def test_dictation_output_row_no_clipping_at_1000px(self):
        """Рядок «Куди текст» на ширині 1000px: усі три кнопки режимів виводу
        (Вставляти біля курсора / Показувати у вікні / Вставляти й показувати)
        вміщаються повністю (width ≥ sizeHint) — підписи не обрізані. Регрес-
        запобіжник до багу, коли п'ята кнопка «Заповнити шаблон» переповнювала
        ряд і різала підписи сусідів посеред слова (канон DESIGN-TYPOGRAPHY §3)."""
        win = self._window()
        win.resize(1000, 720)
        win.show()
        win.set_page(0)                    # індекс «Диктування» у _PAGES
        self._app.processEvents()

        for b in win.dictation._modes.buttons():
            self.assertGreaterEqual(
                b.width(), b.sizeHint().width(),
                f"кнопку режиму виводу обрізано: '{b.text()}' "
                f"({b.width()} < {b.sizeHint().width()})")

    def test_settings_nav_moved_to_bottom_and_opens_settings(self):
        """№12: «Налаштування» винесено ВНИЗ сайдбара, окремо від контентних
        кнопок (не в спільній навколонці), але лишається у групі з правильним
        id → клік відкриває саме SettingsPage. Tab-order будується без падінь."""
        from fronts.desktop.main_window import _PAGES
        from fronts.desktop.pages.settings import SettingsPage

        win = self._window()
        set_i = next(i for i, (_ic, k) in enumerate(_PAGES) if k == "nav_settings")
        btn = win._settings_nav_btn
        self.assertIsNotNone(btn, "кнопка «Налаштування» не збережена окремо")
        # кнопка НЕ в контентній навколонці (її батько-лейаут — не navcol)
        self.assertIs(win._nav.button(set_i), btn,
                      "id кнопки «Налаштування» більше не відповідає індексу сторінки")
        btn.click()
        self.assertEqual(win.pages.currentIndex(), set_i)
        self.assertIsInstance(win.pages.currentWidget(), SettingsPage)

    def test_sidebar_tab_order_follows_visual_order_with_settings_last(self):
        """Винесення Settings униз не має ламати клавіатурну навігацію.

        Перевіряємо реальний Qt focus chain, а не порядок ``_PAGES`` чи layout:
        усі видимі nav-кнопки проходяться згори вниз, «Налаштування» — остання.
        """
        win = self._window()
        win.resize(1000, 720)
        win.show()
        self._app.processEvents()

        nav_buttons = set(win._nav.buttons())
        focused = []
        seen = set()
        widget = win.nextInFocusChain()
        while widget is not None and id(widget) not in seen:
            seen.add(id(widget))
            if widget in nav_buttons:
                focused.append(widget)
            widget = widget.nextInFocusChain()

        visual = sorted(nav_buttons,
                        key=lambda b: b.mapToGlobal(b.rect().topLeft()).y())
        self.assertEqual(focused, visual,
                         "Tab перестрибує між верхом і низом сайдбара")
        self.assertIs(focused[-1], win._settings_nav_btn,
                      "нижні «Налаштування» мають бути останніми в nav tab-order")

    def test_settings_model_selection_reaches_retry_model_menu(self):
        """SettingsPage → DesktopApp cfg → FilesPage «інша модель».

        Це наскрізний регрес для small/medium/власної: вибір у живому комбо має
        стати активною моделлю контролера, а меню повторної розшифровки мусить
        одразу її побачити навіть у порожньому модельному кеші.
        """
        from fronts.desktop.app import DesktopApp
        from fronts.desktop import main_window as main_window_mod
        from fronts.desktop.main_window import MainWindow

        controller = _NavController(self._sandbox)
        empty_cache = self._sandbox / "empty-model-cache"
        empty_cache.mkdir(exist_ok=True)
        controller.cfg.model_dir = str(empty_cache)
        controller.cfg.save = lambda *a, **k: None
        controller.set_model = MethodType(DesktopApp.set_model, controller)
        controller.installed_model_names = MethodType(
            DesktopApp.installed_model_names, controller)
        win = MainWindow(controller)
        self._win = win

        class _Signal:
            def __init__(self):
                self.callback = None

            def connect(self, callback):
                self.callback = callback

            def emit(self):
                self.callback()

        class _Action:
            def __init__(self, text):
                self.text = text
                self.enabled = True
                self.triggered = _Signal()

            def setEnabled(self, enabled):
                self.enabled = enabled

        class _Menu:
            last_actions = []

            def __init__(self, _parent):
                type(self).last_actions = []

            def addAction(self, text):
                action = _Action(text)
                type(self).last_actions.append(action)
                return action

            def exec(self, *_args):
                pass

        original_menu = main_window_mod.QMenu
        main_window_mod.QMenu = _Menu
        retried = []
        win.files.add_files = lambda paths, model=None: retried.append(
            (list(paths), model))
        try:
            for model_name in ("small", "medium", "owner/custom-model"):
                with self.subTest(model=model_name):
                    if win.settings._model.findData(model_name) < 0:
                        win.settings._select_custom_model(model_name)
                    else:
                        win.settings._model.setCurrentIndex(
                            win.settings._model.findData(model_name))
                    self._app.processEvents()

                    self.assertEqual(controller.cfg.model_name, model_name)
                    self.assertEqual(controller.installed_model_names(),
                                     [model_name])
                    win.files._retry_model_menu(win.settings._model, Path(__file__))
                    self.assertEqual(len(_Menu.last_actions), 1,
                                     f"меню не бачить активну модель {model_name}")
                    action = _Menu.last_actions[0]
                    self.assertTrue(action.enabled)
                    action.triggered.emit()
                    self.assertEqual(retried[-1][1], model_name,
                                     "дія меню передала не ту STT-модель")
        finally:
            main_window_mod.QMenu = original_menu

    def test_sidebar_header_is_clickable_about_hub(self):
        """№13: шапка сайдбара — ClickableFrame з accessibleName; клік будує
        інформаційний хаб «Про програму» (exec замокано, щоб не блокувати тест)."""
        from fronts.desktop.i18n import tr
        from fronts.desktop.main_window import ClickableFrame
        from fronts.desktop import about as about_mod

        win = self._window()
        frames = [f for f in win.findChildren(ClickableFrame)
                  if f.accessibleName() == tr("about_open")]
        self.assertTrue(frames, "шапка сайдбара не клікабельна / без accessibleName")

        built = {}
        orig_exec = about_mod.AboutDialog.exec

        def _no_block(self):
            built["dlg"] = self
            return 0

        about_mod.AboutDialog.exec = _no_block
        try:
            frames[0].clicked.emit()
        finally:
            about_mod.AboutDialog.exec = orig_exec
        dlg = built.get("dlg")
        self.assertIsNotNone(dlg, "клік по шапці не відкрив «Про програму»")
        # зовнішній лінк на репозиторій: відкривається у браузері + має ім'я
        from PySide6.QtWidgets import QLabel
        gh = [l for l in dlg.findChildren(QLabel)
              if l.accessibleName() == tr("about_github")]
        self.assertTrue(gh, "у хабі нема лінка на GitHub з accessibleName")
        self.assertTrue(gh[0].openExternalLinks(),
                        "лінк GitHub не відкриває зовнішнє (setOpenExternalLinks)")
        dlg.setParent(win)   # приберемо разом із вікном (стоп-таймери в tearDown)

    def test_model_card_has_about_link_for_preset_not_for_local(self):
        """№7: картка пресета несе клікабельний «Про модель ↗» (https, у браузер,
        з accessibleName); локальний файл — без лінка (нема сторінки в мережі)."""
        from PySide6.QtWidgets import QLabel
        from fronts.desktop.i18n import tr
        from whisper_core import paths
        from whisper_core.protocol import model_manager as mm

        win = self._window()
        root = paths.protocol_models_dir()

        # пресет: лінк є, веде на HF-сторінку моделі
        r_preset = mm.resolve("fast", root, [])
        card = win.meeting._build_model_card(r_preset, None, "quality")
        card.setParent(win)
        label = tr("protocol_model_fast")
        about = [l for l in card.findChildren(QLabel)
                 if l.accessibleName() == tr("protocol_model_about_name", label=label)]
        self.assertTrue(about, "картка пресета без лінка «Про модель»")
        self.assertTrue(about[0].openExternalLinks())
        self.assertIn("https://huggingface.co/", about[0].text())

        # локальний файл: жодного лінка «Про модель»
        cm = mm.CustomModel(id="custom_l", label="Локальна",
                            kind=mm.CUSTOM_KIND_LOCAL, path=r"C:\x\a.gguf")
        r_local = mm.resolve("custom_l", root, [cm])
        card_l = win.meeting._build_model_card(r_local, cm, "fast")
        card_l.setParent(win)
        local_about = [l for l in card_l.findChildren(QLabel)
                       if l.text().startswith("<a") and "huggingface" in l.text()]
        self.assertEqual(local_about, [],
                         "локальна модель не мусить мати мережевий лінк")

    def test_screen_badge_states_never_crash(self):
        """Бейдж ScreenPage — StatusTag (set_state), НЕ QLabel: усі стани запису
        (recording/idle/error/finished) мусять оновлювати пілюлю без AttributeError
        (setText на StatusTag). Прожен у межах блокера «Запис екрана»."""
        win = self._window()
        badge = win.screen._badge

        win.screen._state("recording")
        self.assertEqual(badge._kind, "busy")
        win.screen._state("idle")
        self.assertEqual(badge._kind, "queued")
        win.screen._error("boom")
        self.assertEqual(badge._kind, "error")
        win.screen._finished(Path("x.mp4"), True)
        self.assertEqual(badge._kind, "done")
        win.screen._finished(Path("x.mp4"), False)
        self.assertEqual(badge._kind, "error")


if __name__ == "__main__":
    # Standalone-раннер: після ВЕРДИКТУ виходимо os._exit — навмисно обходимо
    # нативну static-деструкцію offscreen-Qt (джерело флакі-0xC000041D ПІСЛЯ «OK»).
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(NavSmokeTests))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
