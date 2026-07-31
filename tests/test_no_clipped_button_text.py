"""Вартовий: жодна видима текстова кнопка не обрізана на реалістичних ширинах.

Живий дефект від власника (31.07, коміт b4b9b2b): рядок дій над записом
(fronts/desktop/record_action_bar.py) стискався в QHBoxLayout нижче свого
природного розміру — текст п'яти кнопок різався просто посеред слова. Фікс —
FlowLayout (перенос рядка). Візуальний гейт (scripts/visual_gate.py) цього
НЕ ловить: він звіряє знімки з еталоном, а не факт обрізання тексту.

Як міряємо чесно (той самий прийом, що вже ловив реальний баг у
tests/render_nav_smoke.py — test_vocab_header_no_clipping_at_1000px,
test_dictation_output_row_no_clipping_at_1000px): порівнюємо ФАКТИЧНУ ширину
кнопки (button.width(), після реального layout-проходу на реальній ширині
вікна) з button.sizeHint().width(). GlassButton.sizeHint() рахує
QFontMetrics.horizontalAdvance(text) + місце під іконку + запас на padding
(glass.py, _ICON=18 + 40px запасу) — тобто "скільки треба, щоб підпис НЕ
торкався краю". Для звичайних QPushButton sizeHint() дає той самий контракт
через стиль Qt. Якщо layout стиснув кнопку нижче цього — текст обрізається
(підтверджено мутацією: тимчасовий відкат record_action_bar.py на QHBoxLayout
червонить саме цей тест з підписом «recact_rename»/«recact_share» на ширині
1280 — див. звіт сесії; сам тест мутацію НЕ містить).

НЕ тавтологія: ми не порівнюємо tr(ключ) з tr(ключ) — ми порівнюємо
геометрію (px), яку рахує ДВІЖОК Qt/шрифт, з тим, що реально дав layout.

Обхід сторінок: перевикористовуємо інфраструктуру tests/render_nav_smoke.py
(_NavController, _make_sandbox) — та сама MainWindow, той самий сет фікстур,
яким уже довіряють інші render-smoke тести. Дані для сторінок, яким потрібен
непорожній стан (Аудіофайли-черга/диктофон-записи, Нарада, Запис екрана),
підставляємо тим самим способом, що й render_meeting_smoke.py /
render_screen_smoke.py / render_emptystates_smoke.py.

Швидкість: повний обхід (8 сторінок + ~7 вкладок «Налаштування») × 4 ширини
× живий MainWindow — це десятки секунд, зависокий гейт для щоденного запуску.
За замовчуванням тест бере ДВІ найвужчі ширини — 1280 і 1600 (саме на вузьких
ширинах ріже; 1440/1920 — понад ними майже завжди достатньо місця, вони не
додають нових знахідок понад 1280/1600 на цій сторінці). Повний набір
[1280, 1440, 1600, 1920] вмикається змінною середовища
BALACHKY_CLIP_GUARD_FULL=1 (той самий рецепт міг би піти в CI нічним прогоном,
якщо колись знадобиться — зараз використовується лише вручну).

ОКРЕМИЙ процес (як усі render_*_smoke): будуємо живе MainWindow з QTimer
всередині сторінок — teardown жорсткий (спиняємо всі QTimer перед deleteLater).

    python -m unittest tests.test_no_clipped_button_text        # швидкий режим
    python tests/test_no_clipped_button_text.py                 # швидкий режим
    BALACHKY_CLIP_GUARD_FULL=1 python tests/test_no_clipped_button_text.py

Розширення 31.07 (два нові класи нижче, той самий файл — один вартовий):

FirstRunWizardClippingTests — цей файл раніше обходив ЛИШЕ MainWindow;
майстер (FirstRunWizard) не перевірявся взагалі, і саме там був живий дефект
власника на встановленій 1.2.4.1: крок «Додаткові можливості», кнопка
«Завантажити обране» різалась з обох боків («авантажити обран»). Обхід
кроків — та сама інфраструктура, що вже в scripts/visual_gate.py
(scan_onboarding_wizard: живий FirstRunWizard, _stack.count() кроків,
620×460 — той самий розмір, що ставить visual_gate). Другий прогін кожного
кроку — З ІМІТАЦІЄЮ масштабу екрана.

MainWindowMinWidthAndScaleTests — регресія на win.minimumWidth() (РЕАЛЬНИЙ,
не хардкод — суд 31.07: контракт tests/test_narrow_rows_layout.py тримає
мінімум ≤1000, звужувати вікно користувачам не будемо; кнопки наради, що
різались на 1000px, полагоджено в pages/meeting.py, а не підйомом мінімуму)
+ прицільна перевірка head-рядка диктування (fronts/desktop/main_window.py
DictationPage) під імітацією масштабу — того самого рядка, що перевели на
FlowLayout.

Як імітуємо «масштаб екрана понад 100%» (обидва нові класи): множимо КОЖЕН
`font-size: Nxp` у theme.QSS на коефіцієнт і застосовуємо як стиль ПЕРЕД
побудовою вікна/майстра — `_scaled_qss()` нижче. Перевірено емпірично
(живий підбір коефіцієнта, звіт сесії): QApplication.setFont()/QT_SCALE_FACTOR
НЕ впливають на GlassButton/QLabel — вони беруть шрифт із QSS-селекторів
(theme.py `font-size: Npx`), а не з app.font(); тому єдиний спосіб реально
відтворити «текст читається крупніше, а вікно (px) — ні» — підняти px-розмір
шрифту в самому QSS, лишивши розмір вікна (win.resize) буквальним. Це і є
механізм дефекту: Windows text-scale/DPI збільшує РЕНДЕР шрифту в px, а
FlowLayout-кандидати без переносу рахують доступну ширину в тих самих px.
Калібрований множник 1.5 — підтверджено мутацією нижче: пре-фікс головний
рядок диктування і крок майстра «Додаткові можливості» червоніють саме на
ньому (звіт сесії 31.07), пост-фікс — зелені.

Друга хвиля 31.07 (та сама гілка, після розвідки на 86 порушень): під ×1.5
полагоджено ВЕСЬ майстер і всі сторінки MainWindow, крім вкладок Налаштувань:
  - майстер: рядок теки моделі і рядок кроку завантаження → FlowLayout;
    вертикальні стеки кнопок («Озвучення»/GPU) — разове калібрування
    мінімальної ширини діалога в showEvent (FlowLayout там не рятує: одна
    кнопка ширша за діалог, переносити нікуди);
  - нарада: рядок дій під транскриптом і рядок доказовості → FlowLayout;
  - диктування: радіо-рядок «Куди текст» → FlowLayout;
  - словники: рядок кнопок фраз → FlowLayout;
  - сайдбар: колонка ФІКСОВАНОЇ ширини — FlowLayout не пасує; nav-кнопки
    натомість ПЕРЕНОСЯТЬ підпис усередині (GlassButton heightForWidth +
    TextWordWrap; вертикальна політика Preferred, бо при типовому Fixed
    layout мовчки зрізає heightForWidth до одного рядка). Контракт для них
    інший: height() >= heightForWidth(width()), а не ширина.
Тому scale-тести нижче ходять ПОВНИМ обходом (майстер цілком; MainWindow —
всі сторінки, крім вкладок Налаштувань). Вкладки Налаштувань під масштабом —
окрема неторкана задача (частина тих самих 86 порушень розвідки).
"""
import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np

from tests.render_nav_smoke import _NavController, _make_sandbox

# Реалістичні ширини вікна застосунку (десктоп-монітори користувачів).
# Швидкий режим (за замовчуванням) — дві найвужчі, де реально ріже.
# BALACHKY_CLIP_GUARD_FULL=1 — усі чотири (повний обхід, повільніше).
_WIDTHS = ([1280, 1440, 1600, 1920] if os.environ.get("BALACHKY_CLIP_GUARD_FULL")
           else [1280, 1600])
_HEIGHT = 900


class _GuardController(_NavController):
    """_NavController + непорожні дані там, де сторінка інакше рендерить
    порожній стан (і жодної кнопки дій немає що перевіряти). Кожен override —
    мінімальний контракт, який реально читає відповідна сторінка (перевірено
    по коду fronts/desktop/pages/*.py і main_window.py), решта лишається
    у батька no-op."""

    def __init__(self, sandbox: Path, screen_recording, dictaphone_recording):
        super().__init__(sandbox)
        self._screen_recording = screen_recording
        self._dictaphone_recording = dictaphone_recording

    # --- Аудіофайли: стрічка збережених диктофонних записів ---
    def list_recordings(self):
        return [self._dictaphone_recording] if self._dictaphone_recording else []

    # --- Нарада: одна готова сесія (найбагатший на кнопки стан картки) ---
    def list_meetings(self):
        return [SimpleNamespace(
            id="2026-07-31_10-00-00", status="done", title=None,
            preset="onlymic", audio_files={"mic": ["audio/mic/0001.wav"]},
            processing={})]

    def meeting_session_dir(self, sid):
        return Path(sid)

    def read_meeting_utterances(self, sid):
        # ЄДИНИЙ виклик без try/except у meeting.py _fill_done_card — мусить
        # існувати й не кидати.
        return []

    def read_meeting_transcript(self, sid):
        return ""

    def protocol_model_ready(self):
        return False

    def meeting_integrity_meta(self, sid):
        from whisper_core.meeting import audit_log
        return audit_log.ChainResult(
            status=audit_log.STATUS_UNVERIFIED, event_count=0,
            audio_sha=None, events=[])

    # --- Запис екрана: один запис → RecordActionBar видимий ---
    def list_screen_recordings(self):
        return [self._screen_recording] if self._screen_recording else []


def _make_fixtures(sandbox: Path):
    """Реальний WAV (потрібен InlinePlayer — offscreen QtMultimedia не падає
    на реальному файлі, на відміну від деяких шляхів з фейковим Path)."""
    wav = None
    try:
        wav = save_recording_stub(sandbox)
    except Exception:
        wav = None
    screen_rec = Path("C:/rec/screen-20260731-1000.webm")  # як render_screen_smoke
    return screen_rec, wav


def save_recording_stub(sandbox: Path):
    """Реальний WAV на диску + whisper_core.recordings.Recording, який очікує
    DictationPage.refresh_recordings (rec.path/.duration/.name)."""
    from whisper_core import recordings
    wav = recordings.save_recording(sandbox, np.zeros(16000, dtype=np.float32), 16000)
    if wav is None:
        return None
    return recordings.Recording(
        path=wav, name=wav.name, created=wav.stat().st_mtime,
        duration=1.0, size=wav.stat().st_size)


class NoClippedButtonTextTests(unittest.TestCase):
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
        cls._screen_rec, cls._dictaphone_rec = _make_fixtures(cls._sandbox)

        from whisper_core import profiles
        cls._orig_list = staticmethod(profiles.list_profiles)
        profiles.list_profiles = lambda root=None: cls._orig_list(cls._sandbox)

    @classmethod
    def tearDownClass(cls):
        from whisper_core import profiles
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
        from fronts.desktop.i18n import current_language, set_language
        self._language = current_language()
        self.addCleanup(set_language, self._language)
        set_language("uk")
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

    def _window(self):
        from fronts.desktop.main_window import MainWindow
        controller = _GuardController(
            self._sandbox, self._screen_rec, self._dictaphone_rec)
        win = MainWindow(controller)
        self._win = win
        return win

    def _populate_files_queue(self, win):
        """Одна картка «в черзі» + одна «готова» (5 кнопок в одному ряду:
        Копіювати/Зберегти/Зберегти як/Інша модель/Імпорт субтитрів —
        найбагатший на кнопки ряд поза record_action_bar)."""
        if self._dictaphone_rec is None:
            return
        win.files.add_files([str(self._dictaphone_rec.path)])
        jid = next(iter(win.files._rows))
        win.files.controller.file_done.emit(
            jid, "Готовий текст перевірки обрізання кнопок.", "done:5", None, None)

    def _populate_search(self, win):
        from whisper_core.search_index import SearchIndex, SearchDoc, KIND_DICTATION
        doc = SearchDoc(kind=KIND_DICTATION, ref='{"final":"кавунова нота"}',
                        title="", date=1000.0, text="кавунова нота пошуку")
        win.search._index = SearchIndex([doc])
        win.search._search.setText("кавунова")
        win.search._run_search()

    def _scan_visible_buttons(self, win):
        from PySide6.QtWidgets import QPushButton
        found = []
        for btn in win.findChildren(QPushButton):
            if not btn.isVisible():
                continue
            text = (btn.text() or "").strip()
            if not text:
                continue
            found.append(btn)
        return found

    def test_no_button_text_clipped_across_all_pages_and_widths(self):
        from fronts.desktop.main_window import _PAGES

        win = self._window()
        win.show()
        self._app.processEvents()

        settings_i = next(i for i, (_ic, k) in enumerate(_PAGES)
                          if k == "nav_settings")

        failures = []

        for page_i, (_icon, key) in enumerate(_PAGES):
            win.set_page(page_i)
            self._app.processEvents()

            # непорожні дані для сторінок, де інакше нема кнопок дій
            if key == "nav_audio":
                self._populate_files_queue(win)
            elif key == "nav_search":
                self._populate_search(win)
            for _ in range(3):
                self._app.processEvents()

            if page_i == settings_i:
                tab_indices = list(range(win.settings._tabs.count()))
            else:
                tab_indices = [None]

            for tab_i in tab_indices:
                if tab_i is not None:
                    win.settings._tabs.setCurrentIndex(tab_i)
                    self._app.processEvents()
                    page_label = f"{key}/tab{tab_i}:{win.settings._tabs.tabText(tab_i)}"
                else:
                    page_label = key

                for width in _WIDTHS:
                    win.resize(width, _HEIGHT)
                    for _ in range(3):
                        self._app.processEvents()

                    for btn in self._scan_visible_buttons(win):
                        need = btn.sizeHint().width()
                        got = btn.width()
                        if got < need:
                            failures.append(
                                (page_label, btn.text(), width, need, got))

        if failures:
            lines = [
                f"  сторінка={p!r} кнопка={t!r} ширина_вікна={w}px "
                f"потрібно={need}px фактично={got}px"
                for p, t, w, need, got in failures
            ]
            self.fail(
                f"Обрізаний текст кнопок ({len(failures)} випадків):\n"
                + "\n".join(lines))


def _scaled_qss(qss: str, factor: float) -> str:
    """theme.QSS з усіма ``font-size: Npx`` помноженими на ``factor`` — імітація
    масштабу екрана понад 100% (див. пояснення у докстрінзі модуля вище)."""
    def repl(m):
        return f"font-size: {max(1, round(int(m.group(1)) * factor))}px"
    return re.sub(r"font-size:\s*(\d+)px", repl, qss)


class FirstRunWizardClippingTests(unittest.TestCase):
    """Обхід усіх кроків FirstRunWizard (raніше НЕ перевірявся цим файлом —
    лише MainWindow). Той самий контракт: button.width() >= sizeHint().width()
    на кожному видимому кроці, звичайний масштаб + масштаб ×1.5 QSS."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._base_qss = QSS
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))

    @classmethod
    def tearDownClass(cls):
        cls._app.setStyleSheet(cls._base_qss)

    def tearDown(self):
        # кожен тест може підмінити стиль масштабом — повертаємо базовий,
        # щоб не отруїти інші тестові класи в тому самому процесі QApplication
        self._app.setStyleSheet(self._base_qss)
        from PySide6.QtCore import QCoreApplication, QEvent
        for _ in range(3):
            self._app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def _build_wizard(self):
        from unittest.mock import patch
        from fronts.desktop.onboarding import FirstRunWizard
        with patch.object(FirstRunWizard, "_gpu_step_possible", return_value=True):
            wiz = FirstRunWizard()
        wiz.resize(620, 460)
        wiz.show()
        return wiz

    def _cleanup_wizard(self, wiz):
        for name in ("_detach_worker", "_detach_gpu_worker", "_detach_voice_worker",
                     "_detach_extra_worker"):
            fn = getattr(wiz, name, None)
            if fn is not None:
                try:
                    fn()
                except Exception:
                    pass
        try:
            wiz.done(0)
        except Exception:
            pass
        wiz.deleteLater()
        for _ in range(3):
            self._app.processEvents()

    def _scan_step_buttons(self, wiz, step_label, failures, width_label="620"):
        from PySide6.QtWidgets import QPushButton
        for btn in wiz.findChildren(QPushButton):
            if not btn.isVisible():
                continue
            text = (btn.text() or "").strip()
            if not text:
                continue
            need = btn.sizeHint().width()
            got = btn.width()
            if got < need:
                failures.append((step_label, text, width_label, need, got))

    def _walk_all_steps(self, scale):
        """Повний обхід усіх кроків майстра. Під ×1.5 раніше звужувався до
        кроку «Додаткові можливості»; після другої хвилі 31.07 (FlowLayout на
        кроках модель/завантаження + калібрування ширини діалога в showEvent)
        обхід повний і під масштабом — див. докстрінг модуля."""
        if scale != 1.0:
            self._app.setStyleSheet(_scaled_qss(self._base_qss, scale))
        wiz = self._build_wizard()
        self.addCleanup(self._cleanup_wizard, wiz)
        for _ in range(4):
            self._app.processEvents()

        failures = []
        extra_index = None
        for step in range(wiz._stack.count()):
            wiz._stack.setCurrentIndex(step)
            if step == 3:
                wiz._update_voice_page_state()
            for _ in range(4):
                self._app.processEvents()

            # Фіксований порядок кроків у FirstRunWizard.__init__ (welcome,
            # model, language, voice, EXTRA, download, gpu) — той самий
            # хардкод, що й у самому onboarding.py (кілька
            # `_stack.setCurrentIndex(4)  # _page_extra`). wiz._extra_chks
            # існує як атрибут об'єкта ЗАВЖДИ після побудови (не лише коли
            # цей крок показаний) — перевіряти треба індекс, а не hasattr.
            is_extra_step = step == 4 and hasattr(wiz, "_extra_chks")
            if is_extra_step:
                extra_index = step
                # позначаємо чекбокс, щоб підпис кнопки перемкнувся на
                # динамічний («Завантажити обране») — САМЕ цей стан клипався
                # в живому дефекті власника.
                for comp_id, (chk, sz, avail) in wiz._extra_chks.items():
                    if not avail and chk.isEnabled():
                        chk.setChecked(True)
                        break
                for _ in range(4):
                    self._app.processEvents()

            self._scan_step_buttons(wiz, f"wizard/step{step + 1}", failures)

        # Гарантія, що крок «Додаткові можливості» дійсно був у стеку й
        # перевірений — інакше тест міг би мовчки нічого не зловити.
        self.assertIsNotNone(extra_index, "крок 'Додаткові можливості' не знайдено в _stack")
        return failures

    def test_wizard_steps_no_clipped_button_text(self):
        failures = self._walk_all_steps(scale=1.0)
        self._assert_no_failures(failures)

    def test_wizard_all_steps_no_clipped_button_text_scaled(self):
        """УСІ кроки майстра під ×1.5 (друга хвиля 31.07): sum_box кроку
        «Додаткові можливості» (живий дефект власника — ловився вже й БЕЗ
        масштабу), рядок теки моделі і рядок кроку завантаження (FlowLayout),
        вертикальні стеки «Озвучення»/GPU (калібрування ширини діалога в
        showEvent — майстер сам відкривається ширшим за 620, коли шрифт
        більший, тож resize(620, 460) у _build_wizard тут клампиться до
        нового чесного мінімуму)."""
        failures = self._walk_all_steps(scale=1.5)
        self._assert_no_failures(failures)

    def _assert_no_failures(self, failures):
        if failures:
            lines = [
                f"  крок={p!r} кнопка={t!r} ширина_вікна={w}px "
                f"потрібно={need}px фактично={got}px"
                for p, t, w, need, got in failures
            ]
            self.fail(
                f"Обрізаний текст кнопок майстра ({len(failures)} випадків):\n"
                + "\n".join(lines))


class MainWindowMinWidthAndScaleTests(NoClippedButtonTextTests):
    """Успадковує фікстури NoClippedButtonTextTests (сендбокс, контролер,
    populate-хелпери). Два прицільні прогони поверх основного:

    1. Регресія на мінімум вікна (main_window.py MainWindow.setMinimumSize) —
       читаємо РЕАЛЬНИЙ win.minimumWidth(), а НЕ хардкод: суд 31.07 показав,
       що літерал у тесті не ловить мутацію «хтось підняв/опустив мінімум»
       (контракт tests/test_narrow_rows_layout.py: мінімум ≤1000, лишається
       1000). Повний обхід сторінок/вкладок РІВНО на цій ширині — 0 порушень
       (кнопки наради «Зберегти назву»/«Отримати текст наради»/«Видалити
       нараду» полагоджено в pages/meeting.py _fill_done_card — перенос,
       а не підйом мінімуму).

    2. Прицільна перевірка head-рядка диктування (заголовок «Диктування» +
       «Заповнити шаблон» + чіп обробки) під імітацією масштабу ×1.5 — САМЕ
       той рядок, що перевели на FlowLayout першою хвилею гілки.

    3. Друга хвиля 31.07: повний обхід усіх сторінок (крім вкладок
       Налаштувань) під ×1.5 — test_all_pages_no_clipping_scaled — з
       окремим контрактом переносу для nav-кнопок сайдбара (докстрінг
       модуля)."""

    def test_no_clipping_at_new_minimum_width(self):
        from fronts.desktop.main_window import _PAGES

        win = self._window()
        win.show()
        self._app.processEvents()
        min_width = win.minimumWidth()

        settings_i = next(i for i, (_ic, k) in enumerate(_PAGES)
                          if k == "nav_settings")
        failures = []
        for page_i, (_icon, key) in enumerate(_PAGES):
            win.set_page(page_i)
            self._app.processEvents()
            if key == "nav_audio":
                self._populate_files_queue(win)
            elif key == "nav_search":
                self._populate_search(win)
            for _ in range(3):
                self._app.processEvents()

            tab_indices = (list(range(win.settings._tabs.count()))
                           if page_i == settings_i else [None])
            for tab_i in tab_indices:
                if tab_i is not None:
                    win.settings._tabs.setCurrentIndex(tab_i)
                    self._app.processEvents()
                    page_label = f"{key}/tab{tab_i}:{win.settings._tabs.tabText(tab_i)}"
                else:
                    page_label = key

                win.resize(min_width, _HEIGHT)
                for _ in range(3):
                    self._app.processEvents()

                for btn in self._scan_visible_buttons(win):
                    need = btn.sizeHint().width()
                    got = btn.width()
                    if got < need:
                        failures.append((page_label, btn.text(), min_width, need, got))

        if failures:
            lines = [
                f"  сторінка={p!r} кнопка={t!r} ширина_вікна={w}px "
                f"потрібно={need}px фактично={got}px"
                for p, t, w, need, got in failures
            ]
            self.fail(
                f"Обрізаний текст кнопок на мінімумі вікна ({len(failures)} "
                f"випадків):\n" + "\n".join(lines))

    def test_dictation_head_row_no_clipping_scaled_at_minimum_width(self):
        from fronts.desktop.theme import QSS
        from PySide6.QtWidgets import QPushButton, QLabel

        self._app.setStyleSheet(_scaled_qss(QSS, 1.5))
        self.addCleanup(self._app.setStyleSheet, QSS)

        win = self._window()
        win.show()
        win.set_page(0)   # nav_dictation — перше в _PAGES
        win.resize(win.minimumWidth(), _HEIGHT)
        for _ in range(4):
            self._app.processEvents()

        page = win.dictation
        head_widget = getattr(page, "_head_widget", None)
        self.assertIsNotNone(head_widget, "DictationPage._head_widget не знайдено")
        # СКАН ЛИШЕ head-рядка (заголовок + «Заповнити шаблон» + чіп обробки +
        # пауза/скасувати/запис) — той самий, що перевели на FlowLayout. Інші
        # ряди сторінки (радіо «Куди текст» тощо) під ×1.5 теж ламаються
        # (розвідка 31.07), але вони поза цією гілкою — див. докстрінг модуля.
        failures = []
        for btn in head_widget.findChildren(QPushButton):
            if not btn.isVisible():
                continue
            text = (btn.text() or "").strip()
            if not text:
                continue
            need = btn.sizeHint().width()
            got = btn.width()
            if got < need:
                failures.append(("head-row", text, need, got))

        h1 = head_widget.findChild(QLabel, "pageHeaderH1")
        self.assertIsNotNone(h1, "заголовок сторінки не знайдено (objectName pageHeaderH1)")
        if h1 is not None and h1.isVisible():
            need = h1.sizeHint().width()
            got = h1.width()
            if got < need:
                failures.append(("head-row-title", h1.text(), need, got))

        if failures:
            lines = [f"  {label}: {t!r} потрібно={need}px фактично={got}px"
                     for label, t, need, got in failures]
            self.fail(
                f"Обрізаний текст у head-рядку диктування ({len(failures)} "
                f"випадків, ширина {win.minimumWidth()}px, шрифт ×1.5):\n"
                + "\n".join(lines))

    def test_all_pages_no_clipping_scaled_at_minimum_width(self):
        """Друга хвиля 31.07: усі сторінки MainWindow під ×1.5 на мінімумі
        вікна (радіо-рядок «Куди текст», ряди наради, рядок фраз словників —
        усі переведені на FlowLayout цією хвилею). Вкладки Налаштувань —
        поза обходом, окрема неторкана задача (докстрінг модуля).

        Nav-кнопки сайдбара — окремий контракт: сайдбар фіксованої ширини
        204px, ширини їм ніхто не дасть, тож підпис ПЕРЕНОСИТЬСЯ всередині
        кнопки (glass.py heightForWidth). Перевіряємо (а) що layout дав
        кожній nav-кнопці всю потрібну для переносу висоту, (б) що механізм
        реально живий — під ×1.5 хоч один довгий підпис («Запис екрана»)
        став ВИЩИМ за однорядкову висоту, інакше (а) було б зеленим і тоді,
        коли heightForWidth мовчки повертає той самий один рядок."""
        from fronts.desktop.theme import QSS
        from fronts.desktop.main_window import _PAGES

        self._app.setStyleSheet(_scaled_qss(QSS, 1.5))
        self.addCleanup(self._app.setStyleSheet, QSS)

        win = self._window()
        win.show()
        self._app.processEvents()

        settings_i = next(i for i, (_ic, k) in enumerate(_PAGES)
                          if k == "nav_settings")
        failures = []
        nav_wrapped = False
        for page_i, (_icon, key) in enumerate(_PAGES):
            win.set_page(page_i)
            self._app.processEvents()
            if key == "nav_audio":
                self._populate_files_queue(win)
            elif key == "nav_search":
                self._populate_search(win)
            # Вкладки Налаштувань БІЛЬШЕ НЕ пропускаємо: їхні ряди дій
            # (модель, гарячі клавіші, журнали, оновлення, сховище наради)
            # переведено на перенос у цій же хвилі, тож обхід має їх бачити —
            # інакше виправлення лишилося б недоведеним, а регрес там —
            # непоміченим.
            tab_indices = (list(range(win.settings._tabs.count()))
                           if page_i == settings_i else [None])
            for tab_i in tab_indices:
                label = key
                if tab_i is not None:
                    win.settings._tabs.setCurrentIndex(tab_i)
                    self._app.processEvents()
                    label = f"nav_settings/tab{tab_i}"
                win.resize(win.minimumWidth(), _HEIGHT)
                for _ in range(3):
                    self._app.processEvents()

                for btn in self._scan_visible_buttons(win):
                    if getattr(btn, "_nav", False):
                        need_h = btn.heightForWidth(btn.width())
                        if btn.height() < need_h:
                            failures.append(
                                (label, f"NAV {btn.text()} (висота)",
                                 win.minimumWidth(), need_h, btn.height()))
                        if btn.height() > btn.sizeHint().height():
                            nav_wrapped = True
                        continue
                    need = btn.sizeHint().width()
                    got = btn.width()
                    if got < need:
                        failures.append(
                            (label, btn.text(), win.minimumWidth(), need, got))

        if failures:
            lines = [
                f"  сторінка={p!r} кнопка={t!r} ширина_вікна={w}px "
                f"потрібно={need}px фактично={got}px"
                for p, t, w, need, got in failures
            ]
            self.fail(
                f"Обрізаний текст кнопок під ×1.5 ({len(failures)} випадків):\n"
                + "\n".join(lines))
        self.assertTrue(
            nav_wrapped,
            "жодна nav-кнопка не перенесла підпис під ×1.5 — механізм "
            "heightForWidth сайдбара мертвий (очікували ≥1, напр. «Запис екрана»)")


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _suite = unittest.TestSuite([
        _loader.loadTestsFromTestCase(NoClippedButtonTextTests),
        _loader.loadTestsFromTestCase(FirstRunWizardClippingTests),
        _loader.loadTestsFromTestCase(MainWindowMinWidthAndScaleTests),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(_suite)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
