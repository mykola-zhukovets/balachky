"""Regression: idle-unloaded STT remains usable from every user entry point."""
import os
import threading
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fronts.desktop.app import DesktopApp
from fronts.desktop.main_window import FilesPage
from whisper_core.engine import ModelRevisionUnavailable, NullEngine
from whisper_core.heavy_models import HeavyModelCoordinator
from whisper_core.model_lifecycle import LOADED, LOADING, UNLOADED, ModelLifecycle


_8GB = 8 * (1024 ** 3)


class _IdleDesktopHarness:
    """Lightweight controller that uses the real DesktopApp.has_model property."""

    has_model = DesktopApp.has_model

    def __init__(self, state=UNLOADED, *, installed=True, engine=None):
        self.engine = engine
        self._stt_model_installed = installed
        self._model_lifecycle = SimpleNamespace(state=state)
        self._engine_lock = threading.Lock()
        self._models_busy = lambda: False
        self.tray = SimpleNamespace(notify=MagicMock(), set_state=MagicMock())
        self.transcription_error = SimpleNamespace(emit=MagicMock())
        self.rec_state = SimpleNamespace(emit=MagicMock())
        self.model_lifecycle_state = SimpleNamespace(emit=MagicMock())
        self.recorder = SimpleNamespace(
            has_stream=True, recording=False, start=MagicMock())
        self.cfg = SimpleNamespace(ptt_mode="hold")
        self._key_down = False
        self._mic_testing = False
        self._cancel_guard = False
        self._capturing = False
        self._meeting_active = False
        self._note_dictating = False
        self._command_dictating = False
        self._dictaphone_active = False
        self._busy = False
        self._queue = None
        self._mic_warned = False
        self._capture_context_dictionary = MagicMock()
        self._context_terms = []
        self.terms = []
        self._start_live_dictation = MagicMock()
        self._before_microphone_start = MagicMock(return_value=True)
        self._request_model_reload = MagicMock()
        self._start_dictation_watch = MagicMock()
        self._start_recording = DesktopApp._start_recording.__get__(self)


class IdleModelEntryPointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._qapp = QApplication.instance() or QApplication([])

    def _assert_recording_started(self, entry_point):
        app = _IdleDesktopHarness()
        with patch("fronts.desktop.wininput.get_foreground_window",
                   return_value=101), \
                patch("fronts.desktop.wininput.capture_paste_target",
                      return_value=SimpleNamespace()), \
                patch("fronts.desktop.app.diagnostic_event"):
            entry_point(app)

        app.recorder.start.assert_called_once_with()
        app._request_model_reload.assert_called_once_with()
        app.tray.notify.assert_not_called()
        app.transcription_error.emit.assert_not_called()

    @staticmethod
    def _attach_real_lifecycle(app, load):
        lifecycle = ModelLifecycle(
            timeout_seconds=0,
            unload=DesktopApp._unload_idle_models.__get__(app),
            load=load,
            is_busy=app._models_busy,
        )
        app._model_lifecycle = lifecycle
        return lifecycle

    @staticmethod
    def _force_unload(lifecycle):
        with patch("whisper_core.protocol.sidecar.idle_transition",
                   return_value=nullcontext()), \
                patch("whisper_core.protocol.sidecar.shutdown_all"), \
                patch("fronts.desktop.app.diagnostic_event"):
            return lifecycle.force_unload()

    def test_has_model_state_matrix(self):
        cases = (
            ("pre-lifecycle", SimpleNamespace(engine=None), False),
            ("null-engine", _IdleDesktopHarness(
                installed=True, engine=NullEngine()), False),
            ("none-loaded", _IdleDesktopHarness(LOADED), False),
            ("installed-unloaded", _IdleDesktopHarness(UNLOADED), True),
            ("installed-loading", _IdleDesktopHarness(LOADING), True),
        )
        for label, app, expected in cases:
            with self.subTest(label=label):
                self.assertIs(DesktopApp.has_model.fget(app), expected)

    def test_main_window_constructor_sees_initial_model_state(self):
        engine = SimpleNamespace(is_available=True)
        profile = SimpleNamespace(
            name="default", memory_enabled=False, macros_path=Path("macros.toml"))
        observed = {}

        class ConstructorProbeReached(Exception):
            pass

        class MainWindowProbe:
            def __init__(self, controller):
                observed["engine"] = getattr(controller, "engine", None)
                observed["installed"] = getattr(
                    controller, "_stt_model_installed", None)
                observed["has_model"] = controller.has_model
                raise ConstructorProbeReached

        cfg = SimpleNamespace(
            ui_language="uk",
            screen_protection=False,
            player_resume_backstep_s=1.5,
        )
        patchers = (
            patch("fronts.desktop.app._migrate_update_cache"),
            patch("whisper_core.win_hardening.exclude_process_from_wer"),
            patch("whisper_core.win_hardening.set_capture_protection_enabled"),
            patch("fronts.desktop.player.set_resume_backstep_ms"),
            patch("fronts.desktop.app._refresh_frozen_autostart"),
            patch("fronts.desktop.app.i18n.set_language"),
            patch("fronts.desktop.app.profiles.get_active",
                  return_value=profile),
            patch("fronts.desktop.app.profiles.get",
                  return_value=profile),
            patch("fronts.desktop.app.profiles.list_profiles",
                  return_value=[profile]),
            patch.object(DesktopApp, "_migrate_processing_mode"),
            patch.object(DesktopApp, "_profile_terms", return_value=[]),
            patch.object(DesktopApp, "reload_context_profiles"),
            patch("fronts.desktop.app.ContextResolver"),
            patch("fronts.desktop.app.SecurityGate"),
            patch("fronts.desktop.app.paths.context_profiles_path",
                  return_value=Path("context_profiles.toml")),
            patch("fronts.desktop.app.paths.snippets_path",
                  return_value=Path("snippets.toml")),
            patch("fronts.desktop.app.migrate_snippets"),
            patch.object(DesktopApp, "_reload_macros"),
            patch("fronts.desktop.app.navcommands.load_aliases",
                  return_value={}),
            patch("fronts.desktop.app.navcommands.aliases_path",
                  return_value=Path("navcommands.toml")),
            patch("fronts.desktop.app.Tray",
                  return_value=SimpleNamespace(notify=MagicMock())),
            patch("fronts.desktop.app._set_audit_corrupt_notifier"),
            patch("fronts.desktop.app.UndoBuffer"),
            patch("fronts.desktop.app.PasteHistory"),
            patch("fronts.desktop.app.MainWindow", MainWindowProbe),
        )
        with ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            with self.assertRaises(ConstructorProbeReached):
                DesktopApp(self._qapp, engine, cfg)

        self.assertIs(observed["engine"], engine)
        self.assertIs(observed["installed"], True)
        self.assertIs(observed["has_model"], True)

    def test_dictation_button_public_entry_reaches_recording_and_reload(self):
        self._assert_recording_started(DesktopApp.record_start)

    def test_global_hotkey_reaches_recording_and_reload(self):
        self._assert_recording_started(DesktopApp.on_press)

    def test_audio_files_gate_reaches_enqueue(self):
        controller = _IdleDesktopHarness()
        controller.file_status = MagicMock()
        controller.file_done = MagicMock()
        controller.open_recordings_folder = MagicMock()
        controller.dictaphone_level = MagicMock(return_value=0.0)
        controller.enqueue_file = MagicMock(return_value=7)
        page = FilesPage(controller)
        self.addCleanup(page.deleteLater)

        path = Path("idle.wav")
        page.add_files([path])

        controller.enqueue_file.assert_called_once_with(path, model=None)
        controller.tray.notify.assert_not_called()

    def test_absent_model_stays_absent_after_low_ram_tts_eviction(self):
        app = _IdleDesktopHarness(installed=False, engine=NullEngine())
        lifecycle = self._attach_real_lifecycle(app, lambda: None)
        coordinator = HeavyModelCoordinator(
            total_ram_provider=lambda: _8GB,
            stt_force_unload=lifecycle.force_unload,
            stt_is_busy=app._models_busy,
        )

        with patch("whisper_core.protocol.sidecar.idle_transition",
                   return_value=nullcontext()), \
                patch("whisper_core.protocol.sidecar.shutdown_all"), \
                patch("fronts.desktop.app.diagnostic_event"):
            lease = coordinator.acquire_tts()

        self.assertIsNotNone(lease)
        self.assertIsNone(app.engine)
        self.assertEqual(lifecycle.state, UNLOADED)
        self.assertFalse(app.has_model)

    def test_unavailable_model_reload_clears_installed_state(self):
        resident = SimpleNamespace(is_available=True, close=MagicMock())
        app = _IdleDesktopHarness(installed=True, engine=resident)
        lifecycle = self._attach_real_lifecycle(
            app, DesktopApp._load_stt_model.__get__(app))
        self.assertTrue(self._force_unload(lifecycle))
        unavailable = ModelRevisionUnavailable(
            "large-v3-turbo", "models", "revision", False)

        with patch("fronts.desktop.app.Engine", side_effect=unavailable):
            with self.assertRaises(ModelRevisionUnavailable):
                lifecycle.ensure_loaded()

        self.assertEqual(lifecycle.state, UNLOADED)
        self.assertFalse(app._stt_model_installed)
        self.assertFalse(app.has_model)

    def test_transient_reload_failure_keeps_installed_state(self):
        resident = SimpleNamespace(is_available=True, close=MagicMock())
        app = _IdleDesktopHarness(installed=True, engine=resident)
        lifecycle = self._attach_real_lifecycle(
            app, DesktopApp._load_stt_model.__get__(app))
        self.assertTrue(self._force_unload(lifecycle))

        with patch("fronts.desktop.app.Engine",
                   side_effect=RuntimeError("CUDA unavailable")):
            with self.assertRaisesRegex(RuntimeError, "CUDA unavailable"):
                lifecycle.ensure_loaded()

        self.assertEqual(lifecycle.state, UNLOADED)
        self.assertTrue(app._stt_model_installed)
        self.assertTrue(app.has_model)

    def test_reload_worker_restores_engine_through_real_lifecycle(self):
        resident = SimpleNamespace(is_available=True, close=MagicMock())
        reloaded = SimpleNamespace(is_available=True)
        app = _IdleDesktopHarness(installed=True, engine=resident)

        def load():
            app.engine = reloaded

        lifecycle = self._attach_real_lifecycle(app, load)
        self.assertTrue(self._force_unload(lifecycle))
        self.assertTrue(app.has_model)

        DesktopApp._reload_model_worker(app)

        self.assertIs(app.engine, reloaded)
        self.assertEqual(lifecycle.state, LOADED)
        app.model_lifecycle_state.emit.assert_called_once_with("loaded")


if __name__ == "__main__":
    unittest.main()
