"""Offscreen-рендер вкладки «Нарада» — ОКРЕМИЙ процес (integration wave-2).

Чому окремо від test_meeting_ui.py й від основного `unittest discover`:
ці тести — ЄДИНІ, що реально show()/grab() живі QWidget із таймерами
(LevelMeter 30fps, MeetingPage._tick 1s). У спільному процесі з рештою ~295
тестів спільний класовий offscreen-QApplication руйнується на виході
інтерпретатора, і будь-який недобитий Qt-таймер під час нативної static-
деструкції давав флакі-краш ПІСЛЯ «OK» (0xC000041D/139) — рев'юер відтворив
3/15, до 8/12 під навантаженням; без цього класу — 0/15.

Файл НЕ підхоплюється `unittest discover -s tests` (патерн `test*.py`), тож
основний набір лишається детерміновано чистим. Рендер-перевірку ганяємо
окремим процесом:

    python -m unittest tests.render_meeting_smoke          # звичайний прогін
    python -m unittest discover -s tests -p "render_*.py"  # discover-варіант
    python tests/render_meeting_smoke.py                   # standalone-раннер

Скріншоти станів (спокій/запис/сесії) — у
C:\\Users\\nikol\\Desktop\\balachky-diag\\meeting-ui\\.

Teardown жорсткий: перед знесенням віджета ЯВНО спиняємо всі його QTimer
(таймер, що тікає під час GC/деструкції = типова причина 0xC000041D), потім
close -> deleteLater -> флаш DeferredDelete, поки QApplication живий.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Віджети без екрана: рендеру потрібен QApplication, не реальний екран.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# standalone-запуск (python tests/render_meeting_smoke.py): корінь репо у sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# Тека діагностичних знімків рендера. Шлях НЕ зашиваємо: домашня тека
# розробника не має місця в публічному коді. Перевизначається змінною
# BALACHKY_DIAG_DIR, інакше — тимчасова тека системи.
_DIAG = (Path(os.environ.get("BALACHKY_DIAG_DIR", tempfile.gettempdir()))
         / "balachky-diag" / "meeting-ui")


class _RenderController:
    """Легкий контролер із Qt-сигналами наради для рендеру вкладки."""

    def __init__(self, metas):
        from PySide6.QtCore import QObject, Signal

        class _Ctl(QObject):
            meeting_state = Signal(str)
            meeting_track_done = Signal(str, str, str, object)
            meeting_session_done = Signal(str, object)
            meeting_error = Signal(str, str)
            meeting_audio_ready = Signal(str)
            meeting_storage_warning = Signal(str, float, str)
            meeting_processing_progress = Signal(str, object)
            meeting_processing_done = Signal(str, object)

            def __init__(self, metas):
                super().__init__()
                self.cfg = SimpleNamespace(
                    meeting_sources="mic", meeting_record_sources=[],
                    meeting_mic_devices=[], input_device=None,
                    meeting_screen_enabled=False, meeting_screen_monitor=1,
                    live_transcription=False, protocol_model="fast")
                self._metas = metas
                self.integrity_calls = 0     # ПОВНА верифікація (перехешування)
                self.meta_calls = 0          # дешевий статус журналу
                self.source_preset_writes = []
                self.meeting_start_calls = []
                self.meeting_process_calls = []
                self.meeting_cancel_process_calls = []
                self.bookmark_calls = []

            def protocol_model_ready(self):
                return False              # feature/ai-protocol: рендер без моделі

            def meeting_session_dir(self, sid):
                from pathlib import Path
                return Path(sid)

            def list_meetings(self):
                return self._metas

            def meeting_mic_level(self):
                return (0.45, 0.7)

            def meeting_sys_level(self):
                return (0.25, 0.4)

            def read_meeting_transcript(self, sid):
                return ("[00:00] Я: Доброго дня, дякую, що зібралися.\n"
                        "[00:04] Співрозмовники: Вітаю, почнімо.")

            def read_meeting_utterances(self, sid):
                from whisper_core.meeting import postprocess as mpost
                return [
                    mpost.Utterance(0.0, 4.0, "0", "Доброго дня, дякую, що зібралися."),
                    mpost.Utterance(4.0, 8.0, "1", "Вітаю, почнімо."),
                ]

            def list_meeting_screen_monitors(self):
                return []

            def set_meeting_screen_enabled(self, enabled):
                self.cfg.meeting_screen_enabled = enabled

            def set_meeting_screen_monitor(self, monitor):
                self.cfg.meeting_screen_monitor = monitor

            def set_meeting_sources(self, preset):
                self.source_preset_writes.append(preset)

            def set_live_transcription(self, on):
                self.cfg.live_transcription = bool(on)

            def set_meeting_speaker_name(self, sid, spk, name):
                pass

            def save_config(self):
                pass

            def meeting_start(self, preset):
                self.meeting_start_calls.append(preset)
                return True

            def meeting_stop(self):
                pass

            def meeting_cancel(self):
                pass

            def add_meeting_bookmark(self, title="", source="live_button"):
                self.bookmark_calls.append((title, source))
                return True

            def start_meeting_processing(self, sid):
                self.meeting_process_calls.append(sid)
                return True

            def cancel_meeting_processing(self, sid):
                self.meeting_cancel_process_calls.append(sid)
                return True

            def delete_meeting(self, sid):
                pass

            def recover_meeting(self, sid):
                pass

            def open_meeting_audio(self, sid):
                pass

            def open_meeting_folder(self, sid):
                pass

            def meeting_integrity_meta(self, sid):
                # Дешевий статус для рендеру картки (БЕЗ хешування артефактів).
                self.meta_calls += 1
                from whisper_core.meeting import audit_log
                return audit_log.ChainResult(
                    status=audit_log.STATUS_UNVERIFIED, event_count=2,
                    audio_sha="a1b2c3d4e5f6a1b2c3d4e5f6",
                    events=[{"seq": 0, "type": "created", "ts": 1700000000.0},
                            {"seq": 1, "type": "finalized", "ts": 1700000100.0}])

            def meeting_integrity(self, sid):
                # ПОВНА верифікація (перехешування) — лише за явним запитом.
                self.integrity_calls += 1
                from whisper_core.meeting import audit_log
                return audit_log.ChainResult(
                    status=audit_log.STATUS_VERIFIED, event_count=2,
                    audio_sha="a1b2c3d4e5f6a1b2c3d4e5f6",
                    events=[{"seq": 0, "type": "created", "ts": 1700000000.0},
                            {"seq": 1, "type": "finalized", "ts": 1700000100.0}])

            def log_meeting_export(self, sid, kind, output):
                pass

            def write_meeting_transcript(self, sid, text):
                self.written_transcripts = getattr(
                    self, "written_transcripts", []) + [(sid, text)]

        self._impl = _Ctl(metas)

    def __getattr__(self, name):
        return getattr(self._impl, name)


class MeetingRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()                        # як у реальному main(): ДО QSS
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))  # без живих таймерів
        _DIAG.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        # глобальний ~60fps годинник пілюль (glass._TAG_DRIVER): при animations=off
        # він і так не тікав, але явно спиняємо й чистимо реєстр, щоб на виході не
        # лишалось ні таймера, ні посилань на знесені C++-пілюлі.
        try:
            from fronts.desktop import glass
            glass._TAG_DRIVER._timer.stop()
            glass._TAG_DRIVER._pills.clear()
        except Exception:
            pass
        cls._flush_deferred(cls._app)

    @staticmethod
    def _flush_deferred(app):
        """Реально виконати deleteLater: processEvents сам по собі НЕ обробляє
        DeferredDelete поза циклом подій — шлемо їх явно."""
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
        self._live = []            # (page, ctl) — усе створене тестом

    def tearDown(self):
        from PySide6.QtCore import QTimer
        # ПЕРШИМ — спинити ВСІ таймери віджета (LevelMeter 30fps, _tick 1s та будь-
        # які інші дочірні): активний таймер під час деструкції = 0xC000041D. Аж
        # ПОТІМ close -> deleteLater (віджет і контролер) -> флаш DeferredDelete.
        for page, ctl in self._live:
            for t in page.findChildren(QTimer):
                try:
                    t.stop()
                except RuntimeError:
                    pass                # C++-частина вже знесена
            try:
                page.close()
            except Exception:
                pass
            page.deleteLater()
            ctl._impl.deleteLater()
        self._live = []
        self._flush_deferred(self._app)

    def _pump(self, n=4):
        # кілька циклів подій: перший grab після одного processEvents не встигає
        # намалювати H1/пілюлі (другий прохід layout/paint)
        for _ in range(n):
            self._app.processEvents()

    def _page(self, metas):
        from fronts.desktop.pages.meeting import MeetingPage
        ctl = _RenderController(metas)
        page = MeetingPage(ctl)
        self._live.append((page, ctl))   # teardown спинить таймери й знесе (див. tearDown)
        page.resize(1000, 640)
        page.show()
        self._pump()
        return page, ctl

    def _open_settings(self, page):
        """Розкрити секцію «Налаштування наради» (аудит 22.07: у спокої згорнута).
        Потрібно там, де тест міряє геометрію/вміст панелі — інакше приховані
        віджети не розкладені (усі x=0) і перевірка вироджується у хибно-зелену."""
        page._settings_toggle.setChecked(True)
        self._pump()

    def _save(self, page, name):
        self._pump()
        pix = page.grab()
        self.assertFalse(pix.isNull())
        pix.save(str(_DIAG / name))

    def test_render_idle_empty(self):
        page, _ = self._page([])
        # порожній стан видно (немає сесій)
        self.assertEqual(page._stack.currentIndex(), 0)
        self.assertFalse(page._live_host.isVisible())
        self._save(page, "01-idle-empty.png")

    def test_empty_state_hidden_once_first_session_exists(self):
        """Аудит 31.07: порожній стан зникає, щойно з'являється перший запис."""
        meta = SimpleNamespace(
            id="2026-07-31_09-00-00", status="done", title=None, preset="both",
            audio_files={"mic": ["audio/mic/0001.wav"]}, processing={})
        page, _ = self._page([meta])
        self.assertEqual(page._stack.currentIndex(), 1,
                         "перша нарада мала прибрати порожній стан і показати стрічку")

    def test_empty_state_button_starts_recording_same_as_round_button(self):
        """Кнопка першого кроку в порожньому стані веде на ту саму дію, що
        кругла кнопка запису — не в нікуди."""
        page, ctl = self._page([])
        self.assertTrue(page._empty.button.isEnabled())
        self.assertEqual(page._empty.button.text(), page._rec_caption.text())
        page._empty.button.click()
        self._pump()
        self.assertEqual(len(ctl.meeting_start_calls), 1,
                         "клік мав викликати controller.meeting_start(), як і кругла кнопка")

    def test_empty_state_button_disabled_during_processing(self):
        """Під час обробки тумблер запису вимкнено (не можна стартувати новий) —
        кнопка порожнього стану мусить лишатись синхронною з круглою кнопкою."""
        page, _ = self._page([])
        page._on_state("processing")
        self._pump()
        self.assertFalse(page._empty.button.isEnabled())
        self.assertFalse(page._rec_btn.isEnabled())

    def test_record_button_prominent_on_first_screen(self):
        """Аудит Миколи 22.07: головна дія (запис) помітна одразу — кругла кнопка
        з видимим підписом стоїть угорі, а панель налаштувань згорнута за
        замовчуванням (не перекриває кнопку)."""
        page, _ = self._page([])
        self.assertTrue(page._rec_btn.isVisible(), "кнопка запису має бути видима")
        self.assertTrue(page._rec_caption.isVisible(), "підпис під кнопкою видимий")
        self.assertEqual(page._rec_caption.text(), "Почати запис")
        self.assertFalse(page._settings_panel.isVisible(),
                         "налаштування згорнуті за замовчуванням")
        self.assertTrue(page._settings_toggle.isVisible(),
                        "розкривачка налаштувань видима")

    def test_settings_disclosure_is_a_real_button_not_a_thin_row(self):
        """Канон побудови сторінок 30.07 п.3: живий тест власника — людина, яка
        бачила застосунок вперше, НЕ знайшла «Налаштування наради», бо
        розкривач був тонким рядком-написом із дрібною стрілкою (звичайний
        QToolButton: border:transparent у спокої — межа лише на hover).
        property("disclosure") вмикає QSS-правило з видимою межею й більшим
        padding (theme.py) — перевіряємо це ФАКТОМ: розкривач вищий за голий
        QToolButton із тим самим текстом на тій самій палітрі."""
        from PySide6.QtWidgets import QToolButton
        page, _ = self._page([])
        toggle = page._settings_toggle
        self.assertTrue(
            bool(toggle.property("disclosure")),
            "toggle без property(disclosure) — QSS-межа/padding не застосуються")
        bare = QToolButton()
        bare.setText(toggle.text())
        bare.show()
        self._pump()
        try:
            # Поріг АБСОЛЮТНИМ числом, не лише "більше за голий": сам голий
            # QToolButton уже трохи вищий за arrow+checkable (~21px проти
            # ~20px) НЕЗАЛЕЖНО від QSS-правила — відносне порівняння самé
            # по собі не ловить мутацію, де QSS-блок [disclosure] спорожнили,
            # а property лишили. 36px — нижче за реальні ~45px розкривача,
            # але недосяжне без padding 8px+8px із QSS.
            self.assertGreaterEqual(
                toggle.sizeHint().height(), 36,
                "розкривач має бути ПОМІТНО вищим за звичайний тонкий "
                "рядок-кнопку — QSS-padding [disclosure=true] не діє")
            self.assertGreater(
                toggle.sizeHint().height(), bare.sizeHint().height(),
                "розкривач має бути вищим за звичайний тонкий рядок-кнопку")
        finally:
            bare.deleteLater()
            self._flush_deferred(self._app)

    def test_live_transcription_checkbox_absent_on_meeting(self):
        """Аудит Миколи 22.07: чекбокс «Жива розшифровка» — чужий на Нараді
        (стосується диктування) → перенесений у Налаштування → Диктування. На
        сторінці Нарада його бути не повинно (ні атрибута, ні контрола з підписом).
        Перевірку присутності в новому місці робить render_layout_smoke."""
        from PySide6.QtWidgets import QCheckBox
        from fronts.desktop.i18n import tr
        page, _ = self._page([])
        self._open_settings(page)
        self.assertFalse(hasattr(page, "_live_transcription"))
        label = tr("meeting_live_dictation_label")
        stray = [c for c in page.findChildren(QCheckBox) if c.text() == label]
        self.assertEqual(stray, [], "чекбокс живої розшифровки лишився на Нараді")

    def test_model_cards_not_clipped_when_settings_open(self):
        """Регрес (візуальний гейт, 12 порушень text_clipped_v uk+en): розкриті
        налаштування вищі за сторінку, і колонка тиснула картки моделі ШІ нижче
        за їхній мінімум — назва «Швидка (Gemma 4 E4B, ~5 ГБ)» діставала 13px
        замість 22, підписи 12 замість 18. Тепер панель прокручується, а підписи
        мають мінімум за fontMetrics. Міряємо ТИМИ САМИМИ метриками, що ними
        QLabel малює текст (як scripts/visual_gate.py), тож тест не залежить від
        підміни шрифту offscreen."""
        from PySide6.QtCore import QRect, Qt
        from PySide6.QtGui import QFontMetrics
        from PySide6.QtWidgets import QVBoxLayout, QWidget
        from fronts.desktop.pages.meeting import WrapLabel
        page, _ = self._page([])
        # Сторінка МУСИТЬ бути в тісному батькові: окреме вікно Qt ніколи не
        # робить меншим за мінімум layout'а, тож top-level resize(…, 640) дефект
        # НЕ відтворює. У продакшні сторінка живе в QStackedWidget головного
        # вікна — тут це моделює host фіксованої висоти.
        host = QWidget()
        host_lay = QVBoxLayout(host)
        host_lay.setContentsMargins(0, 0, 0, 0)
        host_lay.addWidget(page)
        self.addCleanup(host.deleteLater)
        host.setFixedSize(1000, 700)      # жорстко: інакше вікно виросте під мінімум
        host.show()
        self._open_settings(page)
        self._pump()
        self.assertGreater(
            page._settings_panel.minimumSizeHint().height(), page.height(),
            "панель уміщується у сторінку — тест не відтворює тісноту дефекту")
        labels = [w for w in page._settings_panel.findChildren(WrapLabel)
                  if w.isVisible() and w.text()]
        self.assertGreaterEqual(
            len(labels), 6,          # 2 картки × (назва + розмір/стан + опис)
            "підписів карток моделі ШІ не знайдено — тест виродився")
        for lbl in labels:
            avail = lbl.contentsRect().width()
            self.assertGreater(avail, 0, f"нульова ширина у {lbl.text()!r}")
            flags = int(Qt.TextWordWrap) | int(lbl.alignment())
            need = QFontMetrics(lbl.font()).boundingRect(
                QRect(0, 0, avail, 1 << 20), flags, lbl.text()).height()
            self.assertGreaterEqual(
                lbl.height() + 3, need,      # допуск гейта (VTOL)
                f"підпис картки моделі ріжеться: {lbl.text()!r} — "
                f"треба {need}px, є {lbl.height()}px")

    def test_meeting_page_has_no_source_picker_that_overwrites_custom_selection(self):
        from whisper_core.config import (MEETING_SYSTEM_SOURCE,
                                         meeting_microphone_token)
        page, ctl = self._page([])
        custom = [meeting_microphone_token("Room A"),
                  meeting_microphone_token("Room B"),
                  MEETING_SYSTEM_SOURCE]
        ctl.cfg.meeting_record_sources = list(custom)

        page._on_toggle()

        self.assertFalse(hasattr(page, "_presets"))
        self.assertEqual(ctl.source_preset_writes, [])
        self.assertEqual(ctl.cfg.meeting_record_sources, custom)
        self.assertEqual(ctl.meeting_start_calls, ["both"])

    def test_meeting_source_picker_refuses_fifth_mic_but_allows_system(self):
        from PySide6.QtWidgets import QCheckBox, QWidget
        from fronts.desktop.pages.settings import SettingsPage
        from whisper_core.config import (MEETING_MIC_SOURCE_PREFIX,
                                         MEETING_MULTIMIC_MAX,
                                         MEETING_SYSTEM_SOURCE,
                                         meeting_microphone_token)

        class SourcePicker(QWidget):
            _on_meeting_record_sources = SettingsPage._on_meeting_record_sources

        saved = []
        picker = SourcePicker()
        picker.controller = SimpleNamespace(
            cfg=SimpleNamespace(meeting_record_sources=[],
                                meeting_mic_devices=[], meeting_sources="mic"),
            save_config=lambda: saved.append(True))
        picker._meeting_source_checks = []
        for token in ([meeting_microphone_token(f"Mic {number}")
                       for number in range(1, MEETING_MULTIMIC_MAX + 2)]
                      + [MEETING_SYSTEM_SOURCE]):
            check = QCheckBox(token, picker)
            check.setProperty("meetingSourceToken", token)
            check.toggled.connect(picker._on_meeting_record_sources)
            picker._meeting_source_checks.append(check)

        microphones = [
            check for check in picker._meeting_source_checks
            if str(check.property("meetingSourceToken")).startswith(
                MEETING_MIC_SOURCE_PREFIX)
        ]
        system = picker._meeting_source_checks[-1]
        try:
            with patch("fronts.desktop.pages.settings.motion.toast") as toast:
                for check in microphones:
                    check.click()
                system.click()

            self.assertEqual(sum(check.isChecked() for check in microphones),
                             MEETING_MULTIMIC_MAX)
            self.assertFalse(microphones[MEETING_MULTIMIC_MAX].isChecked())
            self.assertTrue(system.isChecked())
            toast.assert_called_once()
            self.assertEqual(
                picker.controller.cfg.meeting_record_sources[-1],
                MEETING_SYSTEM_SOURCE)
            self.assertEqual(len(saved), MEETING_MULTIMIC_MAX + 1)
        finally:
            picker.deleteLater()
            self._flush_deferred(self._app)

    def test_render_recording(self):
        page, ctl = self._page([])
        ctl.meeting_state.emit("recording")
        self._pump()
        self.assertTrue(page._live_host.isVisible())
        self._save(page, "02-recording.png")

    def test_bookmark_button_does_not_block_recording_with_dialog(self):
        """feature/bookmarks-stage1: клік «Закладка» під час запису фіксує
        момент миттєво — жодного модального діалогу (спека, розд. 2.2)."""
        from PySide6.QtWidgets import QInputDialog
        page, ctl = self._page([])
        ctl.meeting_state.emit("recording")
        self._pump()
        self.assertTrue(page._bookmark_btn.isVisible())

        with patch.object(QInputDialog, "getText") as dlg:
            page._bookmark_btn.click()

        dlg.assert_not_called()
        self.assertEqual(ctl._impl.bookmark_calls, [("", "live_button")])

    def test_bookmark_chips_seek_player(self):
        """feature/bookmarks-stage1: чіпи закладок на картці готової наради
        клікабельні — клік стрибає вбудований плеєр на таймкод мітки (мок
        play_from), як і чіпи розділів."""
        import os
        import tempfile
        from fronts.desktop.glass import GlassButton

        sdir = tempfile.mkdtemp()
        fake_wav = os.path.join(sdir, "mic.wav")
        open(fake_wav, "wb").close()

        page, ctl = self._page([])
        ctl._impl.meeting_audio_paths = lambda sid: {"mic": fake_wav}
        ctl._impl._metas = [SimpleNamespace(
            id=sdir, status="done", title="Нарада", preset="onlymic",
            bookmarks=[{"timestamp": 75.0, "title": "Домовились про бюджет"}])]
        page.refresh()
        self._pump()

        self.assertTrue(page._players)
        player = page._players[-1]
        calls = []
        player.play_from = lambda t, until=None: calls.append(t)

        chip = next(b for b in page.findChildren(GlassButton)
                    if "Домовились про бюджет" in b.text())
        chip.click()
        self.assertEqual(calls, [75.0])

    def test_screen_record_checkbox_locked_during_active_session(self):
        # Рецензія №2: чекбокс «Записувати екран» діє лише з наступної наради,
        # тож поки сесія активна — він disabled із поясненням, а після
        # завершення знову доступний зі звичайною підказкою.
        page, ctl = self._page([])
        chk = page._screen_record
        self.assertTrue(chk.isEnabled())                     # спокій — доступний

        for state in ("recording", "processing", "postprocessing"):
            ctl.meeting_state.emit(state)
            self._pump()
            self.assertFalse(chk.isEnabled(), f"{state}: чекбокс має бути вимкнено")
            self.assertEqual(chk.toolTip(), "Діє з наступної наради",
                             f"{state}: підказка про наступну нараду")

        ctl.meeting_state.emit("idle")
        self._pump()
        self.assertTrue(chk.isEnabled())                     # знову доступний
        self.assertEqual(
            chk.toolTip(),
            "Поряд із записом наради збережеться відео екрана — зручно "
            "переглянути слайди чи демонстрацію. Займає помітно більше місця "
            "на диску, ніж лише звук.")

    def test_render_sessions(self):
        metas = [
            SimpleNamespace(id="2026-07-15_14-30-05", status="done",
                            title=None, preset="both"),
            SimpleNamespace(id="2026-07-15_09-10-00", status="interrupted",
                            title=None, preset="onlymic"),
        ]
        page, _ = self._page(metas)
        self.assertEqual(page._stack.currentIndex(), 1)   # стрічка карток
        self.assertEqual(len(page._cards), 2)
        self._save(page, "03-sessions.png")

    def test_done_card_actions_share_one_row_and_badge_has_tooltip(self):
        """Кнопки готової наради («Зберегти назву», «Обробити нараду»,
        «Звільнити місце», «Видалити нараду») лежать в ОДНОМУ горизонтальному
        контейнері; значок «локально, відкрито» має непорожню підказку."""
        from PySide6.QtWidgets import QHBoxLayout, QPushButton
        from fronts.desktop.glass import StatusTag
        from fronts.desktop.i18n import tr
        sid = "2026-07-15_14-30-05"
        meta = SimpleNamespace(
            id=sid, status="done", title=None, preset="both",
            audio_files={"mic": ["audio/mic/0001.wav"]}, processing={})
        ctl = _RenderController([meta])
        ctl._impl.meeting_raw_audio_bytes = lambda _sid: 5 * 1024 * 1024
        from fronts.desktop.pages.meeting import MeetingPage
        page = MeetingPage(ctl)
        self._live.append((page, ctl))
        page.resize(1000, 640)
        page.show()
        self._pump()

        keys = ("meeting_title_save", "meeting_process",
                "meeting_free_raw_audio", "meeting_card_delete")
        buttons = []
        for key in keys:
            found = [b for b in page.findChildren(QPushButton)
                     if b.text() == tr(key)]
            self.assertEqual(len(found), 1, f"{key}: очікувалася одна кнопка")
            buttons.append(found[0])

        def direct_hbox(widget):
            # Qt репарентить віджет у layout-host, тож шукаємо найближчого
            # предка, чий ВЛАСНИЙ layout — QHBoxLayout із цим предком item'ом.
            node = widget
            while node is not None and node is not page:
                lay = node.layout()
                if isinstance(lay, QHBoxLayout):
                    return lay
                parent = node.parentWidget()
                if parent is None:
                    return None
                lay = parent.layout()
                if isinstance(lay, QHBoxLayout):
                    for idx in range(lay.count()):
                        if lay.itemAt(idx).widget() is node:
                            return lay
                    return None
                node = parent
            return None

        rows = [direct_hbox(b) for b in buttons]
        self.assertTrue(all(rows), "кожна кнопка — прямий item QHBoxLayout")
        self.assertTrue(all(row is rows[0] for row in rows),
                        "усі чотири кнопки — в одному горизонтальному ряді")
        # «Видалити нараду» відділена розтяжкою: між нею і рештою є stretch.
        row = rows[0]
        delete_index = next(
            i for i in range(row.count())
            if row.itemAt(i).widget() is buttons[-1])
        kinds = [type(row.itemAt(i)).__name__ for i in range(delete_index)]
        self.assertIn("QSpacerItem", kinds,
                      "перед «Видалити нараду» має бути addStretch")

        # Значок «локально, відкрито» — саме ГОТОВОЇ картки. Дірявий пошук
        # findChildren(StatusTag) по всій сторінці ловить і прихований
        # _live_security_badge панелі запису (йому завжди шили open_tip), тож
        # мутація setToolTip("") на готовій картці лишала тест зеленим. Беремо
        # лише card цієї сесії — тут значок захисту єдиний і мусить мати підказку.
        card = page._cards[sid][0]
        security = [t for t in card.findChildren(StatusTag)
                    if t.toolTip() == tr("meeting_security_open_tip")]
        self.assertEqual(
            len(security), 1,
            "значок стану захисту готової картки — єдиний і має підказку "
            "meeting_security_open_tip")
        self.assertTrue(security[0].toolTip(), "підказка значка непорожня")

    def test_process_button_names_result_and_is_visually_prominent(self):
        """Канон побудови сторінок 30.07 п.1-2: живий тест власника — головна
        дія називалась «Обробити нараду» (процес, не результат) і "ховалась"
        серед сусідніх кнопок. Тут доводимо ОБИДВА факти окремо, не через
        рядок tr(): (1) новий підпис результат-орієнтований; (2) кнопка —
        справжній QPushButton (НЕ GlassButton — той власним paintEvent
        ігнорує QSS [accent=true] і зовні не відрізняється від сусідів),
        вища й жирнішого шрифту за сусідню другорядну «Видалити нараду»."""
        from PySide6.QtWidgets import QPushButton
        from fronts.desktop.glass import GlassButton
        from fronts.desktop.i18n import tr
        sid = "2026-07-15_14-30-05"
        meta = SimpleNamespace(
            id=sid, status="done", title=None, preset="both",
            audio_files={"mic": ["audio/mic/0001.wav"]}, processing={})
        page, ctl = self._page([meta])

        process = page.findChild(QPushButton, f"meetingProcessButton-{sid}")
        self.assertIsNotNone(process)

        # (1) назва — результат, не процес
        # Жорсткий укр. літерал (не tr(key)): якщо i18n-ключ meeting_process
        # зламають/видалять, tr() поверне сирий ключ і УВІ порівнювані боки
        # тесту стали б однаково "зламані" — тест лишився б зеленим на
        # непереклад, що бачить користувач.
        self.assertEqual(process.text(), "Отримати текст наради")
        self.assertNotIn("Обробити", process.text(),
                         "стара назва процесу — власник її й не шукав")
        self.assertIn("текст", process.text().lower(),
                      "нова назва мусить обіцяти РЕЗУЛЬТАТ (текст наради)")

        # (2) реально акцентна кнопка — не GlassButton, що QSS accent ігнорує
        self.assertIsInstance(process, QPushButton)
        self.assertNotIsInstance(
            process, GlassButton,
            "GlassButton малює все сам у paintEvent і НЕ читає "
            "QSS[accent=true] — кнопка знову стане непомітною склянкою")
        self.assertTrue(bool(process.property("accent")),
                        "без property(accent) кнопка не заливається золотим")
        self.assertTrue(
            bool(process.property("primaryAction")),
            "без property(primaryAction) кнопка отримує ЗВИЧАЙНИЙ розмір "
            "акцентної кнопки — той самий, що й другорядні дії моделей ШІ, "
            "тож не виділяється серед сусідів у рядку картки")

        delete_candidates = [
            b for b in page.findChildren(QPushButton)
            if b.text() == tr("meeting_card_delete")]
        self.assertEqual(len(delete_candidates), 1)
        delete_btn = delete_candidates[0]
        self.assertFalse(bool(delete_btn.property("primaryAction")))

        # .height() ПІСЛЯ розкладки не годиться: QHBoxLayout розтягує всі
        # item'и рядка до найвищого сусіда. sizeHint()/font() — власні,
        # НЕзалежні від сусідів метрики самого widget'а: саме вони і
        # визначають, наскільки він "більший", перш ніж рядок їх вирівняє.
        self.assertGreater(
            process.sizeHint().height(), delete_btn.sizeHint().height(),
            "головна дія картки мусить самостійно просити більшу висоту, "
            "ніж другорядна «Видалити»")
        self.assertTrue(process.font().bold(),
                        "головна дія — жирним шрифтом, щоб виділятись")
        self.assertFalse(delete_btn.font().bold(),
                         "другорядна дія лишається звичайною вагою")
        self.assertGreater(
            process.font().pixelSize(), delete_btn.font().pixelSize(),
            "шрифт головної дії має бути більшим за другорядну кнопку "
            "(база QWidget — 15px, theme.py)")

    def test_done_card_title_is_editable_and_saves_typed_value(self):
        """Назву готової наради можна ПЕРЕЙМЕНУВАТИ.

        Охоронець блокера рецензії 25.07: у картці лишилась сама кнопка
        «Зберегти назву», яка зберігала незмінне захоплене значення, — поле
        вводу зникло, і жоден тест цього не побачив (усі перевіряли лише
        наявність кнопки за текстом). Тепер перевіряємо ланцюг цілком:
        поле є → у нього друкують → кнопка віддає контролеру САМЕ надруковане."""
        from PySide6.QtWidgets import QLineEdit, QPushButton
        from fronts.desktop.i18n import tr
        sid = "2026-07-15_14-30-05"
        meta = SimpleNamespace(
            id=sid, status="done", title=None, preset="both",
            audio_files={"mic": ["audio/mic/0001.wav"]}, processing={})
        page, ctl = self._page([meta])
        page.resize(1000, 640)
        page.show()
        self._pump()

        card = page._cards[sid][0]
        edits = [e for e in card.findChildren(QLineEdit)
                 if e.objectName() == f"meetingTitleEdit-{sid}"]
        self.assertEqual(len(edits), 1,
                         "у картці готової наради має бути поле вводу назви")
        saved = []
        ctl.set_meeting_title = lambda s, t: saved.append((s, t))
        edits[0].setText("Нарада штабу")
        save = [b for b in card.findChildren(QPushButton)
                if b.text() == tr("meeting_title_save")]
        self.assertEqual(len(save), 1, "одна кнопка «Зберегти назву»")
        save[0].click()
        self.assertEqual(
            saved, [(sid, "Нарада штабу")],
            "кнопка мусить віддати НАДРУКОВАНЕ значення, а не захоплене при "
            "побудові картки")

    def test_processing_status_label_is_in_layout(self):
        """Рядок статусу обробки видимий у розкладці, а не осиротілий.

        Знахідка рецензії 25.07: QLabel статусу створювався і ховався, але не
        додавався в layout — усі подальші setText() (помилка, скасування,
        діаризація) не бачив ніхто. Мутація «прибрати lay.addWidget(status)»
        мусить червонити цей тест."""
        sid = "2026-07-15_14-30-05"
        meta = SimpleNamespace(
            id=sid, status="done", title=None, preset="both",
            audio_files={"mic": ["audio/mic/0001.wav"]}, processing={})
        page, ctl = self._page([meta])
        page.resize(1000, 640)
        page.show()
        self._pump()

        status = page._processing_widgets[sid]["status"]
        self.assertIsNotNone(status.parentWidget(),
                             "лейбл статусу без батька — не в розкладці")
        lay = status.parentWidget().layout()
        self.assertIsNotNone(lay, "у батька лейбла статусу немає розкладки")
        in_layout = any(lay.itemAt(i).widget() is status
                        for i in range(lay.count()))
        self.assertTrue(
            in_layout,
            "лейбл статусу обробки не доданий у розкладку — його setText() "
            "ніколи не побачить користувач")

    def _transcript_label(self, card):
        """Знайти TranscriptViewer картки: єдиний QLabel із власним
        атрибутом `_utterances`, який ставить лише клас TranscriptViewer
        (інші лейбли картки — прапорці/статуси, звичайні QLabel)."""
        from PySide6.QtWidgets import QLabel
        found = [w for w in card.findChildren(QLabel)
                 if hasattr(w, "_utterances")]
        self.assertEqual(len(found), 1,
                         "мала бути рівно одна TranscriptViewer у картці")
        return found[0]

    def test_empty_transcript_before_processing_hints_to_process_not_silence(self):
        """Живий тест власника 30.07: бейдж «аудіо готове» + плеєр, що грає
        запис, ОДНОЧАСНО з «У записі немає звуку» — брехня, бо розшифрування
        ще ЖОДНОГО разу не запускали (processing status за замовч. "ready").
        Порожній транскрипт у цьому стані мусить підказувати натиснути кнопку
        обробки, а НЕ стверджувати підтверджену тишу."""
        from fronts.desktop.i18n import tr
        sid = "2026-07-15_14-30-05"
        meta = SimpleNamespace(
            id=sid, status="done", title=None, preset="both",
            audio_files={"mic": ["audio/mic/0001.wav"]}, processing={})
        ctl_holder = _RenderController([meta])
        ctl_holder._impl.read_meeting_transcript = lambda _sid: ""
        from fronts.desktop.pages.meeting import MeetingPage
        page = MeetingPage(ctl_holder)
        self._live.append((page, ctl_holder))
        page.resize(1000, 640)
        page.show()
        self._pump()

        card = page._cards[sid][0]
        label = self._transcript_label(card)
        # Жорсткий укр. літерал — assertEqual з tr(того самого ключа) не
        # ловить видалений/зламаний ключ meeting_transcript_pending.
        self.assertEqual(
            label.text(),
            "Текст ще не отримано. Натисніть “Обробити нараду”, "
            "щоб розшифрувати запис.")
        self.assertNotEqual(label.text(), tr("meeting_error_silence"))

    def test_empty_transcript_after_processing_shows_honest_silence(self):
        """Після завершеної обробки (status "complete") без жодного слова —
        це ЄДИНИЙ стан, де підтверджено справжню тишу запису. Тут повідомлення
        про відсутність звуку — чесне."""
        sid = "2026-07-15_14-30-05"
        meta = SimpleNamespace(
            id=sid, status="done", title=None, preset="both",
            audio_files={"mic": ["audio/mic/0001.wav"]},
            processing={"status": "complete"})
        ctl_holder = _RenderController([meta])
        ctl_holder._impl.read_meeting_transcript = lambda _sid: ""
        from fronts.desktop.pages.meeting import MeetingPage
        page = MeetingPage(ctl_holder)
        self._live.append((page, ctl_holder))
        page.resize(1000, 640)
        page.show()
        self._pump()

        card = page._cards[sid][0]
        label = self._transcript_label(card)
        # Жорсткий укр. літерал — те саме обґрунтування, що й вище.
        self.assertEqual(label.text(), "У записі немає звуку — доріжки порожні.")

    def test_erasing_transcript_in_edit_panel_does_not_claim_silence(self):
        """Суддя 30.07: та сама брехня «У записі немає звуку» лежала і в
        _apply_edit панелі ручного редагування — якщо людина сама стирає
        весь текст і зберігає, `new or tr("meeting_error_silence")`
        підставляв тишу, хоча звук точно був (вона щойно правила його текст).
        Порожній текст після ручного стирання має показувати чесне
        повідомлення про стирання, а НЕ про відсутність звуку."""
        from fronts.desktop.i18n import tr
        from fronts.desktop.pages.edit_search import TranscriptEditPanel
        sid = "2026-07-15_14-30-05"
        meta = SimpleNamespace(
            id=sid, status="done", title=None, preset="both",
            audio_files={"mic": ["audio/mic/0001.wav"]},
            processing={"status": "complete"})
        ctl_holder = _RenderController([meta])
        ctl_holder._impl.cfg.transcript_editing_enabled = True
        from fronts.desktop.pages.meeting import MeetingPage
        page = MeetingPage(ctl_holder)
        self._live.append((page, ctl_holder))
        page.resize(1000, 640)
        page.show()
        self._pump()

        card = page._cards[sid][0]
        label = self._transcript_label(card)
        self.assertNotEqual(label.text(), "",
                             "у фікстурі мав бути непорожній транскрипт")

        panel = card.findChild(TranscriptEditPanel)
        self.assertIsNotNone(panel, "панель редагування транскрипту не знайдена "
                                     "— transcript_editing_enabled увімкнено")
        panel.begin_edit()
        panel._editor.setPlainText("")
        panel._save()
        self._pump()

        # Жорсткий укр. літерал — те саме обґрунтування, що й вище.
        self.assertEqual(
            label.text(),
            "Текст порожній, бо його стерли вручну в редагуванні. "
            "Натисніть “Редагувати”, щоб вписати текст назад.")
        self.assertNotEqual(label.text(), tr("meeting_error_silence"))
        self.assertEqual(ctl_holder._impl.written_transcripts, [(sid, "")])

    def test_recorded_card_has_explicit_processing_progress_and_cancel(self):
        from PySide6.QtWidgets import QProgressBar, QPushButton
        sid = "2026-07-15_14-30-05"
        meta = SimpleNamespace(
            id=sid, status="done", title=None, preset="both",
            audio_files={"mic": ["audio/mic/0001.wav"]}, processing={},
        )
        page, ctl = self._page([meta])
        process = page.findChild(QPushButton, f"meetingProcessButton-{sid}")
        self.assertIsNotNone(process)
        self.assertTrue(process.accessibleName())
        process.click()
        self.assertEqual(ctl._impl.meeting_process_calls, [sid])

        ctl.meeting_processing_progress.emit(
            sid,
            {"status": "running", "completed_chunks": 1, "total_chunks": 2},
        )
        self._pump()
        bar = page.findChild(QProgressBar, f"meetingProcessingProgress-{sid}")
        cancel = page.findChild(
            QPushButton, f"meetingCancelProcessing-{sid}")
        self.assertIsNotNone(bar)
        self.assertEqual(bar.value(), 500)
        self.assertTrue(bar.accessibleName())
        self.assertTrue(cancel.accessibleName())
        self._save(page, "04-processing.png")
        cancel.click()
        self.assertEqual(ctl._impl.meeting_cancel_process_calls, [sid])

    def test_card_render_does_not_run_full_verify(self):
        """Fix продуктивності: рендер картки наради читає ЛИШЕ дешевий статус
        журналу (meeting_integrity_meta), а ПОВНУ верифікацію з перехешуванням
        (meeting_integrity) НЕ запускає — інакше вкладка морозиться на 2-годинних
        записах. Повна верифікація виконується лише у воркері при відкритті
        журналу."""
        from fronts.desktop.pages.meeting import IntegrityVerifyWorker
        metas = [SimpleNamespace(id="2026-07-15_14-30-05", status="done",
                                 title=None, preset="both")]
        page, ctl = self._page(metas)
        self._pump()
        # Рендер done-картки: дешевий meta — так; повна верифікація — НУЛЬ разів.
        self.assertGreaterEqual(ctl._impl.meta_calls, 1)
        self.assertEqual(ctl._impl.integrity_calls, 0)
        # Відкриття журналу запускає повну верифікацію у воркері (run() тут
        # синхронно — доводить, що саме воркер тягне перехешування).
        worker = IntegrityVerifyWorker(ctl, "2026-07-15_14-30-05")
        worker.run()
        self.assertEqual(ctl._impl.integrity_calls, 1)

    def test_diarization_controls_three_capability_states(self):
        """Slice 3: три чесні стани контролів діаризації.

        • пакет sherpa відсутній → фіча недоступна в цій збірці (усе вимкнено);
        • пакет є, моделей нема → пропозиція завантажити, чекбокс вимкнено;
        • моделі перевірені → чекбокс активний, поле кількості з чекбоксом."""
        from unittest import mock
        from whisper_core.meeting import diarize
        page, _ = self._page([])
        self._open_settings(page)

        with mock.patch.object(diarize, "runtime_available", return_value=False):
            page._refresh_diarization_controls()
            self.assertFalse(page._diar_enabled.isEnabled())
            self.assertTrue(page._diar_download.isHidden())
            self.assertEqual(page._diar_status.text(),
                             "Розрізнення співрозмовників недоступне в цій збірці.")

        with mock.patch.object(diarize, "runtime_available", return_value=True), \
                mock.patch.object(diarize, "models_present_fast", return_value=False):
            page._refresh_diarization_controls()
            self.assertFalse(page._diar_enabled.isEnabled())
            self.assertFalse(page._diar_download.isHidden())
            self.assertEqual(page._diar_status.text(),
                             "Завантажте локальні файли для розрізнення "
                             "голосів (~34 МБ).")

        with mock.patch.object(diarize, "runtime_available", return_value=True), \
                mock.patch.object(diarize, "models_present_fast", return_value=True):
            page._diar_enabled.setChecked(False)
            page._refresh_diarization_controls()
            self.assertTrue(page._diar_enabled.isEnabled())
            self.assertTrue(page._diar_download.isHidden())
            self.assertFalse(page._diar_count.isEnabled())   # вимкнено, бо не обрано
            page._diar_enabled.setChecked(True)
            page._refresh_diarization_controls()
            self.assertTrue(page._diar_count.isEnabled())    # обрано → поле активне
            self.assertEqual(
                page._diar_status.text(),
                "Готово. Обробка відбувається локально на цьому комп’ютері.")

    def test_diar_count_field_fits_auto_placeholder(self):
        """Комбобокс «Кількість співрозмовників» вміщує текст «автоматично»/«automatic»
        та «10» без обрізання."""
        from fronts.desktop import i18n
        from PySide6.QtWidgets import QComboBox
        lang0 = i18n.current_language()
        try:
            for lang in ("uk", "en"):
                i18n.set_language(lang)
                page, _ = self._page([])
                self._open_settings(page)
                cnt = page._diar_count
                self.assertIsInstance(cnt, QComboBox)
                cnt.ensurePolished()
                fm = cnt.fontMetrics()
                auto_txt = i18n.tr("set_diarization_auto")
                need = max(fm.horizontalAdvance(auto_txt), fm.horizontalAdvance("10"))
                self.assertGreaterEqual(
                    cnt.maximumWidth(), need + 24 + 28 + 2,
                    f"[{lang}] комбо ріже {auto_txt!r}: "
                    f"{cnt.maximumWidth()}px < {need}+padding")
        finally:
            i18n.set_language(lang0)

    def test_diar_count_combobox_select_and_save(self):
        """QComboBox співрозмовників: 10 пунктів (авто + 2..10), доступне ім'я, збереження в конфіг."""
        from PySide6.QtWidgets import QComboBox
        page, ctl = self._page([])
        self._open_settings(page)
        cnt = page._diar_count
        self.assertIsInstance(cnt, QComboBox)
        # Жорсткі укр. літерали — assertEqual з tr(того самого ключа) не
        # ловить видалений/зламаний ключ.
        self.assertEqual(cnt.accessibleName(), "Кількість співрозмовників")
        self.assertEqual(cnt.count(), 10)
        self.assertEqual(cnt.itemData(0), None)
        self.assertEqual(cnt.itemText(0), "автоматично")
        for k in range(2, 11):
            idx = k - 1
            self.assertEqual(cnt.itemData(idx), k)
            self.assertEqual(cnt.itemText(idx), str(k))

        # Вибір числа 3 -> збіг у конфігу
        cnt.setCurrentIndex(2)  # data 3
        self.assertEqual(ctl.cfg.diarization_num_speakers, 3)

        # Вибір 0 -> None
        cnt.setCurrentIndex(0)  # data None
        self.assertIsNone(ctl.cfg.diarization_num_speakers)


    def test_meeting_left_labels_on_single_axis(self):
        """Регрес зі злиття meeting-record-phase (аудит 1.2.1 №2): описові
        лейбли панелі налаштувань наради стоять на ЄДИНІЙ лівій осі — усі
        формові лейбли (formlabel) у колонці 0 грід-layout'у з однаковим x.
        Раніше «Кількість співрозмовників» жила у сабрядку колонки 1 і зсувалась
        праворуч, а вісь «з'їжджала»; тест тоді видалили при злитті."""
        from unittest import mock
        from PySide6.QtWidgets import QGridLayout, QLabel
        from fronts.desktop.i18n import tr
        from whisper_core.meeting import diarize
        page, _ = self._page([])
        self._open_settings(page)      # розкласти панель, інакше всі x=0 (хибний зелений)
        # Рядок кількості показуємо лише у стані «готово+увімкнено»; для перевірки
        # осі приводимо панель саме в цей стан, щоб лейбл був видимий і мав x.
        with mock.patch.object(diarize, "runtime_available", return_value=True), \
                mock.patch.object(diarize, "models_present_fast", return_value=True):
            page._diar_enabled.setChecked(True)
            page._refresh_diarization_controls()
        panel = page._diar_count.parentWidget()
        while panel is not None and not isinstance(panel.layout(), QGridLayout):
            panel = panel.parentWidget()
        self.assertIsNotNone(panel, "панель налаштувань наради — QGridLayout")
        grid = panel.layout()
        xs = {}
        for i in range(grid.count()):
            w = grid.itemAt(i).widget()
            if isinstance(w, QLabel) and w.property("formlabel"):
                r, c, *_ = grid.getItemPosition(i)
                self.assertEqual(
                    c, 0, f"формовий лейбл {w.text()!r} має бути в колонці 0")
                xs[w.text()] = w.mapTo(panel, w.rect().topLeft()).x()
        for key in ("set_diarization_label", "set_diarization_count",
                    "protocol_model_label"):
            self.assertIn(tr(key), xs,
                          f"{key} має бути формовим лейблом колонки 0")
        self.assertEqual(len(set(xs.values())), 1,
                         f"усі лейбли форми на одному лівому x: {xs}")

    def _local_model_json(self, tmpdir):
        """Завантажена локальна модель для стану «готова» (файл > CUSTOM_MIN_BYTES)."""
        import os
        from whisper_core.protocol import model_manager as mm
        gguf = os.path.join(tmpdir, "local.gguf")
        with open(gguf, "wb") as f:
            f.write(b"\x00" * 4096)
        cm = mm.CustomModel(id=mm.new_custom_id(), label="Локальна тест-модель",
                            kind=mm.CUSTOM_KIND_LOCAL, path=gguf,
                            approx_size_bytes=4096)
        return cm.to_json()

    def test_model_card_actions_by_download_state(self):
        """Аудит Миколи 22.07: поки модель НЕ завантажена — видно ЛИШЕ «Завантажити
        модель» (акцентна); «Зробити активною» з'являється ПІСЛЯ завантаження.
        Активна картка показує бейдж і не має кнопки «Зробити активною». Кнопки —
        звичайні QPushButton (варіанти accent/danger/ghost із QSS)."""
        import tempfile
        from PySide6.QtWidgets import QPushButton
        from fronts.desktop.glass import StatusTag
        from fronts.desktop.i18n import tr
        tmp = tempfile.mkdtemp()
        page, ctl = self._page([])
        ctl.cfg.custom_models = [self._local_model_json(tmp)]   # одна «готова» модель
        page._refresh_model_list()
        self._open_settings(page)
        box = page._model_list_box
        dl_txt = tr("protocol_model_download")
        make_txt = tr("protocol_model_make_active")
        active_txt = tr("protocol_model_active")
        download_cards = ready_make_cards = active_cards = 0
        for i in range(box.count()):
            w = box.itemAt(i).widget()
            if w is None:
                continue
            btns = {b.text(): b for b in w.findChildren(QPushButton)}
            tags = [t for t in w.findChildren(StatusTag) if t._text == active_txt]
            if tags:
                active_cards += 1
                self.assertNotIn(make_txt, btns,
                                 "активна картка не показує «Зробити активною»")
            if dl_txt in btns:                       # незавантажений пресет
                download_cards += 1
                self.assertNotIn(make_txt, btns,
                                 "незавантажена модель не показує «Зробити активною»")
                self.assertTrue(btns[dl_txt].property("accent"),
                                "«Завантажити модель» має бути акцентною")
            if make_txt in btns:                     # завантажена (локальна) модель
                ready_make_cards += 1
                self.assertNotIn(dl_txt, btns,
                                 "готова модель не показує «Завантажити модель»")
        self.assertEqual(active_cards, 1, "рівно одна активна картка (бейдж)")
        self.assertGreaterEqual(download_cards, 1, "є незавантажені пресети з кнопкою")
        self.assertGreaterEqual(ready_make_cards, 1,
                                "готова модель показує «Зробити активною»")

    def test_model_card_action_row_spacing(self):
        """Аудит 1.2.1 №3: ряди кнопок карток моделей мають узгоджений зазор
        ≥10px (токен 12) — кнопки не склеєні впритул. Кнопки — QPushButton
        (accent/danger/ghost) після перекомпонування 22.07."""
        from PySide6.QtWidgets import QHBoxLayout, QPushButton
        page, _ = self._page([])
        self._open_settings(page)
        box = page._model_list_box
        checked = 0
        for i in range(box.count()):
            w = box.itemAt(i).widget()
            if w is None:
                continue
            lay = w.layout()
            for j in range(lay.count()):
                sub = lay.itemAt(j).layout()
                if isinstance(sub, QHBoxLayout):
                    has_btn = any(isinstance(sub.itemAt(k).widget(), QPushButton)
                                  for k in range(sub.count()))
                    if has_btn:
                        self.assertGreaterEqual(sub.spacing(), 10)
                        checked += 1
        self.assertGreater(checked, 0, "знайдено хоча б один ряд кнопок")

    def test_chapter_chips_seek_player(self):
        """feature/protocol-enrich: чіпи розділів із protocol.md клікабельні —
        клік стрибає вбудований плеєр на таймкод розділу (мок play_from)."""
        import os
        import tempfile
        from whisper_core.protocol import service
        from fronts.desktop.glass import GlassButton

        sdir = tempfile.mkdtemp()
        proto = ("## Підсумок\nТест.\n\n"
                 "## Розділи наради\n"
                 "- [00:00–00:30] Вступ\n- [00:30–01:10] Основне\n")
        with open(os.path.join(sdir, service.PROTOCOL_FILENAME), "w",
                  encoding="utf-8") as f:
            f.write(proto)
        fake_wav = os.path.join(sdir, "mic.wav")
        open(fake_wav, "wb").close()

        page, ctl = self._page([])
        ctl._impl.meeting_audio_paths = lambda sid: {"mic": fake_wav}
        ctl._impl._metas = [SimpleNamespace(
            id=sdir, status="done", title="Розділи", preset="onlymic")]
        page.refresh()
        self._pump()

        # плеєр створено; підмінюємо play_from на запис викликів
        self.assertTrue(page._players)
        player = page._players[-1]
        calls = []
        player.play_from = lambda t, until=None: calls.append(t)

        chip = next(b for b in page.findChildren(GlassButton)
                    if b.text().startswith("00:30 · Основне"))
        chip.click()
        self.assertEqual(calls, [30.0])

    def test_two_tracks_use_multitrack_player(self):
        """feature/player-tracks: нарада з mic+sys дає синхронний MultiTrackPlayer
        із панеллю мікшера (рядок на доріжку)."""
        import os
        import tempfile
        from fronts.desktop.player_tracks import MultiTrackPlayer

        sdir = tempfile.mkdtemp()
        mic = os.path.join(sdir, "mic.wav")
        sys_ = os.path.join(sdir, "sys.wav")
        open(mic, "wb").close()
        open(sys_, "wb").close()

        page, ctl = self._page([])
        ctl._impl.meeting_audio_paths = lambda sid: {"mic": mic, "sys": sys_}
        ctl._impl._metas = [SimpleNamespace(
            id=sdir, status="done", title="Дзвінок", preset="both")]
        page.refresh()
        self._pump()

        self.assertTrue(page._players)
        player = page._players[-1]
        self.assertIsInstance(player, MultiTrackPlayer)
        self.assertEqual([c.key for c in player._panel.channels], ["mic", "sys"])

    def test_single_track_uses_inline_player(self):
        """Одна доріжка (очна розмова, лише мікрофон) лишається тонким
        InlinePlayer — без панелі мікшера."""
        import os
        import tempfile
        from fronts.desktop.player import InlinePlayer
        from fronts.desktop.player_tracks import MultiTrackPlayer

        sdir = tempfile.mkdtemp()
        mic = os.path.join(sdir, "mic.wav")
        open(mic, "wb").close()

        page, ctl = self._page([])
        ctl._impl.meeting_audio_paths = lambda sid: {"mic": mic}
        ctl._impl._metas = [SimpleNamespace(
            id=sdir, status="done", title="Розмова", preset="onlymic")]
        page.refresh()
        self._pump()

        self.assertTrue(page._players)
        player = page._players[-1]
        self.assertIsInstance(player, InlinePlayer)
        self.assertNotIsInstance(player, MultiTrackPlayer)

    def test_editor_opens_selected_track(self):
        """feature/player-tracks (регрес рецензії): у нараді mic+sys кнопка
        «Редагувати аудіо» відкриває редактор САМЕ на обраній у селекторі
        доріжці, а не завжди на майстер-доріжці (mic). Інакше обрізати чи
        заглушити (audioedit_redact) голос співрозмовника (sys) було б неможливо
        через UI — саме та можливість, що зникла до фіксу."""
        import os
        import tempfile
        from PySide6.QtWidgets import QComboBox, QWidget
        from fronts.desktop.glass import GlassButton
        from fronts.desktop.i18n import tr
        import fronts.desktop.pages.meeting as meeting_mod

        sdir = tempfile.mkdtemp()
        mic = os.path.join(sdir, "mic.wav")
        sys_ = os.path.join(sdir, "sys.wav")
        open(mic, "wb").close()
        open(sys_, "wb").close()

        opened = []

        class _FakeEditor(QWidget):     # без валідного WAV: ловимо ЛИШЕ шлях доріжки
            def __init__(self, path, player, controller, parent=None, marks=None,
                         source=None):
                super().__init__(parent)
                opened.append(path)

        page, ctl = self._page([])
        ctl._impl.meeting_audio_paths = lambda sid: {"mic": mic, "sys": sys_}
        ctl._impl._metas = [SimpleNamespace(
            id=sdir, status="done", title="Дзвінок", preset="both", bookmarks=None)]

        with patch.object(meeting_mod, "AudioEditorPanel", _FakeEditor):
            page.refresh()
            self._pump()

            combo = next(c for c in page.findChildren(QComboBox)
                         if c.accessibleName() == tr("audioedit_track_pick"))
            edit = next(b for b in page.findChildren(GlassButton)
                        if b.text() == tr("audioedit_open"))
            # Селектор перелічує ОБИДВІ доріжки в порядку запису.
            self.assertEqual(
                [combo.itemData(i) for i in range(combo.count())], ["mic", "sys"])

            edit.click()                                # дефолт — майстер (mic)
            self.assertEqual(opened, [mic])

            combo.setCurrentIndex(combo.findData("sys"))  # обрали співрозмовника
            edit.click()
            self.assertEqual(opened, [mic, sys_])

    def test_qa_dialog_citation_chips_seek(self):
        """feature/meeting-qa: таймкод-цитати у відповіді Q&A клікабельні —
        клік стрибає плеєр на момент (мок seek). Без реальної моделі: перевіряємо
        парсинг цитат у чіпи + прив'язку до seek, не запускаючи worker."""
        from fronts.desktop.pages.protocol_ui import QADialog

        calls = []
        dlg = QADialog([SimpleNamespace(start=0.0, end=1.0, speaker="me",
                                        text="привіт", source="mic")],
                       "fast", {"me_label": "Я", "others_label": "Співрозмовники",
                                "speaker_names": None},
                       seek=lambda t: calls.append(t))
        self.addCleanup(dlg.deleteLater)
        dlg._build_citations("Звіт готує перша рота [00:12], дедлайн у п'ятницю [00:30].")
        self._pump()
        # два чіпи-цитати, з accessibleName та активні (seek заданий)
        self.assertEqual([b.text() for b in dlg._cite_buttons], ["00:12", "00:30"])
        self.assertTrue(all(b.isEnabled() for b in dlg._cite_buttons))
        self.assertTrue(all(b.accessibleName() for b in dlg._cite_buttons))
        dlg._cite_buttons[1].click()
        self.assertEqual(calls, [30.0])

    def test_qa_dialog_citation_chips_disabled_without_player(self):
        """Без плеєра (seek=None) чіпи показуються, але не клікабельні —
        не падаємо на клік, коли стрибати нема куди."""
        from fronts.desktop.pages.protocol_ui import QADialog

        dlg = QADialog([SimpleNamespace(start=0.0, end=1.0, speaker="me",
                                        text="привіт", source="mic")],
                       "fast", {"me_label": "Я", "others_label": "Співрозмовники",
                                "speaker_names": None}, seek=None)
        self.addCleanup(dlg.deleteLater)
        dlg._build_citations("Ось момент [01:05].")
        self._pump()
        self.assertEqual([b.text() for b in dlg._cite_buttons], ["01:05"])
        self.assertFalse(dlg._cite_buttons[0].isEnabled())

    def test_audio_ready_reconnect_no_warning(self):
        """Раунд 2, фікс 4: повторний audio_ready переприв'язує кнопку точковим
        disconnect (без сліпого disconnect() → без RuntimeWarning); активний
        лишається ЛИШЕ останній хендлер."""
        import warnings
        page, ctl = self._page([])
        opened = []
        ctl._impl.open_meeting_audio = lambda sid: opened.append(sid)
        with warnings.catch_warnings():
            warnings.simplefilter("error")     # будь-який warning = провал тесту
            page._on_audio_ready("A")
            page._on_audio_ready("B")
        page._live_open_audio.click()
        self.assertEqual(opened, ["B"])


if __name__ == "__main__":
    # Standalone-раннер: після ВЕРДИКТУ (усі assert відпрацювали) виходимо
    # os._exit — це навмисно обходить нативну static-деструкцію offscreen-Qt на
    # завершенні інтерпретатора (джерело флакі-0xC000041D ПІСЛЯ «OK»). Код виходу
    # чесний: він = результат тестів, а не замасковане echo.
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(MeetingRenderTests))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
