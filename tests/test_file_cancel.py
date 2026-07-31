"""Скасування розпізнавання файлу (fix/cancel-transcription).

Чотири шари, кожен зі своєю мутацією-контролем:

1. ПРОВОДКА (CancelWiringTests): клік по «Скасувати» у картці файлу кличе саме
   controller.cancel_file_job(jid) — і жодного іншого методу контролера; кнопка
   працює і в черзі, і в роботі.
   Мутація: від'єднати cancel.clicked у FilesPage.add_files → тест червоніє
   (список викликів контролера лишається порожнім).

2. ВИДИМІСТЬ (CancelVisibilityTests): смужка ходу є ЛИШЕ у стані розпізнавання,
   «Скасувати» — від постановки в чергу до завершення задачі.
   Мутація: прибрати _show_progress(row, True) з _on_status → тест червоніє
   (смужка не показана в стані розпізнавання); лишити кнопку прихованою в
   черзі → червоніє test_cancel_available_while_queued_without_progress_bar.

3. СПРАВЖНЯ ЗУПИНКА (EngineCancelTests, ControllerCancelTests): рушій уривається
   між сегментами (а не «домальовує» результат), контролер позначає задачу й
   пробрасує зворотний виклик у рушій.
   Мутація: прибрати перевірку should_cancel у Engine.transcribe → тест червоніє
   (виняток не піднімається, генератор дочитується до кінця).

4. РОБОЧИЙ ПОТІК (FileWorkerTests): сам _file_worker із підставним рушієм —
   скасування доходить до картки як «скасовано», без стек-трейсу в лозі, а
   задача, знята ще в черзі, не запускає рушій узагалі.
   Мутації: прибрати except TranscriptionCancelled → картка отримує «помилка» і
   в лог іде стек-трейс; прибрати гілку «скасовано в черзі» → рушій стартує на
   вже знятій задачі. Обидві червонять тести.

Сторінка будується напряму (FilesPage), без MainWindow — легший процес; QTimer
глушимо в tearDown, як у render-smoke-тестах.
"""
import logging
import os
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer, QAbstractAnimation  # noqa: E402
from PySide6.QtWidgets import QApplication, QProgressBar  # noqa: E402

from fronts.desktop import motion  # noqa: E402
from fronts.desktop.glass import GlassButton  # noqa: E402
from fronts.desktop.i18n import (   # noqa: E402
    current_language, set_language, tr,
)
from fronts.desktop.main_window import FilesPage, FileStatus  # noqa: E402
from tests.render_nav_smoke import _NavController, _make_sandbox  # noqa: E402

_JID = 7


class _RecordingController(_NavController):
    """Контролер, що ЗАПИСУЄ звернення сторінки: тест перевіряє не лише «щось
    сталось», а що покликано саме cancel_file_job із правильним номером задачі."""

    def __init__(self, sandbox):
        super().__init__(sandbox)
        self.calls = []

    def enqueue_file(self, path, model=None):
        return _JID

    def cancel_file_job(self, jid):
        self.calls.append(("cancel_file_job", jid))
        return True

    # найімовірніші «сусіди», якими можна помилково підмінити скасування —
    # теж під записом, щоб тест ловив підміну, а не лише мовчання
    def record_cancel(self):
        self.calls.append(("record_cancel", None))

    def dictaphone_cancel(self):
        self.calls.append(("dictaphone_cancel", None))

    def transcribe_recording(self, *a):
        self.calls.append(("transcribe_recording", None))


class _FilesPageCase(unittest.TestCase):
    """Спільний каркас: жива FilesPage на тимчасовому корені словників."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        motion.init_config(SimpleNamespace(animations=False))   # без живих таймерів

    def setUp(self):
        self._language = current_language()
        self.addCleanup(set_language, self._language)
        set_language("uk")
        self.sandbox = _make_sandbox()
        self.controller = _RecordingController(self.sandbox)
        self.page = FilesPage(self.controller)

    def tearDown(self):
        for anim in self.page.findChildren(QAbstractAnimation):
            anim.stop()
        for timer in self.page.findChildren(QTimer):
            timer.stop()
        self.page.deleteLater()
        self.app.processEvents()
        shutil.rmtree(self.sandbox, ignore_errors=True)

    # --- пошук елементів картки ---
    def _row(self, jid=_JID):
        return self.page._rows[jid][2]

    def _cancel_button(self, jid=_JID):
        return [b for b in self._row(jid).findChildren(GlassButton)
                if b.text() == tr("files_cancel")]

    def _progress_bars(self, jid=_JID):
        return self._row(jid).findChildren(QProgressBar)

    def _shown(self, widget, jid=_JID):
        """Чи показаний елемент у межах картки (сторінку не показуємо, тож
        isVisible() завжди False — питаємо саме isVisibleTo(картка))."""
        return widget.isVisibleTo(self._row(jid))

    def _add_row(self):
        self.page.add_files([Path("зразок.wav")])
        self.app.processEvents()

    def _start_transcribing(self):
        self.page._on_status(_JID, FileStatus.TRANSCRIBING)
        self.app.processEvents()


class CancelWiringTests(_FilesPageCase):
    def test_click_calls_cancel_file_job_with_this_job_id(self):
        self._add_row()
        self._start_transcribing()
        buttons = self._cancel_button()
        self.assertEqual(len(buttons), 1, "у картці немає кнопки «Скасувати»")

        buttons[0].click()
        self.app.processEvents()

        self.assertEqual(self.controller.calls, [("cancel_file_job", _JID)])

    def test_click_while_still_queued_cancels_the_job(self):
        """Задачу можна зняти ДО старту: без цього гілка «скасовано в черзі» у
        воркері була б недосяжною."""
        self._add_row()
        self._cancel_button()[0].click()
        self.app.processEvents()
        self.assertEqual(self.controller.calls, [("cancel_file_job", _JID)])

    def test_controller_has_cancel_file_job(self):
        """Контракт: справжній контролер несе метод, до якого підключена кнопка
        (а не сторінка вдає скасування сама)."""
        from fronts.desktop.app import DesktopApp
        self.assertTrue(callable(getattr(DesktopApp, "cancel_file_job", None)))

    def test_second_click_does_not_repeat_the_request(self):
        """Кнопка гасне після натиску: подвійний клік не шле друге скасування."""
        self._add_row()
        self._start_transcribing()
        button = self._cancel_button()[0]
        button.click()
        button.click()
        self.app.processEvents()
        self.assertEqual(self.controller.calls, [("cancel_file_job", _JID)])


class CancelVisibilityTests(_FilesPageCase):
    def test_cancel_available_while_queued_without_progress_bar(self):
        """Поставила десять файлів, передумала: «Скасувати» доступне ще в черзі.
        Смужки ходу при цьому немає — робота ще не почалась."""
        self._add_row()
        self.assertTrue(self._progress_bars(), "смужку ходу не побудовано")
        self.assertFalse(self._shown(self._progress_bars()[0]),
                         "смужка ходу видна ще до початку розпізнавання")
        self.assertTrue(self._shown(self._cancel_button()[0]),
                        "«Скасувати» недоступне у стані «у черзі»")

    def test_bar_and_cancel_hidden_after_cancel_from_queue(self):
        """Скасовано з черги (без переходу в «розпізнаю») — керування зникає."""
        self._add_row()
        self.page._on_done(_JID, tr("files_cancelled_body"),
                           FileStatus.CANCELLED, [], [])
        self.app.processEvents()
        self.assertFalse(self._shown(self._cancel_button()[0]),
                         "«Скасувати» лишилось після скасування з черги")

    def test_bar_and_cancel_shown_while_transcribing(self):
        self._add_row()
        self._start_transcribing()
        bars = self._progress_bars()
        self.assertTrue(bars, "смужку ходу не побудовано")
        self.assertTrue(self._shown(bars[0]),
                        "смужка ходу не показана у стані розпізнавання")
        self.assertEqual((bars[0].minimum(), bars[0].maximum()), (0, 0),
                         "смужка мусить бути невизначеною: рушій не дає відсотків")
        self.assertTrue(self._shown(self._cancel_button()[0]),
                        "«Скасувати» не показано у стані розпізнавання")

    def test_bar_and_cancel_hidden_after_cancel(self):
        self._add_row()
        self._start_transcribing()
        self.page._on_done(_JID, tr("files_cancelled_body"),
                           FileStatus.CANCELLED, [], [])
        self.app.processEvents()
        self.assertFalse(self._shown(self._progress_bars()[0]),
                         "смужка ходу лишилась після скасування")
        self.assertFalse(self._shown(self._cancel_button()[0]),
                         "«Скасувати» лишилось після скасування")

    def test_bar_and_cancel_hidden_after_success(self):
        self._add_row()
        self._start_transcribing()
        self.page._on_done(_JID, "привіт", f"{FileStatus.DONE}:2", [], [])
        self.app.processEvents()
        self.assertFalse(self._shown(self._progress_bars()[0]),
                         "смужка ходу лишилась після готового результату")

    def test_cancelled_card_shows_cancelled_badge_not_transcribing(self):
        """Головний симптом бага: картка не має зависати в «розпізнаю»."""
        self._add_row()
        self._start_transcribing()
        status = self.page._rows[_JID][0]
        self.assertEqual(status.kind(), "busy")
        self.page._on_done(_JID, tr("files_cancelled_body"),
                           FileStatus.CANCELLED, [], [])
        self.app.processEvents()
        self.assertEqual(status.kind(), "warn")
        self.assertEqual(status.accessibleName(), "скасовано")

    def test_cancelled_card_offers_retry(self):
        self._add_row()
        self._start_transcribing()
        self.page._on_done(_JID, tr("files_cancelled_body"),
                           FileStatus.CANCELLED, [], [])
        self.app.processEvents()
        retry = [b for b in self._row().findChildren(GlassButton)
                 if b.text() == tr("files_retry")]
        self.assertEqual(len(retry), 1, "після скасування немає «Повторити»")


class EngineCancelTests(unittest.TestCase):
    """Рушій справді СПИНЯЄТЬСЯ, а не добігає до кінця з відкинутим результатом."""

    @staticmethod
    def _fake_engine(pulled):
        def segments():
            for i in range(5):
                pulled.append(i)
                yield SimpleNamespace(text=f"частина {i}", start=float(i),
                                      end=float(i) + 1.0, words=[])
        model = SimpleNamespace(
            transcribe=lambda *a, **k: (segments(),
                                        SimpleNamespace(duration=5.0)))
        cfg = SimpleNamespace(language="uk", beam_size=1)
        return SimpleNamespace(model=model, cfg=cfg)

    def test_cancel_raises_and_stops_pulling_segments(self):
        from whisper_core.engine import Engine, TranscriptionCancelled
        pulled = []
        eng = self._fake_engine(pulled)
        # скасовано після першого сегмента
        state = {"n": 0}

        def should_cancel():
            state["n"] += 1
            return state["n"] > 1

        with self.assertRaises(TranscriptionCancelled):
            Engine.transcribe(eng, "x.wav", should_cancel=should_cancel)
        self.assertLess(len(pulled), 5,
                        "генератор дочитано до кінця — робота не спинилась")

    def test_without_cancel_full_result(self):
        from whisper_core.engine import Engine
        pulled = []
        eng = self._fake_engine(pulled)
        raw, final, dur, _words, segs = Engine.transcribe(eng, "x.wav")
        self.assertEqual(len(segs), 5)
        self.assertEqual(dur, 5.0)
        self.assertIn("частина 0", raw)
        self.assertIn("частина 4", final)


class _StopWorker(Exception):
    """Черга «скінчилась» — єдиний спосіб вийти з безкінечного циклу воркера."""


class _ScriptedQueue:
    """Черга із заданим сценарієм: віддає задачі по одній, потім спиняє воркер."""

    def __init__(self, jobs):
        self._jobs = list(jobs)

    def get(self):
        if not self._jobs:
            raise _StopWorker
        return self._jobs.pop(0)


class _Signal:
    """Підставний сигнал: замість Qt-проводки просто збирає emit-и."""

    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


class _LogSpy(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class FileWorkerTests(unittest.TestCase):
    """Серце фічі: сам робочий потік черги файлів _file_worker.

    Воркер проганяється як незв'язаний метод на підставному стані — без Qt,
    без моделі й без справжньої черги. Мутація «прибрати except
    TranscriptionCancelled» валить обидві головні перевірки: картка отримує
    «помилка» замість «скасовано», а в лог іде стек-трейс."""

    def setUp(self):
        self.spy = _LogSpy()
        logging.getLogger().addHandler(self.spy)
        self.addCleanup(logging.getLogger().removeHandler, self.spy)
        self.transcribe_calls = []
        self.forgotten = []

    # --- каркас ---
    def _run_worker(self, *, cancelled=(), transcribe=None):
        from fronts.desktop.app import DesktopApp
        cancelled = set(cancelled)

        def done_transcribe(path, terms, model, **kw):
            self.transcribe_calls.append((path, model, kw))
            return ("сирий текст", "готовий текст", 2.0, [], [])

        ns = SimpleNamespace(
            _file_jobs=_ScriptedQueue([("file", _JID, "зразок.wav", None)]),
            profile=SimpleNamespace(history_path=None, memory_enabled=False),
            terms={},
            cfg=SimpleNamespace(highlight_uncertain_words=False,
                                auto_export_enabled=False,
                                auto_export_dir=None),
            file_status=_Signal(),
            file_done=_Signal(),
            _file_job_cancelled=lambda jid: jid in cancelled,
            _forget_file_job=self.forgotten.append,
            _transcribe_file_job=transcribe or done_transcribe,
        )
        with self.assertRaises(_StopWorker):
            DesktopApp._file_worker(ns)
        return ns

    def _cancelling_engine(self, path, terms, model, **kw):
        from whisper_core.engine import TranscriptionCancelled
        self.transcribe_calls.append((path, model, kw))
        raise TranscriptionCancelled()

    def _tracebacks(self):
        return [r for r in self.spy.records if r.exc_info]

    def _errors(self):
        return [r for r in self.spy.records if r.levelno >= logging.ERROR]

    # --- активну задачу скасовано ---
    def test_cancelled_engine_gives_cancelled_card_not_error(self):
        ns = self._run_worker(transcribe=self._cancelling_engine)
        self.assertEqual(len(ns.file_done.emitted), 1,
                         "рядок черги не закрито жодним file_done")
        jid, text, meta, segs, words = ns.file_done.emitted[0]
        self.assertEqual(jid, _JID)
        self.assertEqual(meta, FileStatus.CANCELLED,
                         "скасування дійшло до картки не як «скасовано»")
        self.assertEqual(text, tr("files_cancelled_body"))
        self.assertEqual((segs, words), ([], []),
                         "скасована задача не має віддавати ні сегментів, ні слів")

    def test_cancelled_engine_writes_no_traceback_to_log(self):
        self._run_worker(transcribe=self._cancelling_engine)
        self.assertEqual(self._tracebacks(), [],
                         "свідоме скасування лягло в лог стек-трейсом")
        self.assertEqual(self._errors(), [],
                         "свідоме скасування записано як помилку")

    def test_cancelled_job_forgotten_after_finish(self):
        """Позначка не лишається за номером задачі — інакше повтор одразу «скасує»."""
        self._run_worker(transcribe=self._cancelling_engine)
        self.assertIn(_JID, self.forgotten)

    def test_should_cancel_callback_handed_to_transcribe(self):
        self._run_worker(transcribe=self._cancelling_engine)
        _path, _model, kw = self.transcribe_calls[0]
        self.assertTrue(callable(kw.get("should_cancel")),
                        "воркер не дав рушієві способу дізнатись про скасування")
        self.assertFalse(kw["should_cancel"](),
                         "нескасована задача звітує про скасування")

    # --- контроль: справжній збій лишається помилкою ---
    def test_real_failure_still_reports_error_with_traceback(self):
        def broken(path, terms, model, **kw):
            raise RuntimeError("рушій упав")

        ns = self._run_worker(transcribe=broken)
        self.assertEqual(ns.file_done.emitted[0][2], FileStatus.ERROR)
        self.assertTrue(self._tracebacks(),
                        "справжній збій мусить лишити стек-трейс у лозі")

    # --- задачу знято, поки вона чекала у черзі ---
    def test_job_cancelled_while_queued_never_starts_engine(self):
        ns = self._run_worker(cancelled={_JID})
        self.assertEqual(self.transcribe_calls, [],
                         "знята з черги задача все одно витратила модель")
        self.assertEqual(ns.file_status.emitted, [],
                         "картка знятої задачі показала «розпізнаю»")
        self.assertEqual(ns.file_done.emitted[0][2], FileStatus.CANCELLED)
        self.assertIn(_JID, self.forgotten)

    # --- контроль каркаса: успішна задача ---
    def test_successful_job_reports_done(self):
        ns = self._run_worker()
        self.assertEqual(ns.file_status.emitted, [(_JID, FileStatus.TRANSCRIBING)])
        jid, text, meta, _segs, _words = ns.file_done.emitted[0]
        self.assertEqual((jid, text), (_JID, "готовий текст"))
        self.assertTrue(meta.startswith(f"{FileStatus.DONE}:"), meta)
        self.assertEqual(self._tracebacks(), [])


class ControllerCancelTests(unittest.TestCase):
    """Контролер: позначка задачі й проброс зворотного виклику в рушій."""

    def setUp(self):
        import threading
        self.ns = SimpleNamespace(_job_seq=3, _file_cancelled=set(),
                                  _file_cancel_lock=threading.Lock())

    def _call(self, name, *a):
        from fronts.desktop.app import DesktopApp
        return getattr(DesktopApp, name)(self.ns, *a)

    def test_cancel_marks_job_and_forget_clears_it(self):
        self.assertTrue(self._call("cancel_file_job", 2))
        self.assertTrue(self._call("_file_job_cancelled", 2))
        self.assertFalse(self._call("_file_job_cancelled", 1))
        self._call("_forget_file_job", 2)
        self.assertFalse(self._call("_file_job_cancelled", 2))

    def test_unknown_job_id_rejected(self):
        self.assertFalse(self._call("cancel_file_job", 99))
        self.assertFalse(self._call("cancel_file_job", 0))
        self.assertEqual(self.ns._file_cancelled, set())

    def test_should_cancel_reaches_engine_path(self):
        """_transcribe_file_job пробрасує should_cancel у шлях рушія. Мутація:
        прибрати **cancel_kw → ключа в kwargs не буде → тест червоніє."""
        from fronts.desktop.app import DesktopApp
        seen = {}

        def fake_fallback(path, terms, **kw):
            seen.update(kw)
            return ("raw", "текст", 1.0, [], [])

        ns = SimpleNamespace(cfg=SimpleNamespace(model_name="large-v3"),
                             _transcribe_with_fallback=fake_fallback)
        marker = lambda: True                                   # noqa: E731
        DesktopApp._transcribe_file_job(ns, "x.wav", None, None,
                                        should_cancel=marker)
        self.assertIs(seen.get("should_cancel"), marker)

    def test_no_cancel_callback_not_passed(self):
        """Без скасування ключ у рушій НЕ йде — старі мок-рушії не ламаються."""
        from fronts.desktop.app import DesktopApp
        seen = {}

        def fake_fallback(path, terms, **kw):
            seen.update(kw)
            return ("raw", "текст", 1.0, [], [])

        ns = SimpleNamespace(cfg=SimpleNamespace(model_name="large-v3"),
                             _transcribe_with_fallback=fake_fallback)
        DesktopApp._transcribe_file_job(ns, "x.wav", None, None)
        self.assertNotIn("should_cancel", seen)


if __name__ == "__main__":
    unittest.main()
