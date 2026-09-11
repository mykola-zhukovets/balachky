"""Tests for lazy page materialization and decoupled settings imports."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LazyStartupTests(unittest.TestCase):
    def test_settings_module_import_does_not_pull_ctranslate2(self):
        code = (
            "import sys; "
            "import fronts.desktop.pages.settings; "
            "assert 'ctranslate2' not in sys.modules, 'ctranslate2 unexpectedly imported'; "
            "print('OK')"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        env["QT_QPA_PLATFORM"] = "offscreen"
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}")
        self.assertIn("OK", proc.stdout)

    def test_main_window_materializes_pages_lazily(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.main_window import MainWindow
        from whisper_core.config import Config

        app = QApplication.instance() or QApplication([])

        class _MockSignalOrCallable:
            def connect(self, _slot):
                pass
            def disconnect(self, *args):
                pass
            def emit(self, *args, **kwargs):
                pass
            def __call__(self, *args, **kwargs):
                return []

        class _MockController:
            def __init__(self):
                self.cfg = Config()
                self.window = None
                self.output_mode = "paste"
                self.profile = None
                self.has_model = True

            def __getattr__(self, name):
                return _MockSignalOrCallable()

            def get_models_info(self):
                return {}

            def set_language(self, _):
                pass

            def request_quit(self):
                pass

            def record_cancel(self):
                pass

            def record_pause(self):
                pass

            def record_resume(self):
                pass

            def update_state(self):
                return "1.0.0", "1.1.0", "https://example.invalid", False

            def delivery_state(self):
                return "", "", ""

            def list_voice_memories(self):
                return []

            def list_meeting_screen_monitors(self):
                return []

            def list_meetings(self):
                return []

            def list_recordings(self):
                return []

            def list_audio_files(self):
                return []

        controller = _MockController()
        win = MainWindow(controller)
        controller.window = win

        # Initially, only dictation is created; others are lazy (None)
        self.assertIsNotNone(win._dictation)
        self.assertIsNone(win._files)
        self.assertIsNone(win._meeting)
        self.assertIsNone(win._screen)
        self.assertIsNone(win._history)
        self.assertIsNone(win._vocab)
        self.assertIsNone(win._settings_page)
        self.assertIsNone(win._search)

        # Property access triggers materialization
        files_page = win.files
        self.assertIsNotNone(files_page)
        self.assertIs(win._files, files_page)
        self.assertIs(win.files, files_page)

        # set_page triggers materialization for that index
        self.assertIsNone(win._history)
        win.set_page(5)  # history page (індекс 4 — «Віддалено», feature/telegram-desktop-integration)
        self.assertIsNotNone(win._history)
        self.assertIs(win.history, win._history)

        win.close()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
