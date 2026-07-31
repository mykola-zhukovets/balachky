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


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(NoClippedButtonTextTests))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
