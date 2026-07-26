"""feature/no-model-state: застосунок живе без завантаженого мовного пакета
розпізнавання замість sys.exit(0) (СПЕЦ 2026-07-25, пункт 1).

Фейк-self патерн — як tests/test_win_hardening.py: реальні методи DesktopApp
кличемо на SimpleNamespace, а не будуємо важкий повний застосунок (вікно/трей/
хоткеї). Це чесно ловить регресії в самій логіці гейтів, без крихкості GUI.
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from whisper_core.engine import Engine, NullEngine, ModelAbsentError
from fronts.desktop.app import DesktopApp, _recover_engine_on_gui
from whisper_core.engine import ModelRevisionUnavailable
from fronts.desktop.main_window import FilesPage


class _FakeSignal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


class _FakeTray:
    def __init__(self):
        self.notices = []

    def notify(self, text):
        self.notices.append(text)


class TestNullEngine(unittest.TestCase):
    def test_is_available_false(self):
        self.assertFalse(NullEngine().is_available)

    def test_engine_is_available_true(self):
        # Уніфікований інтерфейс: реальний Engine теж має is_available.
        self.assertTrue(Engine.is_available)

    def test_transcribe_raises_typed_error(self):
        with self.assertRaises(ModelAbsentError):
            NullEngine().transcribe("audio.wav")


class TestHasModelProperty(unittest.TestCase):
    """Мутація-детектор: підміна `and` на `or`, чи забуте getattr — тут падає."""

    def test_true_with_available_engine(self):
        app = SimpleNamespace(engine=SimpleNamespace(is_available=True))
        self.assertTrue(DesktopApp.has_model.fget(app))

    def test_false_with_null_engine(self):
        app = SimpleNamespace(engine=NullEngine())
        self.assertFalse(DesktopApp.has_model.fget(app))

    def test_false_when_engine_absent(self):
        app = SimpleNamespace(engine=None)
        self.assertFalse(DesktopApp.has_model.fget(app))


class TestPttWithoutModel(unittest.TestCase):
    """test_ptt_press_without_model_emits_warning (СПЕЦ, п.1в.2): натиск PTT
    без мовного пакета — 0 падінь, чесний тост + transcription_error.

    Мутація, яку ловить тест: видалення `if not self.has_model` у
    _start_recording() — тоді код пішов би у recorder.has_stream і далі,
    і жодного tr("app_model_absent_ptt") у transcription_error не було б."""

    def _app(self):
        app = SimpleNamespace()
        app.has_model = False
        app.tray = _FakeTray()
        app.transcription_error = _FakeSignal()
        app._key_down = False
        app._mic_testing = False
        app._cancel_guard = False
        app._capturing = False
        app._meeting_active = False
        app._note_dictating = False
        app._command_dictating = False
        app._dictaphone_active = False
        app._busy = False
        app._queue = None
        app.recorder = SimpleNamespace(recording=False)
        app.cfg = SimpleNamespace(ptt_mode="hold", dictation_queue_enabled=True)
        app._start_recording = lambda: DesktopApp._start_recording(app)
        return app

    def test_on_press_emits_model_absent_warning(self):
        app = self._app()
        DesktopApp.on_press(app)  # не мусить кинути жодного винятку
        self.assertEqual(len(app.transcription_error.emitted), 1)
        self.assertIn("Мовний пакет розпізнавання",
                      app.transcription_error.emitted[0][0])
        self.assertEqual(len(app.tray.notices), 1)

    def test_start_recording_returns_before_touching_recorder(self):
        app = self._app()
        # recorder без has_stream: якби гейт не спрацював, доступ до
        # неіснуючого атрибута кинув би AttributeError — доводить, що гейт
        # справді перериває функцію ДО решти тіла.
        DesktopApp._start_recording(app)
        self.assertEqual(len(app.transcription_error.emitted), 1)


class TestRecoveryReturnsNullEngine(unittest.TestCase):
    """Глухий кут майстра/відновлення (СПЕЦ п.1а.1-2): відмова від докачки
    більше не веде до sys.exit(0), а повертає NullEngine.

    Мутація, яку ловить тест: повернення sys.exit(0) замість NullEngine —
    тест впаде на SystemExit замість очікуваного NullEngine."""

    @classmethod
    def setUpClass(cls):
        cls._qapp = QApplication.instance() or QApplication([])

    def test_declining_recovery_dialog_yields_null_engine(self):
        class _FakeDialog:
            def __init__(self, cfg, err):
                pass

            def exec(self):
                return False  # користувач відмовився від докачки

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = SimpleNamespace(model_name="large-v3-turbo",
                              model_dir=str(Path(tmp.name)))
        err = ModelRevisionUnavailable("large-v3-turbo", cfg.model_dir,
                                       "deadbeef", False)
        with patch("fronts.desktop.recovery.RecoveryDialog", _FakeDialog):
            engine = _recover_engine_on_gui(cfg, err, splash=None)
        self.assertIsInstance(engine, NullEngine)


class TestFilesPageStaysAlive(unittest.TestCase):
    """test_other_pages_alive_without_model (СПЕЦ п.1в): вкладка «Файли»
    показує чесний банер і блокує вибір файлів, але БУДУЄТЬСЯ без винятків —
    решта застосунку (плеєр/історія/налаштування) цю сторінку не тягне.

    Мутація, яку ловить тест: видалення `pick.setEnabled(not self._no_model)`
    чи банера — кнопка лишилась би активною без жодного попередження."""

    @classmethod
    def setUpClass(cls):
        cls._qapp = QApplication.instance() or QApplication([])

    def _controller(self, has_model: bool) -> MagicMock:
        controller = MagicMock()
        controller.has_model = has_model
        return controller

    def test_page_builds_without_model_and_disables_pick(self):
        page = FilesPage(self._controller(has_model=False))
        self.addCleanup(page.deleteLater)
        pick_buttons = [w for w in page.findChildren(object)
                        if getattr(w, "text", lambda: "")() == "Вибрати файли…"]
        self.assertTrue(pick_buttons, "кнопка вибору файлів має існувати")
        self.assertFalse(pick_buttons[0].isEnabled())
        self.assertFalse(page.acceptDrops())

    def test_page_builds_normally_with_model(self):
        page = FilesPage(self._controller(has_model=True))
        self.addCleanup(page.deleteLater)
        pick_buttons = [w for w in page.findChildren(object)
                        if getattr(w, "text", lambda: "")() == "Вибрати файли…"]
        self.assertTrue(pick_buttons[0].isEnabled())
        self.assertTrue(page.acceptDrops())


if __name__ == "__main__":
    unittest.main()
